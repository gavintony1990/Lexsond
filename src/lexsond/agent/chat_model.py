from __future__ import annotations

import json
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from ..probe import (
    UnsafeTargetAddress,
    _create_guarded_http_connection,
    _set_connection_deadline_timeout,
    _wrap_connection_with_deadline,
    validate_api_key_value,
    validate_base_url_transport,
)
from ..storage.redaction import redact_text, redact_value


class AgentModelError(RuntimeError):
    """A safe, body-free failure raised by the Agent model transport."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AGENT_MODEL_ERROR",
        http_status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class OpenAICompatibleAgentModel(BaseChatModel):
    """LangChain chat model for bounded Agent decisions and tool calling.

    This model is separate from the measurement transport: it consumes model
    text ephemerally for Agent reasoning, while probe runs continue to use the
    native byte-level observer.  Credentials live only in a private attribute,
    retries are disabled, redirects are rejected, and upstream error bodies are
    never included in exceptions.
    """

    _model_name: str = PrivateAttr()
    _base_url: str = PrivateAttr()
    _api_key: str | None = PrivateAttr(default=None)
    _timeout_seconds: float = PrivateAttr()
    _transport: Callable[..., dict[str, Any]] | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float,
        transport: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        validate_base_url_transport(base_url)
        validate_api_key_value(api_key)
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("model must be a bounded non-empty string")
        if not 0.1 <= float(timeout_seconds) <= 120:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        super().__init__(
            name="lexsond-agent-model",
            cache=False,
            callbacks=[],
            tags=["lexsond", "agent-decision"],
            metadata={"transport": "openai-compatible"},
        )
        self._model_name = model.strip()
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    @property
    def _llm_type(self) -> str:
        return "lexsond-openai-compatible-agent"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def _identifying_params(self) -> dict[str, Any]:
        # LangChain tracers can observe identifying parameters. The target and
        # selected model stay in the control-plane snapshot, not trace metadata.
        return {"transport": "openai-compatible"}

    def _get_ls_params(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"ls_provider": "lexsond", "ls_model_type": "chat"}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice is not None:
            if tool_choice in {"auto", "none", "required"}:
                kwargs["tool_choice"] = tool_choice
            else:
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice},
                }
        return self.bind(tools=formatted, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [_serialize_message(message) for message in messages],
            "stream": False,
            "temperature": 0,
            "max_tokens": 800,
        }
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        if stop:
            payload["stop"] = stop
        response = (
            self._transport(payload=payload)
            if self._transport is not None
            else self._request(payload)
        )
        allowed_tools = _allowed_tool_arguments(tools)
        message = _parse_assistant_message(
            response,
            sensitive_values=(self._api_key,) if self._api_key is not None else (),
            allowed_tools=allowed_tools,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        parsed = urlsplit(self._base_url)
        connection = _create_guarded_http_connection(parsed, self._timeout_seconds)
        path = f"{parsed.path.rstrip('/')}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        deadline_ns = time.perf_counter_ns() + int(
            self._timeout_seconds * 1_000_000_000
        )
        try:
            connection.request("POST", path, body=body, headers=headers)
            _wrap_connection_with_deadline(connection, deadline_ns)
            response = connection.getresponse()
            if response.status != 200:
                mapped = {
                    401: ("AGENT_MODEL_AUTHENTICATION_FAILED", 401),
                    402: ("AGENT_MODEL_PAYMENT_REQUIRED", 402),
                    403: ("AGENT_MODEL_AUTHORIZATION_FAILED", 403),
                    404: ("AGENT_MODEL_NOT_FOUND", 404),
                    429: ("AGENT_MODEL_RATE_LIMITED", 429),
                }.get(response.status, ("AGENT_MODEL_UPSTREAM_ERROR", 502))
                raise AgentModelError(
                    f"Agent model request failed with HTTP {response.status}",
                    code=mapped[0],
                    http_status=mapped[1],
                )
            content = bytearray()
            while True:
                _set_connection_deadline_timeout(connection, deadline_ns)
                chunk = response.read1(65_536)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > 2 * 1024 * 1024:
                    raise AgentModelError("Agent model response exceeded the safe limit")
        except AgentModelError:
            raise
        except UnsafeTargetAddress as exc:
            raise AgentModelError(
                "Agent model target resolved to a blocked network",
                code="TARGET_ADDRESS_BLOCKED",
                http_status=422,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AgentModelError(
                "Agent model request timed out",
                code="AGENT_MODEL_TIMEOUT",
                http_status=504,
            ) from exc
        except (ConnectionError, socket.gaierror, ssl.SSLError, OSError) as exc:
            raise AgentModelError("Agent model request could not reach the target") from exc
        finally:
            connection.close()
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentModelError("Agent model returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise AgentModelError("Agent model response must be a JSON object")
        return value


def _serialize_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _text_content(message.content)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _text_content(message.content)}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": _text_content(message.content),
        }
    if isinstance(message, AIMessage):
        value: dict[str, Any] = {
            "role": "assistant",
            "content": _text_content(message.content),
        }
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(
                            call.get("args", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return value
    raise ValueError(f"unsupported Agent message type: {type(message).__name__}")


def _parse_assistant_message(
    value: dict[str, Any],
    *,
    sensitive_values: tuple[str, ...] = (),
    allowed_tools: dict[str, set[str]] | None = None,
) -> AIMessage:
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AgentModelError("Agent model response has no choice")
    raw = choices[0].get("message")
    if not isinstance(raw, dict):
        raise AgentModelError("Agent model response has no assistant message")
    content = raw.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str) or len(content) > 100_000:
        raise AgentModelError("Agent model content is not a bounded string")
    safe_content = redact_text(content, sensitive_values=sensitive_values)
    tool_calls: list[dict[str, Any]] = []
    raw_calls = raw.get("tool_calls", [])
    if not isinstance(raw_calls, list) or len(raw_calls) > 12:
        raise AgentModelError("Agent model tool_calls are invalid")
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise AgentModelError("Agent model tool call is malformed")
        function = call["function"]
        name, arguments = function.get("name"), function.get("arguments", "{}")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
            or len(arguments) > 100_000
        ):
            raise AgentModelError("Agent model tool call is malformed")
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise AgentModelError("Agent model tool arguments are malformed") from exc
        if not isinstance(args, dict):
            raise AgentModelError("Agent model tool arguments must be an object")
        permitted = (allowed_tools or {}).get(name)
        safe_name = name if permitted is not None else "unknown-tool"
        safe_args = (
            {
                key: redact_value(item, sensitive_values=sensitive_values)
                for key, item in args.items()
                if key in permitted
            }
            if permitted is not None
            else {}
        )
        tool_calls.append(
            {
                "name": safe_name,
                "args": safe_args,
                # Provider-controlled IDs are unnecessary for our bounded loop
                # and could otherwise become a trace or checkpoint side channel.
                "id": f"tool-call-{index + 1}",
                "type": "tool_call",
            }
        )
    return AIMessage(
        content=safe_content,
        tool_calls=tool_calls,
        response_metadata={"transport_status": "accepted"},
    )


def _allowed_tool_arguments(tools: object) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    if not isinstance(tools, list):
        return values
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
            continue
        function = tool["function"]
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties", {})
        if isinstance(properties, dict):
            values[name] = {key for key in properties if isinstance(key, str)}
    return values


def _text_content(value: str | list[str | dict[str, Any]]) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
