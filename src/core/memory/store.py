"""记忆存储层（episodic memory）。

这是一个干净的记忆层实现。parts/deepcode/ 是「记忆模板」参考仓库，
但其 jailbreak.txt 等越狱类材料按内容规范**不在此处使用**——
本模块只用纯结构化的 JSON 记忆，不依赖 deepcode 的任何越狱内容。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, List, Optional


class MemoryStore:
    """极简情节记忆：把每次运行的关键结论记到本地 JSON 文件。

    与 oh-my-fable 的 RunContext.meta 扩展槽思路一致——模块可在不污染
    核心结构的前提下挂接自己的状态。
    """

    def __init__(self, path: str = "memory.json"):
        self.path = path
        self.entries: List[dict] = self._load()

    def _load(self) -> List[dict]:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return []
        return []

    def remember(self, kind: str, content: Any, meta: Optional[dict] = None) -> dict:
        e = {"ts": time.time(), "kind": kind, "content": content, "meta": meta or {}}
        self.entries.append(e)
        self._save()
        return e

    def recall(self, kind: Optional[str] = None, limit: int = 10) -> List[dict]:
        items = self.entries if not kind else [e for e in self.entries if e["kind"] == kind]
        return items[-limit:]

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.entries, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
