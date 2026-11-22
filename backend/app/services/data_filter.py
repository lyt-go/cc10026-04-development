"""数据过滤领域服务：管理API数据过滤规则、字段过滤和精度约束。

跨模块数据流：
- 消费 api_registry 快照 → 关联API过滤规则自动生效
- 被 field_mask 消费 → 过滤后的敏感字段进入脱敏流程
- 被 result_format 消费 → 过滤结果进入格式化
- 被 query_optimize 消费 → 过滤条件优化查询计划

类型校验：
- `validate_types` 针对一批字段样本生成“过滤类型校验报告”，明确每个字段/每行
  是否可进入格式化、哪些字段需要回填类型说明，以及过滤条件按什么口径判定。
- 报告落盘到 data/filter_reports/，按“规则 + 样本批次”形成版本链，
  模板提交校验后可重新校验同一批样本并对比新旧判定。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.services import filter_type_validation as ftv
from app.services.base import BaseModuleService
from app.store import store

# 类型说明的补齐位置：过滤规则的“精度约束”列。
DECLARATION_COLUMN = "精度约束"


class DataFilterService(BaseModuleService):
    MODULE = "data_filter"
    ENTITY = "过滤规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "关联API", "过滤字段", "过滤条件", "模糊策略", "精度约束", "空值处理", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "关联API", "过滤字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---------- 过滤执行 ----------

    def _matches(self, row_value: Any, parsed: dict[str, Any]) -> tuple[bool, str]:
        """按统一口径判断单个值是否满足过滤条件，返回 (是否通过, 原因)。"""
        mode = parsed.get("mode")
        if mode == "未配置":
            return True, "未配置过滤条件"
        if mode == "范围比较":
            number = ftv.coerce_number(row_value)
            if number is None:
                return False, "值不可解析为数值或带前导零，不能参与范围比较"
            try:
                threshold = float(parsed.get("threshold"))
            except (TypeError, ValueError):
                return False, "过滤阈值不是数值，无法做范围比较"
            operator = parsed.get("operator")
            passed = {
                ">": number > threshold,
                ">=": number >= threshold,
                "<": number < threshold,
                "<=": number <= threshold,
                "=": number == threshold,
                "==": number == threshold,
                "!=": number != threshold,
            }.get(operator, False)
            return passed, f"数值 {number:g} {operator} {threshold:g}"

        expected = str(parsed.get("threshold", ""))
        if expected.casefold() not in str(row_value).casefold():
            return False, f"文本不包含“{expected}”"
        return True, f"文本包含“{expected}”"

    def apply_filters(self, api_name: str, data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对指定API的查询结果应用过滤规则。"""
        rows = store.rows(self.MODULE)
        active_rules = [r for r in rows if r.get("关联API") == api_name and r.get("status") == "已生效"]
        if not active_rules:
            return data_rows

        filtered = []
        for row in data_rows:
            passed = True
            for rule in active_rules:
                field = rule.get("过滤字段", "")
                null_handling = rule.get("空值处理", "")
                row_value = row.get(field)
                parsed = ftv.parse_condition(rule.get("过滤条件", ""), field)

                # 空值处理
                if row_value is None or row_value == "":
                    if null_handling == "排除空值":
                        passed = False
                        break
                    if null_handling == "保留空值":
                        continue

                if row_value:
                    ok, _ = self._matches(row_value, parsed)
                    if not ok:
                        passed = False
                        break

            if passed:
                filtered.append(row)

        return filtered

    # ---------- 类型校验报告 ----------

    def _template_for(self, template_no: str) -> dict[str, Any] | None:
        if not template_no:
            return None
        for row in store.rows("result_format"):
            if row.get("模板编号") == template_no:
                return row
        return None

    def _collect_declarations(
        self,
        rule: Mapping[str, Any],
        declared_types: Any,
        template: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        """汇总类型声明：显式声明 > 规则精度约束 > 模板字段映射声明。"""
        filter_field = str(rule.get("过滤字段") or "")
        declarations: dict[str, dict[str, Any]] = {}

        def absorb(raw: Any, source: str) -> None:
            for field, (type_text, category) in ftv.parse_declaration(raw, filter_field).items():
                # 先写入的优先级更高
                declarations.setdefault(field, {
                    "type": type_text,
                    "category": category,
                    "source": source,
                })

        absorb(declared_types, "调用声明")
        absorb(rule.get("精度约束"), "规则精度约束")
        if template is not None:
            absorb(template.get("字段映射类型"), "模板映射声明")
        # 模板映射涉及的源字段如果仍无声明，保持“缺少声明”，供报告提示回填。
        if template is not None:
            for source_field, _output_column in ftv.parse_mapping(template.get("字段映射")):
                declarations.setdefault(source_field, {
                    "type": "",
                    "category": None,
                    "source": "模板映射",
                })
        # 过滤字段始终纳入报告，即使它没有类型声明。
        declarations.setdefault(filter_field, {
            "type": "",
            "category": None,
            "source": "规则过滤字段",
        })
        return {field: value for field, value in declarations.items() if field}

    @staticmethod
    def _declaration_map(declarations: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        return {
            field: str(meta.get("type") or "")
            for field, meta in declarations.items()
            if meta.get("category")
        }

    def _field_reports(
        self,
        fields: Iterable[str],
        samples: list[dict[str, Any]],
        declarations: Mapping[str, Mapping[str, Any]],
        parsed: dict[str, Any],
        template: Mapping[str, Any] | None,
        report_scope: Iterable[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """逐字段核对声明与样本，返回 (字段报告, 模板映射解析)。"""
        scope = set(report_scope) if report_scope is not None else set(fields)
        pairs = ftv.parse_mapping(template.get("字段映射")) if template is not None else []
        resolution, _ = ftv.resolve_mapping(
            pairs,
            {field: (declarations.get(field, {}).get("type") or "") for field in fields},
        )
        output_by_field: dict[str, list[str]] = {}
        for item in resolution:
            output_by_field.setdefault(item["source_field"], []).append(item["output_column"])

        field_reports: list[dict[str, Any]] = []
        for field in fields:
            observed: dict[str, int] = {}
            for row in samples:
                value = row.get(field, ftv.MISSING)
                inferred = "缺失" if value is ftv.MISSING else ftv.infer_value_type(value)
                observed[inferred] = observed.get(inferred, 0) + 1

            meta = declarations.get(field) or {}
            declared_type = str(meta.get("type") or "")
            declared_category = meta.get("category")
            observed_categories = {
                ftv.type_category(item)
                for item in observed
                if item not in {"缺失", "空值", "布尔"} and ftv.type_category(item) != "未知"
            }
            has_leading_zero = any(
                ftv.is_leading_zero(row.get(field))
                for row in samples if row.get(field) is not ftv.MISSING
            )

            if declared_category == "文本":
                # 文本声明对所有可读值都成立；前导零编码本来就是文本。
                incompatible = False
            elif declared_category == "数值":
                # 数值声明遇到编码文本/普通文本即为不符，这些值不能参与范围比较。
                incompatible = any(item in {"编码文本", "文本"} for item in observed)
            elif declared_category == "布尔":
                incompatible = any(
                    ftv.type_category(item) in {"数值", "文本"} for item in observed
                )
            else:
                incompatible = False

            if declared_category:
                status = "声明与样本不符" if incompatible else "已声明"
            else:
                status = "缺少声明"

            # 范围比较要求过滤字段可比较；若声明为文本，属声明与条件口径不符。
            condition_mismatch = (
                parsed.get("mode") == "范围比较"
                and field == parsed.get("filter_field")
                and declared_category not in (None, "数值")
            )
            if condition_mismatch:
                status = "声明与条件不符"

            field_reports.append({
                "field": field,
                "declared_type": declared_type,
                "declaration_source": str(meta.get("source") or ""),
                "declaration_column": DECLARATION_COLUMN,
                "observed_types": observed,
                "observed_categories": sorted(observed_categories),
                "has_leading_zero": has_leading_zero,
                "status": status,
                "needs_declaration": status != "已声明" and field in scope,
                "in_scope": field in scope,
                "condition_mismatch": condition_mismatch,
                "backfill_hint": self._backfill_hint(
                    field, declared_type, observed_categories, parsed, status)
                if field in scope else "",
                "mapping_output_columns": output_by_field.get(field, []),
            })

        return field_reports, resolution

    @staticmethod
    def _backfill_hint(field: str, declared_type: str, observed_categories: set[str],
                       parsed: dict[str, Any], status: str) -> str:
        if status == "已声明":
            return ""
        inferred = "数值" if observed_categories == {"数值"} else "文本"
        if status == "声明与条件不符":
            return (
                f"过滤条件「{parsed.get('expression')}」要求范围比较，但 {field} 声明为"
                f"{declared_type}；请在「{DECLARATION_COLUMN}」列改为 {field}=数值，"
                "或把过滤条件改成文本匹配"
            )
        if status == "声明与样本不符":
            return (
                f"在过滤规则「{parsed.get('rule_no', '')}」的「{DECLARATION_COLUMN}」列修正 "
                f"{field}={inferred}（原声明 {declared_type} 与样本不符）"
            )
        return (
            f"在过滤规则「{parsed.get('rule_no', '')}」的「{DECLARATION_COLUMN}」列补充 "
            f"{field}={inferred}"
        )

    def _row_reports(
        self,
        samples: list[dict[str, Any]],
        fields: list[str],
        field_states: Mapping[str, Mapping[str, Any]],
        parsed: dict[str, Any],
        rule: Mapping[str, Any],
        report_scope: list[str],
    ) -> list[dict[str, Any]]:
        """逐行核对类型与过滤条件。

        类型待核按行判定：只有该行确实存在、且类型无法确认（缺少声明或与声明不符）
        的字段才阻断格式化；空值/缺失不阻断。
        """
        filter_field = str(rule.get("过滤字段") or "")
        null_handling = str(rule.get("空值处理") or "")
        rows: list[dict[str, Any]] = []
        for index, sample in enumerate(samples, start=1):
            reasons: list[str] = []
            pending_fields: list[str] = []
            for field in report_scope:
                state = field_states[field]
                value = sample.get(field, ftv.MISSING)

                # 过滤字段声明与范围条件口径不符：整条规则无法做范围比较，
                # 相关行一律类型待核，而不是悄悄按文本匹配。
                if state["status"] == "声明与条件不符":
                    if field not in pending_fields:
                        pending_fields.append(field)
                        reasons.append(
                            f"{field} 声明为{state['declared_type']}，但过滤条件要求数值范围比较"
                            f"（{parsed.get('expression')}），需在「{DECLARATION_COLUMN}」列修正"
                        )
                    continue

                if value is ftv.MISSING or value is None or str(value).strip() == "":
                    continue  # 空值不阻断类型判定
                declared_type = state["declared_type"]
                declared_category = ftv.type_category(declared_type) if declared_type else None
                inferred = ftv.infer_value_type(value)

                if not declared_type:
                    pending_fields.append(field)
                    reasons.append(
                        f"{field} 缺少类型声明（样本值 {value!r}，推断为{inferred}），"
                        f"需在「{DECLARATION_COLUMN}」列补齐"
                    )
                elif declared_category == "数值" and inferred in {"编码文本", "文本"}:
                    pending_fields.append(field)
                    reasons.append(
                        f"{field} 声明为{declared_type}，但样本值 {value!r} 是{inferred}，不能参与范围比较"
                    )
                elif declared_category == "文本" and inferred == "布尔":
                    pending_fields.append(field)
                    reasons.append(f"{field} 声明为{declared_type}，但样本值是布尔")

            type_state = "类型待核" if pending_fields else "类型已核"
            value = sample.get(filter_field, ftv.MISSING)
            is_empty = value is ftv.MISSING or value is None or str(value).strip() == ""
            filter_state_decl = field_states.get(filter_field, {})
            comparable = (
                not is_empty
                and filter_field in report_scope
                and ftv.type_category(filter_state_decl.get("declared_type")) == "数值"
                and ftv.is_range_comparable(value)
            )

            filter_state = "参与取数"
            filtered_reason = ""
            if is_empty and null_handling == "排除空值":
                filter_state = "不满足过滤条件"
                filtered_reason = "空值处理为排除空值"
            elif is_empty and null_handling == "保留空值":
                filter_state = "参与取数"
                filtered_reason = "空值处理为保留空值，跳过条件判断"
            elif not is_empty:
                ok, matched_reason = self._matches(value, parsed)
                if not ok:
                    filter_state = "不满足过滤条件"
                    filtered_reason = matched_reason

            if type_state == "类型待核":
                verdict = "类型待核"
                verdict_reason = "类型声明待核，暂不进入格式化"
            elif filter_state != "参与取数":
                verdict = "不满足过滤条件"
                verdict_reason = filtered_reason or "过滤条件未命中"
            else:
                verdict = "可进入格式化"
                verdict_reason = filtered_reason or parsed.get("criteria_text", "")
            if reasons:
                verdict_reason = "；".join(reasons + ([verdict_reason] if verdict_reason else []))

            checked_values = {
                field: (None if sample.get(field, ftv.MISSING) is ftv.MISSING else sample.get(field))
                for field in fields
            }
            signature = ftv.row_fingerprint(checked_values)
            rows.append({
                "row_no": index,
                "sample_key": f"{signature}-{index}",
                "signature": signature,
                "values": dict(sample),
                "checked_values": checked_values,
                "type_state": type_state,
                "pending_fields": pending_fields,
                "filter_state": filter_state,
                "filter_reason": filtered_reason or parsed.get("criteria_text", ""),
                "range_comparable": comparable,
                "verdict": verdict,
                "verdict_reason": verdict_reason,
                "formattable": verdict == "可进入格式化",
                "reasons": reasons,
            })
        return rows

    def _mapping_conflicts(self, resolution: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in resolution:
            grouped.setdefault(item["declared_output_column"], []).append(item)
        conflicts = []
        for column, items in grouped.items():
            if len(items) < 2:
                continue
            categories = {item["source_field"]: ftv.type_category(item["source_type"]) for item in items}
            conflicts.append({
                "output_column": column,
                "sources": [item["source_field"] for item in items],
                "source_types": {item["source_field"]: item["source_type"] for item in items},
                "categories": categories,
                "conflict": len({value for value in categories.values() if value != "未知"}) > 1
                or "未知" in categories.values(),
                "actual_output_columns": [item["output_column"] for item in items],
                "resolution": "、".join(
                    f"{item['source_field']} → {item['output_column']}" for item in items
                ),
                "note": "同名列保留首个来源，其余来源以“列名@源字段”区分，避免类型冲突。",
            })
        return conflicts

    def validate_types(
        self,
        entry_id: int,
        samples: list[dict[str, Any]],
        *,
        declared_types: Any = None,
        template_no: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        """为一批字段样本生成过滤类型校验报告。

        - 判定口径：声明类型优先，样本值兜底；前导零编码不参与范围比较。
        - 报告关联规则（原字段、规则版本）与模板版本。
        - persist=True 时写入 data/filter_reports/，并挂到同一批次的版本链上。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档"}
        if not samples:
            return {"ok": False, "message": "样本为空，无法生成过滤类型校验报告"}

        rule_no = str(entry.get("规则编号") or f"DATA-{entry_id}")
        filter_field = str(entry.get("过滤字段") or "")
        parsed = ftv.parse_condition(entry.get("过滤条件", ""), filter_field)
        parsed["rule_no"] = rule_no

        template = self._template_for(template_no)
        if template_no and template is None:
            return {"ok": False, "message": f"模板 {template_no} 不存在，无法关联模板版本"}

        declarations = self._collect_declarations(entry, declared_types, template)

        # 报告范围：过滤字段 + 模板映射的源字段，这些字段决定能否格式化。
        # 其它已声明字段只是补充说明；样本里多出来的旁列只登记观测类型，
        # 都不作为格式化门槛。
        report_scope: list[str] = []
        for field in [filter_field]:
            if field and field not in report_scope:
                report_scope.append(field)
        for pair in ftv.parse_mapping(template.get("字段映射")) if template is not None else []:
            if pair[0] and pair[0] not in report_scope:
                report_scope.append(pair[0])
        observed_fields = list(report_scope)
        for sample in samples:
            for field in sample.keys():
                if str(field) not in observed_fields:
                    observed_fields.append(str(field))
        extra_fields = [
            field for field in observed_fields
            if field not in report_scope and field not in declarations
        ]

        field_reports, resolution = self._field_reports(
            observed_fields, samples, declarations, parsed, template, report_scope)
        conflicts = self._mapping_conflicts(resolution)
        rows = self._row_reports(
            samples, observed_fields, {item["field"]: item for item in field_reports},
            parsed, entry, report_scope)

        pending_rows = [row for row in rows if row["verdict"] == "类型待核"]
        formattable_rows = [row for row in rows if row["formattable"]]
        excluded_rows = [row for row in rows if row["verdict"] == "不满足过滤条件"]
        range_rows = [row for row in rows if row["range_comparable"]]
        scoped_reports = [item for item in field_reports if item["in_scope"]]
        undeclared = [item["field"] for item in scoped_reports if item["status"] != "已声明"]
        backfill_fields = [
            {
                "field": item["field"],
                "column": item["declaration_column"],
                "rule_no": rule_no,
                "hint": item["backfill_hint"],
            }
            for item in scoped_reports if item["status"] != "已声明"
        ]

        batch_fp = ftv.batch_fingerprint(samples)
        report_key = f"{ftv.slugify(rule_no, 'rule')}-{batch_fp[:10]}"
        previous_reports = [
            report for report in self._list_reports()
            if report.get("report_key") == report_key
        ]
        revision = len(previous_reports) + 1
        previous = previous_reports[-1] if previous_reports else None
        template_version = str(template.get("模板版本") or "") if template is not None else ""
        template_id = template.get("id") if template is not None else None
        report_id = f"{report_key}-r{revision}"

        column_bindings = [
            {
                "output_column": item["output_column"],
                "declared_output_column": item["declared_output_column"],
                "source_field": item["source_field"],
                "source_type": item["source_type"],
                "rule_no": rule_no,
                "rule_version": entry.get("规则版本"),
                "template_no": template_no,
                "template_version": template_version,
            }
            for item in resolution
        ]

        report: dict[str, Any] = {
            "ok": True,
            "report_id": report_id,
            "report_key": report_key,
            "revision": revision,
            "previous_report_id": previous.get("report_id") if previous else None,
            "rule_id": entry_id,
            "rule_no": rule_no,
            "rule_version": entry.get("规则版本"),
            "api": entry.get("关联API"),
            "filter_field": filter_field,
            "filter_condition": entry.get("过滤条件"),
            "filter_mode": parsed.get("mode"),
            "operator": parsed.get("operator"),
            "threshold": parsed.get("threshold"),
            "criteria_text": parsed.get("criteria_text"),
            "null_handling": entry.get("空值处理"),
            "precision_constraint": entry.get("精度约束"),
            "declared_types": self._declaration_map(declarations),
            "declarations": declarations,
            "template_id": template_id,
            "template_no": template_no,
            "template_version": template_version,
            "batch_fingerprint": batch_fp,
            "sample_count": len(samples),
            "samples": [dict(sample) for sample in samples],
            "checked_fields": report_scope,
            "observed_fields": observed_fields,
            "extra_fields": extra_fields,
            "source_fields": [item["source_field"] for item in resolution] or report_scope,
            "fields": field_reports,
            "rows": rows,
            "mapping": {
                "resolution": resolution,
                "conflicts": conflicts,
                "output_columns": [item["output_column"] for item in resolution],
            },
            "lineage": {
                "rule_no": rule_no,
                "rule_version": entry.get("规则版本"),
                "template_no": template_no,
                "template_version": template_version,
                "column_bindings": column_bindings,
            },
            "summary": {
                "sample_count": len(samples),
                "formattable_count": len(formattable_rows),
                "formattable_sample_keys": [row["sample_key"] for row in formattable_rows],
                "pending_type_count": len(pending_rows),
                "pending_type_sample_keys": [row["sample_key"] for row in pending_rows],
                "excluded_count": len(excluded_rows),
                "range_comparable_count": len(range_rows),
                "range_comparable_sample_keys": [row["sample_key"] for row in range_rows],
                "undeclared_fields": undeclared,
                "backfill_fields": backfill_fields,
                "extra_fields": extra_fields,
                "conflict_output_columns": [
                    item["output_column"] for item in conflicts if item["conflict"]
                ],
            },
        }

        if persist:
            from app.services.data_manager import write_filter_report
            write_filter_report(report_id, report)
        return report

    @staticmethod
    def _list_reports() -> list[dict[str, Any]]:
        from app.services.data_manager import list_filter_reports
        return list_filter_reports()

    # ---------- 报告查询与对比 ----------

    def get_report(self, report_id: str) -> dict[str, Any]:
        from app.services.data_manager import read_filter_report
        report = read_filter_report(report_id)
        if not report:
            return {"ok": False, "message": f"校验报告 {report_id} 不存在"}
        return report

    def list_reports(self, rule_no: str | None = None, batch_fingerprint: str | None = None,
                     template_no: str | None = None) -> list[dict[str, Any]]:
        from app.services.data_manager import list_filter_reports
        return list_filter_reports(rule_no=rule_no, batch_fingerprint=batch_fingerprint,
                                   template_no=template_no)

    def compare_reports(self, report_id: str, against_report_id: str = "") -> dict[str, Any]:
        """对比同一批样本的新旧两次类型判定。

        - previous：旧报告（默认取报告上的 previous_report_id）
        - current：新报告
        - rows：按样本签名对齐，标记判定/可格式化/类型状态的变化
        """
        current = self.get_report(report_id)
        if not current.get("ok", True) or "report_id" not in current:
            return current
        target_id = against_report_id or str(current.get("previous_report_id") or "")
        if not target_id:
            return {"ok": False, "message": "该报告没有可对比的上一版判定"}
        previous = self.get_report(target_id)
        if not previous.get("report_id"):
            return previous

        previous_rows = {row["signature"]: row for row in previous.get("rows", [])}
        current_rows = {row["signature"]: row for row in current.get("rows", [])}
        changes = []
        for signature in sorted(set(previous_rows) | set(current_rows)):
            before = previous_rows.get(signature)
            after = current_rows.get(signature)
            before_verdict = before.get("verdict") if before else "新增样本"
            after_verdict = after.get("verdict") if after else "样本移除"
            changed = before is None or after is None or before_verdict != after_verdict
            changes.append({
                "signature": signature,
                "sample_key": (after or before or {}).get("sample_key"),
                "before_verdict": before_verdict,
                "after_verdict": after_verdict,
                "before_formattable": bool(before and before.get("formattable")),
                "after_formattable": bool(after and after.get("formattable")),
                "changed": changed,
                "before_reasons": (before or {}).get("reasons", []),
                "after_reasons": (after or {}).get("reasons", []),
            })

        return {
            "ok": True,
            "report_key": current.get("report_key"),
            "batch_fingerprint": current.get("batch_fingerprint"),
            "previous": {
                "report_id": previous.get("report_id"),
                "revision": previous.get("revision"),
                "template_no": previous.get("template_no"),
                "template_version": previous.get("template_version"),
                "rule_version": previous.get("rule_version"),
                "criteria_text": previous.get("criteria_text"),
                "summary": previous.get("summary"),
            },
            "current": {
                "report_id": current.get("report_id"),
                "revision": current.get("revision"),
                "template_no": current.get("template_no"),
                "template_version": current.get("template_version"),
                "rule_version": current.get("rule_version"),
                "criteria_text": current.get("criteria_text"),
                "summary": current.get("summary"),
            },
            "template_changed": previous.get("template_version") != current.get("template_version")
            or previous.get("template_no") != current.get("template_no"),
            "rule_changed": previous.get("rule_version") != current.get("rule_version"),
            "changes": changes,
            "changed_count": sum(1 for item in changes if item["changed"]),
        }

    def rerun_for_template(self, template: Mapping[str, Any]) -> list[dict[str, Any]]:
        """模板提交校验后，重新校验同一批样本，生成新版本判定。"""
        from app.services.data_manager import list_filter_reports
        template_no = str(template.get("模板编号") or "")
        reports = [
            report for report in list_filter_reports()
            if report.get("api") == template.get("关联API")
            and (not report.get("template_no") or report.get("template_no") == template_no)
        ]
        # 同一批次只重跑最新一版，避免版本链无限增殖。
        latest: dict[str, dict[str, Any]] = {}
        for report in reports:
            key = str(report.get("report_key") or "")
            if not key:
                continue
            if key not in latest or int(report.get("revision") or 0) >= int(latest[key].get("revision") or 0):
                latest[key] = report

        generated: list[dict[str, Any]] = []
        for report in latest.values():
            # 重新按当前规则/模板计算声明，不沿用上一版的显式声明，
            # 否则维护人补齐的说明永远覆盖不到新判定。
            result = self.validate_types(
                int(report.get("rule_id") or 0),
                list(report.get("samples") or []),
                declared_types=None,
                template_no=template_no,
                persist=True,
            )
            if result.get("ok"):
                generated.append(result)
        return generated

    # ---------- 动作 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            from app.services.data_manager import read_api_snapshot
            api_name = entry.get("关联API", "")
            registry = read_api_snapshot()
            for api_data in (registry.values() if isinstance(registry, dict) else []):
                if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                    break
            else:
                return None, "关联API尚未注册，无法标记生效过滤规则"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        filter_list = []
        for row in rows:
            filter_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "api": row.get("关联API"),
                "field": row.get("过滤字段"),
                "condition": row.get("过滤条件"),
                "fuzzy": row.get("模糊策略"),
                "precision": row.get("精度约束"),
                "null_handling": row.get("空值处理"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(filter_list),
            "filters": filter_list,
            "active_count": sum(1 for r in filter_list if r.get("status") == "已生效"),
        }
