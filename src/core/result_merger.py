"""子任务结果合并 — src/core/result_merger.py（§54）

主终端收集所有子终端执行结果后，将其合并为一份统一的执行摘要与成功判定，
供最终 Prove（统一验证）使用。
"""

from __future__ import annotations


def merge(results: list[dict]) -> dict:
    """合并各子终端结果。

    返回：
      {
        "all_success": bool,        # 是否全部子任务成功
        "success_count": int,       # 成功子任务数
        "total": int,               # 子任务总数
        "summary": str,             # 合并后的执行摘要（供最终 Prove）
        "results": list[dict],      # 原始结果列表（透传）
      }
    """
    total = len(results)
    success_count = sum(1 for r in results if r.get("success"))
    lines: list[str] = []
    for r in results:
        idx = r.get("index", 0) + 1
        sub = r.get("subtask", "")
        changes = "; ".join(r.get("changes") or []) or "(无改动)"
        status = "成功" if r.get("success") else "失败"
        lines.append(f"[子任务 {idx}] {sub}\n  状态: {status}\n  结果: {changes}")
        _ts = r.get("tool_execution_summary")
        if _ts:
            lines.append(f"  工具摘要: {_ts[:400]}")
    summary = "\n".join(lines)
    return {
        "all_success": success_count == total and total > 0,
        "success_count": success_count,
        "total": total,
        "summary": summary,
        "results": results,
    }
