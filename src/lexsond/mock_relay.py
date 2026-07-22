from __future__ import annotations

import argparse
import base64
import io
import json
import socket
import struct
import time
import wave
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockRelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LexsondMockRelay/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            if getattr(self.server, "reflect_catalog_authorization", False):
                authorization = self.headers.get("Authorization", "")
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [{"id": f"leaked-{authorization}", "object": "model"}],
                    },
                )
                return
            if getattr(self.server, "rich_catalog", False):
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "vision-model",
                                "architecture": {
                                    "input_modalities": ["text", "image"],
                                    "output_modalities": ["text"],
                                },
                            },
                            {
                                "id": "video-model",
                                "architecture": {
                                    "input_modalities": ["text"],
                                    "output_modalities": ["video"],
                                },
                            },
                            {
                                "id": "speech-model",
                                "architecture": {
                                    "input_modalities": ["text"],
                                    "output_modalities": ["speech"],
                                },
                                "supported_voices": ["en-US-Harper:MAI-Voice-2"],
                            },
                        ],
                    },
                )
                return
            self._json(
                200,
                {"object": "list", "data": [{"id": "mock-model", "object": "model"}]},
            )
            return
        self._json(404, _error("not_found", "Route not found"))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.last_post_path = self.path
        self.server.last_content_type = self.headers.get("Content-Type", "").split(
            ";", 1
        )[0].strip().lower()
        self.server.last_accept = self.headers.get("Accept")
        supported_paths = {
            "/v1/chat/completions",
            "/v1/embeddings",
            "/v1/images/generations",
            "/v1/images",
            "/v1/audio/speech",
            "/v1/audio/transcriptions",
        }
        if self.path not in supported_paths:
            self._json(404, _error("not_found", "Route not found"))
            return
        authorization = self.headers.get("Authorization")
        if getattr(self.server, "reject_authorization", False) and authorization:
            self._json(400, _error("unexpected_authorization", "Authorization not allowed"))
            return
        if getattr(self.server, "require_api_key", True) and authorization != "Bearer test-key":
            self._json(401, _error("invalid_api_key", "Invalid API key"))
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_request = self.rfile.read(content_length)
        mode = self.headers.get("X-Mock-Mode", "normal")
        if mode == "slow_header_drip":
            raw_response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            for byte in raw_response:
                try:
                    self.connection.sendall(bytes([byte]))
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.02)
            self.close_connection = True
            return
        if self.path == "/v1/audio/transcriptions":
            if self.server.last_content_type == "application/json":
                try:
                    request = json.loads(raw_request)
                    input_audio = request.get("input_audio", {})
                    base64.b64decode(input_audio.get("data", ""), validate=True)
                    valid = (
                        isinstance(request.get("model"), str)
                        and input_audio.get("format") == "wav"
                    )
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    valid = False
                if not valid:
                    self._json(400, _error("invalid_audio_json", "Missing audio fields"))
                    return
            elif b'name="model"' not in raw_request or b'name="file"' not in raw_request:
                self._json(400, _error("invalid_multipart", "Missing audio fields"))
                return
            self._json(
                200,
                ({"text": {"invalid": True}} if mode == "malformed_endpoint" else {
                    "text": "fixture transcript",
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                }),
            )
            return
        try:
            request = json.loads(raw_request)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, _error("invalid_json", "Malformed JSON"))
            return
        self.server.last_json_request = request

        if mode == "rate_limit":
            self._json(429, _error("rate_limit_exceeded", "Rate limit reached"), {"Retry-After": "1"})
            return
        if mode == "payment_required":
            self._json(402, _error("insufficient_balance", "Insufficient balance"))
            return
        if mode == "model_not_found":
            self._json(404, _error("model_not_found", "Model does not exist"))
            return
        if mode == "server_error":
            self._json(503, _error("upstream_unavailable", "Upstream unavailable"))
            return
        if mode == "slow_error_drip":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            for _ in range(10):
                self._write(b"x")
                time.sleep(0.03)
            self.close_connection = True
            return
        if mode == "reasoning_error":
            self._json(
                400,
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Request rejected",
                        "reasoning_content": "private-reasoning",
                    }
                },
            )
            return
        if mode == "endpoint_secret_error":
            self._json(
                400,
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "private-endpoint-payload",
                    }
                },
            )
            return
        if mode == "slow_endpoint":
            time.sleep(0.12)
        if self.path == "/v1/embeddings":
            if mode == "malformed_endpoint":
                self._json(200, {"data": [{"embedding": {"not": "a vector"}}]})
                return
            self._json(
                200,
                {
                    "object": "list",
                    "model": (
                        "Bearer test-key"
                        if mode == "reflect_response_model"
                        else request.get("model", "embedding-model")
                    ),
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.125, -0.5, 0.75, 1.0],
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
            return
        if self.path in {"/v1/images/generations", "/v1/images"}:
            if mode == "image_url":
                self._json(200, {"data": [{"url": "https://example.test/image.png"}]})
                return
            if mode == "malformed_endpoint":
                image = "private-image-payload"
            elif mode == "truncated_image":
                image = base64.b64encode(b"\x89PNG\r\n\x1a\njunk").decode("ascii")
            elif mode == "image_bomb":
                image = base64.b64encode(_fixture_png_bomb()).decode("ascii")
            elif mode == "fake_jpeg":
                image = base64.b64encode(
                    b"\xff\xd8\xff\xc0\x00\x00\xff\xda" + b"junk" + b"\xff\xd9"
                ).decode("ascii")
            elif mode == "fake_gif":
                image = base64.b64encode(
                    b"GIF89a\x01\x00\x01\x00junk;"
                ).decode("ascii")
            elif mode == "fake_webp":
                image = base64.b64encode(
                    b"RIFF\x0c\x00\x00\x00WEBPVP8 junk"
                ).decode("ascii")
            elif mode == "png_invalid_filter":
                image = base64.b64encode(_fixture_png_with_filter(5)).decode("ascii")
            elif mode == "png_missing_palette":
                image = base64.b64encode(_fixture_indexed_png_without_palette()).decode(
                    "ascii"
                )
            elif mode == "png_invalid_chunk_type":
                image = base64.b64encode(_fixture_png_with_extra_chunk(b"1abc")).decode(
                    "ascii"
                )
            elif mode == "png_invalid_reserved_bit":
                image = base64.b64encode(_fixture_png_with_extra_chunk(b"abca")).decode(
                    "ascii"
                )
            elif mode == "png_too_many_chunks":
                image = base64.b64encode(_fixture_png_with_many_chunks()).decode(
                    "ascii"
                )
            else:
                image = base64.b64encode(_fixture_png()).decode("ascii")
            self._json(200, {"data": [{"b64_json": image}]})
            return
        if self.path == "/v1/audio/speech":
            if request.get("response_format") == "mp3":
                if mode == "malformed_endpoint":
                    audio = b"junk"
                elif mode == "malformed_mp3_header":
                    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00" + (b"0" * 100)
                else:
                    audio = _fixture_mp3()
                content_type = "audio/test-key" if mode == "reflect_metadata" else "audio/mpeg"
                self._bytes(200, audio, content_type)
            else:
                if mode == "malformed_endpoint":
                    audio = b"RIFFprivate-audio-payload"
                elif mode == "truncated_wav":
                    audio = _fixture_wav()[:44]
                else:
                    audio = _fixture_wav()
                content_type = "audio/test-key" if mode == "reflect_metadata" else "audio/wav"
                self._bytes(200, audio, content_type)
            return
        if self.path != "/v1/chat/completions":
            self._json(404, _error("not_found", "Route not found"))
            return
        messages = request.get("messages") or []
        if mode == "native_message_contract":
            expected = [
                {"role": "system", "content": "Follow the exact format."},
                {"role": "user", "content": "Reply now."},
                {"role": "assistant", "content": "Acknowledged."},
                {"role": "user", "content": "Return PROBE_OK."},
            ]
            if messages != expected:
                self._json(400, _error("schema_error", "message contract mismatch"))
                return
        if messages and isinstance(messages[0], dict) and isinstance(messages[0].get("content"), list):
            self._json(
                200,
                {
                    "id": "chatcmpl-vision",
                    "object": "chat.completion",
                    "model": request.get("model", "vision-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "RED"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 16, "completion_tokens": 1, "total_tokens": 17},
                },
            )
            return
        if not request.get("stream", False):
            self._non_streaming(request, mode)
            return
        self._streaming(request, mode)

    def _non_streaming(self, request: dict[str, Any], mode: str) -> None:
        usage = _usage(mode)
        output = "WRONG_OUTPUT" if mode == "wrong_output" else "PROBE_OK"
        if mode == "reflect_output_key":
            output = "test-key"
        if mode == "oversized_json":
            output = "X" * (17 * 1024 * 1024)
        if mode == "whitespace_output":
            output = "   "
        message = {"role": "assistant", "content": output}
        if mode == "reasoning_stream":
            message["reasoning_content"] = "private-reasoningprivate-reasoning"
        self._json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": (
                    "Bearer test-key"
                    if mode == "reflect_response_model"
                    else request.get("model", "mock-model")
                ),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "test-key" if mode == "reflect_metadata" else "stop",
                    }
                ],
                "usage": usage,
            },
        )

    def _streaming(self, request: dict[str, Any], mode: str) -> None:
        self.send_response(200)
        content_type = (
            "text/event-stream; reflected=test-key"
            if mode == "reflect_metadata"
            else "text/event-stream; charset=utf-8"
        )
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        model = request.get("model", "mock-model")
        first_content = "WRONG_" if mode == "wrong_output" else "PROBE_"
        second_content = "OUTPUT" if mode == "wrong_output" else "OK"
        if mode == "reflect_output_key":
            first_content, second_content = "test-", "key"
        if mode == "reflect_then_disconnect":
            first_content, second_content = "test-key", ""
        if mode == "whitespace_output":
            first_content, second_content = "  ", " "
        chunks = [
            _chunk(model, {"role": "assistant", "content": ""}),
            _chunk(model, {"content": first_content}),
            _chunk(model, {"content": second_content}),
            _chunk(model, {}, finish_reason="stop"),
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [],
                "usage": _usage(mode),
            },
        ]
        if mode == "reflect_metadata":
            chunks[3]["choices"][0]["finish_reason"] = "test-key"

        if mode == "slow_ttft":
            time.sleep(0.12)
        if mode == "malformed_sse":
            self._write(b"data: {not-json}\n\n")
            return
        if mode == "invalid_reasoning_content":
            self._write(
                _event_bytes(_chunk(model, {"reasoning_content": {"invalid": True}}))
            )
            return
        if mode == "reasoning_stream":
            reasoning_chunks = [
                _chunk(model, {"role": "assistant", "content": ""}),
                _chunk(model, {"reasoning_content": "private-reasoning"}),
                _chunk(model, {"reasoning_content": "private-reasoning"}),
            ]
            for chunk in reasoning_chunks:
                self._write(_event_bytes(chunk))
                time.sleep(0.01)
            answer_chunks = chunks[1:]
            body = b"".join(_event_bytes(item) for item in answer_chunks)
            self._write(body + b"data: [DONE]\n\n")
            return
        if mode == "single_chunk_stream":
            single_chunks = [
                _chunk(model, {"role": "assistant", "content": ""}),
                _chunk(model, {"content": "PROBE_OK"}),
                _chunk(model, {}, finish_reason="stop"),
                chunks[-1],
            ]
            for chunk in single_chunks:
                self._write(_event_bytes(chunk))
                time.sleep(0.01)
            self._write(b"data: [DONE]\n\n")
            return
        if mode == "event_flood":
            for _ in range(1_100):
                self._write(_event_bytes(_chunk(model, {"content": ""})))
            self._write(b"data: [DONE]\n\n")
            return
        if mode == "pseudo_stream":
            body = b"".join(_event_bytes(item) for item in chunks) + b"data: [DONE]\n\n"
            self._write(body)
            return
        if mode == "done_then_hang":
            body = b"".join(_event_bytes(item) for item in chunks) + b"data: [DONE]\n\n"
            self._write(body)
            time.sleep(0.2)
            return

        for index, chunk in enumerate(chunks):
            self._write(_event_bytes(chunk))
            if mode in {"disconnect", "reflect_then_disconnect"} and index == 1:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            time.sleep(0.03 if mode == "slow_drip" else 0.01)
        self._write(b"data: [DONE]\n\n")

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def _chunk(
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage(mode: str) -> dict[str, int]:
    if mode == "wrong_usage":
        return {"prompt_tokens": 9000, "completion_tokens": 9000, "total_tokens": 18000}
    return {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


def _event_bytes(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "type": "mock_error"}}


def _fixture_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _fixture_png_bomb() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 1_000_000))
        + _png_chunk(b"IEND", b"")
    )


def _fixture_png_with_filter(filter_byte: int) -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes([filter_byte, 255, 0, 0])))
        + _png_chunk(b"IEND", b"")
    )


def _fixture_indexed_png_without_palette() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _fixture_png_with_extra_chunk(kind: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(kind, b"")
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _fixture_png_with_many_chunks() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ancillary = b"".join(_png_chunk(b"aaAa", b"") for _ in range(4_100))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + ancillary
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _fixture_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


def _fixture_mp3() -> bytes:
    frame_header = b"\xff\xfb\x90\x64"
    frame_length = 144 * 128_000 // 44_100
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + frame_header + (
        b"\x00" * (frame_length - len(frame_header))
    )


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    require_api_key: bool = True,
    reject_authorization: bool = False,
    reflect_catalog_authorization: bool = False,
    rich_catalog: bool = False,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MockRelayHandler)
    server.require_api_key = require_api_key
    server.reject_authorization = reject_authorization
    server.reflect_catalog_authorization = reflect_catalog_authorization
    server.rich_catalog = rich_catalog
    server.last_post_path = None
    server.last_content_type = None
    server.last_json_request = None
    server.last_accept = None
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Fault-injecting OpenAI-compatible mock relay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    host, port = server.server_address
    print(f"Mock relay listening on http://{host}:{port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
