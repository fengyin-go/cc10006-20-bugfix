"""操作审计状态机验收测试（不依赖第三方库，直接驱动 service 层）。

覆盖：
- 忽略只能从待审核进入已忽略；重复忽略幂等只生效一次、异常量不增加；
- 恢复生成新的待审核记录而不是复用旧行，旧行保持已忽略终态；
- 概览异常量 / 质量通知 / 审计影响行数读取同一份快照；
- 非法状态跳转被拒绝并说明当前状态；
- 并发忽略只生效一次；
- 刷新后终态保持；状态与回执一致。
"""
from __future__ import annotations

import concurrent.futures
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store  # noqa: E402
from app.services.audit import AuditService, RESTORE_ACTION  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def reset_audit() -> None:
    rows = store.rows("audit")
    rows.clear()
    store.audit_trail.clear()
    # 造一条干净的待审核、带异常标记的权限变更审计
    rows.append({
        "id": 1,
        "status": "待审核",
        "pending": True,
        "abnormal": True,
        "terminal": False,
        "日志编号": "AUDI-PERM-0001",
        "操作类型": "权限变更",
        "操作对象": "角色 admin",
        "操作详情": "授予敏感表权限",
        "操作来源": "控制台",
        "执行时间": "2026-11-13",
        "影响行数": 5,
        "回滚能力": "支持",
        "history": [],
        "restored_from": None,
    })
    store.refresh_audit_snapshot()


svc = AuditService()

# 1. 初始：一条待审核真实异常
reset_audit()
snap = svc.snapshot()
check(snap["abnormal"] == 1, "初始异常量为 1（待审核真实异常）")
check(snap["pending"] == 1, "初始待处理为 1")
check(len(snap["notifications"]) == 1, "初始质量通知 1 条")
check(snap["affected_rows"] == 5, "初始影响行数 5")

# 2. 第一次忽略：待审核 -> 已忽略
entry, msg = svc.run_action(1, "忽略日志")
check(entry is not None and entry["status"] == "已忽略", "第一次忽略进入已忽略")
check(entry["pending"] is False and entry["abnormal"] is False, "忽略后清除待处理与异常标记")
check(entry["terminal"] is True, "忽略后标记为终态")
snap = svc.snapshot()
check(snap["abnormal"] == 0, "忽略后异常量降为 0（不再被当成异常）")
check(snap["pending"] == 0, "忽略后待处理为 0")
check(snap["ignored"] == 1, "已忽略计数为 1")
check(len(snap["notifications"]) == 0, "忽略后质量通知清空")

# 3. 第二次忽略：幂等，只返回已忽略结果，异常量不增加
before_abnormal = snap["abnormal"]
before_trail = len(svc.trail(1))
entry2, msg2 = svc.run_action(1, "忽略日志")
check(entry2 is not None and entry2["status"] == "已忽略", "重复忽略仍返回已忽略结果")
check("不重复生效" in msg2, "重复忽略回执说明幂等不重复生效")
snap2 = svc.snapshot()
check(snap2["abnormal"] == before_abnormal == 0, "重复忽略异常量不增加")
check(entry2["影响行数"] == 6, "重复忽略不重复累计影响行数")
last = svc.trail(1)[-1]
check(last["idempotent"] is True and last["applied"] is False, "重复忽略记录为幂等命中且未生效")

# 4. 忽略不生效时不得残留待审核：尝试在已忽略上做提交审核（非法跳转）
entry3, msg3 = svc.run_action(1, "提交审核")
check(entry3 is None, "已忽略日志上提交审核被拒绝")
check("已忽略" in msg3 and "不允许" in msg3, "拒绝信息说明当前状态不允许")
still = svc.get_entry(1)
check(still["status"] == "已忽略" and still["pending"] is False, "拒绝后不残留待审核，仍为已忽略终态")

# 5. 恢复：生成新的待审核记录，旧行保持已忽略
restored, rmsg = svc.run_action(1, RESTORE_ACTION)
check(restored is not None and restored["id"] != 1, "恢复生成新记录而不是复用旧行")
check(restored["status"] == "待审核" and restored["pending"] is True, "新记录为待审核")
check(restored["abnormal"] is True, "新待审核记录重新标记为真实异常")
check(restored["restored_from"] == 1, "新记录保留追溯来源")
old = svc.get_entry(1)
check(old["status"] == "已忽略" and old["terminal"] is True, "原日志保持已忽略终态未被改回待审核")
snap3 = svc.snapshot()
check(snap3["total"] == 2, "恢复后总记录数 +1")
check(snap3["abnormal"] == 1, "恢复后异常量回到 1（仅新待审核记录）")
check(snap3["pending"] == 1, "恢复后待处理为 1")
check(snap3["ignored"] == 1, "已忽略仍为 1（旧行）")
check(len(snap3["notifications"]) == 1 and snap3["notifications"][0]["id"] == restored["id"],
      "质量通知只含新的待审核异常，与异常量同口径")

# 6. 概览 / 质量通知 / 日志详情同一份结果
overview = store.overview()
audit_module = next(m for m in overview["modules"] if m["name"] == "audit")
check(audit_module["abnormal"] == snap3["abnormal"], "概览异常量与审计快照一致")
check(audit_module["pending"] == snap3["pending"], "概览待处理与审计快照一致")
notif = svc.snapshot()["notifications"]
check(notif[0]["id"] == restored["id"], "质量通知与日志详情指向同一条记录")
detail = svc.get_entry(restored["id"])
check(detail["status"] == notif[0]["状态"] == "待审核", "日志详情状态与通知状态吻合")

# 7. 刷新后终态保持（重新读取/重算快照不改终态）
store.refresh_audit_snapshot()
again = svc.get_entry(1)
check(again["status"] == "已忽略" and again["terminal"] is True, "刷新后已忽略终态保持")
check(svc.get_entry(restored["id"])["status"] == "待审核", "刷新后新记录仍为待审核")

# 8. 并发忽略只生效一次
reset_audit()
results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(svc.run_action, 1, "忽略日志") for _ in range(8)]
    for f in futures:
        results.append(f.result())
applied = [r for r in results if "不重复生效" not in r[1]]
check(len(applied) == 1, f"8 个并发忽略只生效 1 次（实际 {len(applied)}）")
target = svc.get_entry(1)
check(target["status"] == "已忽略", "并发后状态为已忽略")
check(svc.snapshot()["abnormal"] == 0, "并发忽略后异常量为 0")
applied_trails = [t for t in svc.trail(1) if t["action"] == "忽略日志" and t["applied"]]
check(len(applied_trails) == 1, "流水里只有 1 条生效的忽略记录")

# 9. 草稿不能直接忽略（忽略只能从待审核进入）
reset_audit()
draft = svc.get_entry(1)
draft["status"] = "草稿"
draft["pending"] = True
draft["abnormal"] = False
store.refresh_audit_snapshot()
blocked, bmsg = svc.run_action(1, "忽略日志")
check(blocked is None and "草稿" in bmsg, "草稿状态忽略被拒绝并说明当前状态")
check(svc.get_entry(1)["status"] == "草稿", "拒绝后仍停留在草稿")

# 10. 已归档不可忽略、不可恢复
reset_audit()
arc = svc.get_entry(1)
arc["status"] = "已归档"
arc["pending"] = False
arc["abnormal"] = False
arc["terminal"] = True
store.refresh_audit_snapshot()
b1, m1 = svc.run_action(1, "忽略日志")
b2, m2 = svc.run_action(1, RESTORE_ACTION)
check(b1 is None and "已归档" in m1, "已归档不可忽略")
check(b2 is None and "已归档" in m2, "已归档不可恢复")

# 11. 不存在的日志
none_entry, none_msg = svc.run_action(999, "忽略日志")
check(none_entry is None and "999" in none_msg, "不存在日志给出可读错误")

# 12. 非法动作
reset_audit()
bad, bad_msg = svc.run_action(1, "删除日志")
check(bad is None and "不属于" in bad_msg and "待审核" in bad_msg, "非法动作说明当前状态与可执行范围")

print()
if failures:
    print(f"共 {len(failures)} 项失败：")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("全部验收通过")
