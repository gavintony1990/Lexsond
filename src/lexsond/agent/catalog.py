from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    name: str
    description: str
    mode: str = "read_only"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    starters: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "starters": list(self.starters),
        }


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "list_probe_targets",
        "读取探测目标",
        "列出已配置的 API 目标及其非机密连接信息。",
    ),
    ToolDefinition(
        "list_recent_probe_runs",
        "读取最近运行",
        "按状态读取最近探针运行及其结果摘要。",
    ),
    ToolDefinition(
        "inspect_probe_run",
        "检查运行证据",
        "读取一次运行的配置快照、评分、工作流和脱敏测量。",
    ),
    ToolDefinition(
        "inspect_run_events",
        "检查事件时间线",
        "读取一次运行的有序工作流事件，定位失败阶段。",
    ),
    ToolDefinition(
        "list_probe_suites",
        "读取探针套件",
        "列出当前聊天套件、版本和预算摘要。",
    ),
    ToolDefinition(
        "design_probe_plan",
        "生成有界探测方案",
        "根据目标、症状和模态生成需要用户确认的探测方案，不自动发起计费调用。",
    ),
)


_SHARED_GUARDRAILS = """
你是 API 质量探针平台的诊断智能体。你必须区分观测事实、推断和建议；不得把黑盒输出
描述为模型身份的证明。只能使用提供的只读工具，不得声称已经发起、取消或修改了探针。
当建议发起运行时，明确提示用户仍需在“新建运行”页面确认，因为运行可能产生费用。
任何疑似 API Key、Authorization 值或原始模型回复都不得复述。回答使用简洁中文。
""".strip()


SKILLS: tuple[SkillDefinition, ...] = (
    SkillDefinition(
        id="connection-diagnosis",
        name="连接与鉴权诊断",
        description="分析 URL、鉴权、模型目录、限流和协议失败。",
        system_prompt=_SHARED_GUARDRAILS
        + "\n优先读取目标和最近运行，再用事件与证据区分 401、402、403、404、429、TLS、超时和协议错误。",
        allowed_tools=(
            "list_probe_targets",
            "list_recent_probe_runs",
            "inspect_probe_run",
            "inspect_run_events",
            "design_probe_plan",
        ),
        starters=(
            "为什么这个 DeepSeek 目标一直鉴权失败？",
            "根据最近一次失败给我一个最小复测方案。",
            "检查 Base URL、模型 ID 和流式协议是否匹配。",
        ),
    ),
    SkillDefinition(
        id="quality-triage",
        name="质量证据研判",
        description="解释评分、时延、伪流式与 reasoning 扩展证据。",
        system_prompt=_SHARED_GUARDRAILS
        + "\n先读取具体运行证据。说明哪些维度有数据、哪些是 UNKNOWN，并给出可复现的后续探测。",
        allowed_tools=(
            "list_recent_probe_runs",
            "inspect_probe_run",
            "inspect_run_events",
            "design_probe_plan",
        ),
        starters=(
            "解释最近一次运行为什么协议分低。",
            "这次响应是真的流式还是最后一次性吐出？",
            "比较可用性、协议、性能和质量四个维度。",
        ),
    ),
    SkillDefinition(
        id="probe-planner",
        name="探测方案编排",
        description="把问题转成有请求数、超时和模态边界的执行方案。",
        system_prompt=_SHARED_GUARDRAILS
        + "\n使用目标和套件信息设计最小充分方案，优先单项探针，再建议套件；写清模态、流式、超时和停止条件。",
        allowed_tools=(
            "list_probe_targets",
            "list_probe_suites",
            "list_recent_probe_runs",
            "design_probe_plan",
        ),
        starters=(
            "为这个目标设计一次低成本健康检查。",
            "我应该先跑单项还是聊天套件？",
            "为六种模态给出分阶段验收顺序。",
        ),
    ),
)


def get_skill(skill_id: str) -> SkillDefinition:
    for skill in SKILLS:
        if skill.id == skill_id:
            return skill
    raise ValueError("skill_id is not registered")


def public_tools() -> list[dict[str, Any]]:
    return [tool.to_public_dict() for tool in TOOLS]


def public_skills() -> list[dict[str, Any]]:
    return [skill.to_public_dict() for skill in SKILLS]
