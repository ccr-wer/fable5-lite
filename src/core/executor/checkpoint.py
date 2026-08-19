"""Checkpoint / resume store — 移植自 oh-my-fable 的 FileStore。

oh-my-fable (didrod205/oh-my-fable) 的核心不变量：
> 每一次迭代结束时，磁盘上的 RunContext 必须等于内存里的 RunContext——
> 所以任何崩溃都能从最后一个检查点续跑，零进度丢失。

本实现忠实复刻其两条关键设计：
1. 每步执行后调用 save() 落盘（runLoop 的 step 7 "Checkpoint — the invariant"）。
2. save() 用「先写临时文件，再原子 rename」(write-then-rename)，保证即使写到
   一半崩溃，也只会留下上次的好检查点，不会损坏当前检查点。

仅依赖 Python 标准库，可在本地离线运行。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Step:
    """计划中的一步（对应 oh-my-fable 的 Step）。"""
    id: str
    intent: str
    status: str = "pending"          # pending | running | done | failed | skipped
    attempts: int = 0
    depends_on: List[str] = field(default_factory=list)
    result: Optional[str] = None


@dataclass
class Plan:
    """一个计划（对应 oh-my-fable 的 Plan）。"""
    goal: str
    steps: List[Step] = field(default_factory=list)
    status: str = "active"           # active | done | failed
    revision: int = 0


@dataclass
class RunContext:
    """一次运行的唯一事实来源（对应 oh-my-fable 的 RunContext）。

    与原始设计一致：全部字段都是可 JSON 序列化的纯数据。
    """

    run_id: str
    goal: str
    plan: Plan
    history: List[dict] = field(default_factory=list)
    budget_steps: int = 0
    created_at: str = ""
    updated_at: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "plan": {
                "goal": self.plan.goal,
                "steps": [asdict(s) for s in self.plan.steps],
                "status": self.plan.status,
                "revision": self.plan.revision,
            },
            "history": self.history,
            "budget_steps": self.budget_steps,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunContext":
        plan = Plan(
            goal=d["plan"]["goal"],
            steps=[Step(**s) for s in d["plan"]["steps"]],
            status=d["plan"]["status"],
            revision=d["plan"].get("revision", 0),
        )
        return cls(
            run_id=d["run_id"],
            goal=d["goal"],
            plan=plan,
            history=d.get("history", []),
            budget_steps=d.get("budget_steps", 0),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            meta=d.get("meta", {}),
        )


class CheckpointStore:
    """默认持久化：每个 run 一个 JSON 文件。零依赖。

    与 oh-my-fable FileStore 行为一致：save 用 write-then-rename 保证原子性，
    因此崩溃绝不会损坏上一个好检查点。可用 SQLite/Redis 替换，只需实现相同接口。
    """

    def __init__(self, runs_dir: str = "runs"):
        self.dir = runs_dir

    def _ensure(self) -> None:
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, run_id: str) -> str:
        # run_id 由我们生成（无路径分隔符），仍做防御性清洗。
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in run_id)
        return os.path.join(self.dir, f"{safe}.json")

    def save(self, ctx: RunContext) -> None:
        self._ensure()
        ctx.updated_at = _now_iso()
        data = json.dumps(ctx.to_dict(), indent=2, ensure_ascii=False)
        f = self._path(ctx.run_id)
        tmp = f + ".tmp"
        # 先写临时文件，再原子 rename —— 崩溃中_write 不会损坏上次检查点。
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, f)

    def load(self, run_id: str) -> Optional[RunContext]:
        f = self._path(run_id)
        if not os.path.exists(f):
            return None
        try:
            with open(f, encoding="utf-8") as fh:
                return RunContext.from_dict(json.load(fh))
        except Exception:
            return None

    def list(self) -> List[dict]:
        self._ensure()
        out: List[dict] = []
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({
                    "run_id": d["run_id"],
                    "goal": d["goal"],
                    "plan_status": d["plan"]["status"],
                    "steps": len(d["plan"]["steps"]),
                    "updated_at": d.get("updated_at", ""),
                })
            except Exception:
                pass
        return sorted(out, key=lambda x: x["updated_at"], reverse=True)

    def resume(self, run_id: str) -> Optional[RunContext]:
        """从最后一个检查点续跑（对应 oh-my-fable 的 resume 语义）。"""
        return self.load(run_id)
