"""Prove 阶段自动归档报告 — src/core/report_generator.py（§59）

在 Prove 返回 VERIFIED 时生成一份 Markdown 项目报告，归档到 reports/report_<timestamp>.md。

报告内容：任务描述、子任务列表、关键操作、执行结果（裁决）、执行时间。
本模块仅依赖标准库。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "reports"


def generate_report(
    task_input: str,
    subtasks=None,
    tool_summary: str = "",
    verdict=None,
    duration: float = 0.0,
) -> str:
    """生成一份 Markdown 项目报告，返回报告文件路径。

    参数：
      task_input   任务描述（原始输入）
      subtasks     子任务列表（list[str]）或 None（未拆解）
      tool_summary 关键操作摘要（工具执行摘要 / 改动列表）
      verdict      执行结果：dict（含 verdict / reason 字段）或字符串
      duration     执行耗时（秒）
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"report_{ts}.md"

    if isinstance(verdict, dict):
        v = verdict.get("verdict", "UNVERIFIABLE")
        reason = verdict.get("reason", "")
    else:
        v = str(verdict or "UNVERIFIABLE")
        reason = ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 项目报告",
        "",
        f"- 生成时间：{now_str}",
        f"- 执行耗时：{duration:.1f} 秒",
        "",
        "## 任务描述",
        "",
        str(task_input or "(无)"),
        "",
        "## 子任务列表",
        "",
    ]
    if isinstance(subtasks, list) and subtasks:
        for i, s in enumerate(subtasks, 1):
            lines.append(f"{i}. {s}")
    else:
        lines.append("（无拆解，单任务直接执行）")
    lines += ["", "## 关键操作", ""]
    lines.append(str(tool_summary or "（无工具调用）"))
    lines += ["", "## 执行结果", ""]
    lines.append(f"- 裁决：{v}")
    if reason:
        lines.append(f"- 理由：{reason}")
    lines += ["", "## 执行时间", ""]
    lines.append(f"- 报告归档时间：{now_str}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
