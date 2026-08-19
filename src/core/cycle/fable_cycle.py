"""Fable Method 核心循环 — Think -> Act -> Prove。

忠实、可运行的 fable-method (Sahir619/fable-method) 移植版。
原版是一个 Claude-Code / agent 的 skill（markdown 文档），这里把它改写成
自包含的 Python 实现，以便用 Mock 模型在本地离线跑通。

阶段映射（对应 fable-method 的 Step 0-6）：
  THINK = Step 0-3  分类任务 / 定义完成标准 / 收集证据 / 决定方案
  ACT   = Step 4    精准行动（intent gate 在行动前写下意图）
  PROVE = Step 5    通过观察验证（+ fable-judge 的对抗式裁决）

原版没有可运行的 examples/ 代码目录（它的 examples 是一篇 markdown 文档，
见 parts/fable-method/skills/fable-method/references/examples.md）。本文件即
照那篇文档里「Trivial」范例的精神，实现出的「最简循环执行示例」，并把其中
依赖模型客户端的部分用 MockModel 替换，确保本地可跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.core.executor.checkpoint import CheckpointStore, RunContext, Plan, Step
from src.core.validator.judge import Judge
from src.core.memory.store import MemoryStore
from src.integrations.routing.router import Router


@dataclass
class PhaseResult:
    name: str
    ok: bool
    summary: str
    details: dict = field(default_factory=dict)


class MockModel:
    """LLM 的占位实现。返回确定性的假数据，让循环完全离线运行。

    接入真实模型时，把 think / act / prove 换成对应 provider 的调用即可
    （例如 parts/oh-my-fable 的 Provider 接口）。
    """

    def think(self, task: str) -> dict:
        return {
            "classification": "task",
            "definition_of_done": f"任务已完成并通过验证：{task}",
            "evidence": ["(mock) 读取目标文件", "(mock) 定位到 1 处调用点"],
            "decision": f"对任务做最小必要改动：{task}",
            "scope": ["(mock) 目标模块"],
        }

    def act(self, task: str, decision: str) -> dict:
        return {
            "changes": [f"(mock) 已对任务做精准编辑：{task}"],
            # fable-method Step 4 的 INTENT 行：行为改动前必须写下意图
            "intent_line": "INTENT: 代码做 <X>；失败的检查/任务期望 <Y>；规范(README/docs/docstring)说 <Z>",
        }

    def prove(self, task: str, changes: List[str]) -> dict:
        return {
            "done_criterion_met": True,
            "system_healthy": True,
            "observed": f"(mock) 运行验证，观察到成功：{task}",
        }


class FableCycle:
    """跑一次 Think -> Act -> Prove 循环。"""

    def __init__(
        self,
        model: Optional[Any] = None,
        validator: Optional[Judge] = None,
        store: Optional[CheckpointStore] = None,
        memory: Optional[MemoryStore] = None,
        router: Optional[Router] = None,
    ):
        self.model = model or MockModel()
        self.validator = validator or Judge()
        self.store = store
        self.memory = memory
        self.router = router

    def run(self, task: str, run_id: str = "demo"):
        # ── THINK: Step 0-3 ──
        think = self.model.think(task)
        intent = self.router.classify(task) if self.router else "think"
        think_res = PhaseResult("THINK", True, think["decision"], {**think, "routed_intent": intent})

        # ── ACT: Step 4 ──
        act = self.model.act(task, think["decision"])
        act_res = PhaseResult("ACT", True, "; ".join(act["changes"]), act)

        # 检查点（oh-my-fable 不变量：每步执行后落盘）
        if self.store:
            ctx = RunContext(
                run_id=run_id,
                goal=task,
                plan=Plan(
                    goal=task,
                    steps=[
                        Step(id="s1", intent="think", status="done", result=think_res.summary),
                        Step(id="s2", intent="act", status="done", result=act_res.summary),
                    ],
                ),
                created_at="",
                updated_at="",
            )
            self.store.save(ctx)

        # ── PROVE: Step 5 + fable-judge ──
        prove = self.model.prove(task, act["changes"])
        verdict = self.validator.judge(
            claims=[
                {"claim": "任务已完成", "observed": prove["observed"]},
                {"claim": "系统健康", "observed": f"healthy={prove['system_healthy']}"},
            ]
        )
        prove_res = PhaseResult(
            "PROVE", prove["done_criterion_met"], prove["observed"],
            {**prove, "verdict": verdict},
        )

        # 记忆（benign，记录结论，不使用 deepcode 越狱内容）
        if self.memory:
            self.memory.remember(
                "run", {"task": task, "verdict": verdict["verdict"], "intent": intent}
            )

        return [think_res, act_res, prove_res], verdict
