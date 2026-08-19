"""最小循环执行示例 — minimal_demo.py

来源：fable-method 的「Trivial」范例（见 parts/fable-method/skills/
fable-method/references/examples.md 第 1 节）。原范例是文档，这里实现为可
运行的 Python：给定一个「trivial」任务，跑 Think -> Act -> Prove 最简循环。

依赖模型客户端的部分改为调用真实 V4 flash API（见 src/integrations/llm.py
的 RealModel / call_llm）。未配置 V4_API_KEY 时优雅降级，仍可本地跑通并生成产物。

运行：  python src/core/cycle/minimal_demo.py
输出会同时打印到终端，并保存到 logs/first_run.log。

崩溃可恢复（照 oh-my-fable 的「崩溃=暂停」不变量实现）：
- 每次完成 think / act / prove 任一阶段后，把状态写入 runs/first-run.json
  （先写 runs/first-run.tmp.json，再 rename 覆盖，避免写到一半崩溃损坏文件）。
- 启动时若检测到未完成的检查点，提示 [y] 继续执行 / [n] 丢弃并重新开始。
- 捕获 SIGINT (Ctrl+C)，中断时自动保存检查点后退出，下次启动可恢复。
- 可选：设置环境变量 FABLE_DEMO_PAUSE=1.5 可在每个阶段后暂停 1.5 秒，
  方便手动按 Ctrl+C 测试中断恢复（不影响正常流程）。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime

# 把项目根目录 (fable5-lite/) 加入 sys.path，使 `from src...` 可用
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from src.core.cycle.fable_cycle import FableCycle, MockModel, PhaseResult
from src.core.executor.checkpoint import CheckpointStore
from src.core.validator.judge import Judge, judge
from src.core.memory.store import MemoryStore
from src.integrations.routing.router import Router
from src.integrations.llm import RealModel


CKPT_PATH = os.path.join(ROOT, "runs", "first-run.json")
CKPT_TMP = os.path.join(ROOT, "runs", "first-run.tmp.json")

# 模块级会话状态，供 SIGINT handler 使用
_STATE: dict = {}
_LOG_FILE = None


class _Tee:
    """同时写向多个流（stdout + 日志文件）。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def save_checkpoint(state: dict) -> None:
    """原子写检查点：先写 .tmp.json，再 rename 覆盖。"""
    data = dict(state)
    data["timestamp"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(CKPT_PATH), exist_ok=True)
    with open(CKPT_TMP, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(CKPT_TMP, CKPT_PATH)


def load_checkpoint() -> dict | None:
    if not os.path.exists(CKPT_PATH):
        return None
    try:
        with open(CKPT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def prompt_resume() -> dict | None:
    """检测未完成检查点，返回要恢复的状态 dict，或 None（不恢复）。"""
    ck = load_checkpoint()
    if ck is None:
        return None
    # 格式校验：不是本 demo 的检查点（缺少关键字段）则视为无检查点，全新开始
    if not isinstance(ck, dict) or "history" not in ck or "user_input" not in ck:
        return None
    if ck.get("current_stage") == "done":
        # 已完成，不提示恢复
        return None
    print(f"\n检测到未完成的任务（最后阶段：{ck.get('current_stage')}），是否继续？")
    print("  [y] 继续执行")
    print("  [n] 丢弃并重新开始")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans in ("y", "yes"):
        return ck
    # 否则丢弃并重新开始
    try:
        os.remove(CKPT_PATH)
    except Exception:
        pass
    return None


def _maybe_pause() -> None:
    """可选：每个阶段后暂停若干秒，便于测试 Ctrl+C 中断恢复。"""
    try:
        secs = float(os.environ.get("FABLE_DEMO_PAUSE", "0") or "0")
    except ValueError:
        secs = 0.0
    if secs > 0:
        print(f"[pause] 模拟耗时 {secs}s（便于测试 Ctrl+C 中断）...")
        time.sleep(secs)


def _sigint_handler(signum, frame) -> None:
    """Ctrl+C：保存当前检查点后退出。"""
    print("\n[!] 捕获到 Ctrl+C，正在保存检查点后退出...")
    try:
        save_checkpoint(_STATE)
        print(f"[!] 检查点已保存到 {CKPT_PATH}")
    except Exception as e:
        print(f"[!] 保存检查点失败：{e}")
    sys.exit(130)


def build_components():
    """构造 FableCycle 的各组件（模型/验证/记忆/路由）。"""
    store = CheckpointStore(runs_dir=os.path.join(ROOT, "runs"))
    memory = MemoryStore(path=os.path.join(ROOT, "memory.json"))
    router = Router()
    cycle = FableCycle(
        model=RealModel(),
        validator=Judge(),
        store=store,
        memory=memory,
        router=router,
    )
    return cycle


def main() -> None:
    global _STATE, _LOG_FILE

    task = "Rename getUsrData to getUserData in api.ts"

    log_path = os.path.join(ROOT, "logs", "first_run.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _LOG_FILE = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, _LOG_FILE)

    print("=" * 60)
    print("Fable 5 青春版 — minimal_demo (Think -> Act -> Prove)")
    print("=" * 60)

    # 启动：检测检查点 / 恢复
    ck = prompt_resume()
    if ck is not None:
        task = ck.get("user_input", task)
        state = {
            "current_stage": ck.get("current_stage", "think"),
            "user_input": task,
            "history": ck.get("history", []),
            "last_result": ck.get("last_result", ""),
            "timestamp": ck.get("timestamp", ""),
        }
        done = [h["stage"] for h in state["history"]]
        print(f"[恢复] user_input = {task}")
        print(f"[恢复] 已完成阶段：{done}；将从阶段 [{state['current_stage']}] 继续")
    else:
        state = {
            "current_stage": "think",
            "user_input": task,
            "history": [],
            "last_result": "",
            "timestamp": "",
        }
        print(f"\n[任务] {task}")
        print(f"[说明] 取自 fable-method 的 Trivial 范例；模型接入真实 V4 flash API (call_llm)；\n"
              f"        若未设置 V4_API_KEY 则优雅降级。支持 Ctrl+C 中断保存 + 重启恢复。\n")

    _STATE = state

    cycle = build_components()
    model = cycle.model
    router = cycle.router
    validator = cycle.validator

    # 注册 SIGINT 处理（在 prompt_resume 之后，避免 input() 期间被提前接管）
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except Exception:
        pass

    stages = ["think", "act", "prove"]
    cur = state["current_stage"]
    start_idx = stages.index(cur) if cur in stages else 0

    think_res = act_res = prove_res = None
    think = act = prove = None

    # ── THINK: Step 0-3 ──
    if start_idx <= 0:
        think = model.think(task)
        intent = router.classify(task) if router else "think"
        think_res = PhaseResult("THINK", True, think.get("decision", str(think)),
                                {**think, "routed_intent": intent})
        state["history"].append({"stage": "think", "result": think})
        state["last_result"] = think.get("decision", "")
        state["current_stage"] = "act"
        save_checkpoint(state)
        _maybe_pause()
        print("\n--- THINK 完成，检查点已保存（状态: act）---")
    else:
        think = next(h["result"] for h in state["history"] if h["stage"] == "think")
        intent = think.get("routed_intent", "think")
        think_res = PhaseResult("THINK", True, think.get("decision", str(think)),
                                {**think, "routed_intent": intent})

    # ── ACT: Step 4 ──
    if start_idx <= 1:
        act = model.act(task, think.get("decision", ""))
        act_res = PhaseResult("ACT", True, "; ".join(act.get("changes", [])), act)
        state["history"].append({"stage": "act", "result": act})
        state["last_result"] = act_res.summary
        state["current_stage"] = "prove"
        save_checkpoint(state)
        _maybe_pause()
        print("\n--- ACT 完成，检查点已保存（状态: prove）---")
    else:
        act = next(h["result"] for h in state["history"] if h["stage"] == "act")
        act_res = PhaseResult("ACT", True, "; ".join(act.get("changes", [])), act)

    # ── PROVE: Step 5 + fable-judge ──
    if start_idx <= 2:
        prove = model.prove(task, act.get("changes", []))
        result_text = act_res.summary if act_res else "; ".join(act.get("changes", []))
        evidence_text = prove.get("observed", "") if isinstance(prove, dict) else str(prove)
        verdict = judge(task, result_text, evidence_text)
        prove_res = PhaseResult("PROVE", verdict.get("verdict") == "VERIFIED",
                                prove.get("observed", ""), {**prove, "verdict": verdict})
        state["history"].append({"stage": "prove", "result": prove})
        state["last_result"] = prove_res.summary
        state["current_stage"] = "done"
        save_checkpoint(state)
        _maybe_pause()
        print("\n--- PROVE 完成，检查点已保存（状态: done）---")
    else:
        prove = next(h["result"] for h in state["history"] if h["stage"] == "prove")
        result_text = act_res.summary if act_res else "; ".join(act.get("changes", []))
        evidence_text = prove.get("observed", "") if isinstance(prove, dict) else str(prove)
        verdict = judge(task, result_text, evidence_text)
        prove_res = PhaseResult("PROVE", verdict.get("verdict") == "VERIFIED",
                                prove.get("observed", ""), {**prove, "verdict": verdict})

    # 打印 prove 阶段的结构化裁决
    print("\n--- PROVE 阶段裁决（judge 验证层）---")
    print(f"  裁决: {verdict.get('verdict')}")
    print(f"  理由: {verdict.get('reason')}")
    if verdict.get("verdict") == "REFUTED":
        print(f"  建议: {verdict.get('suggestions')}")

    # 记忆（benign，记录结论，不使用 deepcode 越狱内容）
    if cycle.memory:
        cycle.memory.remember("run", {
            "task": task,
            "verdict": verdict.get("verdict") if isinstance(verdict, dict) else verdict,
            "intent": intent,
        })

    # 打印结果
    for p in [think_res, act_res, prove_res]:
        print(f"\n--- {p.name} ---")
        print(f"  {p.summary}")
        if p.name == "THINK":
            d = p.details
            print(f"  分类: {d.get('classification')}")
            print(f"  完成标准: {d.get('definition_of_done')}")
            print(f"  证据: {d.get('evidence')}")
            print(f"  路由意图: {d.get('routed_intent')}")
        if p.name == "ACT":
            print(f"  INTENT: {p.details.get('intent_line')}")
        if p.name == "PROVE":
            v = p.details.get("verdict", {})
            print(f"  完成标准满足: {p.details.get('done_criterion_met')}")
            print(f"  系统健康: {p.details.get('system_healthy')}")
            print(f"  裁决: {v.get('verdict') if isinstance(v, dict) else v}")
            print(f"  理由: {v.get('reason', '') if isinstance(v, dict) else ''}")
            if isinstance(v, dict) and v.get('verdict') == 'REFUTED':
                print(f"  建议: {v.get('suggestions', '')}")

    print("\n" + "=" * 60)
    print("运行后产物：")
    print(f"  - 检查点: runs/first-run.json（支持崩溃恢复）")
    print(f"  - 记忆:   memory.json")
    print(f"  - 日志:   logs/first_run.log")
    print(f"  - 路由后端: {router.backend if router else 'n/a'}")
    print("=" * 60)

    # 收尾：先 flush，再把真实 stdout 还原回来，最后才关日志文件，
    # 避免解释器 shutdown 时 flush 已关闭的流报 ValueError。
    _LOG_FILE.flush()
    sys.stdout = sys.__stdout__
    _LOG_FILE.close()


if __name__ == "__main__":
    main()
