from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import PrivateAttr

from ..models import NormalizedRunResult
from ..probe import ProbeConfig, ProbeType, run_openai_probe


ProgressCallback = Callable[[str, Any], None]
NativeRunner = Callable[..., NormalizedRunResult]


class NativeProbeChatModel(BaseChatModel):
    """LangChain chat-model boundary backed by the evidence-first transport.

    ProbeConfig is a private Pydantic attribute so credentials never appear in
    model dumps, reprs, LangChain metadata, callback inputs, or traces. The raw
    provider output stays in a private one-shot result slot and is sanitized by
    the existing persistence boundary after invoke() returns.
    """

    model_name: str
    _probe_config: ProbeConfig = PrivateAttr()
    _progress: ProgressCallback | None = PrivateAttr(default=None)
    _native_runner: NativeRunner = PrivateAttr()
    _result: NormalizedRunResult | None = PrivateAttr(default=None)

    def __init__(
        self,
        probe_config: ProbeConfig,
        *,
        progress: ProgressCallback | None = None,
        native_runner: NativeRunner = run_openai_probe,
    ) -> None:
        super().__init__(
            model_name=probe_config.model,
            name="lexsond-native-chat",
            cache=False,
            callbacks=[],
            tags=["lexsond", "native-observation"],
            metadata={"probe_type": probe_config.probe_type.value},
        )
        self._probe_config = probe_config
        self._progress = progress
        self._native_runner = native_runner

    @property
    def _llm_type(self) -> str:
        return "lexsond-native-observer"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "probe_type": self._probe_config.probe_type.value,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self._result = self._native_runner(
            self._probe_config,
            progress=self._progress,
        )
        safe_status = self._result.status.value
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        response_metadata={"probe_status": safe_status},
                    )
                )
            ]
        )

    def probe(self) -> NormalizedRunResult:
        self.invoke(
            [HumanMessage(content="Execute the configured bounded quality probe")],
            config={"callbacks": [], "run_name": "native-quality-probe"},
        )
        if self._result is None:
            raise RuntimeError("LangChain probe model returned without a result")
        result = self._result
        self._result = None
        return result


def invoke_native_probe(
    config: ProbeConfig,
    *,
    progress: ProgressCallback | None = None,
    native_runner: NativeRunner = run_openai_probe,
) -> NormalizedRunResult:
    """Invoke every component through a LangChain boundary exactly once."""

    if config.probe_type in {ProbeType.CHAT, ProbeType.VISION}:
        return NativeProbeChatModel(
            config,
            progress=progress,
            native_runner=native_runner,
        ).probe()

    result_slot: list[NormalizedRunResult] = []

    def run_safely(_: object) -> dict[str, str]:
        result = native_runner(config, progress=progress)
        result_slot.append(result)
        # Runnable outputs are observable by process-global tracers. Keep raw
        # provider observations in this private, one-shot slot instead.
        return {"probe_status": result.status.value}

    runnable = RunnableLambda(
        run_safely,
        name=f"native-{config.probe_type.value}-quality-probe",
    )
    runnable.invoke(
        None,
        config={
            "callbacks": [],
            "tags": ["lexsond", "native-observation"],
            "metadata": {"probe_type": config.probe_type.value},
        },
    )
    if len(result_slot) != 1:
        raise RuntimeError("LangChain probe runnable returned without one result")
    return result_slot.pop()
