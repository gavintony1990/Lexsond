from __future__ import annotations

import base64
import http.client
import ipaddress
import io
import json
import math
import re
import socket
import ssl
import struct
import time
import wave
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlsplit

from .models import (
    ChunkMeasurement,
    ErrorClass,
    NormalizedRunResult,
    RequestMeasurement,
    RunStatus,
)
from .probe_components import ComponentStepStatus
from .sse import SSEEvent, SSEParser, SSEProtocolError


class ProbeType(StrEnum):
    CHAT = "chat"
    VISION = "vision"
    EMBEDDING = "embedding"
    IMAGE_GENERATION = "image_generation"
    AUDIO_SPEECH = "audio_speech"
    AUDIO_TRANSCRIPTION = "audio_transcription"


ProbeProgressCallback = Callable[[str, ComponentStepStatus], None]


class _ProbeProgress:
    def __init__(self, callback: ProbeProgressCallback | None) -> None:
        self._callback = callback
        self._active_step: str | None = None
        self._callback_elapsed_ns = 0
        self._measurement_callback_baseline_ns = 0

    def start(self, step_id: str) -> None:
        if self._active_step is not None:
            raise RuntimeError("probe progress already has an active step")
        self._emit(step_id, ComponentStepStatus.RUNNING)
        self._active_step = step_id

    def pass_active(self) -> None:
        if self._active_step is None:
            raise RuntimeError("probe progress has no active step")
        step_id = self._active_step
        self._emit(step_id, ComponentStepStatus.PASS)
        self._active_step = None

    def fail_active(self) -> None:
        if self._active_step is None:
            return
        step_id = self._active_step
        self._emit(step_id, ComponentStepStatus.FAIL)
        self._active_step = None

    def _emit(self, step_id: str, status: ComponentStepStatus) -> None:
        if self._callback is None:
            return
        started_ns = time.perf_counter_ns()
        try:
            self._callback(step_id, status)
        except Exception:
            # Observability is deliberately outside the provider-result boundary.
            # A UI or persistence outage must not reclassify a billable request.
            pass
        finally:
            self._callback_elapsed_ns += time.perf_counter_ns() - started_ns

    def mark_measurement_start(self) -> None:
        self._measurement_callback_baseline_ns = self._callback_elapsed_ns

    def measured_elapsed_ns(self, started_ns: int, observed_ns: int | None = None) -> int:
        observed = time.perf_counter_ns() if observed_ns is None else observed_ns
        observer_ns = self._callback_elapsed_ns - self._measurement_callback_baseline_ns
        return max(0, observed - started_ns - observer_ns)

    def deadline_ns(self, started_ns: int, timeout_seconds: float) -> int:
        observer_ns = self._callback_elapsed_ns - self._measurement_callback_baseline_ns
        return started_ns + int(timeout_seconds * 1_000_000_000) + observer_ns


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    base_url: str
    api_key: str | None = field(repr=False)
    model: str
    timeout_seconds: float = 30.0
    stream: bool = True
    prompt: str = "Reply with exactly: PROBE_OK"
    max_output_tokens: int = 64
    mock_mode: str | None = None
    probe_type: ProbeType = ProbeType.CHAT
    provider_id: str | None = None
    audio_voice: str | None = None
    expected_text: str | None = None

    def __post_init__(self) -> None:
        validate_base_url_transport(self.base_url)
        validate_api_key_value(self.api_key)
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model must be non-empty")
        if self.api_key is not None and self.api_key in self.base_url:
            raise ValueError("base_url must not contain api_key")
        if self.api_key is not None and self.api_key in self.model:
            raise ValueError("model must not contain api_key")
        if self.api_key is not None and self.api_key in self.prompt:
            raise ValueError("prompt must not contain api_key")
        if self.provider_id is not None and (
            not isinstance(self.provider_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.provider_id)
        ):
            raise ValueError("provider_id must use a bounded lowercase identifier")
        if self.audio_voice is not None and (
            not isinstance(self.audio_voice, str)
            or not self.audio_voice.strip()
            or len(self.audio_voice) > 128
            or any(ord(character) < 0x20 for character in self.audio_voice)
        ):
            raise ValueError("audio_voice must be a bounded printable string or null")
        if self.expected_text is not None and (
            not isinstance(self.expected_text, str)
            or not self.expected_text.strip()
            or len(self.expected_text) > 512
            or any(ord(character) < 0x20 for character in self.expected_text)
        ):
            raise ValueError("expected_text must be a bounded printable string or null")
        if (
            self.api_key is not None
            and self.expected_text is not None
            and self.api_key in self.expected_text
        ):
            raise ValueError("expected_text must not contain api_key")
        if (
            self.api_key is not None
            and self.audio_voice is not None
            and self.api_key in self.audio_voice
        ):
            raise ValueError("audio_voice must not contain api_key")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= self.max_output_tokens <= 4096:
            raise ValueError("max_output_tokens must be between 1 and 4096")
        try:
            probe_type = ProbeType(self.probe_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("probe_type is not supported") from exc
        object.__setattr__(self, "probe_type", probe_type)
        if probe_type not in {ProbeType.CHAT, ProbeType.VISION} and self.stream:
            raise ValueError("stream is only supported for chat and vision probes")


def run_openai_probe(
    config: ProbeConfig,
    *,
    progress: ProbeProgressCallback | None = None,
) -> NormalizedRunResult:
    """Dispatch one bounded request to the endpoint family selected by probe_type."""

    if config.probe_type in {ProbeType.CHAT, ProbeType.VISION}:
        return OpenAIChatProbe(config, progress=progress).run()
    return OpenAIEndpointProbe(config, progress=progress).run()


def validate_api_key_value(api_key: str | None) -> None:
    """Validate a credential before it can reach an HTTP header boundary."""

    if api_key is None:
        return
    if not isinstance(api_key, str) or not api_key or len(api_key) > 8192:
        raise ValueError("api_key must be a non-empty string or null")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in api_key):
        raise ValueError("api_key must contain visible ASCII characters only")


def validate_base_url_transport(base_url: str) -> None:
    """Require TLS for remote targets and allow plain HTTP only on loopback IPs."""

    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query, or fragment")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError(
                "base_url must use HTTPS unless it targets a numeric loopback address"
            ) from exc
        if not address.is_loopback:
            raise ValueError(
                "base_url must use HTTPS unless it targets a numeric loopback address"
            )


class UnsafeTargetAddress(OSError):
    """Raised when a target resolves into a network Lexsond must not reach."""


def _create_guarded_http_connection(
    parsed: Any,
    timeout_seconds: float,
) -> http.client.HTTPConnection:
    connection_class = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = connection_class(
        parsed.hostname,
        port,
        timeout=float(timeout_seconds),
    )
    # HTTPConnection stores the socket factory on the instance. Replacing it
    # keeps the original hostname for Host/SNI/certificate checks, while the
    # actual connect uses only the addresses validated in this DNS response.
    connection._create_connection = _guarded_socket_connection  # type: ignore[attr-defined]
    return connection


def _guarded_socket_connection(
    address: tuple[str, int],
    timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    host, port = address
    try:
        candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise
    if not candidates:
        raise socket.gaierror("target hostname returned no addresses")
    _validate_resolved_addresses(host, candidates)
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in candidates:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)  # type: ignore[arg-type]
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("target connection failed")


def _validate_resolved_addresses(host: str, candidates: list[tuple[Any, ...]]) -> None:
    normalized_host = host.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    addresses = []
    for candidate in candidates:
        sockaddr = candidate[4]
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
        except ValueError as exc:
            raise UnsafeTargetAddress("target DNS returned an invalid address") from exc
    if (literal is not None and literal.is_loopback) or normalized_host == "localhost":
        if all(address.is_loopback for address in addresses):
            return
        raise UnsafeTargetAddress("loopback target resolved outside the loopback network")
    if literal is not None and any(address != literal for address in addresses):
        raise UnsafeTargetAddress("numeric target resolved to a different address")
    if not all(_is_public_target_address(address) for address in addresses):
        raise UnsafeTargetAddress("target resolved to a blocked non-public network")


def _is_public_target_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject non-unicast and IPv4-embedding routes to protected networks."""

    if not address.is_global or address.is_multicast or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return True

    embedded = address.ipv4_mapped
    nat64_networks = (
        ipaddress.ip_network("64:ff9b::/96"),
        ipaddress.ip_network("64:ff9b:1::/48"),
    )
    if embedded is None and any(address in network for network in nat64_networks):
        embedded = ipaddress.ip_address(int(address) & 0xFFFFFFFF)
    if embedded is not None:
        return _is_public_target_address(embedded)
    return True


class OpenAIChatProbe:
    _MAX_RESPONSE_BYTES = 16 * 1024 * 1024
    _MAX_SSE_EVENTS = 1_024
    _MAX_OUTPUT_CHARS = 1_000_000

    def __init__(
        self,
        config: ProbeConfig,
        *,
        progress: ProbeProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.progress = _ProbeProgress(progress)

    def run(self) -> NormalizedRunResult:
        run = NormalizedRunResult()
        measurement = RequestMeasurement(
            endpoint=self.config.base_url,
            requested_model=self.config.model,
            streaming=self.config.stream,
        )
        measurement.evidence.update(_probe_modality_evidence(self.config.probe_type))
        run.measurements.append(measurement)

        try:
            self._execute(measurement)
        except TimeoutError as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.TIMEOUT, exc)
        except (ConnectionError, socket.gaierror, ssl.SSLError, OSError) as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.NETWORK, exc)
        except (SSEProtocolError, json.JSONDecodeError, ValueError) as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.PROTOCOL, exc)
        except Exception as exc:  # defensive boundary for a probe process
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.UNKNOWN, exc)

        # A provider can reflect the bearer value before a later timeout or
        # malformed event. Scrub on every exit path, not only successful EOF.
        measurement.output_text = _redact_provider_text(
            measurement.output_text,
            self.config.api_key,
        )

        if measurement.error_class is not None:
            run.finish(RunStatus.FAIL, measurement.error_class.value)
        else:
            self.progress.start("quality_assert")
            if measurement.status_code == 200 and measurement.output_text.strip():
                if (
                    self.config.probe_type == ProbeType.VISION
                    and measurement.output_text.strip().upper() != "RED"
                ):
                    self.progress.fail_active()
                    run.finish(RunStatus.FAIL, "VISION_ASSERTION_FAILED")
                elif (
                    self.config.expected_text is not None
                    and measurement.output_text.strip()
                    != self.config.expected_text.strip()
                ):
                    self.progress.fail_active()
                    run.finish(RunStatus.FAIL, "EXPECTED_TEXT_ASSERTION_FAILED")
                else:
                    self.progress.pass_active()
                    run.finish(RunStatus.PASS, "REQUEST_SUCCEEDED")
            else:
                self.progress.fail_active()
                run.finish(RunStatus.FAIL, "EMPTY_OR_INCOMPLETE_RESPONSE")
        return run

    def _execute(self, measurement: RequestMeasurement) -> None:
        self.progress.start("fixture_prepare")
        parsed = urlsplit(self.config.base_url.rstrip("/"))
        connection = _create_guarded_http_connection(
            parsed, self.config.timeout_seconds
        )
        path_prefix = parsed.path.rstrip("/")
        path = f"{path_prefix}/chat/completions"
        content: str | list[dict[str, Any]] = self.config.prompt
        if self.config.probe_type == ProbeType.VISION:
            content = [
                {
                    "type": "text",
                    "text": (
                        "Inspect the image. Reply with exactly one uppercase English "
                        "word naming its solid fill color."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": _red_probe_image_data_url(), "detail": "low"},
                },
            ]
        payload = json.dumps(
            {
                "model": self.config.model,
                "stream": self.config.stream,
                "stream_options": {"include_usage": True},
                "temperature": 0,
                "max_tokens": self.config.max_output_tokens,
                "messages": [{"role": "user", "content": content}],
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.config.stream else "application/json",
            "User-Agent": "lexsond/0.5.0",
        }
        if self.config.api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.mock_mode:
            headers["X-Mock-Mode"] = self.config.mock_mode
        self.progress.pass_active()

        started_ns = time.perf_counter_ns()
        self.progress.mark_measurement_start()
        deadline_ns = self.progress.deadline_ns(started_ns, self.config.timeout_seconds)
        try:
            self.progress.start("request_dispatch")
            connection.connect()
            connected_ns = time.perf_counter_ns()
            measurement.connect_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns, connected_ns)
            )
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _set_connection_deadline_timeout(connection, deadline_ns)
            connection.request("POST", path, body=payload, headers=headers)
            self.progress.pass_active()
            self.progress.start("transport_check")
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _wrap_connection_with_deadline(connection, deadline_ns)
            response = connection.getresponse()
            headers_ns = time.perf_counter_ns()
            measurement.response_headers_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns, headers_ns)
            )
            measurement.status_code = response.status

            if response.status != 200:
                body = _read_bounded_response(
                    response,
                    16_384,
                    connection,
                    deadline_ns,
                ).decode("utf-8", errors="replace")
                measurement.error_class = classify_http_error(response.status, body)
                # Provider error bodies can echo prompts, reasoning, or credentials.
                # Preserve the normalized class and status, never arbitrary body text.
                measurement.error_message = f"HTTP {response.status}"
                measurement.e2e_ms = _ms(
                    self.progress.measured_elapsed_ns(started_ns)
                )
                self.progress.fail_active()
                return

            self.progress.pass_active()
            self.progress.start("response_validate")
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _wrap_connection_with_deadline(connection, deadline_ns)

            if self.config.stream:
                self._consume_stream(
                    response,
                    measurement,
                    started_ns,
                    connection,
                    deadline_ns,
                )
            else:
                self._consume_json(
                    response,
                    measurement,
                    started_ns,
                    connection,
                    deadline_ns,
                )
            self.progress.pass_active()
        finally:
            connection.close()

    def _consume_stream(
        self,
        response: http.client.HTTPResponse,
        measurement: RequestMeasurement,
        started_ns: int,
        connection: http.client.HTTPConnection,
        deadline_ns: int,
    ) -> None:
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/event-stream":
            raise SSEProtocolError("unexpected streaming content type")
        measurement.evidence["content_type"] = "text/event-stream"

        parser = SSEParser()
        content_times_ns: list[int] = []
        reasoning_times_ns: list[int] = []
        emission_times_ns: list[int] = []
        saw_done = False
        total_bytes = 0
        event_count = 0
        while True:
            if response.isclosed():
                break
            _set_connection_deadline_timeout(connection, deadline_ns)
            block = response.read1(65_536)
            if not block:
                break
            received_ns = time.perf_counter_ns()
            total_bytes += len(block)
            if total_bytes > self._MAX_RESPONSE_BYTES:
                raise SSEProtocolError("stream response exceeds the probe limit")
            if measurement.ttfb_ms is None:
                measurement.ttfb_ms = _ms(
                    self.progress.measured_elapsed_ns(started_ns, received_ns)
                )
            for event in parser.feed(block):
                if saw_done:
                    raise SSEProtocolError("stream contained an event after [DONE]")
                event_count += 1
                if event_count > self._MAX_SSE_EVENTS:
                    raise SSEProtocolError("stream event count exceeds the probe limit")
                done, content_chars, reasoning_chars = self._consume_event(
                    event,
                    measurement,
                    received_ns,
                    started_ns,
                )
                if done:
                    saw_done = True
                if content_chars > 0:
                    content_times_ns.append(received_ns)
                if reasoning_chars > 0:
                    reasoning_times_ns.append(received_ns)
                if content_chars > 0 or reasoning_chars > 0:
                    emission_times_ns.append(received_ns)
            if saw_done:
                break

        if not saw_done:
            for event in parser.finalize():
                if saw_done:
                    raise SSEProtocolError("stream contained an event after [DONE]")
                event_count += 1
                if event_count > self._MAX_SSE_EVENTS:
                    raise SSEProtocolError("stream event count exceeds the probe limit")
                received_ns = time.perf_counter_ns()
                done, content_chars, reasoning_chars = self._consume_event(
                    event,
                    measurement,
                    received_ns,
                    started_ns,
                )
                if done:
                    saw_done = True
                if content_chars > 0:
                    content_times_ns.append(received_ns)
                if reasoning_chars > 0:
                    reasoning_times_ns.append(received_ns)
                if content_chars > 0 or reasoning_chars > 0:
                    emission_times_ns.append(received_ns)

        measurement.e2e_ms = _ms(self.progress.measured_elapsed_ns(started_ns))
        measurement.chunk_count = len(measurement.chunks)
        measurement.evidence["sse_done_received"] = saw_done
        if not saw_done:
            raise SSEProtocolError("stream ended without [DONE]")
        measurement.output_text = _redact_provider_text(
            measurement.output_text,
            self.config.api_key,
        )
        reasoning_output_chars = sum(chunk.reasoning_chars for chunk in measurement.chunks)
        measurement.evidence.update(
            {
                "content_chunk_count": len(content_times_ns),
                "reasoning_chunk_count": len(reasoning_times_ns),
                "reasoning_output_chars": reasoning_output_chars,
                "reasoning_content_observed": reasoning_output_chars > 0,
                "emission_chunk_count": len(emission_times_ns),
                "final_content_burst_observed": (
                    bool(reasoning_times_ns)
                    and len(content_times_ns) > 1
                    and _all_gaps_below(content_times_ns, 1_000_000)
                ),
            }
        )
        if content_times_ns:
            measurement.evidence["first_content_token_ms"] = _ms(
                self.progress.measured_elapsed_ns(started_ns, content_times_ns[0])
            )
        if reasoning_times_ns:
            measurement.evidence["first_reasoning_token_ms"] = _ms(
                self.progress.measured_elapsed_ns(started_ns, reasoning_times_ns[0])
            )
        if emission_times_ns:
            measurement.ttft_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns, emission_times_ns[0])
            )
            decode_ns = max(emission_times_ns[-1] - emission_times_ns[0], 1)
            output_tokens = measurement.provider_reported_output_tokens
            if output_tokens is not None:
                measurement.output_tps = round(output_tokens / (decode_ns / 1_000_000_000), 3)
            if len(emission_times_ns) > 1:
                deltas = [
                    emission_times_ns[index] - emission_times_ns[index - 1]
                    for index in range(1, len(emission_times_ns))
                ]
                measurement.itl_ms = _ms(sum(deltas) / len(deltas))
                measurement.evidence["pseudo_stream_suspected"] = _all_gaps_below(
                    emission_times_ns,
                    1_000_000,
                )
            else:
                emitted_chars = sum(
                    chunk.content_chars + chunk.reasoning_chars
                    for chunk in measurement.chunks
                )
                output_tokens = measurement.provider_reported_output_tokens or 0
                measurement.evidence["pseudo_stream_suspected"] = (
                    len(emission_times_ns) == 1
                    and (emitted_chars > 1 or output_tokens > 1)
                )

    def _consume_event(
        self,
        event: SSEEvent,
        measurement: RequestMeasurement,
        received_ns: int,
        started_ns: int,
    ) -> tuple[bool, int, int]:
        if event.data == "[DONE]":
            return True, 0, 0
        payload = json.loads(event.data)
        content = ""
        reasoning_content = ""
        finish_reason: str | None = None
        choices = payload.get("choices") or []
        if choices:
            choice = choices[0]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("choices[0].delta must be an object")
            content = _optional_text(delta, "content")
            reasoning_content = _optional_text(delta, "reasoning_content")
            finish_reason = _safe_provider_label(
                choice.get("finish_reason"),
                self.config.api_key,
                field_name="finish_reason",
            )
        usage = payload.get("usage")
        if usage:
            if not isinstance(usage, dict):
                raise ValueError("usage must be an object")
            measurement.provider_reported_input_tokens = _validated_usage_token(
                usage.get("prompt_tokens"),
                "prompt_tokens",
            )
            measurement.provider_reported_output_tokens = _validated_usage_token(
                usage.get("completion_tokens"),
                "completion_tokens",
            )
            measurement.provider_reported_total_tokens = _validated_usage_token(
                usage.get("total_tokens"),
                "total_tokens",
            )
        response_model = _safe_response_model(payload.get("model"), self.config.api_key)
        if response_model is not None:
            measurement.response_model = response_model
        if (
            len(measurement.output_text)
            + len(content)
            + sum(chunk.reasoning_chars for chunk in measurement.chunks)
            + len(reasoning_content)
            > self._MAX_OUTPUT_CHARS
        ):
            raise ValueError("stream output exceeds the probe character limit")
        measurement.output_text += content
        if finish_reason:
            measurement.finish_reason = finish_reason
        measurement.chunks.append(
            ChunkMeasurement(
                sequence=len(measurement.chunks),
                received_after_ms=_ms(
                    self.progress.measured_elapsed_ns(started_ns, received_ns)
                ),
                event_type=_safe_provider_label(
                    event.event,
                    self.config.api_key,
                    field_name="event_type",
                ),
                content_chars=len(content),
                reasoning_chars=len(reasoning_content),
                has_usage=usage is not None,
                finish_reason=finish_reason,
            )
        )
        return False, len(content), len(reasoning_content)

    def _consume_json(
        self,
        response: http.client.HTTPResponse,
        measurement: RequestMeasurement,
        started_ns: int,
        connection: http.client.HTTPConnection,
        deadline_ns: int,
    ) -> None:
        _set_connection_deadline_timeout(connection, deadline_ns)
        first_byte = response.read(1)
        if not first_byte:
            raise ValueError("response body is empty")
        measurement.ttfb_ms = _ms(self.progress.measured_elapsed_ns(started_ns))
        body = _read_bounded_response(
            response,
            self._MAX_RESPONSE_BYTES,
            connection,
            deadline_ns,
            initial=first_byte,
        )
        payload = json.loads(body.decode("utf-8"))
        measurement.e2e_ms = _ms(self.progress.measured_elapsed_ns(started_ns))
        measurement.response_model = _safe_response_model(
            payload.get("model"),
            self.config.api_key,
        )
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            if not isinstance(message, dict):
                raise ValueError("choices[0].message must be an object")
            measurement.output_text = _optional_text(message, "content")
            if len(measurement.output_text) > self._MAX_OUTPUT_CHARS:
                raise ValueError("response output exceeds the probe character limit")
            measurement.output_text = _redact_provider_text(
                measurement.output_text,
                self.config.api_key,
            )
            reasoning_content = _optional_text(message, "reasoning_content")
            measurement.evidence["reasoning_output_chars"] = len(reasoning_content)
            measurement.evidence["reasoning_content_observed"] = bool(reasoning_content)
            measurement.finish_reason = _safe_provider_label(
                choices[0].get("finish_reason"),
                self.config.api_key,
                field_name="finish_reason",
            )
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("usage must be an object")
        measurement.provider_reported_input_tokens = _validated_usage_token(
            usage.get("prompt_tokens"),
            "prompt_tokens",
        )
        measurement.provider_reported_output_tokens = _validated_usage_token(
            usage.get("completion_tokens"),
            "completion_tokens",
        )
        measurement.provider_reported_total_tokens = _validated_usage_token(
            usage.get("total_tokens"),
            "total_tokens",
        )
        measurement.chunk_count = 1

    def _fail(
        self,
        measurement: RequestMeasurement,
        error_class: ErrorClass,
        exc: Exception,
    ) -> None:
        measurement.error_class = error_class
        measurement.error_message = self._safe_error(_safe_excerpt(str(exc)))

    def _safe_error(self, value: str) -> str:
        return _redact_error(value, self.config.api_key)


class OpenAIEndpointProbe:
    """Bounded smoke probes for non-chat OpenAI-compatible endpoint families."""

    _MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        config: ProbeConfig,
        *,
        progress: ProbeProgressCallback | None = None,
    ) -> None:
        if config.probe_type in {ProbeType.CHAT, ProbeType.VISION}:
            raise ValueError("chat and vision probes use OpenAIChatProbe")
        self.config = config
        self.progress = _ProbeProgress(progress)

    def run(self) -> NormalizedRunResult:
        run = NormalizedRunResult(
            suite_name=f"openai-{self.config.probe_type.value}-smoke",
            suite_version="0.1.0",
        )
        measurement = RequestMeasurement(
            endpoint=self.config.base_url,
            requested_model=self.config.model,
            streaming=False,
        )
        measurement.evidence.update(_probe_modality_evidence(self.config.probe_type))
        run.measurements.append(measurement)
        try:
            self._execute(measurement)
        except TimeoutError as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.TIMEOUT, exc)
        except (ConnectionError, socket.gaierror, ssl.SSLError, OSError) as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.NETWORK, exc)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.PROTOCOL, exc)
        except Exception as exc:  # defensive boundary for a probe process
            self.progress.fail_active()
            self._fail(measurement, ErrorClass.UNKNOWN, exc)

        if measurement.error_class is not None:
            run.finish(RunStatus.FAIL, measurement.error_class.value)
        else:
            if measurement.status_code == 200 and measurement.evidence.get("response_valid"):
                run.finish(RunStatus.PASS, "REQUEST_SUCCEEDED")
            else:
                self.progress.fail_active()
                run.finish(RunStatus.FAIL, "EMPTY_OR_INCOMPLETE_RESPONSE")
        return run

    def _execute(self, measurement: RequestMeasurement) -> None:
        self.progress.start("fixture_prepare")
        parsed = urlsplit(self.config.base_url.rstrip("/"))
        connection = _create_guarded_http_connection(
            parsed, self.config.timeout_seconds
        )
        suffix, body, content_type, accept, consumer = self._request_contract()
        path = f"{parsed.path.rstrip('/')}{suffix}"
        measurement.evidence["endpoint_type"] = self.config.probe_type.value
        headers = {
            "Content-Type": content_type,
            "Accept": accept,
            "User-Agent": "lexsond/0.5.0",
        }
        if self.config.api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.mock_mode:
            headers["X-Mock-Mode"] = self.config.mock_mode
        self.progress.pass_active()

        started_ns = time.perf_counter_ns()
        self.progress.mark_measurement_start()
        deadline_ns = self.progress.deadline_ns(started_ns, self.config.timeout_seconds)
        try:
            self.progress.start("request_dispatch")
            connection.connect()
            measurement.connect_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns)
            )
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _set_connection_deadline_timeout(connection, deadline_ns)
            connection.request("POST", path, body=body, headers=headers)
            self.progress.pass_active()
            self.progress.start("transport_check")
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _wrap_connection_with_deadline(connection, deadline_ns)
            response = connection.getresponse()
            measurement.response_headers_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns)
            )
            measurement.status_code = response.status
            if response.status != 200:
                error_body = _read_bounded_response(
                    response,
                    16_384,
                    connection,
                    deadline_ns,
                ).decode("utf-8", errors="replace")
                measurement.error_class = classify_http_error(response.status, error_body)
                measurement.error_message = f"HTTP {response.status}"
                measurement.e2e_ms = _ms(
                    self.progress.measured_elapsed_ns(started_ns)
                )
                self.progress.fail_active()
                return
            self.progress.pass_active()
            self.progress.start("response_validate")
            deadline_ns = self.progress.deadline_ns(
                started_ns,
                self.config.timeout_seconds,
            )
            _wrap_connection_with_deadline(connection, deadline_ns)
            _set_connection_deadline_timeout(connection, deadline_ns)
            first = response.read(1)
            if not first:
                raise ValueError("response body is empty")
            measurement.ttfb_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns)
            )
            payload = _read_bounded_response(
                response,
                self._MAX_RESPONSE_BYTES,
                connection,
                deadline_ns,
                initial=first,
            )
            consumer(payload, response.getheader("Content-Type", ""), measurement)
            measurement.e2e_ms = _ms(
                self.progress.measured_elapsed_ns(started_ns)
            )
            measurement.chunk_count = 1
            measurement.evidence["response_valid"] = True
            self.progress.pass_active()
        finally:
            connection.close()

    def _request_contract(
        self,
    ) -> tuple[
        str,
        bytes,
        str,
        str,
        Callable[[bytes, str, RequestMeasurement], None],
    ]:
        if self.config.probe_type == ProbeType.EMBEDDING:
            return (
                "/embeddings",
                _json_bytes({"model": self.config.model, "input": "api quality probe"}),
                "application/json",
                "application/json",
                self._consume_embedding,
            )
        if self.config.probe_type == ProbeType.IMAGE_GENERATION:
            image_request: dict[str, Any] = {
                "model": self.config.model,
                "prompt": "A plain red square on a plain white background.",
                "n": 1,
            }
            if self.config.provider_id == "openrouter":
                image_request["output_format"] = "png"
            else:
                image_request["response_format"] = "b64_json"
            return (
                (
                    "/images"
                    if self.config.provider_id == "openrouter"
                    else "/images/generations"
                ),
                _json_bytes(image_request),
                "application/json",
                "application/json",
                self._consume_image,
            )
        if self.config.probe_type == ProbeType.AUDIO_SPEECH:
            if self.config.provider_id == "openrouter" and self.config.audio_voice is None:
                raise ValueError("OpenRouter speech probe requires a declared voice")
            response_format = (
                "mp3" if self.config.provider_id == "openrouter" else "wav"
            )
            accept = (
                "audio/mpeg, application/octet-stream"
                if self.config.provider_id == "openrouter"
                else "audio/wav, application/octet-stream"
            )
            return (
                "/audio/speech",
                _json_bytes(
                    {
                        "model": self.config.model,
                        "input": "Lexsond probe.",
                        "voice": self.config.audio_voice or "alloy",
                        "response_format": response_format,
                    }
                ),
                "application/json",
                accept,
                self._consume_speech,
            )
        if self.config.probe_type == ProbeType.AUDIO_TRANSCRIPTION:
            if self.config.provider_id == "openrouter":
                body = _json_bytes(
                    {
                        "model": self.config.model,
                        "input_audio": {
                            "data": base64.b64encode(_silent_probe_wav()).decode("ascii"),
                            "format": "wav",
                        },
                    }
                )
                content_type = "application/json"
            else:
                body, content_type = _transcription_request(self.config.model)
            return (
                "/audio/transcriptions",
                body,
                content_type,
                "application/json",
                self._consume_transcription,
            )
        raise ValueError("probe_type is not implemented")

    def _consume_embedding(
        self,
        body: bytes,
        content_type: str,
        measurement: RequestMeasurement,
    ) -> None:
        payload = _json_object(body)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("embedding response data must be a non-empty array")
        vectors: list[list[Any]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise ValueError("embedding response item is invalid")
            vectors.append(item["embedding"])
        measurement.response_model = _safe_response_model(
            payload.get("model"),
            self.config.api_key,
        )
        _apply_usage(payload.get("usage"), measurement)
        self._begin_quality_assert()

        dimensions: int | None = None
        for vector in vectors:
            if not vector or len(vector) > 1_000_000 or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            ):
                raise ValueError("embedding vector is invalid")
            if dimensions is None:
                dimensions = len(vector)
            elif len(vector) != dimensions:
                raise ValueError("embedding vectors have inconsistent dimensions")
        measurement.evidence["embedding_count"] = len(data)
        measurement.evidence["embedding_dimensions"] = dimensions

    def _consume_image(
        self,
        body: bytes,
        content_type: str,
        measurement: RequestMeasurement,
    ) -> None:
        payload = _json_object(body)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("image response data must be a non-empty array")
        outputs: list[tuple[str, bytes | str, str | None]] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("image response item is invalid")
            if isinstance(item.get("b64_json"), str) and item["b64_json"]:
                media_type = item.get("media_type")
                if media_type is not None and not isinstance(media_type, str):
                    raise ValueError("generated image media_type must be a string")
                outputs.append(
                    ("b64_json", _decode_bounded_base64(item["b64_json"]), media_type)
                )
            elif isinstance(item.get("url"), str) and item["url"]:
                _validate_image_url(item["url"])
                outputs.append(("url", item["url"], None))
            else:
                raise ValueError("image response item has no supported output")
        _apply_usage(payload.get("usage"), measurement)
        self._begin_quality_assert()

        transports: list[str] = []
        for transport, output, media_type in outputs:
            if transport == "b64_json":
                if not isinstance(output, bytes):  # defensive type narrowing
                    raise ValueError("generated image payload is invalid")
                image_format = _validate_image_bytes(output)
                if media_type is not None and media_type != f"image/{image_format}":
                    raise ValueError("generated image media_type does not match payload")
                transports.append("b64_json")
            else:
                raise ValueError(
                    "generated image URL is transport-only and cannot pass strict validation"
                )
        measurement.evidence["generated_image_count"] = len(data)
        measurement.evidence["image_transport"] = transports[0]
        measurement.evidence["image_transports"] = sorted(set(transports))

    def _consume_speech(
        self,
        body: bytes,
        content_type: str,
        measurement: RequestMeasurement,
    ) -> None:
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if self.config.provider_id == "openrouter":
            if normalized_type not in {"audio/mpeg", "application/octet-stream"}:
                raise ValueError("speech response content type does not match MP3")
            audio_format = "mp3"
        else:
            if normalized_type not in {
                "audio/wav",
                "audio/x-wav",
                "application/octet-stream",
            }:
                raise ValueError("speech response content type does not match WAV")
            audio_format = "wav"
        measurement.evidence["response_content_type"] = normalized_type
        self._begin_quality_assert()
        if audio_format == "mp3":
            _validate_mp3_audio(body)
        else:
            _validate_wav_audio(body)
        measurement.evidence["audio_bytes"] = len(body)
        measurement.evidence["audio_format"] = audio_format

    def _consume_transcription(
        self,
        body: bytes,
        content_type: str,
        measurement: RequestMeasurement,
    ) -> None:
        payload = _json_object(body)
        transcript = payload.get("text")
        if not isinstance(transcript, str):
            raise ValueError("transcription response text must be a string")
        _apply_usage(payload.get("usage"), measurement)
        self._begin_quality_assert()
        if len(transcript) > 1_000_000:
            raise ValueError("transcription response exceeds the character limit")
        measurement.evidence["transcript_chars"] = len(transcript)
        measurement.evidence["probe_audio_seconds"] = 1.0

    def _begin_quality_assert(self) -> None:
        self.progress.pass_active()
        self.progress.start("quality_assert")

    def _fail(
        self,
        measurement: RequestMeasurement,
        error_class: ErrorClass,
        exc: Exception,
    ) -> None:
        measurement.error_class = error_class
        measurement.error_message = _redact_error(
            _safe_excerpt(str(exc)),
            self.config.api_key,
        )


def _probe_modality_evidence(probe_type: ProbeType) -> dict[str, Any]:
    modalities = {
        ProbeType.CHAT: (["text"], ["text"]),
        ProbeType.VISION: (["text", "image"], ["text"]),
        ProbeType.EMBEDDING: (["text"], ["embeddings"]),
        ProbeType.IMAGE_GENERATION: (["text"], ["image"]),
        ProbeType.AUDIO_SPEECH: (["text"], ["audio"]),
        ProbeType.AUDIO_TRANSCRIPTION: (["audio"], ["text"]),
    }
    inputs, outputs = modalities[probe_type]
    return {
        "probe_type": probe_type.value,
        "input_modalities": inputs,
        "output_modalities": outputs,
    }


def _red_probe_image_data_url() -> str:
    width = 64
    height = 64
    scanlines = b"".join(
        b"\x00" + (b"\xff\x00\x00" * width)
        for _ in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _transcription_request(model: str) -> tuple[bytes, str]:
    boundary = "----lexsond-audio-boundary"
    audio = _silent_probe_wav()
    fields = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{model}\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="probe.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("ascii")
        + audio
        + b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(fields), f"multipart/form-data; boundary={boundary}"


def _silent_probe_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 8_000)
    return buffer.getvalue()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _json_object(body: bytes) -> dict[str, Any]:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response body must be a JSON object")
    return payload


def _set_connection_deadline_timeout(
    connection: http.client.HTTPConnection,
    deadline_ns: int,
) -> None:
    remaining_seconds = (deadline_ns - time.perf_counter_ns()) / 1_000_000_000
    if remaining_seconds <= 0:
        raise TimeoutError("probe absolute deadline exceeded")
    if connection.sock is not None:
        connection.sock.settimeout(max(remaining_seconds, 0.001))


class _DeadlineSocketIO(io.RawIOBase):
    def __init__(self, owner: "_DeadlineSocket") -> None:
        super().__init__()
        self._owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining_seconds = (
            self._owner._deadline_ns - time.perf_counter_ns()
        ) / 1_000_000_000
        if remaining_seconds <= 0:
            raise TimeoutError("probe absolute deadline exceeded")
        self._owner._socket.settimeout(max(remaining_seconds, 0.001))
        return self._owner._socket.recv_into(buffer)

    def fileno(self) -> int:
        return self._owner._socket.fileno()

    def close(self) -> None:
        if not self.closed:
            try:
                self._owner._release_file()
            finally:
                super().close()


class _DeadlineSocket:
    def __init__(self, sock: socket.socket, deadline_ns: int) -> None:
        self._socket = sock
        self._deadline_ns = deadline_ns
        self._file_count = 0
        self._owner_closed = False

    def makefile(self, mode: str, buffering: int | None = None) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("deadline socket only supports binary reads")
        self._file_count += 1
        return io.BufferedReader(_DeadlineSocketIO(self))

    def settimeout(self, value: float | None) -> None:
        self._socket.settimeout(value)

    def set_deadline_ns(self, deadline_ns: int) -> None:
        self._deadline_ns = deadline_ns

    def close(self) -> None:
        self._owner_closed = True
        if self._file_count == 0:
            self._socket.close()

    def _release_file(self) -> None:
        self._file_count = max(0, self._file_count - 1)
        if self._owner_closed and self._file_count == 0:
            self._socket.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)


def _wrap_connection_with_deadline(
    connection: http.client.HTTPConnection,
    deadline_ns: int,
) -> None:
    if isinstance(connection.sock, _DeadlineSocket):
        connection.sock.set_deadline_ns(deadline_ns)
    elif connection.sock is not None:
        connection.sock = _DeadlineSocket(connection.sock, deadline_ns)  # type: ignore[assignment]


def _read_bounded_response(
    response: http.client.HTTPResponse,
    limit: int,
    connection: http.client.HTTPConnection,
    deadline_ns: int,
    *,
    initial: bytes = b"",
) -> bytes:
    payload = bytearray(initial)
    while True:
        if len(payload) > limit:
            raise ValueError("response body exceeds the probe limit")
        if response.isclosed():
            break
        _set_connection_deadline_timeout(connection, deadline_ns)
        block = response.read1(min(65_536, limit + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
    if len(payload) > limit:
        raise ValueError("response body exceeds the probe limit")
    return bytes(payload)


def _safe_provider_label(
    value: Any,
    api_key: str | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{field_name} must be a bounded string or null")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{field_name} must contain printable characters")
    if api_key is not None and api_key in value:
        return None
    if re.search(r"(?i)\bauthorization\b|\bbearer\s+|\bsk-[A-Za-z0-9_-]{8,}\b", value):
        return None
    return value


def _redact_provider_text(value: str, api_key: str | None) -> str:
    return _redact_error(value, api_key)


def _decode_bounded_base64(value: str) -> bytes:
    if len(value) > OpenAIEndpointProbe._MAX_RESPONSE_BYTES * 2:
        raise ValueError("generated image base64 exceeds the probe limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("generated image is not valid base64") from exc
    if not decoded:
        raise ValueError("generated image is empty")
    return decoded


def _validate_image_bytes(value: bytes) -> str:
    if value.startswith(b"\x89PNG\r\n\x1a\n"):
        _validate_png(value)
        return "png"
    raise ValueError("strict image probe currently requires a PNG payload")


def _validate_png(value: bytes) -> None:
    position = 8
    compressed = bytearray()
    expected_pixel_bytes: int | None = None
    row_bytes: int | None = None
    bit_depth: int | None = None
    color_type: int | None = None
    palette_entries: int | None = None
    chunk_count = 0
    saw_idat = False
    left_idat = False
    saw_iend = False
    while position + 12 <= len(value):
        chunk_count += 1
        if chunk_count > 4_096:
            raise ValueError("generated PNG chunk count exceeds the probe limit")
        length = struct.unpack(">I", value[position : position + 4])[0]
        if length > OpenAIEndpointProbe._MAX_RESPONSE_BYTES:
            raise ValueError("generated PNG chunk exceeds the probe limit")
        chunk_end = position + 12 + length
        if chunk_end > len(value):
            raise ValueError("generated PNG chunk is truncated")
        kind = value[position + 4 : position + 8]
        if (
            len(kind) != 4
            or any(
                not (ord("A") <= byte <= ord("Z") or ord("a") <= byte <= ord("z"))
                for byte in kind
            )
            or not ord("A") <= kind[2] <= ord("Z")
        ):
            raise ValueError("generated PNG chunk type is invalid")
        data = value[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", value[position + 8 + length : chunk_end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise ValueError("generated PNG chunk checksum is invalid")
        if kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"} and ord("A") <= kind[0] <= ord("Z"):
            raise ValueError("generated PNG contains an unknown critical chunk")
        if saw_idat and kind != b"IDAT":
            left_idat = True
        if kind == b"IDAT" and left_idat:
            raise ValueError("generated PNG IDAT chunks are not consecutive")
        if kind == b"IHDR":
            if chunk_count != 1 or length != 13:
                raise ValueError("generated PNG has an invalid IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            if width < 1 or height < 1 or width > 16_384 or height > 16_384:
                raise ValueError("generated PNG has invalid dimensions")
            samples_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                color_type not in samples_by_color_type
                or bit_depth not in valid_depths[color_type]
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ValueError("generated PNG uses an unsupported encoding")
            row_bytes = (
                width * samples_by_color_type[color_type] * bit_depth + 7
            ) // 8
            expected_pixel_bytes = height * (row_bytes + 1)
            if expected_pixel_bytes > 64 * 1024 * 1024:
                raise ValueError("generated PNG expands beyond the pixel limit")
        elif kind == b"IDAT":
            saw_idat = True
            compressed.extend(data)
        elif kind == b"PLTE":
            if saw_idat or length == 0 or length % 3 != 0 or length > 768:
                raise ValueError("generated PNG has an invalid palette")
            if palette_entries is not None:
                raise ValueError("generated PNG contains multiple palettes")
            palette_entries = length // 3
        elif kind == b"IEND":
            if length != 0 or chunk_end != len(value):
                raise ValueError("generated PNG has trailing or invalid IEND data")
            saw_iend = True
            break
        position = chunk_end
    if expected_pixel_bytes is None or not saw_idat or not saw_iend:
        raise ValueError("generated PNG is incomplete")
    if color_type == 3:
        if palette_entries is None:
            raise ValueError("indexed generated PNG requires one palette")
        if bit_depth is None or palette_entries is None or palette_entries > 2**bit_depth:
            raise ValueError("indexed generated PNG palette exceeds its bit depth")
    elif color_type in {0, 4} and palette_entries is not None:
        raise ValueError("grayscale generated PNG must not contain a palette")
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(
            bytes(compressed),
            expected_pixel_bytes + 1,
        )
    except zlib.error as exc:
        raise ValueError("generated PNG pixel data is malformed") from exc
    if (
        len(pixels) != expected_pixel_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("generated PNG pixel data length is invalid")
    if row_bytes is None or any(
        pixels[row_start] > 4
        for row_start in range(0, len(pixels), row_bytes + 1)
    ):
        raise ValueError("generated PNG contains an invalid row filter")


def _validate_image_url(value: str) -> None:
    if len(value) > 4096:
        raise ValueError("generated image URL exceeds the probe limit")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("generated image URL must be an absolute credential-free HTTPS URL")


def _validate_wav_audio(value: bytes) -> None:
    if not value.startswith(b"RIFF") or value[8:12] != b"WAVE":
        raise ValueError("speech response is not a WAV payload")
    try:
        with wave.open(io.BytesIO(value), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            if (
                channels < 1
                or sample_width < 1
                or audio.getframerate() < 1
                or audio.getnframes() < 1
            ):
                raise ValueError("speech response WAV contains no playable frames")
            if len(audio.readframes(1)) != channels * sample_width:
                raise ValueError("speech response WAV frame data is truncated")
    except (EOFError, wave.Error) as exc:
        raise ValueError("speech response WAV is malformed") from exc


def _validate_mp3_audio(value: bytes) -> None:
    position = 0
    if value.startswith(b"ID3"):
        if len(value) < 10 or any(byte >= 0x80 for byte in value[6:10]):
            raise ValueError("speech response MP3 has an invalid ID3 header")
        tag_size = (
            (value[6] << 21)
            | (value[7] << 14)
            | (value[8] << 7)
            | value[9]
        )
        position = 10 + tag_size
    if position + 4 > len(value):
        raise ValueError("speech response MP3 has no audio frame")
    header = int.from_bytes(value[position : position + 4], "big")
    if header >> 21 != 0x7FF:
        raise ValueError("speech response MP3 frame sync is invalid")
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0b1111
    sample_rate_index = (header >> 10) & 0b11
    padding = (header >> 9) & 0b1
    if version == 0b01 or layer != 0b01 or bitrate_index in {0, 15} or sample_rate_index == 3:
        raise ValueError("speech response MP3 frame header is unsupported")
    bitrate_tables = {
        0b11: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        0b10: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        0b00: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    }
    sample_rates = {
        0b11: (44_100, 48_000, 32_000),
        0b10: (22_050, 24_000, 16_000),
        0b00: (11_025, 12_000, 8_000),
    }
    bitrate = bitrate_tables[version][bitrate_index - 1] * 1_000
    sample_rate = sample_rates[version][sample_rate_index]
    coefficient = 144 if version == 0b11 else 72
    frame_length = coefficient * bitrate // sample_rate + padding
    if position + frame_length > len(value):
        raise ValueError("speech response MP3 audio frame is truncated")


def _safe_response_model(value: Any, api_key: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("response model must be a bounded string or null")
    if api_key is not None and api_key in value:
        return None
    if re.search(r"(?i)\bauthorization\b|\bbearer\s+|\bsk-[A-Za-z0-9_-]{8,}\b", value):
        return None
    return value


def _apply_usage(value: Any, measurement: RequestMeasurement) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("usage must be an object")
    input_tokens = value.get("prompt_tokens", value.get("input_tokens"))
    output_tokens = value.get("completion_tokens", value.get("output_tokens"))
    measurement.provider_reported_input_tokens = _validated_usage_token(
        input_tokens,
        "input_tokens",
    )
    measurement.provider_reported_output_tokens = _validated_usage_token(
        output_tokens,
        "output_tokens",
    )
    measurement.provider_reported_total_tokens = _validated_usage_token(
        value.get("total_tokens"),
        "total_tokens",
    )


def _redact_error(value: str, api_key: str | None) -> str:
    sanitized = value
    if api_key is not None:
        sanitized = sanitized.replace(api_key, "[REDACTED]")
    sanitized = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        sanitized,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", sanitized)


def _validated_usage_token(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"usage.{field_name} must be a non-negative integer")
    return value


def _optional_text(value: dict[str, Any], field_name: str) -> str:
    text = value.get(field_name)
    if text is None:
        return ""
    if not isinstance(text, str):
        raise ValueError(f"{field_name} must be a string or null")
    return text


def _all_gaps_below(timestamps_ns: list[int], threshold_ns: int) -> bool:
    return len(timestamps_ns) > 1 and all(
        timestamps_ns[index] - timestamps_ns[index - 1] < threshold_ns
        for index in range(1, len(timestamps_ns))
    )


def classify_http_error(status: int, body: str) -> ErrorClass:
    lower = body.lower()
    if status == 401:
        return ErrorClass.AUTHENTICATION
    if status == 402:
        return ErrorClass.PAYMENT_REQUIRED
    if status == 403:
        return ErrorClass.AUTHORIZATION
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if status == 404 and "model" in lower:
        return ErrorClass.MODEL_NOT_FOUND
    if status >= 500:
        return ErrorClass.UPSTREAM_5XX
    return ErrorClass.PROTOCOL


def _safe_excerpt(value: str, limit: int = 500) -> str:
    return " ".join(value.replace("\x00", "").split())[:limit]


def _ms(nanoseconds: float) -> float:
    return round(nanoseconds / 1_000_000, 3)
