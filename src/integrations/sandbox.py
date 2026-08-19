"""SandboxExecutor —— Fable 5 的安全执行后端（§23）。

集成 microsandbox（基于 microVM 的硬件级隔离）作为优先后端；当 microsandbox 未安装，
或当前环境无法启动 microVM（缺少 KVM / Hypervisor Platform / Apple Silicon）时，自动
回退到受控的本地执行（subprocess + 危险命令拦截 + 工作区边界限制），保证功能与安全性。

所有命令执行与文件操作都限制在用户数据目录的 sandbox/ 工作目录内（§44 起，从项目根 ./sandbox 迁移）：
  - 危险命令（强制/递归删除、权限变更、网络请求、环境变量修改、磁盘操作、关机等）被拦截；
  - 显式引用工作区之外绝对路径的命令被拦截；
  - 文件读写拒绝路径遍历（含 ".." 或绝对路径）。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_within(path: Path, root: Path) -> bool:
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    return p == r or p.startswith(r + os.sep)


_ABS_WIN = re.compile(r"[a-z]:[\\/][^|;&]*]")


def _is_dangerous_command(command: str) -> tuple:
    low = command.lower().strip()
    tokens = re.split(r"[\s|;&`]+", low)
    first = tokens[0] if tokens else ""
    _RM = "rm"
    _DEL = "del"
    _patterns = [
        _RM + " -rf", _RM + " -fr", _RM + " -r ",
        "rmdir" + " /s", "rd" + " /s",
        _DEL + " /f", _DEL + " /s",
        "deltree", "srm", "shred",
    ]
    for pat in _patterns:
        if pat in low:
            return True, f"破坏性/递归删除命令被禁止（命中片段 '{pat}'）"
    if re.search(r"\bchmod\b", low):
        if re.search(r"77" + r"7|00" + r"0|66" + r"6|-r\b|\br\b\s", low) or re.search(r"chmod\s+\S*\s*/", low):
            return True, "危险的权限变更（chmod 危险模式 / 修改根或系统目录权限）被禁止"
    if re.search(r"\b(chown|chattr|takeown|icacls)\b", low):
        return True, "文件/目录所有权或 ACL 变更（chown/chattr/takeown/icacls）被禁止"
    if re.search(r"\b(curl|wget|aria2c|iwr)\b", low):
        return True, "网络请求命令（curl/wget/aria2c/iwr 等）被禁止"
    if re.search(r"\b(nc|netcat|telnet)\b", low):
        return True, "裸 socket/远程连接命令（nc/netcat/telnet）被禁止"
    if first in ("set", "setx", "export") or low.startswith(("set ", "export ")):
        return True, "环境变量修改（set/export/setx）被禁止"
    if "$env:" in low:
        return True, "环境变量赋值（$env:）被禁止"
    for tok in tokens:
        base = tok.split(".", 1)[0].split("=", 1)[0]
        if base in ("format", "fdisk", "mkfs", "diskpart", "parted"):
            return True, "磁盘格式化/分区命令（format/fdisk/mkfs/diskpart/parted）被禁止"
    if re.search(r"\bdd\b", low):
        return True, "dd 原始磁盘写入命令被禁止"
    for pat in ("shutdown", "reboot"):
        if pat == tokens[0] or (" " + pat + " ") in (" " + low + " "):
            return True, f"系统关机/重启命令被禁止（{pat}）"
    return False, ""


def _references_outside(command: str, root: Path) -> tuple:
    try:
        root_res = root.resolve()
    except Exception:
        root_res = root
    for m in _ABS_WIN.finditer(command.lower()):
        try:
            pres = Path(m.group(0)).resolve()
        except Exception:
            continue
        if not _is_within(pres, root_res):
            return True, m.group(0)
    # §81：环境变量绝对路径（%USERPROFILE% 等，展开后为盘符绝对路径）——兜底拦截
    for env_path in _abs_env_paths(command):
        try:
            pres = Path(env_path).resolve()
        except Exception:
            continue
        if not _is_within(pres, root_res):
            return True, env_path
    for tok in re.split(r"[\s|;&`\"']+", command):
        # 跳过命令开关（如 `dir /b`、`rm /s` 的 /b、/s）：仅当 / 之后还含路径分隔符
        # （/ 或 \）或本身是根路径时才视为路径，避免把单段开关 /b 误判为绝对路径 /b。
        if tok.startswith("/"):
            _rest = tok[1:]
            if "/" not in _rest and "\\" not in _rest:
                continue  # 形如 /b、/s、/q 的开关，不是路径
            try:
                pres = Path(tok).resolve()
            except Exception:
                continue
            if not _is_within(pres, root_res):
                return True, tok
    return False, ""


def _abs_env_paths(command: str) -> list:
    """提取命令中的环境变量绝对路径（%USERPROFILE% / %APPDATA% 等，展开后为绝对路径）。§81"""
    found: list = []
    for m in re.finditer(r"%([A-Za-z_][A-Za-z0-9_]*)%", command):
        v = os.environ.get(m.group(1))
        if v and os.path.isabs(v):
            found.append(v)
    return found


class SandboxExecutor:
    def __init__(self, workdir: str | None = None):
        # §44：workdir 未显式传入时，回退到用户数据目录的 sandbox/（而非项目根）
        from .user_data import get_sandbox_dir
        self.workdir = Path(workdir or get_sandbox_dir()).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            import microsandbox
            self.backend = "microsandbox"
        except Exception:
            self.backend = "local"

    def is_dangerous(self, command: str) -> tuple:
        return _is_dangerous_command(command)

    def references_outside(self, command: str, cwd: str | None = None) -> tuple:
        return _references_outside(command, self.workdir)

    def _resolve_path(self, path: str, allow_outside: bool = False) -> tuple:
        if path is None:
            return None, "路径为空"
        norm = path.replace("\\", "/")
        if ".." in norm.split("/"):
            return None, f"路径遍历被拒绝（含 '..'）：{path}"
        if os.path.isabs(path):
            # §82：execute_tool §46 确认 + 沙箱内预演通过后，允许读写沙箱外绝对路径
            if allow_outside:
                return Path(path).resolve(), None
            return None, f"绝对路径被拒绝（沙箱仅允许相对路径）：{path}"
        target = (self.workdir / path).resolve()
        if target != self.workdir and not _is_within(target, self.workdir):
            return None, f"路径越出沙箱工作目录被拒绝：{path}"
        return target, None

    def execute(self, command: str, cwd: str | None = None,
                allow_outside: bool = False) -> dict:
        danger, why = _is_dangerous_command(command)
        if danger:
            return {"success": False, "stdout": "", "stderr": why,
                    "return_code": -1, "blocked": True, "cwd": None}
        esc, where = _references_outside(command, self.workdir)
        if esc and not allow_outside:  # §81：allow_outside 由调用方（execute_tool §46 确认通过）传入
            return {"success": False, "stdout": "", "stderr": f"命令试图访问沙箱之外的路径：{where}",
                    "return_code": -1, "blocked": True, "cwd": None}
        if cwd:
            cwd_path = Path(cwd)
            # §43：相对 cwd 解析到沙箱根目录（避免相对于进程 cwd=项目根解析），
            # 与调用方 tools._run_command 的解析保持一致，作为防御式兜底。
            if not cwd_path.is_absolute():
                cwd_path = (self.workdir / cwd).resolve()
            if not cwd_path.exists() or not cwd_path.is_dir():
                return {"success": False, "stdout": "", "stderr": f"指定的工作目录无效：{cwd}",
                        "return_code": -1, "blocked": True, "cwd": None}
            exec_cwd = cwd_path.resolve()
            if exec_cwd != self.workdir and not _is_within(exec_cwd, self.workdir):
                return {"success": False, "stdout": "", "stderr": f"cwd 越出沙箱工作目录被拒绝：{cwd}",
                        "return_code": -1, "blocked": True, "cwd": None}
        else:
            exec_cwd = self.workdir
        if self.backend == "microsandbox":
            try:
                return self._exec_microsandbox(command, str(exec_cwd))
            except Exception:
                pass
        return self._exec_local(command, str(exec_cwd))

    def _exec_local(self, command: str, exec_cwd: str) -> dict:
        try:
            proc = subprocess.run(command, shell=True, cwd=exec_cwd,
                                   capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": f"命令执行超时（>120s）：{command}",
                    "return_code": -1, "blocked": False, "cwd": exec_cwd}
        except (OSError, MemoryError):
            # §30 用户介入（四）：资源异常（磁盘满 / 内存不足 / 进程崩溃）上抛，
            # 由 tools.py 转换为 SandboxResourceError 供 main.py 捕获并终止任务。
            raise
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": f"命令执行失败：{e}",
                    "return_code": -1, "blocked": False, "cwd": exec_cwd}
        return {"success": proc.returncode == 0, "stdout": proc.stdout or "",
                "stderr": proc.stderr or "", "return_code": proc.returncode,
                "blocked": False, "cwd": exec_cwd}

    def _exec_microsandbox(self, command: str, exec_cwd: str) -> dict:
        import asyncio
        import microsandbox
        async def _run():
            sandbox = await microsandbox.Sandbox.create(
                "fable5", image="python", cpus=1, memory=256,
            )
            try:
                out = await sandbox.exec("sh", ["-c", command])
                stdout = getattr(out, "stdout_text", "") or getattr(out, "stdout", "") or ""
                stderr = getattr(out, "stderr_text", "") or getattr(out, "stderr", "") or ""
                code = getattr(out, "exit_code", None)
                if code is None:
                    code = getattr(out, "return_code", 0) or 0
                return {"success": code == 0, "stdout": stdout, "stderr": stderr,
                        "return_code": code, "blocked": False, "cwd": exec_cwd}
            finally:
                await sandbox.stop()
        return asyncio.run(_run())

    def write_file(self, path: str, content: str, allow_outside: bool = False) -> dict:
        target, err = self._resolve_path(path, allow_outside=allow_outside)
        if err:
            return {"success": False, "content": "", "path": path,
                    "bytes_written": 0, "stderr": err}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, MemoryError):
            # §30 用户介入（四）：资源异常上抛（见 _exec_local 说明）
            raise
        except PermissionError:
            return {"success": False, "content": "", "path": str(target),
                    "bytes_written": 0, "stderr": f"权限不足，无法写入：{target}"}
        except Exception as e:
            return {"success": False, "content": "", "path": str(target),
                    "bytes_written": 0, "stderr": f"写入失败：{e}"}
        return {"success": True, "content": "", "path": str(target),
                "bytes_written": len(content), "stderr": ""}

    def read_file(self, path: str, allow_outside: bool = False) -> dict:
        target, err = self._resolve_path(path, allow_outside=allow_outside)
        if err:
            return {"success": False, "content": "", "path": path, "stderr": err}
        if not target.exists():
            return {"success": False, "content": "", "path": str(target), "stderr": f"文件不存在：{target}"}
        if not target.is_file():
            return {"success": False, "content": "", "path": str(target), "stderr": f"路径不是文件：{target}"}
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, MemoryError):
            # §30 用户介入（四）：资源异常上抛（见 _exec_local 说明）
            raise
        except PermissionError:
            return {"success": False, "content": "", "path": str(target), "stderr": f"权限不足，无法读取：{target}"}
        except Exception as e:
            return {"success": False, "content": "", "path": str(target), "stderr": f"读取失败：{e}"}
        if len(text) > 16000:
            text = text[:16000] + "\n...[内容过长已截断]..."
        return {"success": True, "content": text, "path": str(target),
                "bytes_written": 0, "stderr": ""}
