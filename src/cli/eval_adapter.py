#!/usr/bin/env python3
"""eval_adapter.py — Fable 5 系统的 evalkit Agent 接口适配器。

把 Fable 5（Think -> Act -> Prove 连续循环）封装成 evalkit 可调用的 Agent：

  输入协议（来自 evalkit / stdin）：
      {"input": "<任务描述>", ...}      # 也可兼容 {"task": "..."}
  输出协议（写入 stdout，仅此一行 JSON，便于 evalkit 解析）：
      {
        "status": "success" | "failure",
        "verdict": "VERIFIED" | "REFUTED" | "UNVERIFIABLE" | null,
        "tools_used": ["write_file", "read_file", "run_command"],
        "output_summary": "文件已创建、读取并删除",
        "error_message": null
      }

实现要点：
  - 复用 src.cli.main.run_turn 的核心执行逻辑（不进入交互式输入循环），
    把单条任务跑完整的一轮 Think->Act->Prove，再从会话记录中提取结构化结果。
  - run_turn 内部的大量「给人看」的打印（阶段标题、流式字段流、状态行等）会被
    重定向到内存缓冲区丢弃，保证 stdout 上只有本适配器输出的一行 JSON。
  - 任何未捕获异常都转换为 {"status":"failure", ...} 的 JSON，绝不抛出到 stdout，
    保证 evalkit 永远能解析到合法 JSON。
  - 检查点写入独立的 runs/eval_session.json，避免污染交互式会话的检查点。

用法：
  echo '{"input": "创建一个 test.txt 文件"}' | python src/cli/eval_adapter.py
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

# ── 路径：本文件位于 <root>/src/cli/eval_adapter.py，parents[2] 即项目根 ──
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 关键：eval 专用检查点，避免污染交互式会话的检查点（module 级 CKPT 在 run_turn 调用时读取）
import src.cli.main as _cli_main
_cli_main.CKPT = ROOT / "runs" / "eval_session.json"
_cli_main.CKPT_TMP = ROOT / "runs" / "eval_session.tmp.json"

from src.cli.main import run_turn  # noqa: E402  (run_turn 是核心执行逻辑)
from src.integrations.llm import RealModel, get_router  # noqa: E402
from src.integrations.tools import (  # noqa: E402
    get_env_snapshot,
    format_env_block,
    ensure_sandbox_root,
)


def _build_session() -> dict:
    """构造一次评估所需的会话与模型（与 main() 同款初始化，但跳过交互式循环）。"""
    get_router()  # 触发路由层初始化（打印到重定向缓冲区，无副作用）
    snap = get_env_snapshot()
    env_block = format_env_block(snap)
    ensure_sandbox_root()  # 确保用户数据目录 sandbox/ 受限工作区存在
    model = RealModel()
    model.env_block = env_block  # 注入跨平台命令映射，让模型按当前平台生成命令
    return {
        "version": 1,
        "conversations": [],
        "current": None,
        "current_stage": "idle",
        "timestamp": "",
        "_model": model,
    }


def _dedup_keep_order(items: list) -> list:
    seen = set()
    out = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _run_one(task: str) -> dict:
    """跑一条任务，返回适配器标准 JSON 结果字典。"""
    session = _build_session()
    # 把 run_turn 的所有人类向打印重定向到内存缓冲区，保证 stdout 干净
    captured = io.StringIO()
    with redirect_stdout(captured):
        # memory_store=None：评估不写入跨会话记忆层，避免污染
        run_turn(task, session, None)

    conv = (session.get("conversations") or [{}])[-1]
    verdict_obj = conv.get("verdict") or {}
    verdict = verdict_obj.get("verdict") if isinstance(verdict_obj, dict) else None

    act = conv.get("act") or {}
    tool_calls = act.get("tool_calls") or []
    tools_used = _dedup_keep_order(
        tc.get("name") for tc in tool_calls if isinstance(tc, dict) and tc.get("name")
    )

    prove = conv.get("prove") or {}
    observed = prove.get("observed") if isinstance(prove, dict) else ""
    changes = act.get("changes") or []
    tool_summary = act.get("tool_execution_summary") or ""

    summary_parts: list[str] = []
    if changes:
        summary_parts.append("；".join(str(c) for c in changes))
    if tool_summary:
        summary_parts.append(tool_summary)
    if observed:
        summary_parts.append(f"观察：{observed}")
    output_summary = "\n".join(summary_parts)
    if len(output_summary) > 3000:
        output_summary = output_summary[:2000] + "\n...[输出过长已截断]...\n" + output_summary[-800:]

    ok = verdict in ("VERIFIED", "REFUTED", "UNVERIFIABLE")
    return {
        "status": "success" if ok else "failure",
        "verdict": verdict,
        "tools_used": tools_used,
        "output_summary": output_summary,
        "error_message": None if ok else "未产生有效裁决（任务可能触发人工介入或中断）",
    }


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main() -> None:
    # ── 1) 读取 stdin 的 JSON 任务 ──
    try:
        raw = sys.stdin.read()
    except Exception as e:  # pragma: no cover
        _emit({"status": "failure", "verdict": None, "tools_used": [],
               "output_summary": "", "error_message": f"读取 stdin 失败：{e}"})
        return

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _emit({"status": "failure", "verdict": None, "tools_used": [],
               "output_summary": "", "error_message": f"stdin 不是合法 JSON：{e}"})
        return

    task = payload.get("input") or payload.get("task") or ""
    if not task:
        _emit({"status": "failure", "verdict": None, "tools_used": [],
               "output_summary": "", "error_message": "缺少 input 字段或为空"})
        return

    # ── 2) 运行 Fable 5 核心逻辑 ──
    try:
        result = _run_one(task)
    except Exception as e:
        # 任何未捕获异常都转为结构化失败 JSON，绝不让异常冒泡到 stdout
        result = {
            "status": "failure",
            "verdict": None,
            "tools_used": [],
            "output_summary": "",
            "error_message": f"{type(e).__name__}: {e}",
        }
        # 堆栈打到 stderr（不污染 stdout 的 JSON），便于本地调试
        print(traceback.format_exc(), file=sys.stderr)

    _emit(result)


if __name__ == "__main__":
    main()
