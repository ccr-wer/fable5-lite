"""AI Skill Store MCP 客户端（Streamable HTTP）— src/integrations/mcp_client.py

连接到公开的远程 MCP 服务器 https://aiskillstore.io/mcp（AI Skill Store），
提供技能搜索 / 详情 / 安装能力。

实现要点：
  - 用官方 mcp SDK（streamable_http_client + ClientSession）完成 MCP 握手与调用；
    mcp SDK 在此模块内**惰性导入**（首次调用 call_tool/list_tools 时才 import），
    因此普通任务执行（不触发 /skill 命令）不会要求安装 mcp，也不会拖慢启动。
  - 该 MCP 的 download_skill 仅把包写到**服务器端**临时目录并返回路径，不回传字节；
    真正的可安装包通过 REST 端点 GET https://aiskillstore.io/v1/skills/{id}/download
    获取（返回 .skill zip，内含 <name>/SKILL.md）。install_skill() 走这条真实本地路径。
  - 未安装 mcp 时，list_tools/call_tool 会给出清晰报错；install_skill() 仅依赖标准库 +
    网络，不依赖 mcp SDK（因为实际下载走 REST 端点）。

依赖：pip install mcp（仅 search/info 走 MCP 时需要；install/list 不需要）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ── 端点（可通过环境变量覆盖，便于自托管 / 测试）──
DEFAULT_MCP_URL = os.environ.get("SKILL_STORE_MCP_URL", "https://aiskillstore.io/mcp")
DEFAULT_REST_URL = os.environ.get(
    "SKILL_STORE_REST_URL", "https://aiskillstore.io/v1/skills/{skill_id}/download"
)

# download_skill / get_install_guide 要求的平台名（区分大小写，必须与服务器一致）
PLATFORMS = ["OpenClaw", "ClaudeCode", "ClaudeCodeAgentSkill",
             "CustomAgent", "Cursor", "GeminiCLI", "CodexCLI"]


class SkillStoreError(RuntimeError):
    """AI Skill Store 客户端错误（网络 / 解析 / 校验失败时抛出）。"""


# ─────────────────────────────────────────────────────────────────────────────
# MCP 层（惰性导入官方 SDK）
# ─────────────────────────────────────────────────────────────────────────────
def _session_factories():
    """惰性导入 mcp SDK，返回 (streamable_http_client, ClientSession)。

    未安装 mcp 时抛出清晰的 SkillStoreError，而不是晦涩的 ImportError。
    """
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as e:  # pragma: no cover - 依赖缺失时的友好提示
        raise SkillStoreError(
            "未安装 mcp SDK，无法连接 AI Skill Store（search/info 需要）。\n"
            "请运行：pip install mcp"
        ) from e
    return streamable_http_client, ClientSession


async def _mcp_call(mcp_url: str, method: str, *, tool_name: str | None = None,
                    arguments: dict | None = None):
    """在一次性会话里执行一次 MCP 调用（tools/list 或 tools/call）。

    返回：
      - tools/list -> list[dict]{name, description, input_schema}
      - tools/call -> dict{text, structured, is_error}
    """
    streamable_http_client, ClientSession = _session_factories()
    async with streamable_http_client(mcp_url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            if method == "tools/list":
                result = await session.list_tools()
                return [
                    {"name": t.name, "description": t.description or "",
                     "input_schema": t.input_schema}
                    for t in result.tools
                ]
            # tools/call
            result = await session.call_tool(tool_name or "", arguments or {})
            texts = []
            for c in result.content:
                if getattr(c, "type", None) == "text":
                    texts.append(c.text)
            return {
                "text": "\n".join(texts),
                "structured": getattr(result, "structuredContent", None),
                "is_error": bool(getattr(result, "isError", False)),
            }
    return None


def list_tools(mcp_url: str = DEFAULT_MCP_URL) -> list[dict]:
    """列出 AI Skill Store 支持的工具（search_skills / get_skill / download_skill ...）。"""
    import asyncio
    return asyncio.run(_mcp_call(mcp_url, "tools/list"))


def call_tool(tool_name: str, params: dict | None = None,
              mcp_url: str = DEFAULT_MCP_URL) -> dict:
    """调用任意远程 MCP 工具（如 search_skills / get_skill / get_skill_schema / download_skill）。

    返回 {text, structured, is_error}。注意 download_skill 仅把包写到服务器端临时目录，
    返回的是「下载完成 + 服务器路径」信息，不含可安装的包字节——本地安装请改用 install_skill()。
    """
    import asyncio
    return asyncio.run(_mcp_call(mcp_url, "tools/call",
                                 tool_name=tool_name, arguments=params or {}))


# ── 便捷封装（供 CLI 与脚本直接调用）──
def search_skills(query: str = "", limit: int = 10, capability: str = "",
                  platform: str = "", category: str = "",
                  mcp_url: str = DEFAULT_MCP_URL) -> dict:
    args: dict = {"query": query, "limit": limit}
    if capability:
        args["capability"] = capability
    if platform:
        args["platform"] = platform
    if category:
        args["category"] = category
    return call_tool("search_skills", args, mcp_url)


def get_skill(skill_id: str, mcp_url: str = DEFAULT_MCP_URL) -> dict:
    return call_tool("get_skill", {"skill_id": skill_id}, mcp_url)


def get_skill_schema(skill_id: str, mcp_url: str = DEFAULT_MCP_URL) -> dict:
    return call_tool("get_skill_schema", {"skill_id": skill_id}, mcp_url)


# ─────────────────────────────────────────────────────────────────────────────
# 本地安装层（REST 端点，标准库即可，不依赖 mcp SDK）
# ─────────────────────────────────────────────────────────────────────────────
def fetch_package(skill_id: str) -> bytes:
    """通过 REST 端点下载 .skill 包（zip 字节）。

    端点来自 get_install_guide 提示的真实下载地址：
    https://aiskillstore.io/v1/skills/{skill_id}/download
    """
    url = DEFAULT_REST_URL.format(skill_id=skill_id)
    try:
        import requests  # 优先 requests
        resp = requests.get(url, timeout=30, headers={"User-Agent": "fable5-lite"})
        if resp.status_code != 200:
            raise SkillStoreError(f"下载技能包失败：HTTP {resp.status_code} ({url})")
        return resp.content
    except ImportError:
        pass
    except SkillStoreError:
        raise
    except Exception as e:  # pragma: no cover - 网络异常
        raise SkillStoreError(f"下载技能包失败：{e}") from e
    # ── urllib 回退 ──
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "fable5-lite"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SkillStoreError(f"下载技能包失败：HTTP {e.code} ({url})") from e
    except Exception as e:  # pragma: no cover
        raise SkillStoreError(f"下载技能包失败：{e}") from e


def _safe_member(name: str) -> str | None:
    """zip 成员名校验：拒绝绝对路径与路径遍历（zip slip 防护）。

    返回去除顶层目录后的相对路径；不安全返回 None。
    """
    if not name or name.endswith("/"):
        return None
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return None
    return name


def install_skill(skill_id: str, skills_dir: str | os.PathLike | None = None,
                  platform: str | None = None) -> dict:
    """下载技能并解压安装到本地 ./skills/<name>/。

    - 通过 fetch_package() 获取 .skill zip（标准库即可，不走 MCP 的 download_skill，
      因为后者只写服务器临时目录）。
    - zip 内形如 <name>/SKILL.md、<name>/main.py；解压时去掉公共顶层目录，落到
      skills_dir/<name>/，最终得到 skills_dir/<name>/SKILL.md。
    - 含 zip slip 防护。
    返回 {skill_id, name, dir, skill_md, files}。
    """
    skills_dir = Path(skills_dir) if skills_dir else (ROOT / "skills")
    skills_dir.mkdir(parents=True, exist_ok=True)

    data = fetch_package(skill_id)
    if not data or data[:2] != b"PK":
        raise SkillStoreError("下载到的技能包不是有效的 .skill(zip) 格式（缺少 PK 头）。")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n]
        # 仅用「安全成员」求公共顶层目录，避免恶意成员（路径遍历）干扰落位判断
        safe_names = [n for n in names if _safe_member(n)]
        # 求公共顶层目录（修正到目录边界）
        top = os.path.commonprefix(safe_names) if safe_names else ""
        if top and not top.endswith("/"):
            idx = top.rfind("/")
            top = top[:idx + 1] if idx >= 0 else ""
        skill_name = top.rstrip("/") if top else skill_id

        dest = skills_dir / skill_name
        dest.mkdir(parents=True, exist_ok=True)

        extracted: list[str] = []
        for info in zf.infolist():
            rel = _safe_member(info.filename)
            if rel is None:
                continue
            if top and rel.startswith(top):
                rel = rel[len(top):]
            if not rel:
                continue
            if _safe_member(rel) is None:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                continue
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())
            extracted.append(rel)

    skill_md = dest / "SKILL.md"
    return {
        "skill_id": skill_id,
        "name": skill_name,
        "dir": str(dest),
        "skill_md": str(skill_md) if skill_md.exists() else None,
        "files": extracted,
    }


def list_installed_skills(skills_dir: str | os.PathLike | None = None) -> list[dict]:
    """列出 ./skills/ 下已安装的技能（含 SKILL.md 路径）。"""
    skills_dir = Path(skills_dir) if skills_dir else (ROOT / "skills")
    if not skills_dir.exists():
        return []
    out: list[dict] = []
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        sm = d / "SKILL.md"
        out.append({
            "name": d.name,
            "dir": str(d),
            "skill_md": str(sm) if sm.exists() else None,
        })
    return out


if __name__ == "__main__":  # pragma: no cover
    # 简单自测：列出工具
    try:
        for t in list_tools():
            print("-", t["name"], "::", t["description"][:60])
    except SkillStoreError as e:
        print("ERR:", e, file=sys.stderr)
