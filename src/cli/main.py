#!/usr/bin/env python3
"""fable5-lite 交互式终端 — Think -> Act -> Prove 连续对话。

入口：  python src/cli/main.py
依赖：  src.integrations.llm.RealModel（真实 V4 flash API，未配 key 优雅降级）
        src.core.validator.judge（规则版验证层）
        src.integrations.memory.AgentKnowledgeMemory（agent-knowledge 后端 / 本地 JSONL 回退的跨会话记忆层）

行为：
  1. 启动打印欢迎信息 + 当前状态（会话轮数 / 模型 / 检查点路径 / 记忆层路径）。
  2. 循环等待用户输入任务。
  3. 每次输入后：先检索相关历史记忆 -> 注入 Think 提示词 -> 依次 Think -> Act -> Prove。
  4. 每个阶段完成后打印阶段结果 + 当前状态（Think 阶段额外显示复杂度 complexity）。
  5. 输出最终裁决后，仅当裁决为 VERIFIED 才把本轮对话存入记忆层（REFUTED / UNVERIFIABLE 不入库），再等待下一次输入。
  6. Ctrl+C 退出，退出前保存当前会话状态到检查点（并保存未完成的对话到记忆层）。
  7. 重启若存在检查点，提示是否继续上一次会话。

退出方式：输入 exit / quit，或随时按 Ctrl+C（均会先保存检查点）。
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# §64：prompt_toolkit——交互终端下替换 input()，提供历史记录 / 自动补全 / 多行输入；
# 非 tty（管道 / CI / 测试）时由 _read_task_input() 回退内置 input()。
from prompt_toolkit import prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ── §60 启动时沙箱目录存在性检查 ──
# 必须在导入包内模块之前探测：tools 模块级 `SANDBOX_DIR = get_sandbox_dir()` 会在导入时
# 立即创建沙箱，若检查放在 main() 里将永远看到「已存在」，无法感知「沙箱缺失 → 本次
# 启动自动创建」的状态。沙箱路径固定为 <用户数据目录>/fable5/sandbox（不依赖工作目录）。
_SB_PATH = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "fable5" / "sandbox"
try:
    if not _SB_PATH.exists():
        _SB_PATH.mkdir(parents=True, exist_ok=True)
        print(f"沙箱目录已创建：{_SB_PATH}")
    # 已存在：跳过创建步骤，不输出任何信息（§60：不改动既有权限逻辑）
except Exception as _e:
    print(f"沙箱目录创建失败：{_SB_PATH}（{_e}）")

from src.core.validator.judge import judge
from src.core.orchestrator import run_subtasks  # §54：子任务调度（子终端并行）
from src.core.result_merger import merge as merge_subtask_results  # §54：子结果合并
from src.integrations.llm import RealModel, V4_MODEL, get_router, load_system_prompt, load_stage_prompt, reset_token_usage, get_token_usage
from src.integrations.skill_manager import get_skill_context, build_index  # §42：技能树索引与匹配
from src.integrations.memory import AgentKnowledgeMemory, clear_memory  # §46：工作空间切换时清理记忆
from src.integrations.tools import get_env_snapshot, format_env_block, ensure_sandbox_root, _SANDBOX, cleanup_sandbox, find_stray_residue_dirs, clean_stray_dirs, reset_task_sandbox_approval
import src.integrations.tools as tools  # 复用 SandboxResourceError / 工具层常量等（§53：StateProbe 已移除）
import src.integrations.workspace as workspace  # §46：/workspace 命令（工作空间切换）
from src.integrations.user_data import (
    ensure_user_data_dirs, load_config, save_config, get_config_file,
    get_user_data_dir, get_sandbox_dir, get_skills_dir,
    PATH_ALIASES, resolve_path_alias,
)  # §44：用户数据目录（配置/技能/记忆/沙箱）分离；§48：自然语言路径别名映射
from src.core.report_generator import generate_report  # §59：Prove 阶段自动归档报告

# ── 终端颜色（ANSI；非 tty / NO_COLOR 时自动关闭，避免重定向乱码）──
_NO_COLOR = (not sys.stdout.isatty()) or (os.environ.get("NO_COLOR") is not None)
C_RESET = "\033[0m" if not _NO_COLOR else ""
C_CYAN = "\033[36m" if not _NO_COLOR else ""
C_YELLOW = "\033[33m" if not _NO_COLOR else ""
C_MAGENTA = "\033[35m" if not _NO_COLOR else ""
C_GREEN = "\033[32m" if not _NO_COLOR else ""
C_RED = "\033[31m" if not _NO_COLOR else ""
C_DIM = "\033[2m" if not _NO_COLOR else ""
C_BOLD = "\033[1m" if not _NO_COLOR else ""

CKPT = ROOT / "runs" / "session.json"
CKPT_TMP = ROOT / "runs" / "session.tmp.json"

# 模块级会话状态，供 SIGINT handler 使用
_STATE: dict = {}


def _c(code: str, text: str) -> str:
    return f"{code}{text}{C_RESET}"


def _indent(text, prefix="    ") -> str:
    return "\n".join(prefix + line for line in str(text).splitlines())


def _verdict_color(v: str) -> str:
    return {"VERIFIED": C_GREEN, "REFUTED": C_RED, "UNVERIFIABLE": C_YELLOW}.get(v, C_DIM)


def _maybe_store_memory(store, user_input: str, plan: str, result: str, verdict_field) -> None:
    """按记忆存储策略写入一条记忆：仅当 verdict 为 "VERIFIED" 时入库。

    策略（§25）：REFUTED / UNVERIFIABLE（含迭代上限转人工介入）不存储，避免污染记忆层。
    verdict_field 可为字符串（"VERIFIED"）或含 "verdict" 键的 dict（与 conv["verdict"] 一致）；
    未传入 / 非 VERIFIED 时仅打印 dim 提示、不写入。
    """
    if store is None:
        return
    if isinstance(verdict_field, dict):
        v = verdict_field.get("verdict", "")
    else:
        v = str(verdict_field)
    if v != "VERIFIED":
        print(_c(C_DIM, f"[记忆] 未保存：本轮裁决为 {v}（仅 VERIFIED 入库）"))
        return
    store.add({
        "user_input": user_input,
        "plan": plan,
        "result": result,
        "verdict": v,
    })


# ── 用户介入（一）：模型输出异常检测 ──
# 模型层在解析失败时会优雅降级为占位 dict（plan/reasoning 等为空或取占位文本），
# 本函数据此还原「输出异常」语义（真实 API 未配置 key 时尤其常见）。
_MODEL_EMPTY = "(模型未返回内容)"


def _model_output_anomaly(stage: str, obj) -> bool:
    """检测模型输出是否异常（空输出 / 非有效 JSON / 缺少必要字段 / 无工具调用）。

    返回 True 表示异常，触发用户介入（一）：提示「模型输出异常，建议重新输入任务」并等待 y/n。
    """
    if not isinstance(obj, dict):
        return True
    if stage == "think":
        plan = obj.get("plan") or ""
        reasoning = obj.get("reasoning") or ""
        decision = obj.get("decision") or ""
        # reasoning 为空，且 plan 为空或降级占位 -> 视为异常
        if not reasoning and (not plan or plan == _MODEL_EMPTY
                              or (plan == decision and not plan.strip())):
            return True
    elif stage == "act":
        tc = obj.get("tool_calls") or []
        changes = obj.get("changes") or []
        intent = obj.get("intent_line") or ""
        # 无任何工具调用、无意图行、且无实际改动（或仅占位）-> 视为异常
        if not tc and not intent and (not changes or changes == [_MODEL_EMPTY]):
            return True
    return False


# ── 用户介入（二）：敏感数据检测 ──
# 覆盖常见密钥 / 凭证模式：sk- 开头、api_key / secret / password / token /
# access_key / private_key、AWS AKIA、Bearer 令牌等。
_SENSITIVE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{6,}|"
    r"api[_-]?key|secret|passwd|password|token|"
    r"AKIA[0-9A-Z]{16}|"
    r"Bearer\s+[A-Za-z0-9._\-]+|"
    r"access[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)


def _detect_sensitive(task: str) -> bool:
    """检测用户输入是否包含敏感数据模式。"""
    return bool(_SENSITIVE_RE.search(task or ""))


def _ask_yes_no(prompt: str) -> bool:
    """用户介入提示：请求 y/n 确认。

    非交互 / EOF / 中断时默认返回 False（安全默认：不继续）。
    """
    print(_c(C_YELLOW, prompt))
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _read_task_input() -> str:
    """§64：读取任务输入。

    - tty（真实终端）：用 prompt_toolkit 的 prompt()，带 FileHistory（.fable5_history）、
      历史自动补全、Ctrl+Z 挂起（enable_suspend）；Ctrl+C / Ctrl+D 抛 KeyboardInterrupt /
      EOFError 由上层统一处理（保存检查点后退出）。
    - 非 tty（管道 / CI / 测试）：回退内置 input()，保证现有管道/黄金测试可用。
    - 多行输入：行末以 `\\` 结尾时继续等待下一行（去掉结尾 `\\` 后拼接，行间换行）。
    """
    parts: list[str] = []
    while True:
        if sys.stdin.isatty():
            line = prompt(
                ANSI(_c(C_CYAN, ">>> ")),
                history=FileHistory(str(ROOT / ".fable5_history")),
                auto_suggest=AutoSuggestFromHistory(),
                enable_suspend=True,
            )
        else:
            line = input(">>> ")
        if line.endswith("\\"):
            parts.append(line[:-1])
            continue
        parts.append(line)
        return "\n".join(parts)


# ── 用户介入（三）：验证层连续 REFUTED 跟踪 ──
def _update_refute_streak(session: dict, task: str, verdict) -> None:
    """跟踪同一任务的连续 REFUTED 次数与失败原因（用户介入三）。

    - VERIFIED -> 重置连败计数（任务最终通过）。
    - REFUTED  -> 同任务累加计数并记录原因；换任务则重置为新的起点。
    - 其他（UNVERIFIABLE / 迭代上限等）-> 不计入连败。
    是否「连续 3 次且原因各异」由调用方在更新后判断（见 run_turn）。
    """
    streak = session.setdefault("_refute_streak", {"task": None, "count": 0, "reasons": []})
    v = verdict.get("verdict") if isinstance(verdict, dict) else str(verdict)
    if v == "VERIFIED":
        streak["task"] = None
        streak["count"] = 0
        streak["reasons"] = []
        return
    if v != "REFUTED":
        return
    reason = (verdict.get("reason") if isinstance(verdict, dict) else "") or ""
    if streak.get("task") == task:
        streak["count"] += 1
        streak["reasons"].append(reason)
    else:
        streak["task"] = task
        streak["count"] = 1
        streak["reasons"] = [reason]


def save_checkpoint(session: dict) -> None:
    """原子写检查点：剔除不可序列化的 _model，先写 .tmp.json 再 rename 覆盖。"""
    data = {k: v for k, v in session.items() if k != "_model"}
    data["timestamp"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(CKPT.parent, exist_ok=True)
    with open(CKPT_TMP, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(CKPT_TMP, CKPT)


def load_checkpoint() -> dict | None:
    if not CKPT.exists():
        return None
    try:
        with open(CKPT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def prompt_resume() -> dict | None:
    """检测未完成的会话检查点，返回要恢复的 dict，或 None（不恢复）。"""
    data = load_checkpoint()
    if not isinstance(data, dict):
        return None
    if not data.get("conversations") and not data.get("current"):
        return None
    n = len(data.get("conversations", []))
    cur = data.get("current")
    tail = f"，当前轮进行到阶段 [{data.get('current_stage', '?')}]" if cur else ""
    print()
    print(_c(C_YELLOW, f"检测到上一次的会话检查点（已完成 {n} 轮{tail}）。"))
    print("  [y] 继续上一次会话")
    print("  [n] 丢弃并开启新会话")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans in ("y", "yes"):
        return data
    try:
        CKPT.unlink()
    except Exception:
        pass
    return None


def _run_api_key_wizard() -> None:
    """首次启动配置向导（§44）。

    若 <user_data>/config/config.yaml 缺失 api_key 字段，则在终端提示用户输入
    DeepSeek API Key：
      - 输入后写入 config.yaml 并打印确认信息；
      - 若用户未输入（空行 / EOF），打印「API Key 是运行必需项，请重新启动并输入」并退出。
    """
    cfg = load_config()
    if cfg.get("api_key"):
        return
    print()
    print("首次启动配置向导")
    print("=================")
    print("请输入你的 DeepSeek API Key:")
    try:
        key = input().strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        print("API Key 是运行必需项，请重新启动并输入")
        sys.exit(0)
    cfg["api_key"] = key
    try:
        save_config(cfg)
    except Exception as e:
        # §49：保存失败给出明确错误（路径 + 原因），而非静默或 traceback 崩溃
        print(f"[配置] 保存失败：{e}")
        print(f"API Key 未持久化，请检查 {get_config_file()} 所在目录权限后重新启动并输入。")
        sys.exit(1)
    print(f"已保存 API Key 到 {get_config_file()}")


def _maybe_pause() -> None:
    """可选：每个阶段后暂停若干秒，便于测试 Ctrl+C 中断恢复。"""
    try:
        secs = float(os.environ.get("FABLE_DEMO_PAUSE", "0") or "0")
    except ValueError:
        secs = 0.0
    if secs > 0:
        print(_c(C_DIM, f"[pause] 模拟耗时 {secs}s（便于测试 Ctrl+C 中断）..."))
        time.sleep(secs)


def _sigint_handler(signum, frame) -> None:
    """Ctrl+C：保存当前会话检查点后退出。"""
    print("\n" + _c(C_RED, "[!] 捕获到 Ctrl+C，正在保存会话检查点后退出..."))
    try:
        save_checkpoint(_STATE)
        print(_c(C_RED, f"[!] 检查点已保存到 {CKPT}"))
    except Exception as e:
        print(_c(C_RED, f"[!] 保存检查点失败：{e}"))
    sys.exit(130)


def print_status(session: dict, stage: str) -> None:
    n = len(session.get("conversations", []))
    print(_c(C_DIM, f"[状态] 已完成轮数: {n} | 当前阶段: {stage} | 检查点: {CKPT.name}"))


def _is_done(think: dict, act: dict) -> bool:
    """判断模型是否声明任务已完成（提前结束迭代循环）。"""
    if isinstance(think, dict):
        if think.get("done") is True:
            return True
        if think.get("classification") == "done":
            return True
    return False


# ── 过度思考控制（§33 任务三）：简单直接任务跳过链式思考，直接执行 ──
# 命中以下关键词即视为「简单直接操作」，无需模型链式思考：
#   文件操作：创建 / 写入 / 读取
#   目录操作：创建目录 / 列出文件
#   简单命令：运行 / 执行
_SIMPLE_TASK_RE = re.compile(
    r"(创建|写入|读取|列出文件|创建目录|运行|执行)", re.IGNORECASE,
)


def _is_simple_direct_task(task: str) -> bool:
    """判断任务是否为「简单直接操作」（文件 / 目录 / 命令），可跳过链式思考。"""
    return bool(_SIMPLE_TASK_RE.search(task or ""))


def _may_decompose(task: str) -> bool:
    """§54：启发式预筛——任务是否可能可拆解（含「多独立目标」信号）。

    命中才调用真实模型 think 判断拆解（subtasks）；未命中则保持 §33 的过度思考
    控制（简单单目标任务直接执行、不调模型）。信号：
      1) ≥2 个带扩展名的文件/对象（如 a.txt、b.txt、c.txt）；
      2) ≥2 个「创建/生成/新建 X」等独立操作短语；
      3) 数量词（N 个）+ 中文多目标分隔符（、/，/和）。
    """
    t = task or ""
    if len(re.findall(r"[A-Za-z0-9_\-]+\.\w{1,6}", t)) >= 2:
        return True
    if len(re.findall(r"(?:创建|生成|新建|处理|写入)[^。；;\n]*", t)) >= 2:
        return True
    if re.search(r"(?:[一二三四五六七八九十两]|\d+)\s*个", t) and re.search(r"[、，,和]", t):
        return True
    return False


def _think_phase(task: str, model, memory_context: str, wm, skill_context: str = "") -> dict:
    """Think 阶段入口（§33 任务三：过度思考控制的判断逻辑放在此处开头）。

    若任务是简单直接操作（创建/写入/读取文件、创建目录、列出文件、运行/执行命令），
    则跳过模型链式思考，直接构造一个极简 think 结果进入 Act（一次直接执行即进入验证）；
    否则调用模型真实 think，启动完整链式思考。
    """
    # §88：调试日志——打印系统提示词长度，确认提示词完整加载
    try:
        print(_c(C_DIM, f"[DEBUG] 系统提示词长度: {len(load_system_prompt())} 字符"))
    except Exception as e:
        print(_c(C_DIM, f"[DEBUG] 系统提示词读取失败: {e}"))
    if _is_simple_direct_task(task):
        print(_c(C_DIM, "[过度思考控制] 检测到简单直接任务，跳过链式思考，直接进入执行。"))
        return {
            "classification": "direct",
            "decision": task,
            "plan": "",
            "reasoning": "(已跳过链式思考：任务为简单直接操作，直接执行)",
            "done": False,
            "complexity": "simple",
            "definition_of_done": "",
            "evidence": [],
            "scope": [],
            "_skipped_thinking": True,
        }
    return model.think(task, memory_context=memory_context, working_memory=wm,
                      skill_context=skill_context)


def _prompt_stray_cleanup() -> None:
    """§57：任务完成后检查工作空间外的残留目录，提示用户确认后清理。

    残留候选见 tools.find_stray_residue_dirs（桌面 / 用户指定路径下的
    .knowledge、fable5-demo、fable5-test 等，工作空间 / 项目根 / 用户数据目录内的排除）。
    用户输入 y 清理，n 保留；非交互（EOF）默认保留。
    """
    try:
        stray = find_stray_residue_dirs()
    except Exception:
        return
    if not stray:
        return
    print(_c(C_YELLOW, f"\n[清理] 发现 {len(stray)} 个工作空间外的残留目录："))
    for p in stray:
        print(f"  · {p}")
    if _ask_yes_no("是否清理这些残留目录？(y/n): "):
        res = clean_stray_dirs(stray)
        for r in res.get("removed", []):
            print(_c(C_GREEN, f"[清理] 已清理残留目录：{r}"))
        for f in res.get("failed", []):
            print(_c(C_RED, f"[清理] 清理失败：{f}"))
    else:
        print(_c(C_DIM, "[清理] 用户选择保留残留目录。"))


def _generate_report_and_archive(task, think, act, verdict, duration, memory_store) -> None:
    """§59：Prove 返回 VERIFIED 时生成项目报告并归档到记忆层。

    报告写入 reports/report_<timestamp>.md；同时把报告路径 + 内容预览作为一条
    记忆写入记忆层（memory_store.add），便于跨会话召回。
    """
    if not (isinstance(verdict, dict) and verdict.get("verdict") == "VERIFIED"):
        return
    _subs = think.get("subtasks") if isinstance(think, dict) else None
    _ts = ""
    if isinstance(act, dict):
        _ts = act.get("tool_execution_summary") or "; ".join(act.get("changes") or [])
    try:
        _rp = generate_report(task, subtasks=_subs, tool_summary=_ts,
                              verdict=verdict, duration=duration)
        print(_c(C_GREEN, f"📄 项目报告已生成：{_rp}"))
        if memory_store is not None:
            _text = Path(_rp).read_text(encoding="utf-8")
            memory_store.add({
                "user_input": task,
                "plan": "[自动归档] 项目报告",
                "result": f"报告路径：{_rp}\n{_text[:500]}",
                "verdict": "VERIFIED",
            })
    except Exception as e:
        print(_c(C_DIM, f"[报告] 生成失败：{e}"))


def _report_token_usage(task: str, wm=None) -> None:
    """§61/§65：任务完成后显示 token 用量统计（按缓存命中拆分输入），并记录到日志。

    统计来自 llm 模块的当前任务累计（call_llm 每次响应 usage）；同时记入工作记忆。
    §65 格式：输入（命中缓存）/ 输入（未命中缓存）/ 输出 / 总计 + 缓存命中率（token 口径）。
    """
    u = get_token_usage()
    if u["total_tokens"] <= 0:
        return  # 无 API 调用（未配置 Key 等），跳过
    print("\n📊 Token 用量统计")
    print(f"• 输入（命中缓存）: {u['prompt_cache_hit']:,}")
    print(f"• 输入（未命中缓存）: {u['prompt_cache_miss']:,}")
    print(f"• 输出: {u['completion_tokens']:,}")
    print(f"• 总计: {u['total_tokens']:,}")
    print(f"💾 缓存命中率: {u['hit_rate']:.1f}%")
    # §61/§65：记录到工作记忆（completed_actions 摘要）
    if wm is not None:
        try:
            wm.record_action(
                f"Token 用量：输入(命中缓存) {u['prompt_cache_hit']} / "
                f"输入(未命中缓存) {u['prompt_cache_miss']} / 输出 {u['completion_tokens']} / "
                f"总计 {u['total_tokens']} | 缓存命中率 {u['hit_rate']:.1f}%",
                success=True,
            )
        except Exception:
            pass
    # §61/§65：记录到 logs/token_usage.log（追加：时间 | 任务 | 输入(命中) | 输入(未命中) | 输出 | 总计 | 命中率）
    try:
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_dir / "token_usage.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} | 任务: {task} | 输入(命中缓存): {u['prompt_cache_hit']} | "
                    f"输入(未命中缓存): {u['prompt_cache_miss']} | 输出: {u['completion_tokens']} | "
                    f"总计: {u['total_tokens']} | 命中率: {u['hit_rate']:.1f}%\n")
    except Exception as e:
        print(_c(C_DIM, f"[token] 日志写入失败：{e}"))


def _run_parallel_decompose(task, think, subtasks, session, model, memory_store, skill_ctx) -> None:
    """§54：任务拆解并行路径。

    Think 判定任务可拆解（subtasks > 1）时：
      1. orchestrator 为每个子任务启动独立子终端（线程级并行，Think → Act，不 Prove）；
      2. result_merger 合并全部子终端结果；
      3. 主终端执行最终 Prove（统一验证）：所有子任务成功 → VERIFIED，否则 → REFUTED；
      4. 记忆存储（仅 VERIFIED 入库）与沙箱清理（复用单任务策略）。
    """
    conv = session.get("current") or {}
    _t0 = time.time()  # §59：报告执行耗时计时

    # ── 1) 子任务并行执行（orchestrator 管理子终端）──
    results = run_subtasks(subtasks, env_block=model.env_block, skill_context=skill_ctx)

    # ── 2) 结果合并 ──
    merged = merge_subtask_results(results)

    # ── 3) 主终端最终 Prove（统一验证）──
    # §56：接入 judge 做语义匹配 + 子任务一致性 + 依赖校验；全部子任务成功
    # 且 judge 未判 REFUTED 才 VERIFIED，否则 REFUTED。
    prove = model.prove(task, merged["summary"], complexity=think.get("complexity"))
    _obs = prove.get("observed", "") if isinstance(prove, dict) else str(prove)
    _jv = judge(task, merged["summary"], _obs, tool_evidence={"subtasks": results})
    if merged["all_success"] and _jv.get("verdict") != "REFUTED":
        verdict = {
            "verdict": "VERIFIED",
            "reason": f"全部 {merged['total']} 个子任务执行成功（{merged['success_count']}/{merged['total']}）",
            "suggestions": "",
        }
    else:
        _r = (_jv.get("reason") if _jv.get("verdict") == "REFUTED"
              else f"{merged['total'] - merged['success_count']} 个子任务执行失败"
                   f"（成功 {merged['success_count']}/{merged['total']}）")
        verdict = {
            "verdict": "REFUTED",
            "reason": _r,
            "suggestions": "请检查失败子任务，或核对子任务结果与任务目标的一致性（路径 / 依赖关系）。",
        }

    conv["think"] = think
    conv["act"] = {
        "changes": [merged["summary"]],
        "tool_execution_summary": merged["summary"],
    }
    conv["prove"] = prove
    conv["verdict"] = verdict
    session["current_stage"] = "prove"
    save_checkpoint(session)

    # 打印统一验证结果
    print("\n" + "=" * 60)
    print(_c(C_MAGENTA, "PROVE  -  统一验证与裁决（子任务并行完成）"))
    print("=" * 60)
    print(f"  子任务: {merged['total']} 个"
          f"（成功 {merged['success_count']} / 失败 {merged['total'] - merged['success_count']}）")
    _obs = prove.get("observed", "(无)") if isinstance(prove, dict) else prove
    print(f"  观察(observed): {_obs}")
    print(_c(_verdict_color(verdict["verdict"]), f"  裁决: {verdict['verdict']}"))
    print(f"  理由: {verdict['reason']}")
    if verdict["verdict"] == "REFUTED":
        print(f"  建议: {verdict['suggestions']}")
    print_status(session, "PROVE")

    # §59：VERIFIED 时自动生成项目报告并归档到记忆层（并行拆解场景）
    _generate_report_and_archive(task, think, {"tool_execution_summary": merged["summary"]},
                                 verdict, time.time() - _t0, memory_store)
    # §61：任务完成后显示 token 用量统计并记录到 logs/token_usage.log（并行拆解场景）
    _report_token_usage(task)

    # ── 记忆存储（复用单任务策略：仅 VERIFIED 入库）──
    if memory_store is not None:
        try:
            _maybe_store_memory(
                memory_store,
                user_input=task,
                plan=(think.get("decision", "") if isinstance(think, dict) else ""),
                result=merged["summary"],
                verdict_field=verdict,
            )
        except Exception as e:
            print(_c(C_DIM, f"[记忆] 存储失败：{e}"))

    # ── 主动清理副作用 ──
    try:
        _clean = cleanup_sandbox()
        _removed = _clean.get("removed") or []
        if _removed:
            _names = ", ".join(os.path.basename(p) for p in _removed)
            print(_c(C_DIM, f"[清理] 已清理 {len(_removed)} 项沙箱副作用：{_names}"))
    except Exception:
        pass

    # §57：任务完成后检查工作空间外的残留目录，提示用户确认后清理
    _prompt_stray_cleanup()


def run_turn(task: str, session: dict, memory_store=None) -> None:
    """执行（或续跑）一轮动态链式思考：think -> act 迭代循环，最后 Prove。

    防漏洞机制（详见 DEVELOPMENT_LOG §19 / §31 / §32 / §33）：
      - Issue 1 状态不一致：共享 working_memory 在 think/act 间传递，act 后写回。
      - Issue 3 上下文膨胀：think 只读取 working_memory 的【摘要】，不读完整工具输出。
      - Issue 6 模型循环：已移除固定迭代上限（MAX_ITER，§31），并进一步把 §29 独立的
        「工作目录一致性检查」「连续 run_command 失败检查」两个早期介入也并入统一的
        「进展判断」机制（§32）——循环中唯一的过程内终止条件即「连续 2 轮
        completed_actions 无新增」→ 标记「需要人工介入」并终止。失败动作不计入
        completed_actions，故 cwd 错配 / run_command 连败导致的空转会自然被该机制捕获。
      - §33 过度思考控制：简单直接任务（创建/写入/读取文件、创建目录、列出文件、运行/执行
        命令）经 think() 入口跳过链式思考，只执行一轮即进入验证，避免对 trivial 任务过度规划。
      - §33 主动清理副作用：本轮结束前调用 cleanup_sandbox() 清理用户数据目录 sandbox/ 内的 `-` 开头目录、
        *.tmp/*.log 临时文件与空目录（保留 hello-sandbox/test-project），记录到 logs/cleanup.log。
    """
    model = session.get("_model")
    conv = session.get("current")
    if conv is None:
        conv = {"user_input": task}
        session["current"] = conv
    else:
        # 续跑：沿用进行中轮的原始任务
        task = conv.get("user_input", task)

    _t0 = time.time()  # §59：报告执行耗时计时
    reset_token_usage()  # §61：本轮任务 token 用量统计归零
    reset_task_sandbox_approval()  # §85：本轮任务沙箱外操作审批状态归零（每个任务只提醒一次）

    # ── 用户介入（三）：验证层连续 REFUTED 终止守卫 ──
    # 同一任务已连续 3 次 REFUTED 且每次失败原因不同 -> 直接终止，不再执行本轮。
    _streak = session.get("_refute_streak")
    if (_streak and _streak.get("task") == task
            and _streak.get("count", 0) >= 3
            and len(set(_streak.get("reasons", []))) >= 3):
        print(_c(C_RED, "[用户介入] 验证层连续失败，建议检查任务描述或手动介入。"
                      "任务已终止，请修改任务描述或手动介入后再试。"))
        return

    # ── 用户介入（二）：敏感数据检测 ──
    if _detect_sensitive(task):
        print(_c(C_RED, "[用户介入] 检测到敏感信息，请确认是否继续。"))
        if not _ask_yes_no("是否仍要继续执行该任务？(y/n): "):
            print(_c(C_DIM, "[用户介入] 已取消：用户拒绝在包含敏感信息的任务上继续。"))
            return
        print(_c(C_DIM, "[用户介入] 用户确认继续（敏感信息已进入任务上下文，请注意泄露风险）。"))

    # ── 记忆检索（循环开始前做一次）──
    mem_ctx = ""
    if memory_store is not None:
        mem_results = memory_store.search(task)  # list[dict]，最多 3 条
        if mem_results:
            print(_c(C_DIM, f"\n[记忆] 检索到 {len(mem_results)} 条相关记忆"))
            for _m in mem_results:
                _snip = _m.get("user_input") or _m.get("plan") or ""
                _snip = _snip if len(_snip) <= 120 else _snip[:117] + "..."
                print(_c(C_DIM, f"  · {_snip}"))
            mem_ctx = "\n".join(
                f"[历史记忆 {i + 1}] 任务：{m.get('user_input', '')}；"
                f"方案：{m.get('plan', '')}；结果：{m.get('result', '')}；"
                f"裁决：{m.get('verdict', '')}"
                for i, m in enumerate(mem_results)
            )
        else:
            print(_c(C_DIM, "\n[记忆] 未检索到相关历史记忆"))

    # ── 动态链式思考：重置工作记忆，进入 think -> act 迭代循环 ──
    model.reset_working_memory()
    wm = model.working_memory
    iteration = 0
    last_think = last_act = None
    status = "normal"

    # ── 过度思考控制（§33 任务三）：简单直接任务标记，循环内只执行一轮即进入验证 ──
    _direct_mode = _is_simple_direct_task(task)

    # ── §32 进展判断（统一终止条件）：以下早期介入（§29 的工作目录一致性检查 /
    # 连续 run_command 失败检查）已合并入「无进展检测」（§31）。循环中不再有独立的
    # cwd 一致性 / 连续失败检查，唯一的过程内终止条件即「连续 2 轮 completed_actions 无新增」。

    # ── 用户介入（四）：沙箱资源不足终止标志（run_command/write_file 触发资源异常时置位）──
    _sandbox_exhausted = False

    # ── §31 无进展检测：移除固定迭代上限，改以 completed_actions 是否新增作为终止条件 ──
    # 每轮 act 后比较 len(wm.completed_actions) 与上一轮基线；completed_actions 仅收录
    # success=True 的动作（见 WorkingMemory.record_action），故「无新增」即代表「本轮无进展」。
    # 连续 NO_PROGRESS_LIMIT 轮无新增 -> 标记「需要人工介入」并终止。
    NO_PROGRESS_LIMIT = 2
    _prev_completed = len(wm.completed_actions)  # 循环前基线（reset 后应为 0）
    _no_progress_streak = 0

    # ── §42 技能树匹配：根据用户任务检索相关技能分类，注入 Think/Act 系统提示词 ──
    skill_ctx, skill_matched = get_skill_context(task, top_k=3)
    if skill_matched:
        _idx = build_index()
        _names = [_idx.get(c, {}).get("name", c) for c, _ in skill_matched]
        print(_c(C_DIM, f"\n[技能] 根据任务匹配到 {len(skill_matched)} 个相关分类："
                      + "、".join(_names)))
        if skill_ctx:
            # 预览注入块前 240 字符，便于验证「系统提示词已含技能树内容」
            _preview = skill_ctx.replace("\n", " ")[:240]
            print(_c(C_DIM, f"[技能] 注入预览：{_preview}"))
    else:
        print(_c(C_DIM, "\n[技能] 未匹配到相关技能分类，跳过技能注入。"))

    # ── §54 任务拆解预判：仅当任务含「多独立目标」信号时才调用真实模型 think 判断拆解 ──
    # 保留 §33 对纯单目标简单任务的过度思考控制（直接执行、不调模型）；
    # 预判的 think 结果会被循环第一轮复用（_first_think），命中拆解则并行执行后返回。
    _first_think = None
    if _may_decompose(task):
        _first_think = model.think(task, memory_context=mem_ctx, working_memory=wm,
                                   skill_context=skill_ctx)
        _subs = _first_think.get("subtasks")
        if isinstance(_subs, list) and len(_subs) > 1:
            _run_parallel_decompose(task, _first_think, _subs, session, model, memory_store, skill_ctx)
            return

    while True:
        iteration += 1
        wm.iteration_count = iteration
        wm.current_step = iteration
        print("\n" + "=" * 60)
        print(_c(C_BOLD + C_CYAN, f"迭代 #{iteration}  -  Think -> Act"))
        print("=" * 60)

        # ── THINK ──
        # §33 任务三：过度思考控制 —— 简单直接任务经 _think_phase() 入口跳过链式思考，直接执行。
        # §54：循环第一轮复用拆解预判的 think 结果（_first_think），避免重复调用模型。
        if _first_think is not None:
            think = _first_think
            _first_think = None
        else:
            think = _think_phase(task, model, mem_ctx, wm, skill_context=skill_ctx)
        wm.prev_plan = think.get("plan", "")  # 记录本轮计划，供下一轮「计划须不同」约束
        # ── 用户介入（一）：模型输出异常 ──
        if _model_output_anomaly("think", think):
            print(_c(C_RED, "[用户介入] 模型输出异常（空输出 / 非有效 JSON / 缺少必要字段），建议重新输入任务。"))
            if not _ask_yes_no("是否仍要继续本次循环？(y/n): "):
                print(_c(C_DIM, "[用户介入] 已终止本轮：模型输出异常且用户选择不继续。"))
                return
            print(_c(C_DIM, "[用户介入] 用户选择继续（将使用降级输出）。"))
        if think.get("_skipped_thinking"):
            # 简单直接任务：跳过完整思考链展示，仅提示已直接执行
            print(_c(C_DIM, "THINK  -  [已跳过链式思考] 简单直接任务，直接进入执行。"))
        else:
            print(_c(C_CYAN, "THINK  -  计划与完成标准"))
            print(f"  分类:      {think.get('classification', '(无)')}")
            # ── 思考链：先 reasoning（思考过程），再 plan（最终计划）──
            print(_c(C_BOLD, "--- 思考链 ---"))
            print(_indent(think.get('reasoning', '(无)') or '(无)'))
            print(_c(C_BOLD, "--- 计划 ---"))
            print(_indent(think.get('plan', '(无)') or '(无)'))
            print(f"  完成标准:  {think.get('definition_of_done', '(无)')}")
            print(f"  复杂度(complexity): {think.get('complexity', '(未返回)')}")
            print(f"  是否完成(done): {think.get('done', False)}")
        print_status(session, f"think(#{iteration})")

        # ── ACT ──
        complexity = think.get("complexity") if isinstance(think, dict) else None
        try:
            act = model.act(task, think.get("decision", ""), complexity=complexity,
                           working_memory=wm, skill_context=skill_ctx)
        except tools.SandboxResourceError as e:
            # ── 用户介入（四）：沙箱资源不足 -> 提示并终止当前任务 ──
            print(_c(C_RED, f"[用户介入] 沙箱资源不足，请清理沙箱或扩展配额。({e})"))
            status = "terminated"
            _sandbox_exhausted = True
            conv["_termination_reason"] = "沙箱资源不足：run_command/write_file 触发 OSError/MemoryError"
            conv["_terminated_at_iteration"] = iteration
            session["current_stage"] = "prove"
            save_checkpoint(session)
            break
        # ── 用户介入（一）：模型输出异常 ──
        if _model_output_anomaly("act", act):
            print(_c(C_RED, "[用户介入] 模型输出异常（空输出 / 非有效 JSON / 缺少工具调用），建议重新输入任务。"))
            if not _ask_yes_no("是否仍要继续本次循环？(y/n): "):
                print(_c(C_DIM, "[用户介入] 已终止本轮：模型输出异常且用户选择不继续。"))
                return
            print(_c(C_DIM, "[用户介入] 用户选择继续（将使用降级输出）。"))
        print("\n" + _c(C_YELLOW, "ACT  -  执行改动"))
        changes = act.get("changes", [])
        if changes:
            for i, ch in enumerate(changes, 1):
                print(f"  {i}. {ch}")
        else:
            print(f"  {act.get('intent_line') or '(无改动)'}")
        # 工具执行摘要（完整输出仅在 ACT/Prove 阶段展示，不回灌 think，避免上下文膨胀）
        tool_summary = act.get("tool_execution_summary")
        if tool_summary:
            print()
            print(_c(C_DIM, tool_summary))
        # 工作记忆状态（验证：是否正确记录每步操作）
        print(_c(C_DIM, f"[工作记忆] 步数={wm.current_step} 已完成动作数={len(wm.completed_actions)} "
                       f"上一步成功={wm.last_result.get('success')}"))
        print_status(session, f"act(#{iteration})")

        # ── 过度思考控制（§33 任务三）：简单直接任务只执行一轮，直接进入验证 ──
        # 直接执行模式不进入「无进展检测」迭代（它本身就只需一次执行），一轮后 break 到 Prove。
        if _direct_mode:
            print(_c(C_GREEN, "[过度思考控制] 简单直接任务：已执行一轮，直接进入验证。"))
            conv["think"] = think
            conv["act"] = act
            last_think, last_act = think, act
            session["current_stage"] = f"act(#{iteration})"
            save_checkpoint(session)
            break

        # ── §32 进展判断（无进展检测，唯一的过程内终止条件）──
        # 每轮 act 后比较 len(wm.completed_actions) 与上一轮基线；completed_actions 仅收录
        # success=True 的动作（见 WorkingMemory.record_action），故「无新增」即代表「本轮无进展」。
        # 连续 NO_PROGRESS_LIMIT 轮无新增 -> 标记「需要人工介入」并终止。
        # 注：§29 的「工作目录一致性」与「连续 run_command 失败」检查已并入此机制——失败动作
        # 不计入 completed_actions，因此 run_command 连败 / 工作目录错配导致的空转会自然表现为
        # 「连续轮无进展」而被此处捕获，无需再维护独立的守卫。
        _cur_completed = len(wm.completed_actions)
        if _cur_completed > _prev_completed:
            _no_progress_streak = 0
        else:
            _no_progress_streak += 1
        _prev_completed = _cur_completed
        if _no_progress_streak >= NO_PROGRESS_LIMIT:
            print(_c(C_RED,
                      f"[无进展] 连续 {NO_PROGRESS_LIMIT} 轮没有新增已完成动作"
                      f"（completed_actions 停滞在 {_cur_completed}），判定需要人工介入，终止循环。"))
            status = "需要人工介入"
            wm.status = "需要人工介入"
            conv["_termination_reason"] = (f"无进展检测：连续 {NO_PROGRESS_LIMIT} 轮 completed_actions 无新增"
                                           f"（停滞于 {_cur_completed}）")
            conv["_terminated_at_iteration"] = iteration
            save_checkpoint(session)
            break

        conv["think"] = think
        conv["act"] = act
        last_think, last_act = think, act
        session["current_stage"] = f"act(#{iteration})"
        save_checkpoint(session)

        # 完成信号：模型声明 done，则提前结束循环（随后走 Prove）
        if _is_done(think, act):
            print(_c(C_GREEN, f"[完成] 第 {iteration} 轮模型判定任务已完成，结束循环并进入验证。"))
            break

    # ── PROVE（仅当正常完成时才验证；需要人工介入则跳过）──
    if _sandbox_exhausted:
        # ── 用户介入（四）：沙箱资源不足，任务已终止，跳过 Prove ──
        verdict = {
            "verdict": "UNVERIFIABLE",
            "reason": conv.get("_termination_reason", "沙箱资源不足，任务终止"),
            "suggestions": "请清理沙箱工作区或扩展沙箱配额后重试。",
        }
        conv["verdict"] = verdict
        session["current_stage"] = "prove"
        save_checkpoint(session)
        print("\n" + "=" * 60)
        print(_c(C_MAGENTA, "PROVE  -  已跳过（沙箱资源不足，任务终止）"))
        print("=" * 60)
        print(_c(_verdict_color(verdict.get("verdict")), f"  裁决: {verdict.get('verdict')}"))
        print(f"  理由: {verdict.get('reason')}")
    elif status != "需要人工介入" and last_act is not None:
        think = last_think or {}
        complexity = think.get("complexity") if isinstance(think, dict) else None
        tool_summary = last_act.get("tool_execution_summary", "")
        combined_result = "; ".join(last_act.get("changes", []))
        if tool_summary:
            combined_result += "\n" + tool_summary
        prove = model.prove(task, combined_result, complexity=complexity)
        result_text = combined_result or (last_act.get("intent_line") or "")
        evidence_text = prove.get("observed", "") if isinstance(prove, dict) else str(prove)
        # §34：传入结构化工具证据（completed_actions 仅含成功动作，§31/§32），
        # 让 judge 能区分「过程中的失败」与「最终的失败」（如读缺失文件后成功创建文件）。
        verdict = judge(task, result_text, evidence_text,
                        tool_evidence={"completed_actions": wm.completed_actions})
        conv["prove"] = prove
        conv["verdict"] = verdict
        session["current_stage"] = "prove"
        save_checkpoint(session)
        print("\n" + "=" * 60)
        print(_c(C_MAGENTA, "PROVE  -  验证与裁决"))
        print("=" * 60)
        print(f"  观察(observed): {prove.get('observed', '(无)')}")
        print(_c(_verdict_color(verdict.get("verdict")), f"  裁决: {verdict.get('verdict')}"))
        print(f"  理由: {verdict.get('reason')}")
        if verdict.get("verdict") == "REFUTED":
            print(f"  建议: {verdict.get('suggestions')}")
        print_status(session, "PROVE")
        # §59：VERIFIED 时自动生成项目报告并归档到记忆层
        _generate_report_and_archive(task, last_think, last_act, verdict,
                                     time.time() - _t0, memory_store)
        # §61：任务完成后显示 token 用量统计并记录到 logs/token_usage.log
        _report_token_usage(task, wm)
    else:
        # 需要人工介入：跳过 Prove，直接输出结果
        verdict = {
            "verdict": "UNVERIFIABLE",
            "reason": conv.get("_termination_reason",
                               "无进展检测：连续多轮没有新增已完成动作，任务转人工介入。"),
            "suggestions": "请人工检查工作记忆中的已完成动作，判断下一步。",
        }
        conv["verdict"] = verdict
        print("\n" + "=" * 60)
        print(_c(C_MAGENTA, "PROVE  -  已跳过（需要人工介入）"))
        print("=" * 60)
        print(_c(_verdict_color(verdict.get("verdict")), f"  裁决: {verdict.get('verdict')}"))
        print(f"  理由: {verdict.get('reason')}")

    # ── 用户介入（三）：验证层连续 REFUTED 跟踪与提示 ──
    _v = conv.get("verdict") or {}
    if isinstance(_v, dict):
        _update_refute_streak(session, task, _v)
        _st = session.get("_refute_streak", {})
        if (_st.get("task") == task and _st.get("count", 0) >= 3
                and len(set(_st.get("reasons", []))) >= 3):
            print(_c(C_RED, "[用户介入] 验证层连续失败，建议检查任务描述或手动介入。"))

    # ── 记忆存储（跨会话召回）──
    # 策略（§25）：仅保存验证通过（verdict == "VERIFIED"）的任务；
    # REFUTED / UNVERIFIABLE（含迭代上限转人工介入）不入库，避免污染记忆层。
    if memory_store is not None:
        try:
            _act = conv.get("act") or {}
            _think = conv.get("think") or {}
            _result_text = "; ".join(_act.get("changes", [])) or (_act.get("intent_line") or "")
            _tool_summary = _act.get("tool_execution_summary", "")
            if _tool_summary:
                _result_text += "\n" + _tool_summary
            _maybe_store_memory(
                memory_store,
                user_input=task,
                plan=_think.get("decision", "") if isinstance(_think, dict) else "",
                result=_result_text,
                verdict_field=conv.get("verdict") or {},
            )
        except Exception as e:
            print(_c(C_DIM, f"[记忆] 存储失败：{e}"))

    # ── 主动清理副作用（§33 任务二）：任务完成后清理用户数据目录 sandbox/ 内的临时 / 异常产物 ──
    try:
        _clean = cleanup_sandbox()
        _removed = _clean.get("removed") or []
        if _removed:
            _names = ", ".join(os.path.basename(p) for p in _removed)
            print(_c(C_DIM, f"[清理] 已清理 {len(_removed)} 项沙箱副作用：{_names}"))
        else:
            print(_c(C_DIM, "[清理] 沙箱无需要清理的副作用。"))
    except Exception as e:
        print(_c(C_DIM, f"[清理] 清理失败：{e}"))

    # §57：任务完成后检查工作空间外的残留目录，提示用户确认后清理
    _prompt_stray_cleanup()

    # 轮完成：归档到 conversations
    session["conversations"].append(conv)
    session["current"] = None
    session["current_stage"] = "idle"
    save_checkpoint(session)
    print(_c(C_DIM, "\n[完成] 本轮已归档。输入新任务继续，或 Ctrl+C / exit 退出。"))


def _handle_skill_command(task: str, session: dict) -> None:
    """处理 /skill 子命令：search / info / install / list。

    通过 src.integrations.mcp_client 连接 AI Skill Store（远程 MCP 服务器）。
    mcp_client 在此处惰性导入：普通任务（不输入 /skill）不会触发 mcp SDK 依赖，
    也避免拖慢启动。
    """
    import src.integrations.mcp_client as mcp_client

    parts = task.split()
    # parts[0] == "/skill"
    if len(parts) < 2:
        print(_c(C_YELLOW, "[技能] 用法：/skill search <关键词> | /skill info <技能id> | "
                          "/skill install <技能id> | /skill list"))
        return
    sub = parts[1].lower()

    if sub == "list":
        skills = mcp_client.list_installed_skills(get_skills_dir())
        if not skills:
            print(_c(C_DIM, "[技能] 尚未安装任何技能（用户数据目录 skills/ 为空）。"
                            "用 /skill search 查找，/skill install 安装。"))
            return
        print(_c(C_BOLD, f"[技能] 已安装 {len(skills)} 个技能（用户数据目录 skills/）："))
        for s in skills:
            mark = "含 SKILL.md" if s["skill_md"] else "缺少 SKILL.md"
            print(f"  · {s['name']}  ({mark})")
        return

    if sub == "search":
        kw = " ".join(parts[2:]).strip()
        if not kw:
            print(_c(C_YELLOW, "[技能] 用法：/skill search <关键词>"))
            return
        try:
            print(_c(C_CYAN, f"[技能] 正在从 AI Skill Store 搜索：{kw} ..."))
            res = mcp_client.search_skills(query=kw, limit=10)
        except Exception as e:
            print(_c(C_RED, f"[技能] 搜索失败：{e}"))
            return
        if res.get("is_error"):
            print(_c(C_RED, f"[技能] 搜索返回错误：{res.get('text', '')}"))
            return
        print(_c(C_BOLD, "[技能] 搜索结果："))
        print(res.get("text") or "(空)")
        return

    if sub == "info":
        sid = parts[2] if len(parts) > 2 else ""
        if not sid:
            print(_c(C_YELLOW, "[技能] 用法：/skill info <技能id>"))
            return
        try:
            print(_c(C_CYAN, f"[技能] 正在获取技能详情：{sid} ..."))
            g = mcp_client.get_skill(sid)
            sc = mcp_client.get_skill_schema(sid)
        except Exception as e:
            print(_c(C_RED, f"[技能] 获取失败：{e}"))
            return
        print(_c(C_BOLD, "[技能] 基本信息："))
        print(g.get("text") or "(空)")
        print(_c(C_BOLD, "\n[技能] 接口规范："))
        print(sc.get("text") or "(空)")
        return

    if sub == "install":
        sid = parts[2] if len(parts) > 2 else ""
        if not sid:
            print(_c(C_YELLOW, "[技能] 用法：/skill install <技能id>"))
            return
        skills_dir = get_skills_dir()
        try:
            print(_c(C_CYAN, f"[技能] 正在下载并安装技能：{sid} -> {skills_dir} ..."))
            info = mcp_client.install_skill(sid, skills_dir=skills_dir)
        except Exception as e:
            print(_c(C_RED, f"[技能] 安装失败：{e}"))
            return
        print(_c(C_GREEN, f"[技能] 安装成功：{info['name']}"))
        print(f"  目录：{info['dir']}")
        print(f"  文件：{', '.join(info['files'])}")
        if info["skill_md"]:
            print(f"  SKILL.md：{info['skill_md']}")
            print(_c(C_DIM, "  系统提示词已说明用户数据目录 skills/ 机制：模型遇到相关任务会读取并按其执行。"))
        else:
            print(_c(C_YELLOW, "  警告：未找到 SKILL.md，该技能可能不符合 Agent Skills 规范。"))
        return

    print(_c(C_YELLOW, f"[技能] 未知子命令：{sub}（支持 search / info / install / list）"))


def _handle_workspace_command(task: str, session: dict, memory_store) -> object:
    """处理 /workspace 子命令：查看 / 切换工作空间（§46）。

    用法：
      /workspace          -> 显示当前工作空间根目录
      /workspace <路径>   -> 校验新路径有效（存在/自动创建、是目录、可访问）后切换；
                            若目标路径不存在则自动创建（§47 修复）并打印提示；
                            切换成功后触发记忆层清理（clear_memory），
                            再为新工作空间重建记忆层（避免加载旧工作空间记忆）。
    返回切换后的记忆层实例（仅查看 / 切换失败时返回原实例）。
    """
    parts = task.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        # 仅输入 /workspace：显示当前工作空间路径
        print(_c(C_CYAN, f"[工作空间] 当前工作空间：{workspace.get_workspace_root()}"))
        return memory_store

    # /workspace <路径>：校验新路径有效（存在/自动创建、是目录、可访问）后切换
    new_path = parts[1].strip()

    # §48：自然语言路径别名解析（三规则）
    #   1) 标准路径（盘符如 C:\ 或 / 开头）-> 直接使用；
    #   2) 别名（如「桌面」）-> 在 PATH_ALIASES 中查找并替换；
    #   3) 找不到别名 -> 提示「未识别的路径描述，请使用标准路径」并终止。
    resolved = resolve_path_alias(new_path)
    if resolved is None:
        print(_c(C_RED, "未识别的路径描述，请使用标准路径"))
        return memory_store
    if resolved != new_path:
        print(_c(C_DIM, f"[工作空间] 已解析路径别名「{new_path}」-> {resolved}"))
    new_path = resolved

    # §47：切换前记录目标路径是否已存在（用于区分「自动创建」与「直接切换」）
    existed = Path(new_path).expanduser().exists()
    ok, err = workspace.set_workspace_root(new_path)
    if not ok:
        print(_c(C_RED, f"[工作空间] 切换失败：{err}"))
        return memory_store
    if not existed:
        print(_c(C_GREEN, f"[工作空间] 工作空间目录已自动创建：{workspace.get_workspace_root()}"))

    # 切换成功：触发记忆层清理（关闭旧存储 + 删除旧工作空间记忆文件 + 重置索引）
    try:
        cleared = clear_memory(memory_store) if memory_store is not None else clear_memory()
        _removed = cleared.get("removed") or []
        print(_c(C_GREEN, f"[工作空间] 已切换到：{workspace.get_workspace_root()}"))
        if _removed:
            _names = ", ".join(os.path.basename(p) for p in _removed)
            print(_c(C_DIM, f"[记忆] 已清理旧工作空间记忆 {len(_removed)} 项：{_names}"))
        else:
            print(_c(C_DIM, "[记忆] 旧工作空间无记忆文件需清理。"))
    except Exception as e:
        print(_c(C_RED, f"[记忆] 清理失败：{e}"))

    # §48：将新工作空间写入配置文件（config.yaml 的 workspace 字段），重启后可恢复；
    # 内存全局状态已由 set_workspace_root 更新，后续工具调用立即使用新工作空间。
    try:
        _cfg = load_config()
        _cfg["workspace"] = str(workspace.get_workspace_root())
        save_config(_cfg)
        print(_c(C_DIM, f"[配置] 已保存工作空间到 {get_config_file()}"))
    except Exception as e:
        print(_c(C_DIM, f"[配置] 保存工作空间失败：{e}"))

    # 为新工作空间重建记忆层（记忆文件位于新工作空间下，确保不加载旧记忆）
    # §48：全新工作空间首次初始化 agent-knowledge 时部分子目录（sources/ 等）缺失，
    #      会导致记忆写入失败回退本地载荷；此处幂等预建（mkdir exist_ok），不影响已有工作空间。
    _kd = workspace.workspace_knowledge_dir()
    for _sub in ("concepts", "entities", "reports", "sources", "syntheses"):
        try:
            (_kd / _sub).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    try:
        workspace.workspace_memory_dir().mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    memory_store = AgentKnowledgeMemory(
        knowledge_dir=str(_kd),
        fallback_jsonl=str(workspace.workspace_memory_dir() / "memories.jsonl"),
    )
    print(_c(C_DIM, f"[记忆] 新工作空间记忆层已就绪：{memory_store.backend}"
                  f"（{memory_store._payload_path}）"))
    return memory_store


def main() -> None:
    global _STATE
    # §44：确保用户数据目录（config/skills/memory/sandbox）存在，并运行 API Key 向导
    ensure_user_data_dirs()
    _run_api_key_wizard()

    # §48：从配置文件恢复上次工作空间（存在/可自动创建时），使工具默认工作目录随之一致
    _cfg_ws = load_config().get("workspace")
    if _cfg_ws:
        _ok, _msg = workspace.set_workspace_root(_cfg_ws)
        if _ok:
            print(_c(C_DIM, f"[工作空间] 已恢复上次工作空间：{workspace.get_workspace_root()}"))
        else:
            print(_c(C_DIM, f"[工作空间] 上次工作空间不可用（{_msg}），本次使用默认工作空间。"))

    router = get_router()
    print(_c(C_DIM, f"路由层: 本地复杂度路由（{router.flash} / {router.pro}）"))

    # 记忆层初始化（优先 agent-knowledge 后端，失败回退本地 JSONL）
    # §46：记忆随工作空间走 —— 知识库 <workspace>/.knowledge + 本地 JSONL <workspace>/.memory/memories.jsonl；
    #      /workspace 切换时先 clear_memory() 清空旧工作空间记忆，再为新工作空间重建本实例。
    memory_store = AgentKnowledgeMemory(
        knowledge_dir=str(workspace.workspace_knowledge_dir()),
        fallback_jsonl=str(workspace.workspace_memory_dir() / "memories.jsonl"),
    )
    print(_c(C_DIM, f"记忆层: {memory_store.backend}（{memory_store._payload_path}，随工作空间 {workspace.get_workspace_root()} 切换）"))

    # 加载融合系统提示词（src/prompts/system_prompt_merged.md）
    sys_prompt = load_system_prompt()
    if sys_prompt:
        print(_c(C_DIM, f"已加载融合系统提示词：src/prompts/system_prompt_merged.md（{len(sys_prompt)} 字符）"))
    else:
        print(_c(C_DIM, "系统提示词: 未加载（src/prompts/system_prompt_merged.md 不存在，将退化为阶段专属指令）"))

    # 加载 Act 阶段补充规则（src/prompts/act.md），约束 Act 直接执行而非先探索环境
    act_rules = load_stage_prompt("act")
    if act_rules:
        print(_c(C_DIM, f"Act 规则: 已加载（{len(act_rules)} 字符，src/prompts/act.md）"))
    else:
        print(_c(C_DIM, "Act 规则: 未加载（src/prompts/act.md 不存在）"))

    # 一次性环境探测快照：首启动生成 .env-snapshot.json，之后直接读取（跳过探测）。
    # 生成的环境信息块将在系统提示词末尾注入，让模型按当前平台生成命令（跨平台适配）。
    snap_path = ROOT / ".env-snapshot.json"
    fresh = not snap_path.exists()
    snap = get_env_snapshot()
    env_block = format_env_block(snap)
    cm = snap.get("command_map", {})
    print(_c(C_DIM,
             f"环境快照: {'已生成（首次启动）' if fresh else '已读取（复用快照）'}"
             f"（{snap.get('os')} / {snap.get('shell')} / "
             f"list={cm.get('list')}, move={cm.get('move')}）"
             f" -> .env-snapshot.json"))

    # 安全执行工作区（§22 / §44）：创建用户数据目录 sandbox/ 作为 run_command 的默认受限根目录。
    # 所有未显式指定 cwd 的命令都在该目录内执行；越界绝对路径 / 危险命令被拦截。
    sandbox_root = ensure_sandbox_root()
    print(_c(C_DIM,
             f"安全工作区: 已就绪（{sandbox_root}）"
             f"  run_command 默认在此目录内执行，越界/危险命令将被拦截"))
    # 安全执行后端（§23 / §44）：microsandbox 集成；命令/文件操作经 SandboxExecutor 在用户数据目录 sandbox/ 内执行。
    print(_c(C_DIM, f"沙箱已初始化，工作目录：{sandbox_root}"))
    print(_c(C_DIM, f"沙箱后端: {_SANDBOX.backend}（microsandbox 不可用时回退本地安全执行）"))

    model = RealModel()
    model.env_block = env_block  # 注入到 Think/Act/Prove 各阶段的系统提示词
    session = {
        "version": 1,
        "conversations": [],
        "current": None,
        "current_stage": "idle",
        "timestamp": "",
        "_model": model,
    }

    print("\n" + "=" * 60)
    print(_c(C_BOLD + C_CYAN, "fable5-lite 交互式终端  -  Think -> Act -> Prove"))
    print("=" * 60)
    print(f"  模型: {V4_MODEL}（未配置 API Key 将优雅降级）")
    print(f"  检查点: {CKPT}")
    print(f"  用户数据目录: {get_user_data_dir()}")
    print(f"  工作空间: {workspace.get_workspace_root()}（/workspace <路径> 可切换，切换时清空记忆）")

    resumed = prompt_resume()
    if resumed is not None:
        session.update({k: v for k, v in resumed.items() if k != "_model"})
        session["_model"] = model
        n = len(session.get("conversations", []))
        print(_c(C_GREEN, f"已恢复会话：历史 {n} 轮。"))
        if session.get("current"):
            print(_c(C_YELLOW, f"正在从断点（阶段 {session.get('current_stage')}）续跑上一次未完成的轮..."))
            run_turn(session["current"].get("user_input", ""), session, memory_store)
        else:
            print(_c(C_DIM, "无进行中的轮，直接等待新任务输入。"))
    else:
        print(_c(C_DIM, "新会话已开启，等待输入任务。"))

    _STATE = session

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except Exception:
        pass

    print()
    print(_c(C_BOLD, "输入任务开始一轮 Think->Act->Prove；输入 exit/quit 退出；Ctrl+C 随时退出并保存。"))
    print(_c(C_DIM, "技能管理：/skill search <关键词> | /skill info <id> | /skill install <id> | /skill list"))
    print(_c(C_DIM, "工作空间：/workspace 查看当前工作空间 | /workspace <路径或别名> 切换"
                    "（别名支持：桌面/下载/文档/项目；切换时自动清空记忆）"))
    try:
        while True:
            try:
                # §64：prompt_toolkit 输入（tty）/ 内置 input 回退（非 tty）
                task = _read_task_input().strip()
            except (EOFError, KeyboardInterrupt):
                print(_c(C_RED, "\n[!] 已退出。"))
                try:
                    save_checkpoint(_STATE)
                except Exception:
                    pass
                break
            if not task:
                continue
            if task in ("exit", "quit", ":q", "q"):
                print(_c(C_DIM, "再见。"))
                break
            if task.startswith("/skill"):
                _handle_skill_command(task, session)
                continue
            if task.startswith("/workspace"):
                # §46：切换工作空间（切换成功后重建记忆层，返回新实例）
                memory_store = _handle_workspace_command(task, session, memory_store)
                continue
            run_turn(task, session, memory_store)
    finally:
        # 退出前：若仍有进行中（未完成）的轮，把它的部分上下文也存入记忆层
        cur = session.get("current")
        if memory_store is not None and cur is not None:
            try:
                _act = cur.get("act") or {}
                _think = cur.get("think") or {}
                _result_text = "; ".join(_act.get("changes", [])) if isinstance(_act, dict) else ""
                _tool_summary = _act.get("tool_execution_summary", "") if isinstance(_act, dict) else ""
                if _tool_summary:
                    _result_text += "\n" + _tool_summary
                # 中断的轮次同样遵循「仅 VERIFIED 入库」策略（§25）
                _maybe_store_memory(
                    memory_store,
                    user_input=cur.get("user_input", ""),
                    plan=_think.get("decision", "") if isinstance(_think, dict) else "",
                    result=_result_text,
                    verdict_field=cur.get("verdict") or {},
                )
            except Exception as e:
                print(_c(C_DIM, f"[记忆] 退出前保存失败：{e}"))


if __name__ == "__main__":
    main()
