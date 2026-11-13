"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

AUDIT_MODULE = "audit"
AUDIT_TERMINAL_STATUSES = ("已归档", "已忽略")


class Store:
    def __init__(self) -> None:
        # 同一把可重入锁保护所有写动作，保证并发下状态流转与派生快照只生效一次
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._normalize_audit_seed()
        # 审计动作流水：每次请求（含被拒绝的非法跳转、幂等命中）都留一条，供追溯与复现
        self.audit_trail: list[dict[str, Any]] = []
        # 操作审计唯一事实来源：概览异常量、质量通知、日志详情都从这里取同一份结果
        self.audit_snapshot: dict[str, Any] = {}
        self.refresh_audit_snapshot()

    def _normalize_audit_seed(self) -> None:
        """把示例审计日志规整成与状态机一致的结构（影响行数转整数、补齐终态/流水字段）。"""
        for row in self.rows(AUDIT_MODULE):
            try:
                row["影响行数"] = int(str(row.get("影响行数") or 0).strip() or 0)
            except (TypeError, ValueError):
                row["影响行数"] = 0
            status = row.get("status")
            row["terminal"] = status in AUDIT_TERMINAL_STATUSES
            row.setdefault("history", [])
            row.setdefault("restored_from", None)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def append_audit_trail(self, record: dict[str, Any]) -> dict[str, Any]:
        self.audit_trail.append(record)
        return record

    def refresh_audit_snapshot(self) -> dict[str, Any]:
        """重算操作审计的派生结果。

        终态（已归档/已忽略）以条目首次进入终态时的状态为准，刷新后仍保持终态，
        不会因为列表重查或重复动作被改回待审核。
        """
        rows = self.rows(AUDIT_MODULE)
        pending_rows = [row for row in rows if row.get("pending")]
        abnormal_rows = [row for row in rows if row.get("abnormal")]
        ignored_rows = [row for row in rows if row.get("status") == "已忽略"]
        archived_rows = [row for row in rows if row.get("status") == "已归档"]
        affected_total = sum(int(row.get("影响行数") or 0) for row in rows)
        pending_affected = sum(int(row.get("影响行数") or 0) for row in pending_rows)
        snapshot = {
            "module": AUDIT_MODULE,
            "total": len(rows),
            "pending": len(pending_rows),
            "abnormal": len(abnormal_rows),
            "ignored": len(ignored_rows),
            "archived": len(archived_rows),
            "affected_rows": affected_total,
            "pending_affected_rows": pending_affected,
            # 质量通知与异常量同口径：待审核且仍被标记为异常的日志
            "notifications": [
                {
                    "id": row.get("id"),
                    "日志编号": row.get("日志编号"),
                    "操作类型": row.get("操作类型"),
                    "操作对象": row.get("操作对象"),
                    "状态": row.get("status"),
                    "影响行数": int(row.get("影响行数") or 0),
                }
                for row in abnormal_rows
                if row.get("status") == "待审核"
            ],
        }
        self.audit_snapshot = snapshot
        return snapshot

    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in self.module_names():
                rows = self.rows(name)
                if name == AUDIT_MODULE:
                    # 操作审计直接采用统一快照，跨模块看到的异常量与审计页一致
                    modules.append({
                        "name": name,
                        "created": len(rows),
                        "pending": self.audit_snapshot["pending"],
                        "abnormal": self.audit_snapshot["abnormal"],
                    })
                    continue
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            ]
            return {"cards": cards, "modules": modules}


store = Store()
