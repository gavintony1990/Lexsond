from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ..probe import validate_api_key_value
from ..storage.redaction import redact_text, redact_value
from .catalog import get_skill, public_skills, public_tools
from .chat_model import AgentModelError, OpenAICompatibleAgentModel
from .tools import build_agent_tools


AgentModelFactory = Callable[..., Any]
CredentialValidator = Callable[[dict[str, Any], str], None]


class AgentCoordinator:
    """Application service for LangChain decisions, Tools, Skills, and memory."""

    def __init__(
        self,
        store: Any,
        *,
        model_factory: AgentModelFactory | None = None,
        credential_validator: CredentialValidator | None = None,
    ) -> None:
        self.store = store
        self.model_factory = model_factory or OpenAICompatibleAgentModel
        self.credential_validator = credential_validator

    def bootstrap(self) -> dict[str, Any]:
        sessions = self.store.list_agent_sessions()
        return {
            "runtime": {
                "framework": "LangChain",
                "model_adapter": "OpenAI-compatible BaseChatModel",
                "memory": "repository checkpointer",
                "max_iterations": 4,
                "automatic_model_retries": 0,
                "billable_tools_enabled": False,
            },
            "tools": public_tools(),
            "skills": public_skills(),
            "stats": {"sessions": len(sessions)},
        }

    def create_session(
        self,
        *,
        title: str,
        target_id: str,
        model: str | None,
        skill_id: str,
    ) -> dict[str, Any]:
        skill = get_skill(skill_id)
        target = self.store.get_target(target_id)
        model_name = (model or target["default_model"]).strip()
        if not model_name or len(model_name) > 256:
            raise ValueError("model is required and must be at most 256 characters")
        if redact_text(model_name) != model_name:
            raise ValueError("model must not contain a credential")
        title_value = redact_text(title.strip())
        if not title_value or len(title_value) > 120:
            raise ValueError("title must be between 1 and 120 characters")
        return self.store.create_agent_session(
            {
                "title": title_value,
                "target_id": target["id"],
                "target_version": target["version"],
                "base_url": target["base_url"],
                "target_kind": target["target_kind"],
                "provider_id": target["provider_id"],
                "model": model_name,
                "skill_id": skill.id,
            }
        )

    def update_session(
        self,
        session_id: str,
        *,
        version: int,
        title: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if title is not None:
            value = redact_text(title.strip())
            if not value or len(value) > 120:
                raise ValueError("title must be between 1 and 120 characters")
            changes["title"] = value
        if skill_id is not None:
            changes["skill_id"] = get_skill(skill_id).id
        return self.store.update_agent_session(
            session_id,
            changes,
            expected_version=version,
        )

    def respond(
        self,
        session_id: str,
        *,
        content: str,
        api_key: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        lease_seconds = min((float(timeout_seconds) * 4.0) + 60.0, 600.0)
        token = self.store.claim_agent_turn(
            session_id,
            # One turn may make four sequential model calls. Cover the full
            # worst-case loop plus tool/checkpoint overhead so the lease cannot
            # expire between iterations and reopen the history race.
            lease_seconds=lease_seconds,
        )
        stop_heartbeat = threading.Event()

        def renew_lease() -> None:
            interval = min(max(lease_seconds / 3.0, 5.0), 30.0)
            while not stop_heartbeat.wait(interval):
                try:
                    self.store.renew_agent_turn(
                        session_id,
                        token,
                        lease_seconds=lease_seconds,
                    )
                except Exception:
                    # Every write is fenced by token as the final authority. A
                    # transient renewal failure is safe; a stolen/expired lease
                    # makes subsequent writes fail closed.
                    return

        heartbeat = threading.Thread(
            target=renew_lease,
            name=f"agent-turn-heartbeat-{session_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._respond_claimed(
                session_id,
                content=content,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                turn_token=token,
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)
            self.store.release_agent_turn(session_id, token)

    def _respond_claimed(
        self,
        session_id: str,
        *,
        content: str,
        api_key: str | None,
        timeout_seconds: float,
        turn_token: str,
    ) -> dict[str, Any]:
        session = self.store.get_agent_session(session_id)
        if api_key is not None:
            validate_api_key_value(api_key)
            if self.store.quarantine_agent_session_credential(
                session_id,
                api_key,
                turn_token=turn_token,
            ):
                # The value only becomes known to be a credential when it is
                # submitted through SecretStr. The repository atomically scans
                # and scrubs the entire checkpoint before any model call.
                raise ValueError("api_key must not appear in a persisted Agent field")
        if session["target_kind"] == "cloud" and not api_key:
            raise ValueError("api_key is required for a cloud Agent target")
        target = self.store.get_target(session["target_id"])
        if api_key is not None and self.credential_validator is not None:
            self.credential_validator(target, api_key)

        safe_user_text = redact_text(
            content.strip(),
            sensitive_values=(api_key,) if api_key is not None else (),
        )
        if not safe_user_text or len(safe_user_text) > 4_000:
            raise ValueError("content must be between 1 and 4000 characters")
        self.store.append_agent_message(
            session_id,
            role="user",
            content=safe_user_text,
            metadata={"redaction_applied": safe_user_text != content.strip()},
            turn_token=turn_token,
        )

        skill = get_skill(session["skill_id"])
        sensitive_values = (api_key,) if api_key is not None else ()
        tools = build_agent_tools(
            self.store,
            skill.allowed_tools,
            sensitive_values=sensitive_values,
        )
        tools_by_name = {tool.name: tool for tool in tools}
        model = self.model_factory(
            base_url=session["base_url"],
            api_key=api_key,
            model=session["model"],
            timeout_seconds=timeout_seconds,
        )
        runnable = model.bind_tools(tools)
        messages = self._langchain_messages(
            session,
            skill.system_prompt,
            sensitive_values=sensitive_values,
        )
        turn_events: list[dict[str, Any]] = []
        final_text = ""

        for iteration in range(1, 5):
            turn_events.append(
                self.store.append_agent_event(
                    session_id,
                    event_type="LLM_STARTED",
                    name="langchain-agent-model",
                    status="RUNNING",
                    payload={"iteration": iteration},
                    turn_token=turn_token,
                )
            )
            try:
                response = runnable.invoke(
                    messages,
                    config={
                        "callbacks": [],
                        "run_name": "lexsond-agent-turn",
                        "tags": ["lexsond", "agent"],
                        "metadata": {
                            "session_id": session_id,
                            "skill_id": skill.id,
                            "iteration": iteration,
                        },
                    },
                )
            except Exception as exc:
                failure_code = (
                    exc.code if isinstance(exc, AgentModelError) else "MODEL_CALL_FAILED"
                )
                turn_events.append(
                    self.store.append_agent_event(
                        session_id,
                        event_type="LLM_FAILED",
                        name="langchain-agent-model",
                        status="FAIL",
                        payload={"iteration": iteration, "code": failure_code},
                        turn_token=turn_token,
                    )
                )
                raise
            if not isinstance(response, AIMessage):
                raise RuntimeError("LangChain Agent model returned an invalid message")
            safe_content = redact_text(
                _message_text(response.content),
                sensitive_values=sensitive_values,
            )[:12_000]
            safe_calls = [
                _safe_tool_call(
                    call,
                    tools_by_name,
                    sensitive_values=sensitive_values,
                    call_id=f"tool-call-{iteration}-{index}",
                )
                for index, call in enumerate(response.tool_calls, start=1)
            ][:12]
            messages.append(
                AIMessage(content=safe_content, tool_calls=safe_calls)
            )
            if not safe_calls:
                final_text = safe_content or "模型没有返回可展示的诊断结论。"
                turn_events.append(
                    self.store.append_agent_event(
                        session_id,
                        event_type="LLM_COMPLETED",
                        name="langchain-agent-model",
                        status="PASS",
                        payload={"iteration": iteration, "tool_calls": 0},
                        turn_token=turn_token,
                    )
                )
                break

            for call in safe_calls:
                tool_name = str(call.get("name", ""))
                tool = tools_by_name.get(tool_name)
                event_name = tool_name if tool is not None else "unknown-tool"
                turn_events.append(
                    self.store.append_agent_event(
                        session_id,
                        event_type="TOOL_STARTED",
                        name=event_name,
                        status="RUNNING",
                        payload={"iteration": iteration},
                        turn_token=turn_token,
                    )
                )
                if tool is None:
                    output: Any = {
                        "error": {
                            "code": "TOOL_NOT_ALLOWED",
                            "message": "The selected Skill does not allow this tool",
                        }
                    }
                    status = "FAIL"
                else:
                    try:
                        # The call was schema-filtered and credential-redacted
                        # before entering StructuredTool, whose inputs/outputs
                        # may be observed by process-global LangChain tracers.
                        output = tool.invoke(call.get("args", {}))
                        status = "PASS"
                    except Exception:
                        output = {
                            "error": {
                                "code": "TOOL_ERROR",
                                "message": "The tool could not complete safely",
                            }
                        }
                        status = "FAIL"
                safe_output = redact_value(
                    output,
                    sensitive_values=sensitive_values,
                )
                encoded = json.dumps(
                    safe_output,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                if len(encoded) > 30_000:
                    encoded = json.dumps(
                        {"error": {"code": "TOOL_OUTPUT_LIMIT", "message": "Tool output was truncated"}},
                        separators=(",", ":"),
                    )
                    status = "WARN"
                messages.append(
                    ToolMessage(content=encoded, tool_call_id=str(call.get("id", "unknown")))
                )
                turn_events.append(
                    self.store.append_agent_event(
                        session_id,
                        event_type="TOOL_COMPLETED",
                        name=event_name,
                        status=status,
                        payload={
                            "iteration": iteration,
                            "result_keys": sorted(safe_output) if isinstance(safe_output, dict) else [],
                        },
                        turn_token=turn_token,
                    )
                )
        else:
            final_text = "已达到四轮工具调用上限。请缩小问题范围后继续，或在新建运行页确认建议的探测方案。"
            turn_events.append(
                self.store.append_agent_event(
                    session_id,
                    event_type="LLM_COMPLETED",
                    name="langchain-agent-model",
                    status="WARN",
                    payload={"iteration": 4, "reason": "ITERATION_LIMIT"},
                    turn_token=turn_token,
                )
            )

        assistant = self.store.append_agent_message(
            session_id,
            role="assistant",
            content=final_text,
            metadata={"skill_id": skill.id, "iterations": min(iteration, 4)},
            turn_token=turn_token,
        )
        return {"session": self.store.get_agent_session(session_id), "message": assistant, "events": turn_events}

    def _langchain_messages(
        self,
        session: dict[str, Any],
        system_prompt: str,
        *,
        sensitive_values: tuple[str, ...],
    ) -> list[Any]:
        safe_base_url = redact_text(
            session["base_url"], sensitive_values=sensitive_values
        )
        safe_model = redact_text(session["model"], sensitive_values=sensitive_values)
        context = (
            f"\n当前会话目标：{safe_base_url}；模型：{safe_model}；"
            f"目标类型：{session['target_kind']}。这些是创建会话时冻结的非机密快照。"
        )
        messages: list[Any] = [SystemMessage(content=system_prompt + context)]
        for message in self.store.list_agent_messages(session["session_id"], limit=30):
            safe_content = redact_text(
                message["content"], sensitive_values=sensitive_values
            )
            if message["role"] == "user":
                messages.append(HumanMessage(content=safe_content))
            else:
                messages.append(AIMessage(content=safe_content))
        return messages


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_tool_call(
    call: dict[str, Any],
    tools_by_name: dict[str, Any],
    *,
    sensitive_values: tuple[str, ...],
    call_id: str,
) -> dict[str, Any]:
    raw_name = call.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name in tools_by_name else "unknown-tool"
    tool = tools_by_name.get(name)
    raw_args = call.get("args", {})
    allowed_fields: set[str] = set()
    if tool is not None and tool.args_schema is not None:
        allowed_fields = set(tool.args_schema.model_fields)
    args = (
        {
            key: redact_value(value, sensitive_values=sensitive_values)
            for key, value in raw_args.items()
            if isinstance(key, str) and key in allowed_fields
        }
        if isinstance(raw_args, dict)
        else {}
    )
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}
