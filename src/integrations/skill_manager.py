"""技能树索引与 Think/Act 阶段技能匹配（§42）。

把 ./skills/ 技能树与 Fable 5 的思考阶段打通：扫描技能目录建立「分类 -> SKILL.md
元数据」索引，根据用户输入的关键词匹配最相关的分类（默认前 3 个），读取其 SKILL.md
内容并渲染为注入块，供 Think/Act 阶段的系统提示词使用，使模型在规划/执行时参考技能
树中的相关操作指引。

技能树布局（见 DEVELOPMENT_LOG §38）：
  - ./skills/<cat>/SKILL.md 直接存在的为「叶子技能」（如 base64-codec）。
  - ./skills/<cat>/<subskill>/SKILL.md 为「元分类」（fs/command/diagnose/edit/
    search/understand），其根目录原本没有 SKILL.md。本模块会按需为元分类生成一份
    精简的「分类索引 SKILL.md」（仅概述 + 子技能 name/description 清单），既让索引
    中的 path 字段合法可读取，也避免把子技能动辄数百行的正文直接灌入上下文。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

# 项目根目录（src/integrations/ -> 根）
_ROOT = Path(__file__).resolve().parents[2]
from .user_data import get_skills_dir

# 用户数据目录下的技能树（§44 起，从项目根 ./skills 迁移到 <user_data>/skills）
SKILLS_DIR = get_skills_dir()
# 项目内自带的技能树作为兜底（首次运行、用户数据 skills/ 为空时使用）
_PROJECT_SKILLS_DIR = _ROOT / "skills"

# 分类中文显示名（注入与日志用）
CATEGORY_NAMES = {
    "fs": "文件系统操作",
    "command": "命令执行",
    "diagnose": "诊断分析",
    "edit": "编辑修改",
    "search": "搜索检索",
    "understand": "理解分析",
    "base64-codec": "Base64 编解码",
}

# 分类索引缓存（只读，线程安全懒加载）
_index_cache: Optional[dict] = None
_index_lock = threading.Lock()


def _read_frontmatter(path: Path):
    """读取 YAML frontmatter 的 name/description 字段与正文。

    返回 (frontmatter_dict, body_text)。frontmatter 仅简单解析 ``key: value`` 行，
    足以提取技能所需的 name / description；不引入额外 YAML 依赖。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}, ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    fm: dict = {}
    for line in fm_text.splitlines():
        mm = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', line)
        if mm:
            key = mm.group(1)
            val = mm.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            fm[key] = val
    return fm, body


def _generate_category_skill_md(cat: str, subskills: list) -> str:
    """为元分类生成精简的分类索引 SKILL.md（概述 + 子技能清单）。

    只列出子技能的 name/description（单行星级），不展开数百行正文，控制注入体积。
    """
    name = CATEGORY_NAMES.get(cat, cat)
    lines = [
        "---",
        f"name: {cat}",
        f"description: {name}（共 {len(subskills)} 个子技能）",
        "---",
        "",
        f"# {name}",
        "",
        f"本分类包含 {len(subskills)} 个与「{name}」相关的子技能。下列清单给出每个子技能的名称与一句话描述，",
        "模型在规划/执行相关任务时可据此判断应使用哪类能力，并参考",
        "``./skills/<分类>/<子技能>/SKILL.md`` 获取详细步骤。",
        "",
        "## 子技能清单",
        "",
    ]
    for s in subskills:
        desc = s.get("description", "")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(f"- **{s['name']}**：{desc}")
    lines.append("")
    return "\n".join(lines)


def build_index(skills_dir: Optional[Path] = None) -> dict:
    """扫描 ./skills/ 建立索引：``{cat: {name, description, path, subskills, kind}}``。

    - 叶子技能（根目录有 SKILL.md）：直读其 name/description，kind="leaf"。
    - 元分类（根目录无 SKILL.md，但有子技能）：聚合所有子技能描述作为本类 description，
      并按需生成精简的分类索引 SKILL.md（kind="category"）。
    结果带模块级缓存，重复调用不重复扫描磁盘。
    """
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    with _index_lock:
        if _index_cache is not None:
            return _index_cache
        root = skills_dir or SKILLS_DIR
        # §44：用户数据 skills/ 为空（首次运行）时，回退到项目自带技能树，保证功能不退化
        if not root.is_dir() or not any(root.iterdir()):
            root = _PROJECT_SKILLS_DIR
        index: dict = {}
        if not root.is_dir():
            _index_cache = index
            return index
        for cat_dir in sorted(root.iterdir()):
            if not cat_dir.is_dir():
                continue
            cat = cat_dir.name
            top_skill = cat_dir / "SKILL.md"
            if top_skill.exists():
                fm, _ = _read_frontmatter(top_skill)
                index[cat] = {
                    "name": CATEGORY_NAMES.get(cat, fm.get("name", cat)),
                    "description": fm.get("description", ""),
                    "path": str(top_skill),
                    "subskills": [],
                    "kind": "leaf",
                }
                continue
            # 元分类：扫描子技能
            subskills = []
            for sub in sorted(cat_dir.iterdir()):
                if not sub.is_dir():
                    continue
                sk = sub / "SKILL.md"
                if not sk.exists():
                    continue
                fm, _ = _read_frontmatter(sk)
                subskills.append({
                    "name": fm.get("name", sub.name),
                    "description": fm.get("description", ""),
                    "path": f"./skills/{cat}/{sub.name}/SKILL.md",
                })
            # 按需生成分类索引 SKILL.md（精简，仅列出子技能，避免注入超长正文）
            if subskills and not top_skill.exists():
                try:
                    top_skill.write_text(
                        _generate_category_skill_md(cat, subskills),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            parts = [s["description"] for s in subskills if s["description"]]
            agg = f"包含 {len(subskills)} 个与「{CATEGORY_NAMES.get(cat, cat)}」相关的子技能"
            if parts:
                agg += "，例如：" + "；".join(parts[:10])
                if len(parts) > 10:
                    agg += " 等"
            index[cat] = {
                "name": CATEGORY_NAMES.get(cat, cat),
                "description": agg,
                "path": str(top_skill),
                "subskills": subskills,
                "kind": "category",
            }
        _index_cache = index
        return index


def _query_needles(task: str) -> list:
    """从用户输入抽取用于匹配的「针」（中英文子串）。

    - 英文/数字词（>=2）：原样作为匹配子串。
    - 中文连续串：整体 + 2~3 字滑动窗口，使「文件系统」能命中含「文件」的分类描述。
    """
    t = (task or "").lower()
    needles = []
    for w in re.findall(r"[a-z0-9_]{2,}", t):
        needles.append(w)
    for seg in re.findall(r"[一-鿿]+", t):
        needles.append(seg)
        n = len(seg)
        for k in (3, 2):
            for i in range(max(0, n - k + 1)):
                needles.append(seg[i:i + k])
    seen = set()
    out = []
    for x in needles:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def match_skills(task: str, index: Optional[dict] = None, top_k: int = 3) -> list:
    """返回 ``[(cat, score), ...]``，按分数降序，仅含 score>0，最多 top_k 个。

    匹配方式：将分类的 name + description + 所有子技能 name/description 拼成 haystack，
    统计用户输入「针」中有多少作为子串命中；子技能名精确命中额外加权，提升精准度。
    """
    index = index or build_index()
    if not index:
        return []
    needles = _query_needles(task)
    if not needles:
        return []
    scored = []
    for cat, meta in index.items():
        hay = " ".join(
            [meta.get("name", ""), meta.get("description", "")]
            + [s.get("name", "") for s in meta.get("subskills", [])]
            + [s.get("description", "") for s in meta.get("subskills", [])]
        ).lower()
        score = 0
        for nd in needles:
            if nd and nd in hay:
                score += 1
                if any(nd == s.get("name", "").lower() for s in meta.get("subskills", [])):
                    score += 2
        if score > 0:
            scored.append((cat, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def render_injection(matched: list, index: Optional[dict] = None) -> str:
    """根据匹配到的分类渲染注入块（## 可用技能参考 ...）。"""
    index = index or build_index()
    if not matched or not index:
        return ""
    blocks = [
        "## 可用技能参考",
        "以下是当前任务相关的技能分类和具体操作步骤，请在生成计划/执行时参考：",
        "",
    ]
    for cat, _score in matched:
        meta = index.get(cat)
        if not meta:
            continue
        name = meta.get("name", cat)
        path = meta.get("path", f"./skills/{cat}")
        content = ""
        fp = Path(meta["path"]) if meta.get("path") else None
        if fp and fp.exists():
            try:
                content = fp.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                content = ""
        blocks.append(f"### 分类：{name}（{path}）")
        blocks.append(content if content else f"（{meta.get('description', '')}）")
        blocks.append("")
    return "\n".join(blocks)


def get_skill_context(task: str, top_k: int = 3):
    """便捷入口：返回 ``(injection_text, matched_list)``。无匹配时返回 ``("", [])``。"""
    index = build_index()
    matched = match_skills(task, index, top_k=top_k)
    if not matched:
        return "", []
    return render_injection(matched, index), matched
