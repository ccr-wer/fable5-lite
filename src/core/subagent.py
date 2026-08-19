"""子终端执行逻辑 — src/core/subagent.py（§54）

负责「一个子任务」的独立执行：Think → Act（**不执行 Prove**，Prove 由主终端
在结果合并后统一执行）。

设计要点：
  - 每个子终端使用**独立的 RealModel 实例**与独立工作记忆，避免并行线程间
    共享 working_memory 导致的竞态（RealModel() 构造无副作用，API Key 动态读取）。
  - 返回结构化结果（think / act / changes / tool_execution_summary / success），
    供 orchestrator 收集、result_merger 合并。
"""

from __future__ import annotations

from src.integrations.llm import RealModel


def run_subtask(
    subtask: str,
    index: int,
    env_block: str = "",
    skill_context: str = "",
) -> dict:
    """在独立子终端（独立模型实例 + 独立工作记忆）中执行一个子任务。

    参数：
      subtask        子任务自然语言描述
      index          子任务序号（从 0 开始，用于结果标记与打印）
      env_block      环境信息块（注入系统提示词，跨平台命令适配）
      skill_context  技能树匹配注入（可选）

    返回：
      {index, subtask, think, act, changes, tool_execution_summary, success, error?}
    """
    # 独立模型实例（线程安全：不共享主终端的工作记忆）
    model = RealModel()
    model.env_block = env_block or ""
    model.reset_working_memory()
    wm = model.working_memory

    try:
        # ── 子终端 Think ──
        think = model.think(subtask, working_memory=wm, skill_context=skill_context)
        complexity = think.get("complexity")
        decision = think.get("decision") or think.get("plan") or ""

        # ── 子终端 Act（不 Prove）──
        act = model.act(
            subtask,
            decision,
            complexity=complexity,
            working_memory=wm,
            skill_context=skill_context,
        )
        changes = act.get("changes") or []
        tool_summary = act.get("tool_execution_summary") or ""
        # 执行成功判据：有工具调用或实际改动（简单观测，Prove 由主终端统一裁决）
        success = bool(act.get("tool_calls")) or bool(changes)
        return {
            "index": index,
            "subtask": subtask,
            "think": think,
            "act": act,
            "changes": changes,
            "tool_execution_summary": tool_summary,
            "success": success,
        }
    except Exception as e:  # pragma: no cover - 兜底：子终端异常不影响其他子终端
        return {
            "index": index,
            "subtask": subtask,
            "think": {},
            "act": {},
            "changes": [],
            "tool_execution_summary": "",
            "success": False,
            "error": str(e),
        }
