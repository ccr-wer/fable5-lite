"""工作空间管理 — src/integrations/workspace.py（§46）

维护「当前工作空间根目录」，供：
  - src/cli/main.py 的 /workspace 命令查询 / 切换工作空间；
  - src/integrations/memory.py 按工作空间定位记忆文件，并在切换时清空旧工作空间记忆；
  - src/integrations/tools.py 的 execute_tool 在执行文件操作前做「工作空间外操作拦截」。

默认工作空间 = 项目根目录（fable5-lite/，即本文件 parents[2]）；
切换工作空间前需校验新路径有效（存在/自动创建、是目录、可访问）。

§48：用户显式切换工作空间后，工具层（run_command / write_file / read_file）的默认
工作目录跟随新工作空间（get_tool_workdir）；未切换时仍用沙箱目录（默认安全隔离）。

本模块仅依赖标准库，不 import 包内其他模块，避免循环依赖。
"""

from __future__ import annotations

import os
from pathlib import Path

# 默认工作空间：项目根目录（src/integrations/workspace.py -> parents[2] = fable5-lite/）
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[2]

# 当前工作空间根目录（模块级状态，/workspace 命令运行时更新）
_workspace_root: Path = DEFAULT_WORKSPACE
# 是否已被用户显式切换过工作空间（/workspace <路径>，§48）。
# 未切换时（默认状态）工具层默认工作目录仍用沙箱；显式切换后跟随工作空间。
_explicit: bool = False


def get_workspace_root() -> Path:
    """返回当前工作空间根目录（Path）。"""
    return _workspace_root


def get_tool_workdir() -> Path | None:
    """工具层默认工作目录（§48）。

    用户显式切换过工作空间（/workspace <路径>）后，返回该工作空间根目录，
    供 run_command / write_file / read_file 作为默认工作目录；
    未切换时返回 None，由工具层回退到沙箱目录（保持默认安全隔离）。
    """
    return _workspace_root if _explicit else None


def get_workspace_root_str() -> str:
    """返回当前工作空间根目录的字符串形式。"""
    return str(_workspace_root)


def set_workspace_root(path: str | os.PathLike) -> tuple[bool, str]:
    """设置新的工作空间根目录，返回 (成功?, 信息)。

    校验顺序：
      1) 路径非空；
      2) 规范化（expanduser + resolve）成功；
      3) 路径存在 —— 不存在则自动创建（os.makedirs，exist_ok=True，§47 修复）；
      4) 是目录；
      5) 可访问（有读取 + 进入权限）。

    成功返回 (True, 规范化绝对路径)；失败返回 (False, 错误信息)，且不改变当前工作空间。
    """
    global _workspace_root, _explicit
    if not path:
        return False, "路径为空"
    p = Path(os.fspath(path)).expanduser()
    try:
        p = p.resolve()
    except Exception as e:
        return False, f"无法解析路径：{e}"
    if not p.exists():
        # §47 修复：目标路径不存在时自动创建（此前直接失败导致切换失败）
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"路径不存在且自动创建失败：{p}（{e}）"
    if not p.is_dir():
        return False, f"路径不是目录：{p}"
    if not os.access(p, os.R_OK | os.X_OK):
        return False, f"路径不可访问（无读取/进入权限）：{p}"
    _workspace_root = p
    # §48：显式切换标记，工具层默认工作目录随即切换到新工作空间
    _explicit = True
    return True, str(p)


def is_within_workspace(path: str | os.PathLike) -> bool:
    """判断 path 是否位于当前工作空间根目录之内（含根目录本身）。

    相对路径按「相对于工作空间根目录」解析；解析失败视为不在工作空间内。
    """
    try:
        p = Path(os.fspath(path)).expanduser()
        if not p.is_absolute():
            p = _workspace_root / p
        p = p.resolve()
    except Exception:
        return False
    return _is_within(p, _workspace_root)


def _is_within(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 之内（含 root 本身）。"""
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    return p == r or p.startswith(r + os.sep)


# ── 工作空间下的记忆 / 知识库目录约定（§46）──
def workspace_memory_dir() -> Path:
    """工作空间下的本地记忆目录：<workspace>/.memory。"""
    return _workspace_root / ".memory"


def workspace_knowledge_dir() -> Path:
    """工作空间下的 agent-knowledge 知识库目录：<workspace>/.knowledge。"""
    return _workspace_root / ".knowledge"
