"""操作审计业务规则：状态流转、字段校验与筛选口径都收在这里。

状态机（只允许以下跳转，其余一律拒绝并说明当前状态）：
    草稿   --提交审核--> 待审核
    待审核 --归档日志--> 已归档
    待审核 --忽略日志--> 已忽略   （唯一入口，忽略只能从待审核进入已忽略）
    已忽略 --恢复日志--> 待审核   （生成一条新的待审核记录，原已忽略行保留为终态）

约束：
- 忽略是幂等动作：对已忽略的日志重复忽略只返回已忽略结果，不重复落流水、不增加异常量；
- 恢复必须生成新的可追溯记录，而不是把旧行改回待审核；
- 异常标记 abnormal 只跟随“待审核的真实异常”，忽略会清除异常标记，终态刷新后保持不变；
- 所有写动作在同一把锁内完成，并发忽略只生效一次。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import AUDIT_TERMINAL_STATUSES, store

MODULE = "audit"
REQUIRED_FIELDS = ["日志编号", "操作类型", "操作对象"]
STATUS_ORDER = ["草稿", "待审核", "已归档", "已忽略"]
ACTION_RULES = {"提交审核": "待审核", "归档日志": "已归档", "忽略日志": "已忽略"}
RESTORE_ACTION = "恢复日志"
# 每个动作允许的来源状态；不在表里的跳转一律拒绝
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "提交审核": {"草稿"},
    "归档日志": {"待审核"},
    "忽略日志": {"待审核"},
    RESTORE_ACTION: {"已忽略"},
}

# 各动作对“影响行数”的实际作用行数，供审计影响行数同步
ACTION_AFFECTED_ROWS = {
    "提交审核": 1,
    "归档日志": 1,
    "忽略日志": 1,
    RESTORE_ACTION: 2,  # 原已忽略行收口 + 新待审核行登记
}


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 其余展示字段原样保留，没有就给空串，避免前端列出现 None
            for field in ("操作详情", "操作来源", "执行时间", "回滚能力"):
                entry[field] = values.get(field, "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["影响行数"] = 0
            entry["terminal"] = False
            entry["history"] = [self._trace(entry["id"], "登记日志", "草稿", applied=True)]
            store.rows(MODULE).append(entry)
            store.refresh_audit_snapshot()
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            now = self._now()
            if entry is None:
                message = f"审计日志 {entry_id} 不存在或已归档"
                store.append_audit_trail(
                    self._trace(entry_id, action, "不存在", applied=False, message=message, at=now)
                )
                return None, message
            current = str(entry.get("status"))

            if action == "忽略日志" and current == "已忽略":
                # 幂等命中：重复忽略只返回已忽略结果，不重复生效、不增加异常量
                message = "审计日志已忽略，重复忽略不重复生效"
                store.append_audit_trail(
                    self._trace(entry_id, action, current, applied=False, idempotent=True,
                                message=message, at=now)
                )
                return entry, message

            if action not in ACTION_RULES and action != RESTORE_ACTION:
                message = f"动作「{action}」不属于操作审计可执行范围，当前状态为「{current}」，不允许执行"
                store.append_audit_trail(
                    self._trace(entry_id, action, current, applied=False, message=message, at=now)
                )
                return None, message

            allowed = ALLOWED_TRANSITIONS.get(action, set())
            if current not in allowed:
                # 非法状态跳转：拒绝并明确当前状态，不残留待审核、不改任何字段
                message = (
                    f"当前状态为「{current}」，不允许执行「{action}」；"
                    f"该动作仅支持从「{'、'.join(sorted(allowed))}」发起"
                )
                store.append_audit_trail(
                    self._trace(entry_id, action, current, applied=False, message=message, at=now)
                )
                return None, message

            if action == RESTORE_ACTION:
                return self._restore(entry, now)

            target = ACTION_RULES[action]
            entry["status"] = target
            entry["terminal"] = target in AUDIT_TERMINAL_STATUSES
            if target == "已忽略":
                # 忽略清除待处理与异常标记：质量看板据此把它与真实异常区分开
                entry["pending"] = False
                entry["abnormal"] = False
            elif target == "已归档":
                entry["pending"] = False
                entry["abnormal"] = False
            else:  # 待审核
                entry["pending"] = True
                entry["abnormal"] = True
            entry["影响行数"] = int(entry.get("影响行数") or 0) + ACTION_AFFECTED_ROWS.get(action, 1)
            entry.setdefault("history", []).append(
                self._trace(entry_id, action, target, applied=True, at=now)
            )
            store.append_audit_trail(
                self._trace(entry_id, action, target, applied=True, at=now,
                            affected_rows=ACTION_AFFECTED_ROWS.get(action, 1))
            )
            store.refresh_audit_snapshot()
            return entry, f"审计日志已{action}"

    def _restore(self, entry: dict[str, Any], now: str) -> tuple[dict[str, Any], str]:
        """恢复已忽略日志：旧行保持已忽略终态，另起一条新的待审核记录。"""
        source_id = int(entry["id"])
        # 旧行收口为终态，不允许被改回待审核
        entry["terminal"] = True
        entry["pending"] = False
        entry["abnormal"] = False
        entry.setdefault("history", []).append(
            self._trace(source_id, RESTORE_ACTION, "已忽略", applied=True,
                        message="原日志终态收口，已生成新的待审核记录", at=now)
        )

        new_id = store.next_id(MODULE)
        new_entry: dict[str, Any] = {"id": new_id}
        for field in ("日志编号", "操作类型", "操作对象", "操作详情", "操作来源", "回滚能力"):
            new_entry[field] = entry.get(field, "")
        # 新日志编号可区分，但保留与原日志的追溯关系
        new_entry["日志编号"] = f"{entry.get('日志编号', '')}-R{new_id}"
        new_entry["执行时间"] = now[:10]
        new_entry["status"] = "待审核"
        new_entry["pending"] = True
        new_entry["abnormal"] = True
        new_entry["terminal"] = False
        new_entry["影响行数"] = ACTION_AFFECTED_ROWS[RESTORE_ACTION]
        new_entry["restored_from"] = source_id
        new_entry["history"] = [
            self._trace(new_id, RESTORE_ACTION, "待审核", applied=True,
                        message=f"由已忽略日志 {source_id} 恢复生成的新待审核记录", at=now)
        ]
        store.rows(MODULE).append(new_entry)

        store.append_audit_trail(
            self._trace(source_id, RESTORE_ACTION, "已忽略", applied=True, at=now,
                        new_entry_id=new_id, affected_rows=ACTION_AFFECTED_ROWS[RESTORE_ACTION])
        )
        store.refresh_audit_snapshot()
        # 以最新有效动作结果为准：回执返回新的待审核记录
        return new_entry, f"审计日志已恢复，新待审核记录 {new_id} 已生成，原日志保留已忽略终态"

    def trail(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        if entry_id is None:
            return list(store.audit_trail)
        return [item for item in store.audit_trail if int(item.get("entry_id", 0)) == entry_id]

    def snapshot(self) -> dict[str, Any]:
        with store.lock:
            return dict(store.audit_snapshot)

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _trace(
        entry_id: int,
        action: str,
        status: str,
        *,
        applied: bool,
        message: str = "",
        idempotent: bool = False,
        at: str | None = None,
        new_entry_id: int | None = None,
        affected_rows: int = 0,
    ) -> dict[str, Any]:
        record = {
            "entry_id": entry_id,
            "action": action,
            "status": status,
            "applied": applied,
            "idempotent": idempotent,
            "message": message,
            "affected_rows": affected_rows,
            "at": at or AuditService._now(),
        }
        if new_entry_id is not None:
            record["new_entry_id"] = new_entry_id
        return record
