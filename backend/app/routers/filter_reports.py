"""过滤类型校验报告接口：生成、查询、对比报告，以及按报告数据源做结果格式化。

固定路径必须注册在通用资源路由 `/api/{module}/{entry_id}` 之前，
否则会被动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/filter-reports", tags=["过滤类型校验"])


class ValidateTypesPayload(BaseModel):
    samples: list[dict[str, Any]] = Field(default_factory=list)
    declared_types: dict[str, str] | str | None = None
    template_no: str = ""
    persist: bool = True


class FormatPayload(BaseModel):
    api: str
    template_no: str = ""
    report_id: str = ""
    rows: list[dict[str, Any]] | None = None


def _filter_service():
    service = service_for("data_filter")
    if service is None:
        raise HTTPException(status_code=500, detail="数据过滤服务未注册")
    return service


@router.post("/rules/{rule_id}")
def create_filter_report(rule_id: int, payload: ValidateTypesPayload) -> dict[str, Any]:
    """按过滤规则对一批字段样本生成类型校验报告。"""
    report = _filter_service().validate_types(
        rule_id,
        payload.samples,
        declared_types=payload.declared_types,
        template_no=payload.template_no,
        persist=payload.persist,
    )
    if not report.get("ok"):
        raise HTTPException(status_code=400, detail=report.get("message", "生成校验报告失败"))
    return report


@router.get("")
def list_filter_reports(rule_no: str | None = None,
                        batch_fingerprint: str | None = None,
                        template_no: str | None = None) -> dict[str, Any]:
    """按规则编号、样本批次或模板版本筛选历史报告。"""
    reports = _filter_service().list_reports(rule_no, batch_fingerprint, template_no)
    return {
        "items": [
            {
                "report_id": item.get("report_id"),
                "revision": item.get("revision"),
                "rule_no": item.get("rule_no"),
                "rule_version": item.get("rule_version"),
                "template_no": item.get("template_no"),
                "template_version": item.get("template_version"),
                "batch_fingerprint": item.get("batch_fingerprint"),
                "created_at": item.get("created_at"),
                "summary": item.get("summary"),
            }
            for item in reports
        ],
        "total": len(reports),
    }


@router.get("/{report_id}")
def get_filter_report(report_id: str) -> dict[str, Any]:
    report = _filter_service().get_report(report_id)
    if not report.get("report_id"):
        raise HTTPException(status_code=404, detail=report.get("message", "校验报告不存在"))
    return report


@router.get("/{report_id}/compare")
def compare_filter_reports(report_id: str, against: str = "") -> dict[str, Any]:
    """对比同一批样本的新旧判定；默认对比该报告的上一版。"""
    result = _filter_service().compare_reports(report_id, against)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "报告对比失败"))
    return result


@router.post("/{report_id}/format")
def format_from_report(report_id: str, payload: FormatPayload) -> dict[str, Any]:
    """按校验报告给出的数据源与字段范围生成格式化结果。"""
    format_svc = service_for("result_format")
    if format_svc is None:
        raise HTTPException(status_code=500, detail="结果格式化服务未注册")
    result = format_svc.format_results(
        payload.api,
        payload.rows,
        template_no=payload.template_no,
        report_id=report_id,
    )
    if isinstance(result, dict) and result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result.get("message", "格式化失败"))
    return result if isinstance(result, dict) else {"ok": True, "data": result}


@router.get("/{report_id}/lineage")
def report_lineage(report_id: str, template_no: str = "") -> dict[str, Any]:
    """按报告/模板查回同名列实际使用的字段与规则版本。"""
    format_svc = service_for("result_format")
    if format_svc is None:
        raise HTTPException(status_code=500, detail="结果格式化服务未注册")
    result = format_svc.trace_template_usage(template_no or None, report_id=report_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "映射沿革不存在"))
    return result
