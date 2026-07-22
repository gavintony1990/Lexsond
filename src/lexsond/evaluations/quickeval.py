from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .compiler import compile_document_items


def _item(
    item_id: str,
    category: str,
    language: str,
    prompt: str,
    reference: dict[str, Any],
    *,
    choices: list[str] | None = None,
) -> dict[str, Any]:
    input_value: dict[str, Any] = {"messages": [{"role": "user", "content": prompt}]}
    if choices is not None:
        input_value["choices"] = choices
    return {
        "id": item_id,
        "category": category,
        "language": language,
        "input": input_value,
        "reference": reference,
        "metadata": {"difficulty": "basic", "provenance": "lexsond-original"},
    }


def quickeval_items() -> list[dict[str, Any]]:
    """Return the 80 original, deterministic Lexsond QuickEval v1 items."""

    items: list[dict[str, Any]] = []
    arithmetic = [
        ("17 + 25", "42"), ("90 - 37", "53"), ("8 × 7", "56"),
        ("144 ÷ 12", "12"), ("6 + 9 × 2", "24"), ("(18 + 6) ÷ 3", "8"),
        ("15% of 200", "30"), ("3² + 4²", "25"), ("1.5 + 2.75", "4.25"),
        ("the next integer after 999", "1000"),
    ]
    for index, (expression, answer) in enumerate(arithmetic, 1):
        items.append(_item(f"arithmetic-{index:03d}", "arithmetic", "en", f"Return only the answer to: {expression}", {"scorer": "normalized_exact_match", "answer": answer}))

    logic = [
        ("Mira arrives before Niko, and Niko before Oren. Who arrives first?", "Mira"),
        ("The blue box is left of the green box; the green box is left of the gold box. Which box is rightmost?", "gold"),
        ("A bell rings after the lamp turns on but before the door opens. What happens second?", "bell rings"),
        ("Pine is taller than Reed; Reed is taller than Moss. Which is shortest?", "Moss"),
        ("If every noro is a vela and every vela is a tira, is every noro a tira? Answer yes or no.", "yes"),
        ("A token moves north, east, then south by equal distances. Relative to start, where is it?", "east"),
        ("Tasks K, L, M run in that order. Which task is immediately before M?", "L"),
        ("No amber cards are round. Card Q is amber. Can Q be round? Answer yes or no.", "no"),
        ("Rin is older than Sol but younger than Tavi. Who is oldest?", "Tavi"),
        ("A switch alternates OFF, ON, OFF, ON. What is the fifth state?", "OFF"),
    ]
    for index, (prompt, answer) in enumerate(logic, 1):
        items.append(_item(f"logic-{index:03d}", "logic", "en", f"{prompt} Return only the answer.", {"scorer": "normalized_exact_match", "answer": answer}))

    zh_tasks = [
        ("只输出两个汉字：信号", "信号"), ("只输出大写英文 READY", "READY"),
        ("把“青色”原样输出，不要解释。", "青色"), ("仅回答数字七的阿拉伯数字。", "7"),
        ("只输出：探测完成", "探测完成"), ("将“左 右”两个字按相反顺序输出，中间保留一个空格。", "右 左"),
        ("只回答“是”，不要标点。", "是"), ("输出三个连续字母 ABC。", "ABC"),
        ("把“小舟”重复两次，中间用斜杠分隔。", "小舟/小舟"), ("仅输出英文单词 cyan 的大写形式。", "CYAN"),
    ]
    for index, (prompt, answer) in enumerate(zh_tasks, 1):
        items.append(_item(f"zh-instruction-{index:03d}", "zh_instruction", "zh-CN", prompt, {"scorer": "exact_match", "answer": answer}))

    en_tasks = [
        ("Reply with exactly SIGNAL.", "SIGNAL"), ("Output the lowercase word ready and nothing else.", "ready"),
        ("Return only these three digits: 314", "314"), ("Write north/south exactly as shown.", "north/south"),
        ("Reply with two words separated by one space: probe stable", "probe stable"),
        ("Output the word ECHO twice with a comma and no spaces.", "ECHO,ECHO"),
        ("Return only YES in uppercase.", "YES"), ("Output the sequence A-B-C.", "A-B-C"),
        ("Reply with the single symbol #.", "#"), ("Return exactly: latency known", "latency known"),
    ]
    for index, (prompt, answer) in enumerate(en_tasks, 1):
        items.append(_item(f"en-instruction-{index:03d}", "en_instruction", "en", prompt, {"scorer": "exact_match", "answer": answer}))

    json_tasks = [
        ("Return JSON with status equal to ready.", {"status": {"type": "string", "enum": ["ready"]}}, ["status"]),
        ("Return JSON with count equal to 3.", {"count": {"type": "integer", "enum": [3]}}, ["count"]),
        ("Return JSON with ok equal to true.", {"ok": {"type": "boolean", "enum": [True]}}, ["ok"]),
        ("Return JSON with color equal to cyan.", {"color": {"type": "string", "enum": ["cyan"]}}, ["color"]),
        ("Return JSON with values as an array of exactly two integers.", {"values": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}}}, ["values"]),
        ("Return JSON with name equal to beacon.", {"name": {"type": "string", "enum": ["beacon"]}}, ["name"]),
        ("Return JSON with latency_ms as a nonnegative number.", {"latency_ms": {"type": "number", "minimum": 0}}, ["latency_ms"]),
        ("Return JSON with tags as an array containing one string.", {"tags": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "string"}}}, ["tags"]),
        ("Return JSON with mode equal to smoke and active equal to true.", {"mode": {"type": "string", "enum": ["smoke"]}, "active": {"type": "boolean", "enum": [True]}}, ["mode", "active"]),
        ("Return JSON with score as an integer from zero through ten.", {"score": {"type": "integer", "minimum": 0, "maximum": 10}}, ["score"]),
    ]
    for index, (prompt, properties, required) in enumerate(json_tasks, 1):
        items.append(_item(f"json-structure-{index:03d}", "json_structure", "en", prompt + " Do not add prose.", {"scorer": "json_schema_valid", "schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}))

    extraction = [
        ("Beacon B-17 reports temperature 24 C. Return only the beacon code.", "B-17"),
        ("Shipment label: zone=west; crate=K42. Return only the crate value.", "K42"),
        ("记录：节点=青岚；状态=稳定。只输出节点名称。", "青岚"),
        ("The reading is 8.75 volts at port P3. Return only the voltage number.", "8.75"),
        ("Ticket owner is Mira; priority is low. Return only the owner.", "Mira"),
        ("样本编号 Q-204，颜色为青色。只输出样本编号。", "Q-204"),
        ("Route starts at Elm and ends at Bay. Return only the destination.", "Bay"),
        ("A note says batch 61 contains twelve units. Return only the batch number.", "61"),
        ("设备代号为 R7，当前模式为休眠。只输出模式。", "休眠"),
        ("The file is named pulse.txt and has 20 lines. Return only the filename.", "pulse.txt"),
    ]
    for index, (prompt, answer) in enumerate(extraction, 1):
        items.append(_item(f"extraction-{index:03d}", "extraction", "zh-CN" if any("\u4e00" <= c <= "\u9fff" for c in prompt) else "en", prompt, {"scorer": "normalized_exact_match", "answer": answer}))

    classifications = [
        ("A request completed successfully.", ["success", "timeout", "authentication", "rate limit"], 0),
        ("The server did not answer before the deadline.", ["success", "timeout", "authentication", "rate limit"], 1),
        ("The supplied credential was rejected.", ["success", "timeout", "authentication", "rate limit"], 2),
        ("Too many requests were sent in a short interval.", ["success", "timeout", "authentication", "rate limit"], 3),
        ("文本表达明显赞同。", ["赞同", "反对", "中立"], 0),
        ("文本明确表示不同意。", ["赞同", "反对", "中立"], 1),
        ("文本只陈述时间，没有态度。", ["赞同", "反对", "中立"], 2),
        ("A metric has not been observed.", ["PASS", "FAIL", "UNKNOWN"], 2),
        ("The assertion matched its expected value.", ["PASS", "FAIL", "UNKNOWN"], 0),
        ("The assertion contradicted its expected value.", ["PASS", "FAIL", "UNKNOWN"], 1),
    ]
    for index, (prompt, choices, answer_index) in enumerate(classifications, 1):
        items.append(_item(f"classification-{index:03d}", "classification", "zh-CN" if any("\u4e00" <= c <= "\u9fff" for c in prompt) else "en", f"{prompt} Return only the option letter.", {"scorer": "multiple_choice_accuracy", "answer_index": answer_index}, choices=choices))

    readings = [
        ("A small observatory stores three lamps. The cyan lamp marks normal service, while amber marks delay. Tonight the cyan lamp is lit. What service condition is shown?", "normal service", "en"),
        ("Lena placed the silver token inside the north drawer and the copper token inside the south drawer. Where is the silver token?", "north drawer", "en"),
        ("The ferry leaves Oak Pier at eight and reaches Reed Pier at nine. What is the destination?", "Reed Pier", "en"),
        ("A gardener waters mint on Monday and thyme on Tuesday. Which plant is watered Tuesday?", "thyme", "en"),
        ("The log says the first check passed, the second was unknown, and the third failed. What was the second result?", "unknown", "en"),
        ("小站有两扇门，东门涂成青色，西门涂成灰色。青色的是哪扇门？", "东门", "zh-CN"),
        ("晨班在七点开始，晚班在十九点开始。十九点开始的是哪个班次？", "晚班", "zh-CN"),
        ("纸船先经过石桥，再经过木桥，最后到达码头。纸船最后到达哪里？", "码头", "zh-CN"),
        ("记录册写着：一号盒装松果，二号盒装贝壳。二号盒里是什么？", "贝壳", "zh-CN"),
        ("风铃响一次表示准备，响两次表示停止。今天风铃响了两次，表示什么？", "停止", "zh-CN"),
    ]
    for index, (passage, answer, language) in enumerate(readings, 1):
        items.append(_item(f"reading-{index:03d}", "reading", language, passage + (" Return only the answer." if language == "en" else "只输出答案。"), {"scorer": "normalized_exact_match", "answer": answer}))
    return deepcopy(items)


def quickeval_manifest() -> dict[str, Any]:
    items = quickeval_items()
    compiled = compile_document_items(items)
    return {
        "slug": "lexsond-quickeval",
        "name": "Lexsond QuickEval v1",
        "version": "1.0.0",
        "scope": "SYSTEM",
        "license_spdx": "Apache-2.0",
        "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "distribution_policy": "BUNDLED",
        "authors": ["Lexsond Contributors"],
        "authorship": "Project-original prompts and references; deterministic programmatic validation plus manual category review.",
        "item_count": len(items),
        "categories": dict(sorted(Counter(item["category"] for item in items).items())),
        "content_sha256": compiled.content_sha256,
        "schema_version": compiled.schema_version,
    }
