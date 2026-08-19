"""子任务调度器 — src/core/orchestrator.py（§54 / §55）

负责子任务调度与「子终端」管理：为每个子任务启动一个独立的子终端（线程级并行），
收集全部执行结果后按原顺序返回，交给主终端做结果合并与统一 Prove。

并行策略：ThreadPoolExecutor 线程级并行（每个子任务独立模型实例 + 独立工作记忆，
IO 密集的模型请求天然并行；避免 Windows 下 multiprocessing spawn 的复杂度）。

§55 完成信号机制：
  - 每个子终端完成后，向共享状态池（线程安全 dict + Lock）发送「完成」信号；
  - 主终端在启动全部子终端后进入**等待状态**，定期轮询状态池；
  - 仅当所有子任务都已发送「完成」信号，主终端才结束等待、进入结果合并阶段。
"""

from __future__ import annotations

import threading
import time

from concurrent.futures import ThreadPoolExecutor

from src.core.subagent import run_subtask


def run_subtasks(
    subtasks: list[str],
    env_block: str = "",
    skill_context: str = "",
    max_workers: int | None = None,
    poll_interval: float = 0.2,
) -> list[dict]:
    """为每个子任务启动独立子终端并行执行（Think → Act，不 Prove）。

    §55：子终端完成后发送「完成」信号（共享状态池），主终端轮询等待，
    全部完成才进入合并。

    返回：按 subtasks 原顺序排列的结果 dict 列表（与 subagent.run_subtask 结构一致；
    异常子终端兜底为 success=False 的结果，不中断其他子终端）。
    """
    n = len(subtasks)
    if n <= 0:
        return []
    results: list[dict | None] = [None] * n
    workers = max_workers or min(n, 4)  # 默认最多 4 个并行子终端

    # ── §55 完成信号机制：共享状态池（线程安全）──
    # done_pool[i] == True 表示子终端 i 已完成并发出了「完成」信号。
    done_pool: dict[int, bool] = {i: False for i in range(n)}
    pool_lock = threading.Lock()

    def _mark_done(idx: int) -> None:
        """子终端完成 → 发送「完成」信号（更新共享状态池）。"""
        with pool_lock:
            done_pool[idx] = True

    def _all_done() -> bool:
        """主终端检查：所有子任务是否都已发送「完成」信号。"""
        with pool_lock:
            return all(done_pool.values())

    print(f"\n[拆解] 检测到 {n} 个独立子任务，启动 {min(n, workers)} 个子终端并行执行...")

    def _run(idx_sub: tuple[int, str]) -> dict:
        idx, sub = idx_sub
        print(f"  └ [子终端 {idx + 1}] 启动：{sub[:60]}")
        res = run_subtask(sub, idx, env_block=env_block, skill_context=skill_context)
        # §55：任务完成 → 发送「完成」信号
        _mark_done(idx)
        _flag = "成功" if res.get("success") else "失败"
        print(f"  └ [子终端 {idx + 1}] 完成（{_flag}）：{sub[:60]} → 已发送完成信号")
        return res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run, (i, s)): i for i, s in enumerate(subtasks)}

        # ── §55 主终端等待状态：定期轮询所有子任务的完成信号 ──
        print(f"[等待] 已启动全部 {n} 个子终端，主终端进入等待状态"
              f"（轮询完成信号，每 {poll_interval}s 检查一次）...")
        while not _all_done():
            time.sleep(poll_interval)
        print("[等待] 所有子终端已发送完成信号，结束等待，进入结果合并阶段。")

        # 全部完成后再取结果（futures 均已就绪）
        for fut, idx in futures.items():
            try:
                results[idx] = fut.result()
            except Exception as e:  # pragma: no cover - 兜底
                results[idx] = {
                    "index": idx,
                    "subtask": subtasks[idx],
                    "think": {},
                    "act": {},
                    "changes": [],
                    "tool_execution_summary": "",
                    "success": False,
                    "error": str(e),
                }

    # 按原顺序返回（并发完成顺序不定，这里重排）
    ordered = [r for r in results if r is not None]
    _ok = sum(1 for r in ordered if r.get("success"))
    print(f"[拆解] 全部子终端执行完毕：{_ok}/{n} 成功。")
    return ordered
