"""结果格式化领域服务：管理查询结果的输出格式、字段映射和分页配置。

跨模块数据流：
- 消费 api_registry 快照 → 关联API自动选择格式模板
- 消费 data_filter 快照/校验报告 → 只格式化“过滤类型校验报告”允许的行与字段
- 消费 field_mask 快照 → 脱敏后数据按模板输出
- 被 export_task 消费 → 格式化结果写入导出文件

字段映射的换版追溯：
- 每次“提交校验/标记生效”都会沉淀一条模板版本绑定，记录模板版本、映射解析结果
  以及同名列实际使用的来源字段。
- `resolve_template_columns`/`trace_template_usage` 可按模板或报告查回实际使用的
  字段与规则。
"""
from __future__ import annotations

from typing import Any, Mapping

from app.services import filter_type_validation as ftv
from app.services.base import BaseModuleService
from app.store import store


class ResultFormatService(BaseModuleService):
    MODULE = "result_format"
    ENTITY = "格式模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板编号", "关联API", "输出格式", "字段映射", "字段映射类型", "嵌套展开", "分页策略", "排序字段", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "关联API", "输出格式"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    # ---------- 模板版本绑定 ----------

    def _template_declarations(self, template: Mapping[str, Any]) -> dict[str, str]:
        declarations = ftv.parse_declaration(template.get("字段映射类型"))
        return {field: text for field, (text, _) in declarations.items()}

    def resolve_template_columns(self, template: Mapping[str, Any]) -> dict[str, Any]:
        """解析模板字段映射，返回输出列与同名列的实际来源字段。"""
        pairs = ftv.parse_mapping(template.get("字段映射"))
        declared_types = self._template_declarations(template)
        resolution, conflicts = ftv.resolve_mapping(pairs, declared_types)
        return {
            "template_no": template.get("模板编号"),
            "template_version": template.get("模板版本"),
            "api": template.get("关联API"),
            "raw_mapping": template.get("字段映射"),
            "declared_types": declared_types,
            "resolution": resolution,
            "conflicts": conflicts,
            "output_columns": [item["output_column"] for item in resolution],
            "lineage": {
                "column_bindings": [
                    {
                        "output_column": item["output_column"],
                        "declared_output_column": item["declared_output_column"],
                        "source_field": item["source_field"],
                        "source_type": item["source_type"],
                        "template_no": template.get("模板编号"),
                        "template_version": template.get("模板版本"),
                    }
                    for item in resolution
                ],
            },
        }

    def record_template_binding(self, entry: Mapping[str, Any], action: str,
                                report: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """把当前模板版本的映射解析结果写入共享目录，供换版后查回。"""
        from app.services.data_manager import _now_iso, write_template_binding

        resolved = self.resolve_template_columns(entry)
        template_no = str(entry.get("模板编号") or "")
        template_version = str(entry.get("模板版本") or "") or "v1"
        binding = {
            "template_id": entry.get("id"),
            "template_no": template_no,
            "template_version": template_version,
            "api": entry.get("关联API"),
            "action": action,
            "status": entry.get("status"),
            "raw_mapping": entry.get("字段映射"),
            "declared_types": resolved["declared_types"],
            "resolution": resolved["resolution"],
            "conflicts": resolved["conflicts"],
            "output_columns": resolved["output_columns"],
            "report_id": (report or {}).get("report_id"),
            "rule_no": (report or {}).get("rule_no"),
            "rule_version": (report or {}).get("rule_version"),
            "recorded_at": _now_iso(),
        }
        write_template_binding(template_no, template_version, binding)
        return binding

    def list_template_bindings(self, template_no: str) -> list[dict[str, Any]]:
        from app.services.data_manager import list_template_bindings

        return list_template_bindings(template_no)

    def trace_template_usage(self, template_no: str | None = None, *, report_id: str = "",
                             output_column: str = "") -> dict[str, Any]:
        """按模板编号 / 报告 / 输出列查回实际使用的字段与规则版本。"""
        from app.services.data_manager import read_filter_report

        report: dict[str, Any] = {}
        if report_id:
            report = read_filter_report(report_id)
            if not report:
                return {"ok": False, "message": f"校验报告 {report_id} 不存在"}
            if not template_no:
                template_no = str(report.get("template_no") or "")

        bindings: list[dict[str, Any]] = []
        if template_no:
            bindings = [
                {"template_no": template_no, "template_version": row.get("模板版本"),
                 "api": row.get("关联API"), "resolution": self.resolve_template_columns(row)["resolution"],
                 "conflicts": self.resolve_template_columns(row)["conflicts"],
                 "report_id": report.get("report_id") if report else None,
                 "rule_no": report.get("rule_no") if report else None,
                 "rule_version": report.get("rule_version") if report else None,
                 "current": True}
                for row in store.rows(self.MODULE)
                if row.get("模板编号") == template_no
            ]
            bindings.extend(self.list_template_bindings(template_no))

        if not bindings and not report:
            return {"ok": False, "message": f"模板 {template_no or '(未指定)'} 没有可追溯的映射记录"}

        # 早期版本可能只留下映射绑定、没带报告；按模板+版本回查当时的报告补齐规则信息。
        from app.services.data_manager import list_filter_reports

        def report_for(binding: Mapping[str, Any]) -> dict[str, Any]:
            if binding.get("rule_no"):
                return {}
            candidates = [
                item for item in list_filter_reports(
                    template_no=str(binding.get("template_no") or template_no or ""))
                if str(item.get("template_version")) == str(binding.get("template_version"))
            ]
            return candidates[-1] if candidates else {}

        seen: set[tuple[str, str, str]] = set()
        columns: list[dict[str, Any]] = []
        for binding in bindings:
            linked = report_for(binding)
            for item in binding.get("resolution") or []:
                if output_column and output_column not in {
                    item.get("output_column"), item.get("declared_output_column")
                }:
                    continue
                key = (
                    str(binding.get("template_version")),
                    str(item.get("output_column")),
                    str(item.get("source_field")),
                )
                if key in seen:
                    continue
                seen.add(key)
                columns.append({
                    "template_no": binding.get("template_no") or template_no,
                    "template_version": binding.get("template_version"),
                    "output_column": item.get("output_column"),
                    "declared_output_column": item.get("declared_output_column"),
                    "source_field": item.get("source_field"),
                    "source_type": item.get("source_type"),
                    "rule_no": binding.get("rule_no") or linked.get("rule_no")
                    or (report.get("rule_no") if report else None),
                    "rule_version": binding.get("rule_version") or linked.get("rule_version")
                    or (report.get("rule_version") if report else None),
                    "report_id": binding.get("report_id") or linked.get("report_id"),
                    "current": bool(binding.get("current")),
                })

        result: dict[str, Any] = {
            "ok": True,
            "template_no": template_no,
            "output_column": output_column or None,
            "columns": columns,
            "version_count": len({(item["template_version"], item["source_field"]) for item in columns}),
        }
        if report:
            result["report"] = {
                "report_id": report.get("report_id"),
                "rule_no": report.get("rule_no"),
                "rule_version": report.get("rule_version"),
                "template_version": report.get("template_version"),
                "source_fields": report.get("source_fields"),
                "mapping": report.get("mapping"),
            }
        return result

    # ---------- 格式化 ----------

    def _pick_template(self, api_name: str, template_no: str = "") -> dict[str, Any] | None:
        if template_no:
            for row in store.rows(self.MODULE):
                if row.get("模板编号") == template_no:
                    return row
            return None
        for row in store.rows(self.MODULE):
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                return row
        return None

    def _select_from_report(
        self,
        report: Mapping[str, Any],
        data_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按校验报告筛出允许格式化的行和字段。

        - 类型待核 / 不满足过滤条件的行不进入格式化；
        - 行内字段按报告声明的字段范围裁剪，避免把未校验字段偷偷带出；
        - 返回报告与行/字段的对应关系，便于回溯。
        """
        # 报告行签名覆盖其观测到的全部字段（含旁列），格式化候选行也必须用同一
        # 字段集合计算签名，否则旁列会让同一行对不上。
        signature_fields = list(report.get("observed_fields") or report.get("checked_fields") or [])
        allowed_signatures = {
            row.get("signature")
            for row in report.get("rows", [])
            if row.get("formattable")
        }
        fields = list(report.get("checked_fields") or [])
        selected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for index, row in enumerate(data_rows):
            signature = ftv.row_fingerprint({field: row.get(field) for field in signature_fields})
            if signature in allowed_signatures:
                selected.append({field: row[field] for field in fields if field in row})
            else:
                selected_row = {field: row.get(field) for field in signature_fields}
                matching = next(
                    (item for item in report.get("rows", []) if item.get("signature") == signature),
                    None,
                )
                skipped.append({
                    "row_no": index + 1,
                    "signature": signature,
                    "verdict": (matching or {}).get("verdict", "类型待核"),
                    "reason": (matching or {}).get(
                        "verdict_reason",
                        "不在校验报告的可格式化范围内（类型待核或不满足过滤条件）",
                    ),
                    "values": selected_row,
                })
        return {
            "rows": selected,
            "skipped": skipped,
            "report_id": report.get("report_id"),
            "fields": fields,
            "source_fields": report.get("source_fields") or fields,
        }

    def format_results(
        self,
        api_name: str,
        data_rows: list[dict[str, Any]] | None = None,
        format_type: str = "json",
        *,
        template_no: str = "",
        report_id: str = "",
        report: Mapping[str, Any] | None = None,
    ) -> Any:
        """对查询结果按模板格式化。

        传入 report_id（或 report）时，以“过滤类型校验报告”给出的数据源和字段范围
        为输入：未显式传 data_rows 时直接使用报告里的样本；类型待核的行不会被
        格式化，字段也限定在报告的校验范围里。
        """
        template = self._pick_template(api_name, template_no)
        if not template:
            return data_rows if data_rows is not None else []

        resolved_report: Mapping[str, Any] = report or {}
        if report_id and not resolved_report:
            from app.services.data_manager import read_filter_report
            resolved_report = read_filter_report(report_id)
        if report_id and not resolved_report:
            return {"ok": False, "message": f"校验报告 {report_id} 不存在，未生成格式化结果"}

        if data_rows is None:
            data_rows = list(resolved_report.get("samples") or []) if resolved_report else []
        if not data_rows:
            return data_rows

        selection: dict[str, Any] | None = None
        if resolved_report:
            selection = self._select_from_report(resolved_report, data_rows)
            data_rows = selection["rows"]
            if not data_rows:
                return {
                    "ok": True,
                    "report_id": resolved_report.get("report_id"),
                    "template_no": template.get("模板编号"),
                    "template_version": template.get("模板版本"),
                    "data": [] if (template.get("输出格式") or format_type) != "csv" else "",
                    "fields": selection["fields"],
                    "skipped": selection["skipped"],
                    "message": "校验报告没有可进入格式化的行",
                }

        output_format = template.get("输出格式", format_type)
        output_format = output_format if output_format in {"csv", "nested", "json"} else format_type
        field_mapping = template.get("字段映射", "")
        sort_field = template.get("排序字段", "")

        resolved = self.resolve_template_columns(template)
        declared_types = self._template_declarations(template)

        # 字段映射：同一结果列有多个来源时按版本解析，非首个来源以“列名@源字段”区分。
        mapping_rows: list[dict[str, Any]] = []
        if field_mapping and resolved["resolution"]:
            for row in data_rows:
                mapped_row: dict[str, Any] = {}
                conflict_skipped = False
                for item in resolved["resolution"]:
                    source = item["source_field"]
                    output_column = item["output_column"]
                    value = row.get(source, "")
                    declared_category = ftv.type_category(item["source_type"])
                    observed_category = ftv.type_category(ftv.infer_value_type(value))
                    if (
                        declared_category == "数值"
                        and observed_category not in {"数值", "未知", "布尔"}
                    ):
                        # 声明为数值但实际是文本（如前导零编码）：整行类型待核，
                        # 不交出残缺的映射结果，并登记跳过原因。
                        conflict_skipped = True
                        break
                    mapped_row[output_column] = value
                if conflict_skipped:
                    if selection is not None:
                        selection["skipped"].append({
                            "row_no": len(selection["skipped"]) + 1,
                            "signature": ftv.row_fingerprint(row),
                            "verdict": "类型待核",
                            "reason": "映射来源字段声明为数值，但样本值不是数值，整行不进入格式化",
                            "values": dict(row),
                        })
                    continue
                mapping_rows.append(mapped_row)
            data_rows = mapping_rows
        else:
            data_rows = [
                {
                    key: value for key, value in row.items()
                    if not declared_types
                    or key not in declared_types
                    or ftv.type_category(ftv.infer_value_type(value)) == ftv.type_category(declared_types[key])
                }
                for row in data_rows
            ]

        # 排序
        if sort_field:
            target_field = sort_field
            if resolved["resolution"]:
                for item in resolved["resolution"]:
                    if sort_field in {item["source_field"], item["declared_output_column"]}:
                        target_field = item["output_column"]
                        break
            if data_rows and target_field in data_rows[0]:
                data_rows = sorted(data_rows, key=lambda r: str(r.get(target_field, "")))

        if output_format == "csv":
            data = self._to_csv(data_rows)
        elif output_format == "nested":
            data = {"nested": True, "total": len(data_rows), "items": data_rows}
        else:
            data = data_rows

        if selection is None:
            return data

        return {
            "ok": True,
            "report_id": resolved_report.get("report_id"),
            "rule_no": resolved_report.get("rule_no"),
            "rule_version": resolved_report.get("rule_version"),
            "template_no": template.get("模板编号"),
            "template_version": template.get("模板版本"),
            "fields": resolved["output_columns"] or selection["fields"],
            "source_fields": selection["source_fields"],
            "data": data,
            "skipped": selection["skipped"],
            "mapping": {
                "resolution": resolved["resolution"],
                "conflicts": resolved["conflicts"],
            },
            "lineage": {
                "column_bindings": [
                    {
                        "output_column": item["output_column"],
                        "source_field": item["source_field"],
                        "template_no": template.get("模板编号"),
                        "template_version": template.get("模板版本"),
                        "rule_no": resolved_report.get("rule_no"),
                        "rule_version": resolved_report.get("rule_version"),
                    }
                    for item in resolved["resolution"]
                ],
            },
        }

    def _to_csv(self, data_rows: list[dict[str, Any]]) -> str:
        if not data_rows:
            return ""
        keys = list(data_rows[0].keys())
        lines = [",".join(keys)]
        for row in data_rows:
            lines.append(",".join(str(row.get(k, "")) for k in keys))
        return "\n".join(lines)

    # ---------- 动作 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        report: dict[str, Any] | None = None
        if action == "提交校验":
            # 模板提交校验：重新校验同一批样本，旧判定保留在版本链里供对比。
            from app.services import service_for
            filter_svc = service_for("data_filter")
            if filter_svc is not None and hasattr(filter_svc, "rerun_for_template"):
                reports = filter_svc.rerun_for_template(entry)
                if reports:
                    report = reports[0]
                    entry["_last_report_id"] = report.get("report_id")
                    entry["_last_report_summary"] = report.get("summary")

        result = super().run_action(entry_id, action)
        if result[0] is not None:
            self.record_template_binding(entry, action, report)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        if report is not None:
            message = (
                f"{result[1]}；已完成同批样本类型校验，"
                f"可格式化 {report['summary']['formattable_count']}/{report['summary']['sample_count']} 行，"
                f"类型待核 {report['summary']['pending_type_count']} 行"
            )
            return result[0], message
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        format_list = []
        for row in rows:
            format_list.append({
                "id": row.get("id"),
                "template_no": row.get("模板编号"),
                "api": row.get("关联API"),
                "output_format": row.get("输出格式"),
                "field_mapping": row.get("字段映射"),
                "nesting": row.get("嵌套展开"),
                "pagination": row.get("分页策略"),
                "sort": row.get("排序字段"),
                "version": row.get("模板版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(format_list),
            "templates": format_list,
            "active_count": sum(1 for r in format_list if r.get("status") == "已生效"),
        }
