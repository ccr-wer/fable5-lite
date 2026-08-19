"""Rubric 行为验证观测层 — src/integrations/rubric_guard.py（§53）

在 execute_tool 的每个工具调用前做 Rubric 风格检查。当前为「观测层」：
检查不通过只记录警告并打印 [Rubric] 结果，**不拦截执行**（拦截能力留待后续迭代）。

检查维度：
  - ToolCallAccuracy：工具名是否合法、必需参数是否齐全（与预期行为一致性）。
  - TraceQuality    ：工具调用顺序 / 冗余是否合理（基于本轮 trace 历史，
                      例如连续相同调用视为冗余）。

本模块仅依赖标准库，供 tools.execute_tool 调用（观测式，不改变执行流程）。
"""

from __future__ import annotations

# 合法工具集（与 tools.TOOLS 定义保持一致）
_VALID_TOOLS = {"read_file", "write_file", "run_command", "get_environment_info"}

# 各工具必需参数
_REQUIRED_ARGS = {
    "read_file": ["path"],
    "write_file": ["path", "content"],
    "run_command": ["command"],
    "get_environment_info": [],
}

# 本轮工具调用 trace（供 TraceQuality 检查）
_TRACE: list[dict] = []

# ToolCallAccuracy 累计统计（跨调用，输出「工具调用准确率：X%」）
_TOTAL_CHECKS = 0
_PASSED_CHECKS = 0

# 连续相同调用超过该次数 -> 冗余警告
_REDUNDANT_LIMIT = 3


def reset_trace() -> None:
    """清空 trace 与统计（每轮任务开始时调用可让准确率按单轮统计）。"""
    global _TRACE, _TOTAL_CHECKS, _PASSED_CHECKS
    _TRACE = []
    _TOTAL_CHECKS = 0
    _PASSED_CHECKS = 0


def _accuracy_update(ok: bool) -> None:
    global _TOTAL_CHECKS, _PASSED_CHECKS
    _TOTAL_CHECKS += 1
    if ok:
        _PASSED_CHECKS += 1


def _current_accuracy() -> float:
    return (_PASSED_CHECKS / _TOTAL_CHECKS * 100) if _TOTAL_CHECKS else 100.0


def check_tool_call(tool_name: str, arguments: dict) -> dict:
    """Rubric 行为验证（观测层，不拦截执行）。

    返回 ``{"accuracy": float, "quality": "ok"|"warning", "warnings": [str]}``，
    并在终端打印 [Rubric] 检查结果。
    """
    arguments = arguments or {}
    warnings: list[str] = []
    checks: list[bool] = []

    # ── ToolCallAccuracy：工具名合法 + 必需参数齐全 ──
    name_ok = tool_name in _VALID_TOOLS
    checks.append(name_ok)
    if not name_ok:
        warnings.append(f"未知工具名：{tool_name}")
    else:
        missing = [
            k for k in _REQUIRED_ARGS.get(tool_name, [])
            if not str(arguments.get(k, "")).strip()
        ]
        checks.append(not missing)
        if missing:
            warnings.append(
                f"缺少必需参数：{tool_name}({', '.join(missing)})"
            )

    # ── TraceQuality：顺序 / 冗余检查 ──
    quality = "ok"
    # 冗余：从 trace 尾部数连续相同调用次数
    streak = 1
    for t in reversed(_TRACE):
        if t.get("name") == tool_name and t.get("args") == arguments:
            streak += 1
        else:
            break
    if streak >= _REDUNDANT_LIMIT:
        warnings.append(
            f"检测到连续 {streak} 次相同调用（{tool_name}），可能存在冗余"
        )
        quality = "warning"
    _TRACE.append({"name": tool_name, "args": arguments})

    # 统计并输出（观测层：只提示，不拦截）
    for ok in checks:
        _accuracy_update(ok)
    accuracy = _current_accuracy()
    if warnings:
        print(f"[Rubric] 工具调用准确率：{accuracy:.0f}% | 追踪质量：警告（action=observe）")
        for w in warnings:
            print(f"[Rubric 警告] {w}")
    else:
        print(f"[Rubric] 工具调用准确率：{accuracy:.0f}% | 追踪质量：OK（action=observe）")

    return {"accuracy": accuracy, "quality": quality, "warnings": warnings}
