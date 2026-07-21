from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tracers.context import collect_runs

from lexsond.agent.chat_model import AgentModelError, OpenAICompatibleAgentModel
from lexsond.agent.service import AgentCoordinator
from lexsond.web.app import create_app
from lexsond.web.control_service import ControlPlaneService
from lexsond.web.control_store import ControlPlaneConflict, ControlPlaneStore


class _ToolCallingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.bound_tools = []

    def bind_tools(self, tools, **kwargs):
        del kwargs
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages, config=None):
        del config
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_probe_targets",
                        "args": {"limit": 5},
                        "id": "call-targets",
                        "type": "tool_call",
                    }
                ],
            )
        self.last_messages = messages
        return AIMessage(content="已读取目标清单，建议先运行聊天协议探针。")


class _FailingModel:
    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages, config=None):
        del messages, config
        raise AgentModelError("safe model failure")


class _BlockingModel:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages, config=None):
        del messages, config
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test model was not released")
        return AIMessage(content="completed")


class AgentCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "agent.sqlite3"
        self.store = ControlPlaneStore(self.database)
        self.target = self.store.create_target(
            {
                "name": "Agent mock target",
                "target_kind": "local",
                "provider_id": None,
                "base_url": "http://127.0.0.1:8091/v1",
                "default_model": "mock-model",
                "credential_ref": None,
            }
        )

    def test_agent_uses_langchain_tool_loop_and_checkpoints_messages(self) -> None:
        fake_model = _ToolCallingModel()
        coordinator = AgentCoordinator(
            self.store,
            model_factory=lambda **_: fake_model,
        )
        session = coordinator.create_session(
            title="连接诊断",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )

        response = coordinator.respond(
            session["session_id"],
            content="帮我判断这个目标下一步该测什么",
            api_key=None,
            timeout_seconds=10,
        )

        self.assertEqual(fake_model.calls, 2)
        self.assertEqual(response["message"]["role"], "assistant")
        self.assertIn("聊天协议探针", response["message"]["content"])
        self.assertEqual(
            [message["role"] for message in self.store.list_agent_messages(session["session_id"])],
            ["user", "assistant"],
        )
        events = self.store.list_agent_events(session["session_id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["LLM_STARTED", "TOOL_STARTED", "TOOL_COMPLETED", "LLM_STARTED", "LLM_COMPLETED"],
        )
        self.assertIn("list_probe_targets", [tool.name for tool in fake_model.bound_tools])

    def test_secrets_are_redacted_before_memory_model_and_events(self) -> None:
        fake_model = _ToolCallingModel()
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: fake_model)
        session = coordinator.create_session(
            title="Secret boundary",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        secret = "sk-agent-secret-value-123456"

        coordinator.respond(
            session["session_id"],
            content=f"不要保存这个 {secret}",
            api_key=secret,
            timeout_seconds=10,
        )

        durable = self.database.read_bytes()
        self.assertNotIn(secret.encode(), durable)
        self.assertNotIn(secret, json.dumps(fake_model.last_messages, default=str))
        self.assertIn("[REDACTED]", json.dumps(fake_model.last_messages, default=str))

    def test_agent_chat_model_keeps_credentials_private(self) -> None:
        secret = "sk-private-agent-model-key"
        model = OpenAICompatibleAgentModel(
            base_url="https://api.example.com/v1",
            api_key=secret,
            model="chat-model",
            timeout_seconds=5,
        )

        self.assertNotIn(secret, repr(model))
        self.assertNotIn(secret, str(model.model_dump()))

    def test_agent_chat_model_parses_tool_calls_through_langchain_once(self) -> None:
        payloads = []

        def transport(*, payload):
            payloads.append(payload)
            return {
                "model": "chat-model",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "list_probe_targets",
                                        "arguments": '{"limit":3}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            }

        secret = "sk-private-agent-model-key"
        model = OpenAICompatibleAgentModel(
            base_url="https://api.example.com/v1",
            api_key=secret,
            model="chat-model",
            timeout_seconds=5,
            transport=transport,
        )
        runnable = model.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_probe_targets",
                        "description": "list targets",
                        "parameters": {
                            "type": "object",
                            "properties": {"limit": {"type": "integer"}},
                        },
                    },
                }
            ]
        )

        reply = runnable.invoke([HumanMessage(content="检查目标")], config={"callbacks": []})

        self.assertEqual(len(payloads), 1)
        self.assertEqual(reply.tool_calls[0]["name"], "list_probe_targets")
        self.assertEqual(reply.tool_calls[0]["args"], {"limit": 3})
        self.assertNotIn(secret, json.dumps(payloads))

    def test_agent_model_and_tool_traces_receive_only_redacted_values(self) -> None:
        secret = "plain-agent-runtime-secret"
        calls = 0

        def transport(*, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "model": secret,
                    "choices": [{"message": {
                        "content": secret,
                        "tool_calls": [{
                            "id": secret,
                            "type": "function",
                            "function": {
                                "name": "design_probe_plan",
                                "arguments": json.dumps({
                                    "target_id": self.target["id"],
                                    "symptom": secret,
                                    "probe_type": "chat",
                                    secret: secret,
                                }),
                            },
                        }],
                    }}],
                }
            return {"choices": [{"message": {"content": f"done {secret}"}}]}

        def factory(**values):
            return OpenAICompatibleAgentModel(**values, transport=transport)

        coordinator = AgentCoordinator(self.store, model_factory=factory)
        session = coordinator.create_session(
            title="Trace boundary",
            target_id=self.target["id"],
            model=None,
            skill_id="probe-planner",
        )
        with collect_runs() as collector:
            result = coordinator.respond(
                session["session_id"],
                content="design a bounded check",
                api_key=secret,
                timeout_seconds=10,
            )

        observed = json.dumps(
            [{"inputs": run.inputs, "outputs": run.outputs, "extra": run.extra} for run in collector.traced_runs],
            default=str,
        )
        self.assertNotIn(secret, observed)
        self.assertNotIn(secret, json.dumps(result, default=str))
        self.assertNotIn(secret.encode(), self.database.read_bytes())
        self.assertIn("[REDACTED]", result["message"]["content"])

    def test_submitted_key_collision_quarantines_the_session(self) -> None:
        secret = "plain-agent-key-collision-value"
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: _ToolCallingModel())
        session = coordinator.create_session(
            title="Collision boundary",
            target_id=self.target["id"],
            model=secret,
            skill_id="connection-diagnosis",
        )

        with self.assertRaisesRegex(ValueError, "persisted Agent field"):
            coordinator.respond(
                session["session_id"],
                content="check it",
                api_key=secret,
                timeout_seconds=10,
            )

        quarantined = self.store.get_agent_session(
            session["session_id"], include_archived=True
        )
        self.assertIsNotNone(quarantined["archived_at"])
        self.assertEqual(quarantined["model"], "[REDACTED]")
        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_future_key_in_checkpoint_is_scrubbed_before_second_turn(self) -> None:
        secret = "plain-future-agent-key-456789"
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: _ToolCallingModel())
        session = coordinator.create_session(
            title="Two turn collision",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        coordinator.respond(
            session["session_id"],
            content=f"remember this ordinary value {secret}",
            api_key=None,
            timeout_seconds=10,
        )
        self.assertIn(secret.encode(), self.database.read_bytes())

        with self.assertRaisesRegex(ValueError, "persisted Agent field"):
            coordinator.respond(
                session["session_id"],
                content="now use the same value as a key",
                api_key=secret,
                timeout_seconds=10,
            )

        quarantined = self.store.get_agent_session(
            session["session_id"], include_archived=True
        )
        self.assertIsNotNone(quarantined["archived_at"])
        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_concurrent_turn_cannot_write_a_key_after_the_atomic_scan(self) -> None:
        secret = "plain-concurrent-agent-key-456789"
        entered, release = threading.Event(), threading.Event()
        coordinator = AgentCoordinator(
            self.store,
            model_factory=lambda **_: _BlockingModel(entered, release),
        )
        session = coordinator.create_session(
            title="Concurrent boundary",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        errors: list[Exception] = []

        def key_turn() -> None:
            try:
                coordinator.respond(
                    session["session_id"],
                    content="authorized key turn",
                    api_key=secret,
                    timeout_seconds=10,
                )
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        thread = threading.Thread(target=key_turn)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        with self.assertRaisesRegex(ControlPlaneConflict, "active turn"):
            coordinator.respond(
                session["session_id"],
                content=f"concurrent prompt {secret}",
                api_key=None,
                timeout_seconds=10,
            )
        release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_turn_lease_covers_all_four_model_timeouts(self) -> None:
        captured: list[float] = []
        original_claim = self.store.claim_agent_turn

        def claim(session_id: str, *, lease_seconds: float) -> str:
            captured.append(lease_seconds)
            return original_claim(session_id, lease_seconds=lease_seconds)

        self.store.claim_agent_turn = claim  # type: ignore[method-assign]
        coordinator = AgentCoordinator(
            self.store,
            model_factory=lambda **_: _ToolCallingModel(),
        )
        session = coordinator.create_session(
            title="Lease envelope",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        coordinator.respond(
            session["session_id"],
            content="check the lease envelope",
            api_key=None,
            timeout_seconds=120,
        )

        self.assertEqual(captured, [540.0])

    def test_session_rejects_a_credential_disguised_as_model(self) -> None:
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: _ToolCallingModel())
        secret = "sk-model-field-secret-123456"

        with self.assertRaisesRegex(ValueError, "model must not contain a credential"):
            coordinator.create_session(
                title="Rejected model",
                target_id=self.target["id"],
                model=secret,
                skill_id="connection-diagnosis",
            )

        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_model_failure_records_a_safe_terminal_event(self) -> None:
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: _FailingModel())
        session = coordinator.create_session(
            title="Failure trace",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        secret = "sk-failure-event-secret-123456"

        with self.assertRaisesRegex(AgentModelError, "safe model failure"):
            coordinator.respond(
                session["session_id"],
                content=f"检查失败 {secret}",
                api_key=secret,
                timeout_seconds=10,
            )

        events = self.store.list_agent_events(session["session_id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["LLM_STARTED", "LLM_FAILED"],
        )
        self.assertNotIn(secret, json.dumps(events))
        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_agent_repository_enforces_archive_and_field_contracts(self) -> None:
        coordinator = AgentCoordinator(self.store, model_factory=lambda **_: _ToolCallingModel())
        session = coordinator.create_session(
            title="Repository boundary",
            target_id=self.target["id"],
            model=None,
            skill_id="connection-diagnosis",
        )
        with self.assertRaises(ValueError):
            self.store.update_agent_session(
                session["session_id"],
                {"title": "sk-store-title-secret-123456"},
                expected_version=session["version"],
            )
        with self.assertRaises(ValueError):
            self.store.append_agent_event(
                session["session_id"],
                event_type="bogus",
                name="",
                status="BOGUS",
            )
        with self.assertRaises(ValueError):
            self.store.append_agent_message(
                session["session_id"],
                role="user",
                content="safe",
                metadata={"nested": {"credential_ref": "not-durable"}},
            )
        with self.assertRaises(ValueError):
            self.store.append_agent_event(
                session["session_id"],
                event_type="LLM_STARTED",
                name="langchain-agent-model",
                status="RUNNING",
                payload={"nested": {"access_token": "not-durable"}},
            )
        invalid = {
            "title": "Invalid direct session",
            "target_id": self.target["id"],
            "target_version": self.target["version"],
            "base_url": "https://user:password@example.invalid/v1",
            "target_kind": "cloud",
            "provider_id": None,
            "model": "mock",
            "skill_id": "connection-diagnosis",
        }
        with self.assertRaises(ValueError):
            self.store.create_agent_session(invalid)
        invalid["base_url"] = "https://example.invalid/v1"
        invalid["skill_id"] = "连接诊断"
        with self.assertRaises(ValueError):
            self.store.create_agent_session(invalid)

        stale_token = self.store.claim_agent_turn(
            session["session_id"], lease_seconds=1
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE agent_sessions SET turn_lease_until = ? WHERE session_id = ?",
                ("1970-01-01T00:00:00+00:00", session["session_id"]),
            )
        fresh_token = self.store.claim_agent_turn(
            session["session_id"], lease_seconds=30
        )
        with self.assertRaisesRegex(ControlPlaneConflict, "fencing token"):
            self.store.append_agent_message(
                session["session_id"],
                role="user",
                content="stale writer",
                metadata={},
                turn_token=stale_token,
            )
        self.store.renew_agent_turn(
            session["session_id"], fresh_token, lease_seconds=30
        )
        self.store.release_agent_turn(session["session_id"], fresh_token)
        with self.assertRaises(sqlite3.IntegrityError):
            with sqlite3.connect(self.database) as connection:
                connection.execute(
                    "UPDATE agent_sessions SET turn_lease_token = ? WHERE session_id = ?",
                    ("broken-pair", session["session_id"]),
                )

        self.store.archive_agent_session(session["session_id"])
        with self.assertRaisesRegex(Exception, "archived Agent session"):
            self.store.append_agent_event(
                session["session_id"],
                event_type="LLM_COMPLETED",
                name="langchain-agent-model",
                status="PASS",
            )
        self.store.archive_target(self.target["id"])
        with self.assertRaisesRegex(Exception, "target must be restored"):
            self.store.restore_agent_session(session["session_id"])


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project_root = Path(__file__).resolve().parents[1]
        fake_model = _ToolCallingModel()
        self.service = ControlPlaneService(
            database_path=Path(self.temporary.name) / "api.sqlite3",
            default_suite_path=project_root / "suites/canary/openai-compatible.json",
            agent_model_factory=lambda **_: fake_model,
        )
        self.addCleanup(self.service.close)
        self.client = TestClient(
            create_app(
                service=self.service,
                frontend_path=Path(self.temporary.name) / "missing-dist",
            )
        )
        self.target = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Agent API target",
                "target_kind": "local",
                "provider_id": None,
                "base_url": "http://127.0.0.1:8091/v1",
                "default_model": "mock-model",
                "credential_ref": None,
            },
        ).json()["data"]

    def test_session_message_and_catalog_api(self) -> None:
        bootstrap = self.client.get("/api/v1/agent/bootstrap")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertTrue(bootstrap.json()["data"]["tools"])
        self.assertTrue(bootstrap.json()["data"]["skills"])

        created = self.client.post(
            "/api/v1/agent/sessions",
            json={
                "title": "API diagnosis",
                "target_id": self.target["id"],
                "model": None,
                "skill_id": "connection-diagnosis",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        session = created.json()["data"]

        reply = self.client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/messages",
            json={"content": "检查最近探针", "api_key": None, "timeout_seconds": 10},
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["data"]["message"]["role"], "assistant")
        history = self.client.get(
            f"/api/v1/agent/sessions/{session['session_id']}/messages"
        ).json()["data"]
        self.assertEqual(len(history), 2)

        archived = self.client.delete(f"/api/v1/agent/sessions/{session['session_id']}")
        self.assertEqual(archived.status_code, 200)
        restored = self.client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/restore"
        )
        self.assertEqual(restored.status_code, 200)


if __name__ == "__main__":
    unittest.main()
