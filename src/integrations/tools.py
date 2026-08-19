"""工具注册表 — src/integrations/tools.py

为 Fable 5 的 Act 阶段提供 Function Calling 工具能力。

对外接口：
  TOOLS                 —— 传给 LLM（call_llm）的 3 个工具 JSON Schema 定义
  execute_tool(name, arguments)  —— 按工具名分发执行，返回字符串结果（含错误信息）

工具清单：
  read_file    读取文件内容（参数 path）—— 只读，直接执行
  write_file   写入文件内容（参数 path, content）—— 直接执行（§30 起不再请求用户确认）
  run_command  执行系统命令（参数 command，可选 cwd）—— 安全执行后端：默认在用户数据目录的 sandbox/ 受限工作区内执行（§44 起从项目根 用户数据目录 sandbox/ 迁移）；传入 cwd 则在指定目录执行；只读命令直接执行、写/改/删命令同样直接执行（§30 起不再请求用户确认），仅由沙箱安全策略拦截危险命令（del /F、rm -rf、chmod 777、curl/wget、set/export、format/fdisk/dd 等）与越界绝对路径（如 C:\、/）并返回 [拦截] 警告

所有调用、结果、错误都会写入 logs/tools.log，便于审计与排查。

注意：本文件仅依赖 Python 标准库，不引入额外依赖；也不反向 import llm，避免循环依赖。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ── §23 安全执行后端（microsandbox / 本地回退）──
from .sandbox import SandboxExecutor

# ── 用户数据目录（§44）：沙箱工作目录迁移到 <user_data>/sandbox ──
from .user_data import get_sandbox_dir

# ── 工作空间（§46）：/workspace 切换；execute_tool 执行文件操作前做「工作空间外操作拦截」──
# §48：get_tool_workdir —— 显式切换工作空间后，工具默认工作目录跟随新工作空间。
from .workspace import get_workspace_root, get_tool_workdir

# ── §53 Rubric 行为验证观测层（执行前检查；先作为观测层，不拦截）──
from .rubric_guard import check_tool_call as _rubric_check

# ── 项目根目录：src/integrations/tools.py -> parents[2] = fable5-lite/ ──
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "tools.log"

# ── 安全执行工作区（§22 / §44）：run_command 的默认受限根目录 ──
# 所有未显式指定 cwd 的命令都在该目录内执行；越界绝对路径 / 危险命令被拦截。
# §44：根目录从项目根的 用户数据目录 sandbox/ 迁移到用户数据目录下的 sandbox/
#      （Windows %APPDATA%/fable5/sandbox，Linux/macOS ~/.local/share/fable5/sandbox）。
SANDBOX_DIR = get_sandbox_dir()
_SANDBOX = SandboxExecutor(workdir=str(SANDBOX_DIR))


def _default_workdir() -> Path:
    """工具层默认工作目录（§48）。

    用户显式切换工作空间（/workspace <路径>）后返回新工作空间根目录；
    未切换时回退到沙箱根目录（用户数据目录 sandbox/，保持默认安全隔离）。
    """
    wd = get_tool_workdir()
    return wd if wd is not None else SANDBOX_DIR


def _sandbox_for_tool() -> SandboxExecutor:
    """工具执行用的执行器（§48）：工作空间被显式切换后以新工作空间为根，否则用沙箱根。

    切换后按需创建执行器（__init__ 仅 resolve + mkdir，开销极小），使
    run_command / write_file / read_file 的默认工作目录立即跟随新工作空间。
    """
    wd = _default_workdir()
    try:
        same = wd.resolve() == _SANDBOX.workdir.resolve()
    except Exception:
        same = False
    if same:
        return _SANDBOX
    return SandboxExecutor(workdir=str(wd))

# ── §30 沙箱资源不足异常：run_command / write_file / read_file 触发
# OSError / MemoryError（磁盘满 / 内存不足 / 沙箱进程崩溃等）时由工具层转换为此异常，
# main.py 在循环顶层捕获后提示「沙箱资源不足，请清理沙箱或扩展配额」并终止当前任务。
class SandboxResourceError(Exception):
    """沙箱执行资源不足（OSError / MemoryError）。"""


# ── §33 任务一：工具调用失败的结构化错误返回 ──
# 工具调用失败时（文件不存在 / 权限不足 / 命令失败），不再返回自由文本 [错误]，
# 而是返回结构化 JSON 字符串，便于模型在下一轮迭代中解析 error_type 并生成备选计划
# （如 file_not_found -> 先创建目录再写入）。三种 error_type：
#   file_not_found    文件 / 目录不存在（读取、写入目标目录缺失等）
#   permission_denied 权限不足（无法读 / 写 / 执行）
#   command_failed    命令执行失败（非零退出、被沙箱策略拦截等）
def _error_result(error_type: str, message: str) -> str:
    """生成结构化错误返回（任务一）。返回 JSON 字符串。"""
    return json.dumps(
        {"status": "error", "error_type": error_type, "message": message},
        ensure_ascii=False,
    )


def _is_error_result(s: str) -> bool:
    """判断工具结果是否为「错误」（兼容新结构化 JSON 与旧 [错误]/[拦截] 前缀）。

    用于 _log 的 status 判定，也供调用方识别是否需要走「备选计划」逻辑。
    """
    if not s:
        return False
    t = s.lstrip()
    if t.startswith("[错误]") or t.startswith("[拦截]"):
        return True
    return '"status": "error"' in t


def _classify_file_error(stderr: str) -> str:
    """按沙箱 stderr 推断读文件失败的错误类型（file_not_found / permission_denied）。"""
    s = (stderr or "").lower()
    if "permission" in s or "denied" in s or "拒绝" in s or "权限" in s or "read-only" in s or "readonly" in s:
        return "permission_denied"
    return "file_not_found"


def _classify_write_error(stderr: str) -> str:
    """按沙箱 stderr 推断写文件失败的错误类型（permission_denied / file_not_found）。

    写入失败通常由「权限不足」或「目标目录不存在」导致；优先判权限，其余归 file_not_found。
    """
    s = (stderr or "").lower()
    if ("permission" in s or "denied" in s or "拒绝" in s or "权限" in s
            or "read-only" in s or "readonly" in s or "read only" in s):
        return "permission_denied"
    return "file_not_found"


# ── 危险命令拦截（跨平台，按「行为类别 + 整词/命令起始」匹配，避免误伤 git reset 等） ──
# 拦截类别（与开发日志 §22 一致）：
#   1) 破坏性/递归删除：rm -rf / rm -fr / rm -r / rmdir /s / rd /s / del /f / del /s / deltree / srm / shred
#   2) 权限变更：chmod 777 / 000 / 666 / -R / 改根或系统目录权限、chown、chattr、takeown、icacls
#   3) 网络请求：curl / wget / aria2c / invoke-webrequest / iwr / nc / netcat / telnet
#   4) 环境变量修改：set / setx / export（仅当作为命令起始）、PowerShell $env: 赋值
#   5) 磁盘操作：format / fdisk / mkfs / diskpart / parted、dd（任意 dd 原语）
#   6) 其他：shutdown / reboot / fork bomb / > /dev/sd / powershell -e(enc) / | sh / | bash
_ABS_WIN = re.compile(r"[a-z]:[\\/][^|;&]*")  # Windows 盘符绝对路径：C:\... / D:/...


def _is_within(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 之内（含 root 本身）。"""
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    return p == r or p.startswith(r + os.sep)


def _is_dangerous_command(command: str) -> tuple[bool, str]:
    """危险命令检测：命中则返回 (True, 原因)；否则 (False, '')。

    采用「整词 / 命令起始」匹配，避免误伤 git reset、git log --format 等正常命令。
    """
    low = command.lower().strip()
    tokens = re.split(r"[\s|;&`]+", low)
    first = tokens[0] if tokens else ""

    # 1) 破坏性 / 递归删除
    for pat in ("rm -rf", "rm -fr", "rm -r ", "rmdir /s", "rd /s",
                "del /f", "del /s", "deltree", "srm", "shred"):
        if pat in low:
            return True, f"破坏性/递归删除命令被禁止（命中片段 '{pat}'）"

    # 2) 权限变更
    if re.search(r"\bchmod\b", low):
        if re.search(r"777|000|666|-r\b|\br\b\s", low) or re.search(r"chmod\s+\S*\s*/", low):
            return True, "危险的权限变更（chmod 777/000/666/-R 或修改根/系统目录权限）被禁止"
    if re.search(r"\b(chown|chattr|takeown|icacls)\b", low):
        return True, "文件/目录所有权或 ACL 变更（chown/chattr/takeown/icacls）被禁止"

    # 3) 网络请求
    if re.search(r"\b(curl|wget|aria2c|invoke-webrequest|iwr)\b", low):
        return True, "网络请求命令（curl/wget/aria2c/iwr 等）被禁止"
    if re.search(r"\b(nc|netcat|telnet)\b", low):
        return True, "裸 socket/远程连接命令（nc/netcat/telnet）被禁止"

    # 4) 环境变量修改（仅当作为命令起始，避免误伤 git reset）
    if first in ("set", "setx", "export") or low.startswith(("set ", "export ")):
        return True, "环境变量修改（set/export/setx）被禁止"
    if "$env:" in low:
        return True, "PowerShell 环境变量赋值（$env:）被禁止"

    # 5) 磁盘格式化 / 分区 / dd（按命令 token 判定，剥离 .ext4 /= 避免误伤路径）
    for tok in tokens:
        base = tok.split(".", 1)[0].split("=", 1)[0]
        if base in ("format", "fdisk", "mkfs", "diskpart", "parted"):
            return True, "磁盘格式化/分区命令（format/fdisk/mkfs/diskpart/parted）被禁止"
    if re.search(r"\bdd\b", low):
        return True, "dd 原始磁盘写入命令被禁止"

    # 6) 其他高危片段
    for pat in ("shutdown", "reboot", ":(){", "fork bomb",
                "> /dev/sd", "powershell -e", "powershell -enc",
                "| sh", "| bash"):
        if pat in low:
            return True, f"危险命令片段被禁止（命中 '{pat}'）"

    return False, ""


def _references_outside_sandbox(command: str, cwd: str | None = None) -> tuple[bool, str]:
    """检测命令是否显式引用了工作区之外的绝对路径（§48：基准为工具默认工作目录）。

    返回 (True, 路径) 表示逃逸；默认 root 为工具默认工作目录（显式切换后为新工作空间，
    否则为用户数据目录 sandbox/，cwd 未指定时）。
    用于验证「命令被限制在默认工作目录内」：如 `dir C:\\` / `ls -la /` 应被阻止。
    """
    root = Path(cwd) if cwd else _default_workdir()
    try:
        root_res = root.resolve()
    except Exception:
        root_res = root
    # Windows 盘符绝对路径：C:\... / D:/...
    for m in _ABS_WIN.finditer(command.lower()):
        try:
            pres = Path(m.group(0)).resolve()
        except Exception:
            continue
        if not _is_within(pres, root_res):
            return True, m.group(0)
    # §81：环境变量绝对路径（%USERPROFILE% / %APPDATA% 等，展开后为绝对路径）
    for env_path in _abs_env_paths(command):
        try:
            pres = Path(env_path).resolve()
        except Exception:
            continue
        if not _is_within(pres, root_res):
            return True, env_path
    # Unix 绝对路径（以 / 起始的 token，含裸根目录 /）
    for tok in re.split(r"[\s|;&`\"']+", command):
        if tok.startswith("/"):
            try:
                pres = Path(tok).resolve()
            except Exception:
                continue
            if not _is_within(pres, root_res):
                return True, tok
    return False, ""


def _abs_env_paths(command: str) -> list:
    """提取命令中的环境变量绝对路径（%USERPROFILE% 等，展开后为绝对路径）。§81"""
    found: list = []
    for m in re.finditer(r"%([A-Za-z_][A-Za-z0-9_]*)%", command):
        v = os.environ.get(m.group(1))
        if v and os.path.isabs(v):
            found.append(v)
    return found


# ── §46 工作空间外操作拦截（/workspace 切换配套）──
# 在执行任何文件操作（read_file / write_file / run_command）前，提取目标路径并检查
# 是否位于当前工作空间根目录（workspace.get_workspace_root()）内；越界则打印警告并
# 等待用户 y/n 确认，输入 n 则取消操作（结果标记为「已取消」）。
def _extract_target_paths(tool_name: str, arguments: dict) -> list:
    """从工具调用参数中提取目标路径（供工作空间边界检查）。

    - read_file / write_file：参数 path；
    - run_command：命令中显式引用的绝对路径（Windows 盘符路径 C:\\... / D:/...，
      Unix 根路径 /...），以及显式传入的绝对 cwd。
    相对路径（如 'docs/notes.txt'）不在此提取——按工作空间内处理（§46）。
    """
    targets: list[str] = []
    if tool_name in ("read_file", "write_file"):
        p = arguments.get("path")
        if p:
            targets.append(str(p))
    elif tool_name == "run_command":
        command = str(arguments.get("command", "") or "")
        for m in _ABS_WIN.finditer(command.lower()):
            t = m.group(0).strip().strip('"').strip("'")  # §82：去除盘符路径首尾引号
            if t:
                targets.append(t)
        # §81：环境变量绝对路径（%USERPROFILE% / %APPDATA% 等，展开后为盘符绝对路径）
        for env_path in _abs_env_paths(command):
            targets.append(env_path)
        for tok in re.split(r"[\s|;&`\"']+", command):
            if tok.startswith("/"):
                # 跳过单段命令开关（如 /b、/s、/q），仅当含路径分隔符或为裸根时视为路径
                _rest = tok[1:]
                if "/" in _rest or "\\" in _rest or not _rest:
                    targets.append(tok)
        cwd = arguments.get("cwd")
        if cwd and os.path.isabs(str(cwd)):
            targets.append(str(cwd))
    return targets


def _outside_workspace_targets(tool_name: str, arguments: dict) -> list:
    """返回目标路径中位于当前工作空间根目录之外的部分（规范化绝对路径，去重）。

    相对路径按「相对于工作空间根解析」处理，解析后必在根内，不触发拦截；
    仅显式绝对路径（盘符 / Unix 根路径 / 绝对 cwd）可能越界。
    """
    ws = get_workspace_root()
    outside: list[str] = []
    seen: set = set()
    for t in _extract_target_paths(tool_name, arguments):
        try:
            p = Path(t).expanduser()
            if not p.is_absolute():
                p = ws / p
            p = p.resolve()
        except Exception:
            continue
        key = os.path.normcase(str(p))
        if key in seen:
            continue
        seen.add(key)
        if not _is_within(p, ws):
            outside.append(str(p))
    return outside


def _confirm_outside_workspace(targets: list) -> bool:
    """工作空间外操作拦截提示：打印警告并等待用户 y/n 确认。

    返回 True 表示用户允许执行；False 表示取消（输入 n / 非交互 EOF / 中断时安全默认取消）。
    （§84 起 execute_tool 不再调用本函数，改用任务级一次确认 _ask_task_sandbox_approval。）
    """
    print()
    for t in targets:
        print(f"[警告] 操作目标在工作空间外：{t}")
    print("是否允许执行？(y/n)")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


# ── §85 任务级沙箱外操作确认（每个任务只提醒一次）──
# _task_sandbox_approved=True：本任务已允许所有沙箱外操作，后续直接执行不再提示；
# _task_sandbox_denied=True：本任务已拒绝，后续沙箱外操作直接取消（不反复提示）。
# main.py 每轮任务开始时调用 reset_task_sandbox_approval() 重置。
_task_sandbox_approved: bool = False
_task_sandbox_denied: bool = False


def reset_task_sandbox_approval() -> None:
    """§85：任务开始时重置沙箱外操作审批状态（main.py run_turn 开头调用）。"""
    global _task_sandbox_approved, _task_sandbox_denied
    _task_sandbox_approved = False
    _task_sandbox_denied = False


def _ask_task_sandbox_approval() -> str:
    """§85：任务级一次确认——「是否允许执行所有沙箱外操作？」，返回 "approved" / "denied"。"""
    print("⚠️  当前任务涉及沙箱外操作，是否允许执行所有沙箱外操作？(y/n)")
    try:
        ans = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans in ("y", "yes"):
        return "approved"
    return "denied"


# ── §82 沙箱外操作的「沙箱内预演」验证 ──
# 用户在 §46 确认允许越界后，先把操作在沙箱内临时目录做一次无害化试执行：
#   - read_file / write_file：对沙箱内临时 probe 文件执行同操作；
#   - run_command：把命令中的越界绝对路径替换为沙箱内临时路径（并预建同名 probe 文件）
#     后在沙箱内执行（等价 --dry-run）。
# 模拟成功 → 提示「沙箱内验证通过，是否继续执行？」；失败 → 提示「沙箱内验证失败，操作
# 可能存在问题，是否继续？」；用户 y 执行真实操作，n 取消。
def _sandbox_dry_run(tool_name: str, arguments: dict) -> tuple[bool, str]:
    """沙箱内预演（§82）：返回 (是否成功, 结果文本)。绝不抛异常。"""
    import uuid as _uuid
    try:
        sandbox = _default_workdir()
        tmp = Path(sandbox) / f".dryrun-{_uuid.uuid4().hex[:10]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            if tool_name in ("read_file", "write_file"):
                path = str(arguments.get("path", "") or "")
                ext = Path(path).suffix or ".txt"
                probe = tmp / f"probe{ext}"
                if tool_name == "write_file":
                    r = _write_file(str(probe), str(arguments.get("content", "") or ""),
                                    allow_outside=True)
                    ok = "已写入" in r
                    return ok, f"[沙箱内预演] 写入沙箱临时文件{'成功' if ok else '失败'}: {r[:100]}"
                probe.write_text("probe\n", encoding="utf-8")
                r = _read_file(str(probe), allow_outside=True)
                ok = "[read_file]" in r
                return ok, f"[沙箱内预演] 读取沙箱临时文件{'成功' if ok else '失败'}: {r[:100]}"
            if tool_name == "run_command":
                command = str(arguments.get("command", "") or "")
                remapped = command
                for t in _outside_workspace_targets(tool_name, arguments):
                    t2 = tmp / Path(t).name
                    if not t2.exists():
                        t2.write_text("dry-run probe\n", encoding="utf-8")
                    remapped = remapped.replace(t, str(t2)).replace(t.lower(), str(t2))
                res = _run_command(remapped, cwd=str(tmp))
                ok = ("[run_command] OK" in res) and ("EXIT=" not in res)
                return ok, f"[沙箱内预演] 命令（路径替换后）执行{'成功' if ok else '失败'}：{res[:160]}"
            return True, "[沙箱内预演] 无越界目标，跳过模拟"
        finally:
            # §82：用 _rmtree（§57 手动回退版）清理临时目录——Windows 句柄占用时
            # shutil.rmtree 可能静默失败导致 .dryrun-* 残留
            _rmtree(tmp)
    except Exception as e:
        return False, f"[沙箱内预演] 异常：{e}"


# ── 一次性环境探测 + 静态注入（跨平台命令适配） ──
# 探测当前运行环境（操作系统 / 默认 shell / 跨平台命令映射），生成一份快照，
# 在启动时写入 .env-snapshot.json；之后每次组装系统提示词时把该快照格式化为
# 「环境信息」块注入，让模型按当前平台生成命令，避免 Windows 上调 ls、Linux
# 上调 dir 这类跨平台不适配问题。
SNAPSHOT_FILENAME = ".env-snapshot.json"


def _detect_shell() -> str:
    """根据环境变量推断默认 shell（cmd / powershell / bash / zsh / fish / sh）。"""
    if sys.platform == "win32":
        compsec = os.environ.get("ComSpec", "")
        if "powershell" in compsec.lower():
            return "powershell"
        return "cmd"
    shell = os.environ.get("SHELL", "")
    if shell:
        base = Path(shell).name.lower()
        if "zsh" in base:
            return "zsh"
        if "bash" in base:
            return "bash"
        if "fish" in base:
            return "fish"
        if base.endswith("sh"):
            return "sh"
    return "bash"


def get_environment_info() -> dict:
    """探测当前运行环境，返回快照 dict：os / shell / command_map。

    - os: 由 sys.platform 判定（Windows / Linux / Darwin）。
    - shell: 由 ComSpec（Windows）或 SHELL（类 Unix）判定。
    - command_map: 按操作系统预填默认命令（Windows 用 dir/move/del/mkdir/copy，
      否则用 ls -la/mv/rm/mkdir -p/cp）。
    """
    if sys.platform == "win32":
        os_name = "Windows"
    elif sys.platform == "darwin":
        os_name = "Darwin"
    else:
        os_name = "Linux"
    if os_name == "Windows":
        command_map = {"list": "dir", "move": "move", "remove": "del",
                       "mkdir": "mkdir", "copy": "copy"}
    else:
        command_map = {"list": "ls -la", "move": "mv", "remove": "rm",
                       "mkdir": "mkdir -p", "copy": "cp"}
    return {"os": os_name, "shell": _detect_shell(), "command_map": command_map}


def get_env_snapshot() -> dict:
    """读取（存在则）或生成（不存在则）`.env-snapshot.json` 环境快照。

    文件存在：直接读取，跳过探测；不存在：调用 get_environment_info() 生成并写盘。
    返回快照 dict；任何异常都回退到实时探测结果（保证调用方总能拿到环境信息）。
    """
    snap_path = PROJECT_ROOT / SNAPSHOT_FILENAME
    if snap_path.exists():
        try:
            return json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    snap = get_environment_info()
    try:
        snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except Exception:
        pass
    return snap


def format_env_block(snapshot: dict) -> str:
    """把环境快照格式化为系统提示词末尾的「环境信息」文本块。"""
    cm = snapshot.get("command_map", {})
    return (
        "## 当前运行环境\n"
        f"- 操作系统: {snapshot.get('os', '未知')}\n"
        f"- Shell: {snapshot.get('shell', '未知')}\n"
        f"- 命令映射: list={cm.get('list')}, move={cm.get('move')}, "
        f"remove={cm.get('remove')}, mkdir={cm.get('mkdir')}, copy={cm.get('copy')}\n"
        "请根据上述命令映射生成工具调用。不要使用该环境不支持的命令。"
    )


# ── 安全执行工作区初始化（§22） ──
def ensure_sandbox_root() -> str:
    """创建 用户数据目录 sandbox/ 工作区根目录（不存在则创建），返回其绝对路径字符串。

    所有未显式指定 cwd 的 run_command 都在此目录内执行；越界绝对路径或危险命令
    会被拦截（见 _run_command / _is_dangerous_command / _references_outside_sandbox）。
    """
    try:
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(SANDBOX_DIR)


def get_sandbox_root() -> str:
    """返回 用户数据目录 sandbox/ 工作区根目录绝对路径字符串。"""
    return str(SANDBOX_DIR)


# ── §33 任务二：主动清理副作用 ──
# 每次任务完成后（无论成功或失败）清理 用户数据目录 sandbox/ 内的临时 / 异常产物：
#   - 以 '-' 开头的目录（如 `-p`：命令误把选项当目录名创建）；
#   - 临时文件（*.tmp / *.log）；
#   - 清理后变为空的目录（保留目录 hello-sandbox / test-project 除外）。
# 清理动作记录到 logs/cleanup.log（追加，含时间戳）。
_PRESERVED_DIRS = {"hello-sandbox", "test-project"}   # 保留目录：不清空、不删除
_CLEANUP_LOG = LOG_DIR / "cleanup.log"


def _safe_list(d: Path) -> list:
    """安全列出目录条目名；失败返回空列表（不抛异常）。"""
    try:
        return os.listdir(d)
    except Exception:
        return []


def _rmtree(p: Path) -> None:
    """删除目录树（优先 shutil.rmtree，失败手动回退）。绝不抛异常。"""
    try:
        shutil.rmtree(p)
        return
    except Exception:
        pass
    # 手动回退删除
    try:
        for root, dirs, files in os.walk(str(p), topdown=False):
            for f in files:
                try:
                    os.unlink(os.path.join(root, f))
                except Exception:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except Exception:
                    pass
        os.rmdir(p)
    except Exception:
        pass


def _log_cleanup(removed: list) -> None:
    """把本次清理动作记录到 logs/cleanup.log（追加 JSON 行，含时间戳）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "removed": removed,
        }
        with open(_CLEANUP_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def cleanup_sandbox() -> dict:
    """清理 用户数据目录 sandbox/ 工作区内的临时 / 异常副作用产物（任务二）。

    清理规则：
      - 以 '-' 开头的目录整体删除（如 `-p`）；
      - 临时文件（*.tmp / *.log）删除；
      - 保留目录（_PRESERVED_DIRS）跳过，不递归、不删除；
      - 第二遍：删除清理后变为空的目录（保留目录除外）。
    返回 {"removed": [被删除路径列表]}；清理动作记录到 logs/cleanup.log。
    """
    removed: list[str] = []
    try:
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # 第一遍：删除以 '-' 开头的目录、临时文件；跳过保留目录
    for name in _safe_list(SANDBOX_DIR):
        p = SANDBOX_DIR / name
        try:
            if p.is_dir():
                if name.startswith("-"):
                    _rmtree(p)
                    removed.append(str(p))
                elif name in _PRESERVED_DIRS:
                    continue
            elif p.is_file():
                if name.endswith(".tmp") or name.endswith(".log"):
                    p.unlink()
                    removed.append(str(p))
        except Exception:
            continue
    # 第二遍：删除清理后变为空的目录（保留目录除外）
    for name in _safe_list(SANDBOX_DIR):
        p = SANDBOX_DIR / name
        if p.is_dir() and name not in _PRESERVED_DIRS:
            try:
                if not any(p.iterdir()):
                    os.rmdir(p)
                    removed.append(str(p))
            except Exception:
                continue
    _log_cleanup(removed)
    return {"removed": removed}


# ── §57 工作空间外残留目录清理 ──
# 每次任务完成后检查「工作空间外」的 fable5 残留产物（如桌面上的 .knowledge、fable5-demo、
# 用户指定的其他路径如 D:/fable5-test），经用户确认后清理。规则（§57.1）：
#   - 属于当前工作空间内的目录不清理；
#   - 属于项目根 / 用户数据目录（正式数据）内的不清理；
#   - 仅清理「已知残留模式」出现在工作空间外的候选（避免扫描整个磁盘）。
_STRAY_BASES = ["Desktop"]                       # 相对 home 的常见残留位置
_STRAY_NAMES = (".knowledge", ".memory", "fable5-demo", "fable5-test", "test-workspace")
_STRAY_EXTRA = ("D:/fable5-test", "D:/fable5-demo")  # 用户指定的其他工作空间路径（示例）


def _is_within_any(p: Path, roots: list) -> bool:
    """判断 p 是否位于 roots 中任一目录之内（含本身）。"""
    try:
        rp = os.path.normcase(str(p.resolve()))
    except Exception:
        rp = os.path.normcase(str(p))
    for root in roots:
        try:
            rr = os.path.normcase(str(root.resolve()))
        except Exception:
            rr = os.path.normcase(str(root))
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def find_stray_residue_dirs() -> list:
    """返回当前工作空间外的 fable5 残留目录列表（只探测，不删除）。

    候选来源：
      - 用户主目录 / 桌面下的已知残留模式（.knowledge、.memory、fable5-demo、fable5-test…）；
      - 用户指定的其他路径（D:/fable5-test 等）。
    过滤：属于当前工作空间 / 项目根 / 用户数据目录（正式数据）的不返回。
    """
    from .user_data import get_user_data_dir
    protected = [get_workspace_root(), Path(__file__).resolve().parents[2], get_user_data_dir()]
    cands: list = []
    home = Path.home()
    for base_name in _STRAY_BASES:
        base = home / base_name
        for name in _STRAY_NAMES:
            p = base / name
            if p.exists():
                cands.append(p)
    for extra in _STRAY_EXTRA:
        p = Path(extra)
        if p.exists():
            cands.append(p)
    return [str(p) for p in cands if not _is_within_any(p, protected)]


def clean_stray_dirs(paths) -> dict:
    """删除指定的工作空间外残留目录（由 main.py 在用户确认后调用）。

    返回 {"removed": [...], "failed": [...]}；删除动作记录到 logs/cleanup.log；绝不抛异常。
    """
    removed, failed = [], []
    for p in paths:
        pp = Path(p)
        try:
            if pp.is_dir():
                _rmtree(pp)
            elif pp.exists():
                pp.unlink()
            removed.append(str(pp))
        except Exception as e:
            failed.append(f"{pp}（{e}）")
    if removed:
        _log_cleanup(removed)
    return {"removed": removed, "failed": failed}


# ── 工具的 JSON Schema（Function Calling 格式） ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的完整文本内容，用于在给出方案/改动前了解现有实现。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件路径（相对项目根目录或绝对路径）",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入指定文件（覆盖写入）。用于落实 Act 阶段产生的改动。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的文件路径（相对项目根目录或绝对路径）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入文件的完整文本内容",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "执行一条系统命令（如编译、运行测试、查看目录、移动文件）。安全执行后端：默认在 用户数据目录 sandbox/ 受限工作区内执行；危险命令（del /F、rm -rf、chmod 777、curl/wget、set/export、format/fdisk/dd、删系统目录等）与越界绝对路径（如 C:\\、/）会被拦截并返回警告。只读命令（ls/dir/cat/pwd/echo/git status 等）直接执行、无需确认；写/改/删命令（rm/del/mkdir/mv/cp 及含 > 重定向）同样直接执行（§30 起不再请求用户确认），由沙箱安全策略拦截危险/越界命令。可用 cwd 指定命令执行目录（覆盖默认 用户数据目录 sandbox/）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令字符串",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "可选：命令执行的工作目录。不传则在用户数据目录的 sandbox/ 沙箱根目录执行；传入相对路径（如 'archive'、'./sub'）会自动解析到该沙箱下的对应子目录，而非项目根目录。例如整理文件时可指定 cwd='archive' 让命令在 sandbox/archive 内运行。",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_environment_info",
            "description": "探测当前运行环境（操作系统 / 默认 shell / 跨平台命令映射 list/move/remove/mkdir/copy），返回环境快照。只读、无需确认。当你要在本机执行命令、但不确定某个命令在当前平台是否可用（如分不清 dir 与 ls）时调用，以选择正确的命令。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _log(event: str, detail: dict) -> None:
    """把工具调用事件追加写入 logs/tools.log（JSON 行，含时间戳）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **detail,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 日志失败绝不影响主流程
        pass


def _read_file(path: str, allow_outside: bool = False) -> str:
    """读文件：委托给 SandboxExecutor（§48：默认根为工具默认工作目录，拒绝 .. / 绝对路径；
    §82：allow_outside=True 时允许读沙箱外绝对路径（execute_tool §46 确认 + 预演通过）。"""
    try:
        res = _sandbox_for_tool().read_file(path, allow_outside=allow_outside)
    except (OSError, MemoryError) as e:
        # §30 用户介入（四）：沙箱资源不足 -> 转换为可捕获的专用异常
        raise SandboxResourceError(f"沙箱读取失败（资源不足）：{e}") from e
    if not res["success"]:
        # 任务一：结构化错误返回（error_type 按 stderr 区分 file_not_found / permission_denied）
        return _error_result(
            _classify_file_error(res.get("stderr", "")),
            f"读取文件失败：{res.get('stderr', '文件不存在或无法读取')}（路径：{path}）",
        )
    text = res["content"]
    return f"[read_file] 文件 {res['path']}（{len(text)} 字符）:\n{text}"
def _write_file(path: str, content: str, allow_outside: bool = False) -> str:
    """写文件：委托给 SandboxExecutor（§48：默认根为工具默认工作目录，拒绝 .. / 绝对路径；
    §82：allow_outside=True 时允许写沙箱外绝对路径（execute_tool §46 确认 + 预演通过）。"""
    try:
        res = _sandbox_for_tool().write_file(path, content, allow_outside=allow_outside)
    except (OSError, MemoryError) as e:
        # §30 用户介入（四）：沙箱资源不足 -> 转换为可捕获的专用异常
        raise SandboxResourceError(f"沙箱写入失败（资源不足）：{e}") from e
    if not res["success"]:
        # 任务一：结构化错误返回（error_type 按 stderr 区分 permission_denied / file_not_found）
        return _error_result(
            _classify_write_error(res.get("stderr", "")),
            f"写入文件失败：{res.get('stderr', '无法写入')}（路径：{path}）",
        )
    return f"[write_file] 已写入 {res['bytes_written']} 字符到 {res['path']}"

# ── 只读命令白名单（§24 修复补全：沙箱迁移时遗漏定义，导致 _classify_command 抛 NameError） ──
# 仅做「查看 / 列举」、不产生写/改/删副作用的命令归为 readonly，execute_tool 据此跳过用户确认。
# 注意：含写重定向（> / >> / 2>）或管道写（| tee）的命令即便以只读命令开头，也已在上方
# 重定向判定中先行归为 write，不会误判为 readonly。
_READONLY_COMMANDS = {
    "dir", "ls", "cat", "type", "pwd", "echo", "head", "tail", "more", "less",
    "find", "grep", "which", "where", "ps", "tasklist", "hostname", "whoami",
    "ver", "ipconfig", "systeminfo", "date", "time", "env", "printenv", "wc",
    "sort", "uniq", "nl", "tree",
}

# git 只读子命令白名单：仅当 git 的第二个 token 在此集合内时归为 readonly（如 git push 仍归 write）。
_READONLY_GIT_SUBCOMMANDS = {
    "status", "log", "show", "diff", "branch", "remote", "tag", "stash",
    "ls-files", "ls-remote", "cat-file", "rev-parse", "rev-list", "grep",
    "blame", "reflog", "shortlog", "describe", "whatchanged",
}

def _classify_command(command: str, cwd: str | None = None, allow_outside: bool = False) -> str:
    """按命令的「实际行为」分类，决定是否需要用户确认。

    返回：
      "blocked"  —— 命中危险命令或试图访问沙箱（用户数据目录 sandbox/）之外，禁止执行
      "readonly" —— 只读命令，不触发确认提示，直接执行
      "write"    —— 写 / 改 / 删行为，触发确认提示

    判定优先级：
      1) 危险命令命中 -> blocked（del /F、rm -rf、chmod 777、curl、set/export、format/fdisk/dd 等）
      2) 显式引用工作区外绝对路径 -> blocked（如 `dir C:\\`、`ls -la /`）；
         当 allow_outside=True（§81：execute_tool §46 确认通过）时跳过越界拦截，危险命令仍拦
      3) 含写重定向（> / >> / 2>）或管道写（| tee）-> write
      4) 首个管道段解析出命令：
         - git <只读子命令> -> readonly
         - 命令在只读白名单 -> readonly
         - 其余 -> write
    说明：以「行为」而非单纯命令名判断——例如 `echo hello > file.txt` 因含 `>`
    被归为 write；`cat a.txt | grep x` 的 cat 属只读，整体也归 readonly。
    """
    low = command.lower().strip().strip('"').strip("'")
    if not low:
        return "write"
    # 1) 危险命令拦截优先
    danger, _ = _is_dangerous_command(command)
    if danger:
        return "blocked"
    # 2) 工作区越界（显式引用 用户数据目录 sandbox/ 之外的绝对路径）；§81：确认通过后放行
    esc, _ = _references_outside_sandbox(command, cwd=cwd)
    if esc and not allow_outside:
        return "blocked"
    # 3) 写重定向 / 管道写 -> write
    if ">>" in command or " 2>" in command or command.rstrip().endswith(">"):
        return "write"
    if "> " in command or command.strip().startswith(">") or " >" in command:
        return "write"
    if "| tee" in low or "|tee" in low:
        return "write"
    # 3) 取首个管道段做命令分类（管道后的只读检测只看第一段行为）
    head = command.split("|")[0].strip()
    tokens = head.split()
    if not tokens:
        return "write"
    base = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base == "git" and len(tokens) >= 2:
        sub = tokens[1].lower().lstrip("-")
        return "readonly" if sub in _READONLY_GIT_SUBCOMMANDS else "write"
    if base in _READONLY_COMMANDS:
        return "readonly"
    return "write"


def _run_command(command: str, cwd: str | None = None, allow_outside: bool = False) -> str:
    """执行命令：委托给 SandboxExecutor（microsandbox 优先，不可用时回退本地安全执行）。
    危险命令 / 工作区越界会被拦截并返回 [拦截] 警告（§81：allow_outside=True 时跳过越界拦截，
    由 execute_tool §46 确认通过后传入；危险命令仍始终拦截）。

    §43 / §48：当 cwd 为相对路径时，自动拼接在工具默认工作目录（显式切换后为新工作空间，
    否则为用户数据目录 sandbox/）下，避免相对于进程 cwd（项目根目录）解析；
    未指定 cwd 时由执行器默认使用该工作目录。
    """
    # §43/§48：相对 cwd 解析到工具默认工作目录（_default_workdir()）
    if cwd:
        _cwdp = Path(cwd)
        if not _cwdp.is_absolute():
            cwd = str((_default_workdir() / cwd).resolve())
    try:
        res = _sandbox_for_tool().execute(command, cwd=cwd, allow_outside=allow_outside)
    except (OSError, MemoryError) as e:
        # §30 用户介入（四）：沙箱资源不足 -> 转换为可捕获的专用异常
        raise SandboxResourceError(f"沙箱命令执行失败（资源不足）：{e}") from e
    if res.get("blocked"):
        return f"[拦截] {res.get('stderr', '命令被安全策略拦截')}\n命令：`{command}`"
    if res.get("success"):
        out = (res.get("stdout") or "") + (res.get("stderr") or "")
        exec_cwd = res.get("cwd") or (cwd or str(_default_workdir()))
        if len(out) > 16000:
            out = out[:16000] + "\n...[输出过长已截断]..."
        code = res.get("return_code")
        status = "OK" if code == 0 else f"EXIT={code}"
        return f"[run_command] {status}（在 {exec_cwd} 执行 `{command}`）:\n{out}"
    # 任务一：命令执行失败 -> 结构化 error（command_failed）。
    # 注：被安全策略拦截的情况由上方 res.get("blocked") 分支返回 [拦截] 文案（属安全拦截，非工具失败）。
    return _error_result("command_failed", f"命令执行失败：{res.get('stderr', '')}")
def execute_tool(tool_name: str, arguments: dict, user_input: str | None = None,
                 plan: str | None = None) -> str:
    """按工具名分发执行，返回字符串结果（成功或错误信息）。

    §53：调用沙箱执行前，先经 Rubric 行为验证观测层（check_tool_call）检查本次
    工具调用——ToolCallAccuracy（工具名 / 必需参数与预期行为一致性）、
    TraceQuality（调用顺序 / 冗余合理性）。Rubric 当前为观测层：检查不通过
    只记录警告并打印 [Rubric] 结果，不拦截执行。
    user_input / plan 参数为兼容旧调用保留，Rubric 观测层不再使用。

    arguments: 模型给出的参数字典。缺参 / 异常都会被捕获并以友好文案返回，
    不会让整个 Agent 循环崩溃。
    """
    global _task_sandbox_approved, _task_sandbox_denied  # §85：任务级审批状态
    arguments = arguments or {}
    print(f"\n[工具调用] {tool_name}({arguments})")

    # ── §53 Rubric 行为验证（执行前；观测层，不拦截）──
    _rubric_check(tool_name, arguments)

    # ── §46/§82/§84/§85 工作空间外操作拦截（文件操作执行前）──
    # 检查 read_file / write_file / run_command 的目标路径是否在当前工作空间根目录内；
    # §85：任务级一次确认——首次越界时自动预演（不提示）并询问「是否允许执行所有沙箱外
    # 操作？」；y 批准本任务所有沙箱外操作（后续直接执行不再提示），n 拒绝（当前操作取消，
    # 本任务不再询问）。危险命令仍由沙箱拦截。
    _allowed_outside = False
    if tool_name in ("read_file", "write_file", "run_command"):
        _outside = _outside_workspace_targets(tool_name, arguments)
        if _outside:
            print(f"[工作空间] 当前工作空间根目录：{get_workspace_root()}")
            if _task_sandbox_approved:
                _allowed_outside = True  # 本任务已批准：直接执行，不再提示
            elif _task_sandbox_denied:
                _cancelled = ("[已取消] 本任务已拒绝沙箱外操作（任务级审批，不再询问）："
                              + "; ".join(_outside))
                print(_cancelled)
                _log("tool_cancelled",
                     {"tool": tool_name, "args": arguments,
                      "reason": "task_sandbox_denied", "targets": _outside})
                return _cancelled
            else:
                _dry_ok, _dry_msg = _sandbox_dry_run(tool_name, arguments)  # 自动预演
                for t in _outside:
                    print(f"[警告] 操作目标在工作空间外：{t}")
                _verdict = "已通过" if _dry_ok else "失败"
                if _ask_task_sandbox_approval() == "approved":
                    _task_sandbox_approved = True
                    _allowed_outside = True
                    print(f"[确认] 已允许本任务的所有沙箱外操作（沙箱内验证{_verdict}）。")
                else:
                    _task_sandbox_denied = True
                    _cancelled = ("[已取消] 用户拒绝沙箱外操作（任务级审批，本任务不再询问）"
                                  f"（沙箱内验证{_verdict}）：" + "; ".join(_outside))
                    print(_cancelled)
                    _log("tool_cancelled",
                         {"tool": tool_name, "args": arguments,
                          "reason": "task_sandbox_denied", "targets": _outside,
                          "dryrun_ok": _dry_ok, "dryrun_msg": _dry_msg})
                    return _cancelled

    try:
        if tool_name == "read_file":
            path = arguments.get("path")
            if not path:
                return "[错误] 缺少参数 path"
            # §82：§46 确认 + 预演通过（_allowed_outside）后允许读取沙箱外绝对路径
            result = _read_file(path, allow_outside=_allowed_outside)
        elif tool_name == "write_file":
            # 写文件：直接执行，不再请求用户确认（§30 移除思考层工具调用确认逻辑）
            path = arguments.get("path")
            content = arguments.get("content", "")
            if not path:
                return "[错误] 缺少参数 path"
            # §82：§46 确认 + 预演通过（_allowed_outside）后允许写入沙箱外绝对路径
            result = _write_file(path, content, allow_outside=_allowed_outside)
        elif tool_name == "run_command":
            command = arguments.get("command")
            # cwd：可选工作目录。调用者（模型或 main.py）可指定命令执行目录；
            # 不传则 execute_tool 透传 None，_run_command 回退到 用户数据目录 sandbox/ 工作区。
            cwd = arguments.get("cwd")
            if not command:
                return "[错误] 缺少参数 command"
            # 按命令实际行为分类（readonly / write / blocked），但无论哪类都【直接执行】，
            # 不再请求用户确认（§30 移除思考层工具调用确认逻辑）。
            # 危险命令 / 工作区越界命令由沙箱安全策略在 _run_command 内拦截并返回 [拦截] 文案。
            # §81：§46 确认通过（_allowed_outside）后越界命令可执行（危险命令仍拦）。
            cls = _classify_command(command, cwd=cwd, allow_outside=_allowed_outside)
            if cls == "readonly":
                print(f"[只读命令] 直接执行（无需确认）：{command}"
                      + (f"  (cwd={cwd})" if cwd else ""))
            else:
                print(f"[执行] run_command（{cls}）：{command}"
                      + (f"  (cwd={cwd})" if cwd else ""))
            result = _run_command(command, cwd=cwd, allow_outside=_allowed_outside)
        elif tool_name == "get_environment_info":
            # 只读、无需确认：返回当前环境快照（格式化文本）
            snap = get_environment_info()
            result = format_env_block(snap)
        else:
            return f"[错误] 未知工具：{tool_name}"
    except Exception as e:
        result = f"[错误] 工具 {tool_name} 执行异常：{e}"

    # 打印并落盘记录（[错误] / [拦截] / 结构化 error JSON 均记为 error）
    status = "error" if _is_error_result(result) else "ok"
    snippet = result[:300].replace("\n", " ")
    print(f"[工具结果] {tool_name} -> {status}: {snippet}")
    _log("tool_exec",
         {"tool": tool_name, "args": arguments,
          "status": status, "result": result[:2000]})
    return result
