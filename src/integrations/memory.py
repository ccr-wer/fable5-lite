"""记忆层（agent-knowledge 后端 + 本地 JSONL 回退） — src/integrations/memory.py

为 fable5-lite 提供跨会话记忆的存储与检索。

设计要点：
  - 优先使用 agent-knowledge（来自 compiled-memory 包，Python 模块名 agent_knowledge）
    作为记忆后端，知识库落盘在 ./.knowledge/（YAML vault + SQLite 事件索引，纯本地）。
  - 若 compiled_memory 未安装或初始化失败，自动回退到零依赖的本地 JSONL 存储
    （./.memory/memories.jsonl，仅用标准库 json/os/re）。
  - 两种后端对外接口完全一致，调用方（main.py）无感知差异：
        AgentKnowledgeMemory(...)
          .add(messages)            messages: 含 user_input/plan/result/verdict 的 dict -> 存储一条记忆
          .search(query, limit=3)  -> list[dict]  按相关性返回最多 limit 条（每条含完整字段）
  - agent-knowledge 模式下，检索由 Vault 内的 BM25 + 精确匹配 + 知识图谱 + RRF 融合完成，
    结构化字段（user_input/plan/result/verdict）则保存在 ./.knowledge/memories.jsonl 作为载荷，
    检索命中后按 source_id 回填为结构化 dict。

公共接口（与旧 Mem0 封装的调用方式保持兼容）：
  AgentKnowledgeMemory(knowledge_dir="./.knowledge", fallback_jsonl="./.memory/memories.jsonl")
    .backend        属性：'agent-knowledge' 或 'local-jsonl'
    .add(messages)
    .search(query, limit=3)
    .clear_memory() 关闭存储并清空当前工作空间下的记忆文件（§46：/workspace 切换时调用）
  模块级函数：
    clear_memory(store=None)  清空记忆层（传入 store 实例时关闭其存储并清理记忆文件）

记忆文件布局随工作空间走（§46）：<workspace>/.knowledge（agent-knowledge 知识库）
+ <workspace>/.memory/memories.jsonl（本地 JSONL 回退 / payload）；
切换工作空间时调用 clear_memory() 清空旧工作空间记忆，确保新工作空间不会加载旧记忆。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .workspace import get_workspace_root  # §46：按当前工作空间定位 / 清空记忆文件

# agent-knowledge 后端：知识库根目录（Vault）
DEFAULT_KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "./.knowledge")
# 本地 JSONL 回退：记忆文件路径
DEFAULT_JSONL = os.getenv("MEMORY_PATH", "./.memory/memories.jsonl")

# 一条记忆记录的字段顺序（也用于检索时的全文拼接）
_RECORD_FIELDS = ("user_input", "plan", "result", "verdict")


class LocalMemory:
    """零依赖的本地记忆存储：JSONL 追加写 + 关键词检索。

    作为 agent-knowledge 不可用时的回退方案，也作为 agent-knowledge 模式下的
    结构化载荷存储（payload），统一复用其 add/search 逻辑。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or DEFAULT_JSONL).resolve()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ── 公共 API ──
    def add(self, messages: dict) -> None:
        """追加一条记忆。

        messages 应包含 user_input / plan / result / verdict；缺失字段留空。
        额外字段（如 source_id）会一并保留。同时写入时间戳 ts。
        """
        if not isinstance(messages, dict):
            messages = {}
        row = {k: messages.get(k, "") for k in _RECORD_FIELDS}
        for k, v in messages.items():
            if k not in row and k != "ts":
                row[k] = v
        row["ts"] = datetime.now().isoformat(timespec="seconds")
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # pragma: no cover - 存储失败不应中断主流程
            print(f"[记忆] 本地存储失败：{e}", file=sys.stderr)

    def search(self, query: str, limit: int = 3) -> list:
        """按关键词匹配相似记忆，返回最近 limit 条（list[dict]，含完整字段）。"""
        rows = self._load()
        if not rows:
            return []
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []
        scored = []
        for idx, r in enumerate(rows):
            overlap = len(q_tokens & self._tokenize(self._record_text(r)))
            if overlap:
                scored.append((overlap, idx, r))
        if not scored:
            return []
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [r for _, _, r in scored[:limit]]

    def _load(self) -> list:
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except Exception:  # pragma: no cover
            return []

    @staticmethod
    def _record_text(r: dict) -> str:
        return " ".join(str(r.get(k, "")) for k in _RECORD_FIELDS)

    @staticmethod
    def _tokenize(text: str) -> set:
        """分词：英文/数字按词，中文按单字（提升部分匹配召回率）。"""
        toks = re.findall(r"[a-z0-9_]+", (text or "").lower())
        toks += re.findall(r"[\u4e00-\u9fff]", (text or ""))
        return set(toks)


class AgentKnowledgeMemory:
    """跨会话记忆层：优先 agent-knowledge 后端，失败回退本地 JSONL。

    对外暴露 add(messages) / search(query, limit) 两个方法，records 为 dict，
    字段固定为 user_input / plan / result / verdict，调用方无需感知后端差异。
    """

    def __init__(
        self,
        knowledge_dir: str | None = None,
        fallback_jsonl: str | None = None,
    ) -> None:
        self.backend = "local-jsonl"
        self.init_error: Exception | None = None
        self.vault = None
        self.compiler = None
        self.engine = None
        self._payload_path: Path = Path(
            fallback_jsonl or DEFAULT_JSONL
        ).resolve()
        # §46：知识库目录（clear_memory 清理 / 重置索引时需要）
        self._knowledge_dir: Path = Path(
            knowledge_dir or DEFAULT_KNOWLEDGE_DIR
        ).resolve()

        # 尝试加载 compiled_memory（agent_knowledge），失败则回退本地 JSONL
        try:
            from agent_knowledge import Vault, Compiler, SearchEngine  # type: ignore

            kd = self._knowledge_dir
            kd.mkdir(parents=True, exist_ok=True)
            self.vault = Vault(kd)
            self.vault.init(lang="zh")
            self.compiler = Compiler(self.vault)
            self.engine = SearchEngine(self.vault)
            # 结构化载荷与知识库同目录，便于一起管理
            self._payload_path = kd / "memories.jsonl"
            self.backend = "agent-knowledge"
        except Exception as e:  # pragma: no cover - 依赖缺失/初始化失败
            self.init_error = e
            self.backend = "local-jsonl"

        # payload 存储：agent-knowledge 模式用于保存结构化字段 + source_id 映射；
        # 回退模式则同时承担检索（关键词匹配）。
        self._store = LocalMemory(str(self._payload_path))

    # ── 公共 API ──
    def add(self, messages: dict) -> None:
        """存储一条记忆。

        agent-knowledge 模式下：把 user_input/plan/result/verdict 拼成文本写入
        Vault（用于智能检索），并把完整结构化记录（含 source_id）写入 payload JSONL。
        回退模式下：直接写入本地 JSONL。
        """
        rec = {
            k: (messages.get(k, "") if isinstance(messages, dict) else "")
            for k in _RECORD_FIELDS
        }
        rec["ts"] = datetime.now().isoformat(timespec="seconds")
        if self.backend == "agent-knowledge":
            try:
                text = self._format(messages)
                title = (rec.get("user_input") or "")[:80] or "fable5 记忆"
                src = self.compiler.ingest(text, title=title)
                rec["source_id"] = getattr(src, "id", "")
            except Exception as e:
                # 后端写入失败也要保住本地载荷，确保记忆不丢
                rec["source_id"] = ""
                print(f"[记忆] agent-knowledge 写入失败，已回退本地载荷：{e}", file=sys.stderr)
        self._store.add(rec)

    def search(self, query: str, limit: int = 3) -> list:
        """检索相关记忆，返回最近 limit 条 list[dict]（含完整字段）。

        agent-knowledge 模式：用 Vault 的 BM25+图谱+RRF 排序得到 source 命中，
        再按 source_id 从 payload 取回结构化记录；命中为空或异常时回退关键词检索。
        回退模式：直接用 LocalMemory 关键词匹配。
        """
        if self.backend == "agent-knowledge" and self.engine is not None:
            try:
                # 重新建索引，确保本轮刚 ingest 的记忆也能被检索到
                self.engine.build_index()
                hits = self.engine.search(query, top_k=limit)
                recs = self._store._load()
                by_id = {r.get("source_id"): r for r in recs if r.get("source_id")}
                out: list = []
                for h in hits:
                    r = by_id.get(h.id)
                    if r:
                        out.append({k: r.get(k, "") for k in _RECORD_FIELDS})
                    if len(out) >= limit:
                        break
                if out:
                    return out
            except Exception:
                pass
        # 回退：本地关键词检索
        return self._store.search(query, limit)

    # ── §46 工作空间切换：记忆层清理 ──
    def clear_memory(self) -> dict:
        """关闭本记忆存储并清空当前工作空间下的记忆文件（切换工作空间前调用）。

        流程：
          1) 关闭当前记忆存储：释放 agent-knowledge 的 vault / compiler / engine
             （若有 close 方法），并把后端标记重置为 local-jsonl（等待重建）；
          2) 删除 / 清空当前工作空间下的记忆文件：
             - <workspace>/.memory/memories.jsonl（本地 JSONL 回退 / 旧布局）
             - <workspace>/memory.json（历史记忆快照布局）
             - <workspace>/.knowledge/（agent-knowledge 知识库索引内容，保留 .ak-schema.yaml）
             - 本实例的 payload 文件（agent-knowledge 模式下为 <workspace>/.knowledge/memories.jsonl）
          3) 重置记忆索引：以空 LocalMemory 重建 payload 存储，
             确保新工作空间启动时不会加载旧工作空间的记忆。

        返回 {"removed": [被删除文件列表], "cleared": 说明}；任何失败都不抛异常。
        """
        # 1) 关闭当前记忆存储
        try:
            if self.vault is not None and hasattr(self.vault, "close"):
                self.vault.close()
        except Exception:
            pass
        self.vault = None
        self.compiler = None
        self.engine = None
        self.backend = "local-jsonl"
        self.init_error = None

        # 2) 删除 / 清空当前工作空间下的记忆文件（含本实例 payload）
        removed = _clear_workspace_memory_files(extra=[self._payload_path])

        # 3) 重置记忆索引：重建空的 payload 存储
        try:
            self._payload_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._store = LocalMemory(str(self._payload_path))
        return {"removed": removed, "cleared": f"记忆层已清空（{self._payload_path}）"}

    # ── 内部工具 ──
    @staticmethod
    def _format(messages: dict) -> str:
        """把结构化记忆拼成可读文本，供 agent-knowledge 编译/检索。"""
        m = messages if isinstance(messages, dict) else {}
        return (
            f"任务：{m.get('user_input', '')}\n"
            f"方案：{m.get('plan', '')}\n"
            f"结果：{m.get('result', '')}\n"
            f"裁决：{m.get('verdict', '')}"
        )


# ── §46 工作空间切换：记忆层清理（模块级辅助）──
def _clear_workspace_memory_files(extra: list | None = None) -> list:
    """删除 / 清空当前工作空间下的记忆文件，返回被删除文件列表（绝不抛异常）。

    覆盖布局（§46）：
      - <workspace>/.memory/memories.jsonl   本地 JSONL 回退 / payload
      - <workspace>/.memory/memory.json      历史快照（兜底）
      - <workspace>/memory.json              历史快照（项目根旧布局）
      - <workspace>/.knowledge/              agent-knowledge 知识库索引（保留 .ak-schema.yaml）
    额外传入的文件（extra，如实例的 payload 路径）一并尝试删除（去重）。
    """
    removed: list[str] = []
    ws = get_workspace_root()
    targets = [
        ws / ".memory" / "memories.jsonl",
        ws / ".memory" / "memory.json",
        ws / "memory.json",
    ]
    if extra:
        targets += [Path(p) for p in extra]
    seen: set = set()
    for p in targets:
        try:
            p = Path(p).resolve()
        except Exception:
            continue
        key = os.path.normcase(str(p))
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
        except Exception:
            pass
    # 清空 agent-knowledge 知识库索引内容（保留 .ak-schema.yaml 与目录本身）
    kd = ws / ".knowledge"
    if kd.exists():
        for p in sorted(kd.rglob("*"), key=lambda x: len(str(x)), reverse=True):
            try:
                if p.name == ".ak-schema.yaml":
                    continue
                if p.is_file():
                    p.unlink()
                    removed.append(str(p))
                elif p.is_dir() and p != kd:
                    p.rmdir()
            except Exception:
                pass
    return removed


def clear_memory(store: "AgentKnowledgeMemory | None" = None) -> dict:
    """清空记忆层（工作空间切换前调用，§46）。

    - 传入 store 实例：关闭其记忆存储，删除当前工作空间下的记忆文件并重置记忆索引
      （等价于 store.clear_memory()）；
    - 未传入：仅清理当前工作空间下的记忆文件（无实例时的兜底，不重置实例状态）。

    返回 {"removed": [被删除文件列表], "cleared": 说明}；绝不抛异常。
    """
    if store is not None:
        return store.clear_memory()
    return {
        "removed": _clear_workspace_memory_files(),
        "cleared": "记忆文件已清理（未传入存储实例）",
    }
