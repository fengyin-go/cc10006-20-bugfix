"""操作审计接口：维护审计日志，覆盖提交审核、归档日志、忽略日志、恢复日志等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import AuditActionResult, EntryPayload, PageResult
from app.services.audit import RESTORE_ACTION, AuditService

router = APIRouter(prefix="/api/audit", tags=["操作审计"])

service = AuditService()

LIST_FIELDS = ["日志编号", "操作类型", "操作对象", "操作详情", "操作来源", "执行时间", "影响行数", "回滚能力"]
STATUSES = ["草稿", "待审核", "已归档", "已忽略"]
ACTIONS = ["提交审核", "归档日志", "忽略日志", RESTORE_ACTION]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="草稿、待审核、已归档、已忽略"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按日志编号与状态过滤操作审计列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshot")
def audit_snapshot() -> dict[str, Any]:
    """操作审计统一结果：概览异常量、质量通知、审计影响行数的同一份事实来源。"""
    return service.snapshot()


@router.get("/notifications")
def quality_notifications() -> dict[str, Any]:
    """质量通知：与概览异常量、日志详情同口径，只看待审核的真实异常。"""
    snapshot = service.snapshot()
    return {
        "module": "audit",
        "total": snapshot["abnormal"],
        "notifications": snapshot["notifications"],
        "abnormal": snapshot["abnormal"],
        "affected_rows": snapshot["affected_rows"],
    }


@router.get("/trail")
def audit_trail(entry_id: int | None = Query(default=None, description="按日志编号过滤动作流水")) -> dict[str, Any]:
    """动作流水：每条请求都可追溯，含被拒绝的非法跳转与幂等命中。"""
    items = service.trail(entry_id)
    return {"module": "audit", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出操作审计清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "audit", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条审计日志明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计日志 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=AuditActionResult)
def create_entry(payload: EntryPayload) -> AuditActionResult:
    """登记一条审计日志，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return AuditActionResult(
            ok=False, applied=False, message=f"缺少必填字段：{'、'.join(missing)}"
        )
    return AuditActionResult(
        ok=True,
        applied=True,
        message="审计日志已登记",
        entry=entry,
        snapshot=service.snapshot(),
    )


@router.post("/{entry_id}/actions", response_model=AuditActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> AuditActionResult:
    """对单条审计日志执行提交审核、归档日志、忽略日志、恢复日志。

    非法状态跳转返回 ok=False 并说明当前状态；重复忽略幂等命中，不重复生效。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return AuditActionResult(ok=False, applied=False, message=message, snapshot=service.snapshot())
    # 以流水里最新一条的结构化标记为准，避免用文案猜测是否幂等命中
    latest = service.trail(entry_id)[-1] if service.trail(entry_id) else {}
    idempotent = bool(latest.get("idempotent"))
    return AuditActionResult(
        ok=True,
        applied=not idempotent,
        idempotent=idempotent,
        message=message,
        entry=entry,
        snapshot=service.snapshot(),
    )
