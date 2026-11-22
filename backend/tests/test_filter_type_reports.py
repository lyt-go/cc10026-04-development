"""过滤类型校验报告测试：前导零编码、纯数字、缺少声明三类样本，

覆盖报告生成、范围比较判定、同名列冲突、模板换版对比与按报告格式化。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

temp_data = tempfile.mkdtemp(prefix="filter_type_test_")
os.environ["DATA_ROOT"] = temp_data


def _seed_rule(rule_id: int, *, field: str, condition: str, declared: str,
               version: str = "v1", null_handling: str = "保留空值") -> dict:
    from app.store import store

    entry = {
        "id": rule_id,
        "status": "待校验",
        "pending": True,
        "abnormal": False,
        "规则编号": f"DATA-TYPE-{rule_id:03d}",
        "关联API": "订单查询API",
        "过滤字段": field,
        "过滤条件": condition,
        "模糊策略": "精确",
        "精度约束": declared,
        "空值处理": null_handling,
        "规则版本": version,
    }
    store.rows("data_filter").append(entry)
    return entry


def _seed_template(template_id: int, *, mapping: str, version: str = "v1",
                   declared: str = "编码=文本;数量=数值") -> dict:
    from app.store import store

    entry = {
        "id": template_id,
        "status": "草稿",
        "pending": True,
        "abnormal": False,
        "模板编号": f"RESU-TYPE-{template_id:03d}",
        "关联API": "订单查询API",
        "输出格式": "json",
        "字段映射": mapping,
        "嵌套展开": "否",
        "分页策略": "不分页",
        "排序字段": "",
        "模板版本": version,
        "字段映射类型": declared,
    }
    store.rows("result_format").append(entry)
    return entry


SAMPLES = [
    {"编码": "007", "数量": 7},   # 前导零编码 + 可参与比较的数值
    {"编码": "8", "数量": 12},    # 纯数字编码，但按文本声明，不做范围比较
    {"编码": "A12", "数量": ""},  # 文本编码，数量空值
]


def setup_function() -> None:
    from app.seed import SEED_ROWS
    from app.store import store as s

    s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    import shutil
    shutil.rmtree(temp_data, ignore_errors=True)
    os.makedirs(temp_data, exist_ok=True)


def test_leading_zero_and_numeric_samples_are_separated():
    """前导零编码不可范围比较，纯数字在数值声明下可比较。"""
    from app.services import service_for

    rule = _seed_rule(101, field="数量", condition="数量 >= 10",
                      declared="数量=数值;编码=文本", null_handling="排除空值")
    _seed_template(201, mapping="编码:结果值,数量:结果值")
    service = service_for("data_filter")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-201")

    rows = {row["values"]["编码"]: row for row in report["rows"]}
    # 007 数量=7 可范围比较但未达到 >=10；这个字段本身是可比较类型
    assert rows["007"]["range_comparable"] is True
    assert rows["007"]["filter_state"] == "不满足过滤条件"
    assert rows["8"]["range_comparable"] is True
    assert rows["8"]["verdict"] == "可进入格式化"
    assert rows["A12"]["range_comparable"] is False

    assert report["summary"]["range_comparable_count"] == 2
    assert report["summary"]["formattable_count"] == 1
    assert report["summary"]["excluded_count"] == 2


def test_leading_zero_code_is_not_range_comparable():
    """过滤字段本身是前导零编码时，任何行都不能参与范围比较。"""
    from app.services import service_for

    rule = _seed_rule(102, field="编码", condition="编码 >= 5", declared="编码=文本;数量=数值")
    _seed_template(202, mapping="编码:结果值,数量:结果值")
    service = service_for("data_filter")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-202")

    assert report["summary"]["range_comparable_count"] == 0
    assert all(not row["range_comparable"] for row in report["rows"])
    # 文本字段仍按包含匹配，007 不含 "编码 >= 5" 的阈值表达式，因此不通过。
    assert report["summary"]["formattable_count"] == 0


def test_missing_declaration_blocks_formatting_and_suggests_column():
    """缺少声明的字段进入类型待核，并提示从精度约束列补齐。"""
    from app.services import service_for

    rule = _seed_rule(103, field="数量", condition="数量 >= 10", declared="")
    _seed_template(203, mapping="编码:编码,数量:数量", declared="")
    service = service_for("data_filter")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-203")

    assert report["summary"]["pending_type_count"] == 3
    assert report["summary"]["formattable_count"] == 0
    assert report["summary"]["range_comparable_count"] == 0

    hints = {item["field"]: item for item in report["summary"]["backfill_fields"]}
    assert hints["数量"]["column"] == "精度约束"
    assert "数量=数值" in hints["数量"]["hint"]
    assert hints["编码"]["column"] == "精度约束"
    assert "编码=文本" in hints["编码"]["hint"]


def test_declaration_contradicting_samples_is_flagged_per_row():
    """声明数值但样本是编码文本时，仅该行进入类型待核。"""
    from app.services import service_for

    rule = _seed_rule(104, field="数量", condition="数量 >= 10", declared="数量=数值;编码=数值")
    _seed_template(204, mapping="编码:编码,数量:数量", declared="")
    service = service_for("data_filter")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-204")

    rows = {row["values"]["编码"]: row for row in report["rows"]}
    assert rows["007"]["verdict"] == "类型待核"  # 编码 007 声明数值，但前导零编码
    assert rows["A12"]["verdict"] == "类型待核"  # A12 声明数值，但文本
    assert rows["8"]["verdict"] == "可进入格式化"  # 纯数字编码满足数值声明
    assert "编码" in rows["007"]["pending_fields"]


def test_report_links_rule_and_template_and_resolves_same_name_columns():
    """报告关联规则原字段/版本与模板版本，同名列映射给出实际输出列。"""
    from app.services import service_for

    rule = _seed_rule(105, field="数量", condition="数量 >= 10", declared="数量=数值;编码=文本")
    _seed_template(205, mapping="编码:结果值,数量:结果值")
    service = service_for("data_filter")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-205")

    assert report["rule_no"] == rule["规则编号"]
    assert report["rule_version"] == "v1"
    assert report["template_no"] == "RESU-TYPE-205"
    assert report["template_version"] == "v1"
    assert report["filter_field"] == "数量"
    assert report["checked_fields"] == ["数量", "编码"]
    assert "前导零" in report["criteria_text"] or "数值范围比较" in report["criteria_text"]

    conflict = report["mapping"]["conflicts"][0]
    assert conflict["output_column"] == "结果值"
    assert conflict["conflict"] is True
    assert conflict["categories"] == {"编码": "文本", "数量": "数值"}
    assert report["mapping"]["output_columns"] == ["结果值", "结果值@数量"]
    bindings = report["lineage"]["column_bindings"]
    assert {item["source_field"] for item in bindings} == {"编码", "数量"}
    assert all(item["template_version"] == "v1" for item in bindings)
    assert all(item["rule_no"] == rule["规则编号"] for item in bindings)


def test_formatting_uses_report_rows_and_field_scope():
    """格式化只使用报告允许的行和字段，类型待核数据不交给格式化。"""
    from app.services import service_for

    rule = _seed_rule(106, field="数量", condition="数量 >= 10",
                      declared="数量=数值;编码=文本", null_handling="排除空值")
    _seed_template(206, mapping="编码:结果值,数量:结果值")
    service = service_for("data_filter")
    format_svc = service_for("result_format")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-206")
    result = format_svc.format_results(
        "订单查询API", None, template_no="RESU-TYPE-206", report_id=report["report_id"])

    assert result["ok"] is True
    assert result["report_id"] == report["report_id"]
    # 只有 编码=8、数量=12 这一行进入格式化，同名列按版本消歧。
    assert len(result["data"]) == 1
    assert result["data"][0]["结果值"] == "8"
    assert result["data"][0]["结果值@数量"] == 12
    assert {item["source_field"] for item in result["lineage"]["column_bindings"]} == {"编码", "数量"}
    assert len(result["skipped"]) == 2


def test_type_pending_rows_are_never_formatted():
    """类型待核的行即使传了原始数据也不会进入格式化。"""
    from app.services import service_for

    rule = _seed_rule(107, field="数量", condition="数量 >= 10", declared="", null_handling="排除空值")
    _seed_template(207, mapping="编码:编码,数量:数量", declared="")
    service = service_for("data_filter")
    format_svc = service_for("result_format")

    report = service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-207")
    result = format_svc.format_results(
        "订单查询API", SAMPLES, template_no="RESU-TYPE-207", report_id=report["report_id"])

    assert result["ok"] is True
    assert result["data"] == []
    assert len(result["skipped"]) == len(SAMPLES)


def test_template_resubmission_regenerates_and_compares_judgement():
    """模板提交后重新校验同一批样本，可对比新旧判定并查回换版字段。"""
    from app.services import service_for

    rule = _seed_rule(108, field="数量", condition="数量 >= 10",
                      declared="数量=数值;编码=文本", null_handling="排除空值")
    template = _seed_template(208, mapping="编码:结果值,数量:结果值")
    filter_svc = service_for("data_filter")
    format_svc = service_for("result_format")

    # v1 先提交校验，留下模板版本绑定。
    format_svc.run_action(template["id"], "提交校验")
    first = filter_svc.validate_types(rule["id"], SAMPLES, template_no=template["模板编号"])
    assert first["revision"] == 1

    # 维护人补齐说明：把编码改为数值声明，同批样本重新判定。
    rule["精度约束"] = "数量=数值;编码=数值"
    template["模板版本"] = "v2"
    template["字段映射"] = "编码:结果值,数量:结果值"

    report, message = format_svc.run_action(template["id"], "提交校验")
    assert report is not None, message
    latest = filter_svc.get_report(template["_last_report_id"])
    assert latest["revision"] == 2
    assert latest["previous_report_id"] == first["report_id"]
    assert latest["template_version"] == "v2"

    diff = filter_svc.compare_reports(latest["report_id"])
    assert diff["ok"] is True
    assert diff["template_changed"] is True
    assert diff["previous"]["report_id"] == first["report_id"]
    assert diff["current"]["report_id"] == latest["report_id"]
    assert diff["changed_count"] > 0

    # 换版后仍能查回“结果值”这一列实际用了哪些源字段。
    lineage = format_svc.trace_template_usage(template["模板编号"], output_column="结果值")
    assert lineage["ok"] is True
    sources = {item["source_field"] for item in lineage["columns"]}
    assert {"编码", "数量"} <= sources
    assert {item["template_version"] for item in lineage["columns"]} >= {"v1", "v2"}
    assert all(item["rule_no"] == rule["规则编号"] for item in lineage["columns"])


def test_report_versions_are_persisted_and_filterable():
    """报告落盘后可按规则/模板筛选，并保留历次修订。"""
    from app.services import service_for

    rule = _seed_rule(109, field="数量", condition="数量 >= 10",
                      declared="数量=数值;编码=文本", null_handling="排除空值")
    _seed_template(209, mapping="编码:编码,数量:数量")
    service = service_for("data_filter")

    service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-209")
    service.validate_types(rule["id"], SAMPLES, template_no="RESU-TYPE-209")

    reports = service.list_reports(rule_no=rule["规则编号"])
    assert len(reports) == 2
    assert [item["revision"] for item in reports] == [1, 2]
    assert reports[1]["previous_report_id"] == reports[0]["report_id"]
    assert all(item["batch_fingerprint"] == reports[0]["batch_fingerprint"] for item in reports)


def test_apply_filters_uses_range_semantics_for_comparison_conditions():
    """已生效规则的 `字段 >= 阈值` 按数值比较，前导零编码不参与。"""
    from app.services import service_for

    rule = _seed_rule(110, field="数量", condition="数量 >= 10", declared="数量=数值;编码=文本")
    rule["status"] = "已生效"
    service = service_for("data_filter")

    kept = service.apply_filters("订单查询API", [
        {"编码": "007", "数量": 7},
        {"编码": "8", "数量": 12},
        {"编码": "A12", "数量": "15"},
        {"编码": "B1", "数量": "abc"},
    ])
    assert [row["编码"] for row in kept] == ["8", "A12"]


def test_extra_columns_do_not_break_row_matching_and_numeric_guard_still_applies():
    """样本带旁列时仍能对上可格式化行；标注数值的编码不会被当作数值输出。"""
    from app.services import service_for

    rule = _seed_rule(111, field="数量", condition="数量 >= 10",
                      declared="数量=数值", null_handling="排除空值")
    # 模板把编码输出为“编码”，并在映射类型里把编码标为数值，制造同列类型冲突。
    _seed_template(211, mapping="编码:编码,数量:数量", declared="编码=数值;数量=数值")
    service = service_for("data_filter")
    format_svc = service_for("result_format")

    samples = [{"编码": "007", "数量": 12, "备注": "加急"}]
    report = service.validate_types(rule["id"], samples, template_no="RESU-TYPE-211")

    row = report["rows"][0]
    assert row["verdict"] == "类型待核"
    assert "编码" in row["pending_fields"]
    assert "备注" in report["extra_fields"]

    result = format_svc.format_results(
        "订单查询API", samples, template_no="RESU-TYPE-211", report_id=report["report_id"])
    assert result["ok"] is True
    # 类型待核的数据不直接交给格式化。
    assert result["data"] == []
    assert result["skipped"]


def test_numeric_declared_column_with_text_value_is_not_formatted():
    """通过报告筛选后，若映射来源声明为数值而值是文本，整行仍不输出。"""
    from app.services import service_for

    rule = _seed_rule(112, field="数量", condition="数量 >= 10",
                      declared="数量=数值;编码=文本", null_handling="排除空值")
    _seed_template(212, mapping="编码:编码,数量:数量", declared="编码=文本;数量=数值")
    service = service_for("data_filter")
    format_svc = service_for("result_format")

    samples = [{"编码": "8", "数量": 12}]
    report = service.validate_types(rule["id"], samples, template_no="RESU-TYPE-212")
    assert report["rows"][0]["verdict"] == "可进入格式化"

    # 模拟模板换版后把“编码”声明为数值，但报告允许的行里该值仍是文本形式。
    tampered = dict(report)
    tampered["checked_fields"] = ["编码", "数量"]
    tampered["observed_fields"] = ["编码", "数量"]
    result = format_svc.format_results(
        "订单查询API", [{"编码": "A12", "数量": 12}],
        template_no="RESU-TYPE-212", report=tampered)

    assert result["data"] == []
    assert any(item["verdict"] == "类型待核" for item in result["skipped"])
