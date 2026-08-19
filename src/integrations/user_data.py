"""用户数据目录管理 — src/integrations/user_data.py

将 fable5 的用户数据（配置 / 技能树 / 记忆 / 沙箱）与项目代码分离，
统一存放在跨平台的用户数据目录下：

  Windows : %APPDATA%/fable5            (通常 C:/Users/<user>/AppData/Roaming/fable5)
  Linux   : ~/.local/share/fable5
  macOS   : ~/.local/share/fable5

包含四个子目录：
  config/   配置（config.yaml：api_key、model 等）
  skills/   技能树
  memory/   记忆日志
  sandbox/  沙箱工作目录

所有子目录在首次访问时自动创建。config.yaml 的读写带 mtime 缓存，
支持「API Key 配置向导」在运行时写入后立即可被模型层读取。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


# ── 自然语言路径别名映射（§48：支持「桌面/下载/文档/项目」等中文描述切换工作空间）──
# 键为用户输入的自然语言描述，值为解析后的真实绝对路径。
PATH_ALIASES = {
    "桌面": os.path.expanduser("~/Desktop"),
    "下载": os.path.expanduser("~/Downloads"),
    "文档": os.path.expanduser("~/Documents"),
    "项目": os.path.expanduser("~/Projects"),
}

# 标准路径判定：盘符开头（如 C:\ D:/ E:foo）
_IS_STANDARD_PATH_RE = re.compile(r"^[A-Za-z]:")


def resolve_path_alias(text: str) -> str | None:
    """将自然语言路径描述解析为真实路径。

    - 标准路径（以盘符或 / 开头）原样返回；
    - 别名（如「桌面」）返回映射的真实路径；
    - 无法识别的描述返回 None（调用方据此提示「未识别的路径描述」）。
    """
    text = (text or "").strip()
    if not text:
        return None
    # 标准路径：盘符（如 C:\、D:/）或 / 开头 -> 直接使用
    if text.startswith("/") or _IS_STANDARD_PATH_RE.match(text):
        return text
    # 别名 -> 查表替换
    return PATH_ALIASES.get(text)


def get_user_data_dir() -> Path:
    """返回 fable5 用户数据根目录（跨平台解析）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            base = Path.home() / "AppData" / "Roaming"
        return Path(base) / "fable5"
    return Path.home() / ".local" / "share" / "fable5"


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_config_dir() -> Path:
    """配置目录：<user_data>/config（自动创建）。"""
    return _ensure(get_user_data_dir() / "config")


def get_skills_dir() -> Path:
    """技能树目录：<user_data>/skills（自动创建）。"""
    return _ensure(get_user_data_dir() / "skills")


def get_memory_dir() -> Path:
    """记忆日志目录：<user_data>/memory（自动创建）。"""
    return _ensure(get_user_data_dir() / "memory")


def get_sandbox_dir() -> Path:
    """沙箱工作目录：<user_data>/sandbox（自动创建）。"""
    return _ensure(get_user_data_dir() / "sandbox")


def ensure_user_data_dirs() -> Path:
    """创建用户数据根目录及其四个子目录，返回根目录路径。"""
    get_config_dir()
    get_skills_dir()
    get_memory_dir()
    get_sandbox_dir()
    return get_user_data_dir()


def get_config_file() -> Path:
    """config.yaml 完整路径：<user_data>/config/config.yaml。"""
    return get_config_dir() / "config.yaml"


# ── config.yaml 读写（带 mtime 缓存，写后立即可被模型层读取）──
_CONFIG_CACHE: dict | None = None
_CONFIG_MTIME: float | None = None


def load_config() -> dict:
    """读取 config.yaml；不存在或解析失败返回空 dict。结果按 mtime 缓存。

    加固（§49）：内容损坏 / 非键值映射（如标量、列表）时强制回退空 dict 并在
    stderr 打印明确提示，避免调用方 `cfg.get(...)` 抛 AttributeError 崩溃。
    """
    global _CONFIG_CACHE, _CONFIG_MTIME
    p = get_config_file()
    if not p.exists():
        _CONFIG_CACHE = {}
        _CONFIG_MTIME = None
        return _CONFIG_CACHE
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _CONFIG_CACHE = {}
        _CONFIG_MTIME = None
        return _CONFIG_CACHE
    if _CONFIG_CACHE is not None and _CONFIG_MTIME == mtime:
        return _CONFIG_CACHE
    try:
        import yaml
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else {}
    except ImportError:
        data = {}
        print("[config] pyyaml 不可用，config.yaml 读取降级为空配置。", file=sys.stderr)
    except Exception as e:
        data = {}
        print(f"[config] config.yaml 解析失败，按空配置处理：{e}", file=sys.stderr)
    if not isinstance(data, dict):
        print(f"[config] config.yaml 内容不是键值映射（{type(data).__name__}），按空配置处理。",
              file=sys.stderr)
        data = {}
    _CONFIG_CACHE = data or {}
    _CONFIG_MTIME = mtime
    return _CONFIG_CACHE


def _dump_yaml_text(cfg: dict) -> str:
    """把配置序列化为 YAML 文本。

    pyyaml 可用时用 safe_dump（保留中文）；pyyaml 缺失时零依赖手写 `key: value` 行
    （标量 bool/int/float 原样，其余加双引号），保证 api_key 等核心配置始终可持久化。
    """
    try:
        import yaml
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except ImportError:
        lines = []
        for k, v in cfg.items():
            if isinstance(v, bool):
                lines.append(f"{k}: {str(v).lower()}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k}: {v}")
            else:
                lines.append(f"{k}: \"{str(v).replace(chr(34), chr(39))}\"")
        return "\n".join(lines) + "\n"


def save_config(cfg: dict) -> None:
    """写入 config.yaml（保留中文），并更新内存缓存（写后立即可读）。

    写入失败时抛出含完整路径信息的异常（不静默），由调用方（如配置向导）
    捕获并给出明确提示。
    """
    p = get_config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(_dump_yaml_text(cfg), encoding="utf-8")
    except OSError as e:
        raise OSError(f"写入 {p} 失败：{e}") from e
    global _CONFIG_CACHE, _CONFIG_MTIME
    _CONFIG_CACHE = dict(cfg)
    try:
        _CONFIG_MTIME = p.stat().st_mtime
    except OSError:
        _CONFIG_MTIME = None
