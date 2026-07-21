from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any, Mapping


COMPONENT_RUN_SCHEMA_VERSION = "probe.ai/component-run/v1alpha2"


class ComponentStepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ProbeStepSpec:
    step_id: str
    stage: str
    label: str
    description: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.step_id,
            "stage": self.stage,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ProbeComponentSpec:
    component_id: str
    label: str
    icon: str
    scenario: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    steps: tuple[ProbeStepSpec, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.component_id,
            "label": self.label,
            "icon": self.icon,
            "scenario": self.scenario,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "steps": [step.to_public_dict() for step in self.steps],
        }


_STAGES = (
    "PRECHECK",
    "FIXTURE",
    "REQUEST",
    "TRANSPORT",
    "PROTOCOL",
    "ASSERTION",
    "EVIDENCE",
)
_STEP_IDS = (
    "target_binding",
    "fixture_prepare",
    "request_dispatch",
    "transport_check",
    "response_validate",
    "quality_assert",
    "evidence_seal",
)


def _steps(labels: tuple[str, ...], descriptions: tuple[str, ...]) -> tuple[ProbeStepSpec, ...]:
    if len(labels) != len(_STEP_IDS) or len(descriptions) != len(_STEP_IDS):
        raise ValueError("component step definitions must cover every stage")
    return tuple(
        ProbeStepSpec(step_id, stage, label, description)
        for step_id, stage, label, description in zip(
            _STEP_IDS,
            _STAGES,
            labels,
            descriptions,
            strict=True,
        )
    )


_COMPONENTS = (
    ProbeComponentSpec(
        "chat",
        "文本对话检测",
        "TXT",
        "文本输入 → 文本响应",
        ("text",),
        ("text",),
        _steps(
            (
                "绑定聊天能力",
                "生成确定性文本探针",
                "发送 Chat 请求",
                "检查 HTTP 通道",
                "解析 JSON / SSE",
                "核验非空回答与终止",
                "封存脱敏证据",
            ),
            (
                "核对目录能力与所选聊天端点，错误绑定不会发出模型请求。",
                "使用固定短提示和有界输出上限，不采集用户业务提示词。",
                "通过厂商适配器构造并发送 OpenAI 兼容聊天请求。",
                "记录连接、响应头、状态码与首字节时延。",
                "流式模式逐事件校验 SSE，非流式模式校验 JSON 响应结构。",
                "要求回答非空；流式响应必须出现合法终止语义。",
                "清除原始回答与错误正文，只保留哈希、长度和测量值。",
            ),
        ),
    ),
    ProbeComponentSpec(
        "vision",
        "视觉理解检测",
        "VIS",
        "文本 + 图像输入 → 文本回答",
        ("text", "image"),
        ("text",),
        _steps(
            (
                "绑定视觉能力",
                "生成红色图像夹具",
                "发送图文消息",
                "检查多模态通道",
                "解析视觉响应",
                "断言颜色回答为 RED",
                "封存视觉证据",
            ),
            (
                "确认模型目录明确支持图像输入与文本输出，未知能力需人工选择。",
                "本地生成 64×64 红色 PNG；图像字节不会进入历史库。",
                "将固定问题与内置图像按内嵌数据 URI 组成单次请求。",
                "记录连接、HTTP 状态和首响应时间，不跟随外部图像 URL。",
                "校验 Chat JSON 或 SSE 事件、分片和终止标记。",
                "将脱敏前回答与唯一允许值 RED 做精确比较。",
                "只保留答案哈希、字符数、时延和断言结果。",
            ),
        ),
    ),
    ProbeComponentSpec(
        "embedding",
        "向量嵌入检测",
        "VEC",
        "文本输入 → 数值向量",
        ("text",),
        ("embeddings",),
        _steps(
            (
                "绑定向量端点",
                "准备文本向量夹具",
                "发送 Embeddings 请求",
                "检查向量通道",
                "解析向量数组",
                "核验维度与有限数值",
                "封存向量元数据",
            ),
            (
                "确认模型声明 Embeddings 端点，避免误向聊天模型发送计费请求。",
                "使用固定短文本，不读取或持久化用户语料。",
                "向 /embeddings 发送一个有界输入样本。",
                "记录 HTTP 状态、首字节和端到端时延。",
                "要求响应 data 为非空向量数组并具有合法对象结构。",
                "所有向量必须同维，且每个元素都为有限数值。",
                "只保存向量数量、维度和用量；原始向量不落盘。",
            ),
        ),
    ),
    ProbeComponentSpec(
        "image_generation",
        "图像生成检测",
        "IMG",
        "文本提示 → PNG 图像",
        ("text",),
        ("image",),
        _steps(
            (
                "绑定图像生成端点",
                "准备受控图像提示",
                "发送 Images 请求",
                "检查图像通道",
                "解码图像传输",
                "验证 PNG 可完整解码",
                "封存图像元数据",
            ),
            (
                "确认模型声明图像输出，错误模态在发请求前终止。",
                "使用固定红色方块提示和单图输出上限。",
                "按厂商协议选择 /images 或 /images/generations。",
                "限制响应体大小并记录 HTTP 与时延证据。",
                "当前严格模式只接受完整 base64 PNG；远程 URL 不判定通过。",
                "校验签名、CRC、关键块、解压上限、扫描行和过滤器。",
                "只保存格式、数量、字节边界与用量；图像字节不落盘。",
            ),
        ),
    ),
    ProbeComponentSpec(
        "audio_speech",
        "语音合成检测",
        "TTS",
        "文本输入 → 可播放音频",
        ("text",),
        ("audio",),
        _steps(
            (
                "绑定语音与音色",
                "准备短文本夹具",
                "发送 Speech 请求",
                "检查音频通道",
                "核对音频内容类型",
                "验证音频帧可播放",
                "封存音频元数据",
            ),
            (
                "确认语音输出能力；需要音色声明的厂商必须先绑定合法 voice。",
                "使用固定短句和单次音频请求，不使用用户文本。",
                "向 /audio/speech 发送厂商适配后的 WAV 或 MP3 请求。",
                "记录 HTTP、首字节、端到端时延和有界音频体积。",
                "响应 Content-Type 必须与请求的 WAV 或 MP3 契约一致。",
                "WAV 必须含完整采样帧；MP3 必须含完整受支持音频帧。",
                "只保存格式、内容类型和字节数；音频正文不落盘。",
            ),
        ),
    ),
    ProbeComponentSpec(
        "audio_transcription",
        "音频转写检测",
        "STT",
        "内置音频输入 → 文本转写",
        ("audio",),
        ("text",),
        _steps(
            (
                "绑定音频转写端点",
                "生成一秒 WAV 夹具",
                "发送 Transcriptions 请求",
                "检查转写通道",
                "解析转写 JSON",
                "核验文本字段边界",
                "封存转写元数据",
            ),
            (
                "确认模型声明音频输入与文本输出。",
                "本地生成一秒有界 WAV；原始音频不会写入历史。",
                "按厂商协议发送 multipart 或 JSON input_audio 请求。",
                "记录 HTTP、首字节、端到端时延和响应上限。",
                "要求响应为 JSON 对象并包含字符串类型的 text 字段。",
                "记录转写字符数；静音夹具不用于宣称语义准确率。",
                "只保存时长、字符数和用量；转写正文不落盘。",
            ),
        ),
    ),
)
_BY_ID = {component.component_id: component for component in _COMPONENTS}


def component_catalog() -> list[dict[str, Any]]:
    return [component.to_public_dict() for component in _COMPONENTS]


def create_component_run(
    probe_type: str,
    *,
    run_mode: str,
    occurred_at: str,
    binding_source: str,
) -> dict[str, Any]:
    component = _BY_ID.get(probe_type)
    if component is None:
        raise ValueError("probe component is not supported")
    if run_mode not in {"single", "canary"}:
        raise ValueError("run_mode must be single or canary")
    if binding_source not in {"PROVIDER_METADATA", "MANUAL_CONFIRMATION"}:
        raise ValueError("binding_source is invalid")
    binding_fact = (
        "PROVIDER CAPABILITY VERIFIED"
        if binding_source == "PROVIDER_METADATA"
        else "MANUAL PROBE TYPE CONFIRMED"
    )
    steps: list[dict[str, Any]] = []
    for index, spec in enumerate(component.steps):
        completed = index == 0
        steps.append(
            {
                **spec.to_public_dict(),
                "status": (
                    ComponentStepStatus.PASS.value
                    if completed
                    else ComponentStepStatus.PENDING.value
                ),
                "started_at": occurred_at if completed else None,
                "finished_at": occurred_at if completed else None,
                "facts": [binding_fact] if completed else [],
            }
        )
    return {
        "schema_version": COMPONENT_RUN_SCHEMA_VERSION,
        "component_id": component.component_id,
        "component_label": component.label,
        "icon": component.icon,
        "scenario": component.scenario,
        "input_modalities": list(component.input_modalities),
        "output_modalities": list(component.output_modalities),
        "run_mode": run_mode,
        "binding_source": binding_source,
        "status": "RUNNING",
        "current_step_id": None,
        "started_at": occurred_at,
        "finished_at": None,
        "failure_code": None,
        "steps": steps,
    }


def advance_component_run(
    workflow: Mapping[str, Any],
    step_id: str,
    status: ComponentStepStatus,
    *,
    occurred_at: str,
) -> dict[str, Any]:
    value = _validated_workflow_copy(workflow)
    try:
        next_status = ComponentStepStatus(status)
    except (TypeError, ValueError) as exc:
        raise ValueError("component step status is invalid") from exc
    if next_status not in {
        ComponentStepStatus.RUNNING,
        ComponentStepStatus.PASS,
        ComponentStepStatus.FAIL,
    }:
        raise ValueError("component step transition is invalid")
    if value["status"] != "RUNNING":
        raise ValueError("component run is already terminal")
    index = _step_index(value, step_id)
    step = value["steps"][index]

    if next_status == ComponentStepStatus.RUNNING:
        if step["status"] != ComponentStepStatus.PENDING.value:
            raise ValueError("component step cannot start from its current state")
        allowed_previous = (
            {
                ComponentStepStatus.PASS.value,
                ComponentStepStatus.FAIL.value,
                ComponentStepStatus.SKIPPED.value,
            }
            if step_id == "evidence_seal"
            else {ComponentStepStatus.PASS.value}
        )
        if any(previous["status"] not in allowed_previous for previous in value["steps"][:index]):
            raise ValueError("component steps must be started in ordered sequence")
        if value["current_step_id"] is not None:
            raise ValueError("another component step is already running")
        step["status"] = next_status.value
        step["started_at"] = occurred_at
        value["current_step_id"] = step_id
        return value

    if step["status"] != ComponentStepStatus.RUNNING.value:
        raise ValueError("component step must be running before it can finish")
    step["status"] = next_status.value
    step["finished_at"] = occurred_at
    value["current_step_id"] = None
    if next_status == ComponentStepStatus.FAIL:
        for later in value["steps"][index + 1 : -1]:
            if later["status"] == ComponentStepStatus.PENDING.value:
                later["status"] = ComponentStepStatus.SKIPPED.value
                later["finished_at"] = occurred_at
    return value


def begin_component_evidence(
    workflow: Mapping[str, Any],
    *,
    result: Mapping[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    """Settle execution stages and expose evidence sanitization as a live step."""

    value = _validated_workflow_copy(workflow)
    if value["status"] != "RUNNING":
        raise ValueError("component run is already terminal")
    _settle_execution_steps(value, result, occurred_at)
    evidence = value["steps"][-1]
    if evidence["status"] == ComponentStepStatus.RUNNING.value:
        return value
    if evidence["status"] != ComponentStepStatus.PENDING.value:
        raise ValueError("component evidence step cannot be started")
    if value["current_step_id"] is not None:
        raise ValueError("another component step is already running")
    evidence["status"] = ComponentStepStatus.RUNNING.value
    evidence["started_at"] = occurred_at
    value["current_step_id"] = "evidence_seal"
    return value


def finalize_component_run(
    workflow: Mapping[str, Any],
    *,
    result: Mapping[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    value = begin_component_evidence(
        workflow,
        result=result,
        occurred_at=occurred_at,
    )
    result_status = result["status"]
    evidence = value["steps"][-1]
    if evidence["status"] != ComponentStepStatus.RUNNING.value:
        raise ValueError("component evidence step must be running before completion")
    evidence["status"] = "PASS"
    evidence["started_at"] = evidence["started_at"] or occurred_at
    evidence["finished_at"] = occurred_at
    _attach_safe_facts(value, result)
    value["status"] = result_status
    value["current_step_id"] = None
    value["finished_at"] = occurred_at
    value["failure_code"] = None
    return value


def fail_component_run(
    workflow: Mapping[str, Any],
    *,
    failure_code: str,
    occurred_at: str,
) -> dict[str, Any]:
    value = _validated_workflow_copy(workflow)
    if (
        not isinstance(failure_code, str)
        or not failure_code
        or len(failure_code) > 64
        or not all(character.isupper() or character.isdigit() or character == "_" for character in failure_code)
    ):
        raise ValueError("failure_code must be a bounded reason code")
    active_id = value.get("current_step_id")
    if active_id is not None:
        failure_index = _step_index(value, active_id)
    else:
        failure_index = next(
            (
                index
                for index, step in enumerate(value["steps"])
                if step["status"] == "PENDING"
            ),
            len(value["steps"]) - 1,
        )
    for index, step in enumerate(value["steps"]):
        if index == failure_index:
            step["status"] = "FAIL"
            step["started_at"] = step["started_at"] or occurred_at
            step["finished_at"] = occurred_at
        elif index > failure_index and step["status"] in {"PENDING", "RUNNING"}:
            step["status"] = "SKIPPED"
            step["finished_at"] = occurred_at
    value["status"] = "FAIL"
    value["current_step_id"] = None
    value["finished_at"] = occurred_at
    value["failure_code"] = failure_code
    return value


def _validated_workflow_copy(workflow: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(workflow, Mapping):
        raise ValueError("component workflow must be an object")
    value = deepcopy(dict(workflow))
    if value.get("schema_version") != COMPONENT_RUN_SCHEMA_VERSION:
        raise ValueError("component workflow schema is unsupported")
    if value.get("component_id") not in _BY_ID:
        raise ValueError("component workflow id is unsupported")
    if value.get("binding_source") not in {
        "PROVIDER_METADATA",
        "MANUAL_CONFIRMATION",
        "LEGACY_UNSPECIFIED",
    }:
        raise ValueError("component workflow binding source is unsupported")
    steps = value.get("steps")
    if not isinstance(steps, list) or [step.get("id") for step in steps] != list(_STEP_IDS):
        raise ValueError("component workflow steps are invalid")
    return value


def _step_index(workflow: Mapping[str, Any], step_id: str) -> int:
    for index, step in enumerate(workflow["steps"]):
        if step["id"] == step_id:
            return index
    raise ValueError("component workflow step is unknown")


def _failure_step_for_result(result: Mapping[str, Any]) -> str:
    measurements = result.get("measurements")
    if isinstance(measurements, list):
        classes = {
            measurement.get("error_class")
            for measurement in measurements
            if isinstance(measurement, Mapping)
        }
        if "PROTOCOL" in classes:
            return "response_validate"
        if classes & {
            "AUTHENTICATION",
            "PAYMENT_REQUIRED",
            "AUTHORIZATION",
            "RATE_LIMIT",
            "MODEL_NOT_FOUND",
            "UPSTREAM_5XX",
            "TIMEOUT",
            "NETWORK",
        }:
            return "transport_check"
    failed_dimensions = {
        score.get("dimension")
        for score in result.get("dimension_scores", [])
        if isinstance(score, Mapping) and score.get("status") == "FAIL"
    } if isinstance(result.get("dimension_scores"), list) else set()
    if "protocol" in failed_dimensions:
        return "response_validate"
    if failed_dimensions & {"availability", "performance"}:
        return "transport_check"
    reason_codes = result.get("reason_codes")
    if isinstance(reason_codes, list) and any(
        code in {
            "PSEUDO_STREAM_SUSPECTED",
            "SSE_DONE_MISSING",
            "STREAM_PROTOCOL_FAILED",
        }
        for code in reason_codes
    ):
        return "response_validate"
    return "quality_assert"


def _settle_execution_steps(
    workflow: dict[str, Any],
    result: Mapping[str, Any],
    occurred_at: str,
) -> None:
    result_status = result.get("status")
    if result_status not in {"PASS", "WARN", "FAIL"}:
        raise ValueError("component result status is invalid")
    execution_steps = workflow["steps"][:-1]
    existing_failure = next(
        (step for step in execution_steps if step["status"] == "FAIL"),
        None,
    )
    if result_status in {"PASS", "WARN"} and existing_failure is not None:
        raise ValueError("successful result conflicts with failed component step")

    if result_status == "FAIL" and existing_failure is None:
        failure_id = _failure_step_for_result(result)
        failure_index = _step_index(workflow, failure_id)
        for index, step in enumerate(execution_steps):
            if index < failure_index and step["status"] in {"PENDING", "RUNNING"}:
                step["status"] = "PASS"
                step["started_at"] = step["started_at"] or occurred_at
                step["finished_at"] = occurred_at
            elif index == failure_index:
                step["status"] = "FAIL"
                step["started_at"] = step["started_at"] or occurred_at
                step["finished_at"] = occurred_at
            elif index > failure_index and step["status"] in {"PENDING", "RUNNING"}:
                step["status"] = "SKIPPED"
                step["finished_at"] = occurred_at
    elif result_status in {"PASS", "WARN"}:
        for step in execution_steps:
            if step["status"] in {"PENDING", "RUNNING"}:
                step["status"] = "PASS"
                step["started_at"] = step["started_at"] or occurred_at
                step["finished_at"] = occurred_at
    workflow["current_step_id"] = None


def _attach_safe_facts(workflow: dict[str, Any], result: Mapping[str, Any]) -> None:
    measurements = result.get("measurements")
    measurement = (
        measurements[0]
        if isinstance(measurements, list)
        and measurements
        and isinstance(measurements[0], Mapping)
        else {}
    )
    evidence = measurement.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    status_code = measurement.get("status_code")
    transport_facts: list[str] = []
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        transport_facts.append(f"HTTP {status_code}")
    for label, key in (("TTFB", "ttfb_ms"), ("E2E", "e2e_ms")):
        number = measurement.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and isfinite(number):
            transport_facts.append(f"{label} {number:.1f} ms")

    protocol_facts: list[str] = []
    if evidence.get("sse_done_received") is True:
        protocol_facts.append("SSE DONE VERIFIED")
    if evidence.get("response_valid") is True:
        protocol_facts.append("RESPONSE STRUCTURE VALID")
    image_transport = evidence.get("image_transport")
    if image_transport == "b64_json":
        protocol_facts.append("BASE64 IMAGE DECODED")
    response_content_type = evidence.get("response_content_type")
    if isinstance(response_content_type, str) and response_content_type in {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "application/octet-stream",
    }:
        protocol_facts.append("AUDIO CONTENT-TYPE VALID")

    assertion_facts = [f"RESULT {result['status']}"]
    if image_transport == "b64_json":
        assertion_facts.append("PNG VERIFIED")
    audio_format = evidence.get("audio_format")
    if audio_format in {"wav", "mp3"}:
        assertion_facts.append(f"{audio_format.upper()} FRAMES VERIFIED")
    for label, key in (
        ("VECTOR DIMS", "embedding_dimensions"),
        ("IMAGES", "generated_image_count"),
        ("AUDIO BYTES", "audio_bytes"),
        ("TRANSCRIPT CHARS", "transcript_chars"),
        ("OUTPUT CHARS", "output_text_chars"),
    ):
        number = evidence.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            assertion_facts.append(f"{label} {number}")

    by_id = {step["id"]: step for step in workflow["steps"]}
    by_id["transport_check"]["facts"] = transport_facts
    by_id["response_validate"]["facts"] = protocol_facts
    by_id["quality_assert"]["facts"] = assertion_facts
    by_id["evidence_seal"]["facts"] = ["SANITIZED METADATA ONLY", "RAW PAYLOAD DISCARDED"]
