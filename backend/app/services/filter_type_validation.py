"""过滤类型校验核心逻辑：样本类型推断、过滤口径解析、模板同名映射消歧。

这一层不依赖内存仓库，方便 data_filter 生成校验报告、result_format 复用同一套
口径做格式化，也方便单独测试。所有判定都以“声明类型优先、样本值兜底”为口径：

- 前导零编码（如 007）始终视为文本，不参与数值范围比较；
- 纯数字（如 7）只有在字段类型声明为数值、或规则精度约束声明为数值时才可做范围比较；
- 既没有字段声明又无法唯一判断类型的列记为“缺少声明”，所在行进入“类型待核”，
  不直接交给格式化。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

MISSING = object()

_INT_RE = re.compile(r"^-?(0|[1-9]\d*)$")
_DEC_RE = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")
_LEADING_ZERO_RE = re.compile(r"^-?0\d+$")
_COMPARISON_RE = re.compile(
    r"^\s*(?P<field>[\w一-龥.\-]+)\s*(?P<op>>=|<=|!=|==|>|<|=)\s*(?P<value>.+?)\s*$"
)

NUMERIC_TYPES = {"整数", "小数", "数值"}
TEXT_TYPES = {"文本", "编码文本", "编码"}
DECLARED_NUMERIC = NUMERIC_TYPES | {"number", "numeric", "int", "integer", "float", "double", "range", "范围", "可范围比较"}
DECLARED_TEXT = TEXT_TYPES | {"string", "text", "char", "varchar", "字符"}


# ========== 类型推断 ==========


def infer_value_type(value: Any) -> str:
    """推断单个样本值的实际类型。前导零文本单独标成“编码文本”。"""
    if value is None:
        return "缺失"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "小数"
    text = str(value).strip()
    if text == "":
        return "空值"
    if _LEADING_ZERO_RE.match(text):
        return "编码文本"
    if _INT_RE.match(text):
        return "整数"
    if _DEC_RE.match(text):
        return "小数"
    return "文本"


def type_category(type_name: str | None) -> str:
    """把具体类型归并成数值/文本/布尔/未知四类。"""
    if not type_name:
        return "未知"
    if type_name in NUMERIC_TYPES:
        return "数值"
    if type_name in TEXT_TYPES:
        return "文本"
    if type_name == "布尔":
        return "布尔"
    return "未知"


def normalize_declaration(raw: Any) -> tuple[str, str | None]:
    """归一化字段类型声明，返回 (原文, 数值/文本/布尔/None)。"""
    if raw is None:
        return "", None
    text = str(raw).strip()
    if not text:
        return "", None
    lowered = text.casefold()
    if text in NUMERIC_TYPES or lowered in DECLARED_NUMERIC:
        return text, "数值"
    if text in TEXT_TYPES or lowered in DECLARED_TEXT:
        return text, "文本"
    if lowered in {"bool", "boolean", "布尔"} or text == "布尔":
        return text, "布尔"
    return text, None


def is_leading_zero(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    return bool(_LEADING_ZERO_RE.match(str(value).strip()))


def is_range_comparable(value: Any) -> bool:
    """值本身是否可参与数值范围比较（前导零编码一律排除）。"""
    inferred = infer_value_type(value)
    return inferred in NUMERIC_TYPES


def coerce_number(value: Any) -> float | None:
    if not is_range_comparable(value):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_declaration_spec(raw: Any, default_field: str = "") -> list[tuple[str, str]]:
    """解析字段类型声明。

    支持 `数量=数值;编码=文本`、`数量:数值`、dict，以及只写类型名的
    “裸声明”（此时作用于 default_field，通常是规则的过滤字段）。
    无法识别成已知类型的片段会被忽略，不会被误当成声明。
    """
    entries: list[tuple[str, str]] = []
    if not raw:
        return entries
    if isinstance(raw, Mapping):
        raw_items = list(raw.items())
    elif isinstance(raw, str):
        raw_items = []
        for chunk in re.split(r"[;；,，]", raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            separator = "=" if "=" in chunk else (":" if ":" in chunk else "")
            if separator:
                field, _, type_name = chunk.partition(separator)
                raw_items.append((field.strip(), type_name.strip()))
            else:
                raw_items.append((default_field, chunk))
    else:
        raw_items = list(raw)

    for item in raw_items:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            field = str(item[0] or "").strip()
            type_name = str(item[1] or "").strip()
        else:
            field, type_name = default_field, str(item or "").strip()
        if not type_name:
            continue
        # 只接受能归一化成类型类别的写法，避免把历史精度描述当成类型。
        _, category = normalize_declaration(type_name)
        if category is None:
            continue
        entries.append((field, type_name))
    return entries


def parse_declaration(raw: Any, default_field: str = "") -> dict[str, tuple[str, str]]:
    """把声明规格解析成 {字段: (声明原文, 类别)}。"""
    resolved: dict[str, tuple[str, str]] = {}
    for field, type_name in parse_declaration_spec(raw, default_field):
        text, category = normalize_declaration(type_name)
        if field and category:
            resolved[field] = (text, category)
    return resolved


# ========== 过滤条件口径 ==========


def parse_condition(condition: Any, default_field: str = "") -> dict[str, Any]:
    """解析过滤条件，输出可复核的判定口径。

    支持 `字段 >= 5` 这类范围比较；其余条件按“文本包含”处理，
    与历史行为保持一致。
    """
    expression = "" if condition is None else str(condition).strip()
    if not expression:
        return {
            "expression": "",
            "mode": "未配置",
            "filter_field": default_field,
            "operator": "",
            "threshold": "",
            "comparable_required": False,
            "criteria_text": "未配置过滤条件，样本仅做类型核查。",
        }

    match = _COMPARISON_RE.match(expression)
    if match:
        field = match.group("field") or default_field
        operator = match.group("op")
        threshold = match.group("value")
        return {
            "expression": expression,
            "mode": "范围比较",
            "filter_field": field,
            "operator": operator,
            "threshold": threshold,
            "comparable_required": True,
            "criteria_text": (
                f"按 {field} {operator} {threshold} 做数值范围比较；"
                "只有可解析为数值且无前导零的样本参与比较，"
                "文本样本只能做等值/包含匹配，不能进入范围过滤。"
            ),
        }

    return {
        "expression": expression,
        "mode": "文本包含",
        "filter_field": default_field,
        "operator": "contains",
        "threshold": expression,
        "comparable_required": False,
        "criteria_text": (
            f"按字段 {default_field} 包含“{expression}”做文本匹配；"
            "类型校验只要求字段有可读值，不要求数值可比较。"
        ),
    }


# ========== 模板映射 ==========


def parse_mapping(mapping: Any) -> list[tuple[str, str]]:
    """解析字段映射，支持 `源:目标,源:目标`、dict 和二元组列表。"""
    pairs: list[tuple[str, str]] = []
    if not mapping:
        return pairs
    if isinstance(mapping, Mapping):
        raw_pairs = list(mapping.items())
    elif isinstance(mapping, str):
        raw_pairs = []
        for chunk in re.split(r"[,;，；]", mapping):
            if ":" not in chunk:
                continue
            source, _, target = chunk.partition(":")
            raw_pairs.append((source, target))
    else:
        raw_pairs = list(mapping)
    for pair in raw_pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) < 2:
            continue
        source = str(pair[0]).strip()
        target = str(pair[1]).strip()
        if source and target:
            pairs.append((source, target))
    return pairs


def resolve_mapping(
    pairs: Sequence[tuple[str, str]],
    source_types: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把映射解析成可执行的输出列，并识别同名结果列冲突。

    同名结果列出现多个来源时，映射顺序靠前的来源保留原名，其余来源输出为
    `原名@来源字段`，同时在沿革里记录实际使用的字段。返回 (resolution, conflicts)。
    """
    source_types = source_types or {}
    resolution: list[dict[str, Any]] = []
    taken: set[str] = set()
    grouped: dict[str, list[str]] = {}
    for source, target in pairs:
        grouped.setdefault(target, []).append(source)

    for source, target in pairs:
        sources = grouped.get(target, [])
        conflict = len(sources) > 1
        if target not in taken:
            output = target
        else:
            output = f"{target}@{source}"
            suffix = 2
            while output in taken:
                output = f"{target}@{source}#{suffix}"
                suffix += 1
        taken.add(output)
        resolution.append({
            "source_field": source,
            "output_column": output,
            "declared_output_column": target,
            "source_type": source_types.get(source, "未知"),
            "conflict": conflict,
            "alternates": [item for item in sources if item != source] if conflict else [],
        })

    conflicts: list[dict[str, Any]] = []
    for target, sources in grouped.items():
        if len(sources) < 2:
            continue
        categories = {source: type_category(source_types.get(source)) for source in sources}
        conflicts.append({
            "output_column": target,
            "sources": sources,
            "source_types": {source: source_types.get(source, "未知") for source in sources},
            "categories": categories,
            "conflict": len(set(categories.values())) > 1 or "未知" in categories.values(),
            "resolution": (
                f"{sources[0]} 保留为输出列 {target}，"
                + "、".join(f"{source} 输出为 {target}@{source}" for source in sources[1:])
            ),
        })
    return resolution, conflicts


# ========== 批次与行标识 ==========


def canonical_samples(samples: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(list(samples), ensure_ascii=False, sort_keys=True, default=str)


def batch_fingerprint(samples: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha1(canonical_samples(samples).encode("utf-8")).hexdigest()
    return f"B{digest[:12]}"


def _canonical_value(value: Any) -> str:
    """把样本值规范化成可比较的字符串：数字 7 与 "7" 视为同一行，
    但前导零编码 "007" 保持独立，不会被当成数值 7。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def row_fingerprint(values: Mapping[str, Any]) -> str:
    canonical = {field: _canonical_value(value) for field, value in values.items()}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def slugify(value: Any, fallback: str = "current") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    slug = re.sub(r"[^0-9A-Za-z一-龥_.-]+", "_", text)
    return slug[:48] or fallback
