"""V4 flash API 封装 — src/integrations/llm.py

统一的 LLM 调用层。从环境变量 / .env 读取配置，调用 OpenAI 兼容的
chat/completions 接口（默认指向 DeepSeek 兼容地址，可按需替换为真实地址）。

配置（通过 .env 或环境变量）：
  V4_API_KEY   必填，API 密钥
  V4_API_URL   选填，chat completions 地址（默认 DeepSeek 兼容，请替换为实际地址）
  V4_MODEL     选填，call_llm 未显式指定模型时的默认模型（默认 deepseek-v4-flash）

对外接口：
  call_llm(messages: list, system: str = "") -> str
  RealModel  —— 实现 fable_cycle 需要的 .think / .act / .prove 接口
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 路由层模型注册表（集中管理可用模型，方便开发者接入自己的 API）
from src.config.models import AVAILABLE_MODELS
# 工具注册表（Function Calling）：TOOLS 传给模型，execute_tool 执行模型选定的工具
from src.integrations.tools import TOOLS, execute_tool, get_env_snapshot, format_env_block

# ── .env 加载（零依赖：优先 python-dotenv，失败则自写最小解析） ──
def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        return
    except Exception:
        pass
    # 向上查找 .env：项目根 (fable5-lite/) 或当前目录
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    cur = here
    for _ in range(6):
        candidates.append(os.path.join(cur, ".env"))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    candidates.append(os.path.join(os.getcwd(), ".env"))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break


_load_dotenv()


# ── 融合系统提示词加载（src/prompts/system_prompt_merged.md）──
# 该文件由 Fable 5 通用系统提示词（src/prompts/system_prompt.md，已备份为
# fable5_system.md.bak）与 DeepSeek V4 增强（docs/prompts/deepseek_v4_enhance.md，
# 已备份为 deepseek_v4_enhance.md.bak）融合而成；作为各阶段调用的基础 system 提示词，
# 阶段专属指令（think/act/prove 的 JSON 格式要求）会拼接在其后。
_PROMPTS_PATH = Path(__file__).resolve().parents[2] / "src" / "prompts" / "system_prompt_merged.md"
_SYSTEM_PROMPT_CACHE: str | None = None


def load_system_prompt() -> str:
    """读取 src/prompts/system_prompt_merged.md 作为融合系统提示词（缓存一次）。

    失败（文件缺失）时返回空串并告警，不中断程序；此时各阶段退化为仅使用
    阶段专属指令。
    """
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is not None:
        return _SYSTEM_PROMPT_CACHE
    try:
        with open(_PROMPTS_PATH, encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        print(f"[llm] 未找到系统提示词文件 {_PROMPTS_PATH}，将退化为空系统提示词。", file=sys.stderr)
        content = ""
    _SYSTEM_PROMPT_CACHE = content
    return content


# ── 阶段补充提示词加载（src/prompts/{name}.md）──
# 用于加载 Act 等阶段的专属补充指令（如「直接执行、不要先列举目录」），
# 与融合系统提示词（system_prompt_merged.md）叠加，约束模型行为。
_STAGE_PROMPT_CACHE: dict[str, str] = {}


def load_stage_prompt(name: str) -> str:
    """读取 src/prompts/{name}.md 作为阶段补充提示词（按名缓存）。

    失败（文件缺失）时返回空串，不中断程序；调用方应把返回值拼接到
    阶段专属指令后（缺失则退化为原指令）。
    """
    if name in _STAGE_PROMPT_CACHE:
        return _STAGE_PROMPT_CACHE[name]
    path = Path(__file__).resolve().parents[2] / "src" / "prompts" / f"{name}.md"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        content = ""
    _STAGE_PROMPT_CACHE[name] = content
    return content


from src.integrations.user_data import load_config


def get_api_key() -> str:
    """API Key 来源优先级：环境变量 V4_API_KEY > config.yaml 的 api_key 字段。

    配置向导在运行时写入 config.yaml 后，经 load_config 的 mtime 缓存立即可读，
    无需重启即可生效。
    """
    return os.environ.get("V4_API_KEY", "") or load_config().get("api_key", "")


# 模块级快照（兼容旧引用，如 main.py 启动横幅）；运行时请以 get_api_key() 为准。
V4_API_KEY = get_api_key()
# 注意：下面这个默认地址来自任务参考代码，请通过 V4_API_URL 替换为真实地址
V4_API_URL = os.environ.get(
    "V4_API_URL", load_config().get("api_url", "https://api.deepseek.com/v1/chat/completions")
)
V4_MODEL = os.environ.get("V4_MODEL", load_config().get("model", "deepseek-v4-flash"))


# ── §61 token 用量监测：累计当前任务的所有 API 调用用量 ──
# call_llm 的每次请求（_post_full / _post_stream）都会把响应 usage 追加到这里；
# main.py 每轮任务开始时 reset_token_usage()，Prove 结束后读取并打印/落盘。
_TOKEN_USAGE: list = []


def reset_token_usage() -> None:
    """开始新任务时清空累计（main.py 每轮任务调用）。"""
    _TOKEN_USAGE.clear()


def get_token_usage() -> dict:
    """返回当前任务累计 token 用量，按缓存命中拆分输入 token（§65 / §66）。

    §66 修复：命中率 100% 的根因是「按调用整体判 HIT」——DeepSeek 的前缀缓存是**部分命中**
    （system 前缀命中、user 部分未命中），usage 里的 prompt_cache_hit_tokens 才是真实命中的
    token 数。因此：
      - prompt_cache_hit ：Σ prompt_cache_hit_tokens（真实命中 token）；
      - prompt_cache_miss：Σ (prompt_tokens - prompt_cache_hit_tokens)（未命中 token；
        响应头/usage 均无命中证据时，整个 prompt_tokens 计未命中，保守）；
      - hit_rate（token 口径）= 命中 /（命中+未命中）输入 × 100。
    兼容：老记录（无 prompt_cache_hit_tokens 字段）按 cache_status 整调用分类回退。
    """
    hits = sum(1 for u in _TOKEN_USAGE if u.get("cache_status") == "HIT")
    misses = sum(1 for u in _TOKEN_USAGE if u.get("cache_status") == "MISS")
    prompt_hit = sum(u.get("prompt_cache_hit_tokens", 0) for u in _TOKEN_USAGE)
    prompt_miss = sum(u.get("prompt_cache_miss_tokens", 0) for u in _TOKEN_USAGE)
    # 兼容回退：无 hit/miss token 字段（旧记录）→ 按 cache_status 整调用分类
    if prompt_hit == 0 and prompt_miss == 0:
        prompt_hit = sum(u.get("prompt_tokens", 0) for u in _TOKEN_USAGE if u.get("cache_status") == "HIT")
        prompt_miss = sum(u.get("prompt_tokens", 0) for u in _TOKEN_USAGE if u.get("cache_status") != "HIT")
    total_input = prompt_hit + prompt_miss
    return {
        "calls": len(_TOKEN_USAGE),
        "prompt_tokens": total_input,
        "completion_tokens": sum(u.get("completion_tokens", 0) for u in _TOKEN_USAGE),
        "total_tokens": sum(u.get("total_tokens", 0) for u in _TOKEN_USAGE),
        "prompt_cache_hit": prompt_hit,
        "prompt_cache_miss": prompt_miss,
        "cache_hit": hits,
        "cache_miss": misses,
        "cache_unavailable": len(_TOKEN_USAGE) - hits - misses,
        "hit_rate": (prompt_hit / total_input * 100) if total_input else 0.0,
    }


def _extract_cache_status(headers) -> str:
    """从 API 响应头提取缓存状态（HIT / MISS）；缺失或未知值 → UNAVAILABLE。

    兼容 requests 的 CaseInsensitiveDict 与 urllib 的 email.message.Message。
    优先级（§63 修复）：
      1. `X-DS-Cache-Status` —— DeepSeek 前缀缓存状态头（命中缓存时返回 HIT）；
      2. `X-Cache-Status`  —— DeepSeek 官方文档头；
      3. `EO-Cache-Status` —— EdgeOne/CDN 头（旧实现，保留兼容；对 chat 请求恒 MISS）。
    值规范化为大写。
    """
    if not headers:
        return "UNAVAILABLE"
    try:
        v = (headers.get("X-DS-Cache-Status")
             or headers.get("x-ds-cache-status")
             or headers.get("X-Cache-Status")
             or headers.get("x-cache-status")
             or headers.get("EO-Cache-Status")
             or headers.get("eo-cache-status")
             or "")
    except Exception:
        return "UNAVAILABLE"
    v = str(v or "").strip().upper()
    return v if v in ("HIT", "MISS") else "UNAVAILABLE"


def _record_usage(usage, cache_status: str = "UNAVAILABLE") -> None:
    """把一次 API 响应的 usage 字段追加到累计（§61 / §62 / §63，含缓存状态）。

    §63：DeepSeek 前缀缓存的权威数据在响应体 usage（prompt_cache_hit_tokens > 0 即
    命中前缀缓存）——命中时即使响应头缺失/为 MISS，也按 HIT 记录（响应头 X-DS-Cache-Status
    在部分端点不返回，且 EO-Cache-Status 对 chat 请求恒 MISS）。
    """
    if not isinstance(usage, dict):
        return
    pt = int(usage.get("prompt_tokens") or 0)
    hit_tok = int(usage.get("prompt_cache_hit_tokens") or 0)
    try:
        if hit_tok > 0:
            cache_status = "HIT"
    except (TypeError, ValueError):
        pass
    # §66 调试：确认缓存状态读取（响应头判定 + usage 前缀缓存证据）
    print(f"[DEBUG] 缓存状态: {cache_status} | usage命中={hit_tok} | prompt={pt}")
    _TOKEN_USAGE.append({
        "prompt_tokens": pt,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        # §66：真实命中/未命中 token（DeepSeek 前缀缓存按 token 计数，而非整调用 HIT/MISS）
        "prompt_cache_hit_tokens": hit_tok,
        "prompt_cache_miss_tokens": max(pt - hit_tok, 0),
        "cache_status": cache_status,
    })


def _post_full(payload: dict) -> dict | None:
    """发送请求并返回完整的 message dict；失败打印错误并返回 None（不中断程序）。

    返回 message 而非单纯 content，是为了支持 Function Calling：需要从中读取
    tool_calls，并在多轮对话里把 assistant（带 tool_calls）与 tool 消息回填。
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_api_key()}",
    }
    try:
        _cache_headers = None  # §62：响应头（含 X-Cache-Status）
        try:
            import requests  # 优先用 requests（若已安装）
            resp = requests.post(V4_API_URL, headers=headers, json=payload, timeout=60)
            text = resp.text
            status = resp.status_code
            _cache_headers = resp.headers
        except ImportError:
            # 回退到标准库 urllib，保证零依赖也能跑
            import urllib.error
            import urllib.request
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(V4_API_URL, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode("utf-8")
                    status = r.status
                    _cache_headers = r.headers
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "ignore")
                status = e.code
        if status != 200:
            print(f"[llm] API 返回错误 HTTP {status}: {text[:300]}", file=sys.stderr)
            return None
        data = json.loads(text)
        # §61/§62：记录本次请求的 token 用量与缓存状态（openai 兼容响应顶层 usage 字段）
        _record_usage(data.get("usage"), _extract_cache_status(_cache_headers))
        return data["choices"][0]["message"]
    except Exception as e:
        print(f"[llm] 调用失败（已优雅降级返回 None）: {e}", file=sys.stderr)
        return None


def _post(payload: dict) -> str:
    """兼容旧调用的薄封装：仅返回 content 文本。"""
    msg = _post_full(payload)
    if msg is None:
        return ""
    return msg.get("content", "") or ""


# ── 终端颜色（流式字段流的实时进度着色；非 tty / NO_COLOR 时自动关闭）──
_NO_COLOR = (not sys.stdout.isatty()) or (os.environ.get("NO_COLOR") is not None)
_C_RESET = "\033[0m" if not _NO_COLOR else ""
C_BLUE = "\033[34m" if not _NO_COLOR else ""    # 思考：蓝
C_GREEN = "\033[32m" if not _NO_COLOR else ""   # 执行：绿
C_YELLOW = "\033[33m" if not _NO_COLOR else ""  # 验证：黄
C_CYAN = "\033[36m" if not _NO_COLOR else ""


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_C_RESET}"


def _http_stream_lines(payload: dict, out_headers: dict | None = None):
    """逐行 yield SSE 的 `data: ...` 事件（已解码为 str）。

    out_headers: 可选出参 dict——若传入，会把响应头复制进去（§62：供调用方
    提取 X-Cache-Status 等缓存状态字段）。

    优先用 requests（stream=True）做真正的分块流式；requests 不可用时回退到
    urllib 分块读取并自行按行切分。任一后端失败都打印错误并停止迭代（优雅降级，
    不抛异常中断上层循环）。
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_api_key()}",
        "Accept": "text/event-stream",
    }
    sp = dict(payload)
    sp["stream"] = True
    try:
        import requests  # 优先 requests 流式
        resp = requests.post(V4_API_URL, headers=headers, json=sp, stream=True, timeout=60)
        if out_headers is not None:
            try:
                out_headers.update(dict(resp.headers))
            except Exception:
                pass
        for raw in resp.iter_lines(decode_unicode=False):
            if not raw:
                continue
            yield raw.decode("utf-8", "ignore")
        return
    except ImportError:
        pass  # 回退 urllib
    except Exception as e:  # 网络错误等：上报后停止（上层会捕获并返回已收集内容）
        print(f"[llm] 流式请求失败: {e}", file=sys.stderr)
        return
    # ── urllib 回退（无 requests 时）──
    import urllib.error
    import urllib.request
    data = json.dumps(sp).encode("utf-8")
    req = urllib.request.Request(V4_API_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if out_headers is not None:
                try:
                    out_headers.update(dict(r.headers))
                except Exception:
                    pass
            buf = b""
            while True:
                block = r.read(4096)
                if not block:
                    break
                buf += block
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", "ignore")
            if buf:
                yield buf.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "ignore")
        print(f"[llm] API 返回错误 HTTP {e.code}: {text[:300]}", file=sys.stderr)
    except Exception as e:
        print(f"[llm] 流式请求失败: {e}", file=sys.stderr)


def _post_stream(payload: dict, stage: str | None = None) -> dict | None:
    """流式请求：逐 SSE 事件接收，按 stage 实时打印字段流；返回完整 message dict。

    字段流规则（与任务要求一致）：
      - stage == "think"：把模型文本逐字打印（实时思考流）。
      - stage == "act"  ：检测到 tool_calls（工具字段）时打印「[工具] 调用工具：{name}」。
      - stage == "prove"：从流式 JSON 中检测到 verdict 字段时打印「[裁决] {verdict}」。
    失败（网络/解析异常）时捕获并返回已收集内容，不中断整轮。
    """
    role = "assistant"
    content = ""
    tool_calls: list = []
    tool_printed: set = set()
    verdict_printed = False
    usage_recorded = False  # §61：流式仅记录一次 usage（末 chunk 可能 choices 为空）
    _resp_headers: dict = {}  # §62：收集响应头（含 X-Cache-Status）
    try:
        for raw in _http_stream_lines(payload, out_headers=_resp_headers):
            if not raw:
                continue
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            # §61/§62：流式 usage 可能只在末 chunk 携带（此时 choices 可能为空）——先于 choices 判断捕获
            if not usage_recorded and obj.get("usage"):
                _record_usage(obj.get("usage"), _extract_cache_status(_resp_headers))
                usage_recorded = True
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}) or {}
            if delta.get("role"):
                role = delta["role"]
            # ── 文本流 ──
            piece = delta.get("content") or ""
            if piece:
                content += piece
                if stage == "think":
                    print(piece, end="", flush=True)
                # prove 阶段：流式 JSON 一旦可解析出 verdict 即单独提取显示
                if stage == "prove" and not verdict_printed:
                    d = _extract_json(content)
                    if isinstance(d, dict) and "verdict" in d:
                        print(_c(C_YELLOW, f"\n[裁决] {d['verdict']}"))
                        verdict_printed = True
            # ── 工具调用流（act 阶段）──
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls)
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "") or ""
                args_frag = fn.get("arguments", "") or ""
                while len(tool_calls) <= idx:
                    tool_calls.append({"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
                slot = tool_calls[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if name and not slot["function"]["name"]:
                    slot["function"]["name"] = name
                    if stage == "act" and idx not in tool_printed:
                        print(_c(C_CYAN, f"\n[工具] 调用工具：{name}"))
                        tool_printed.add(idx)
                slot["function"]["arguments"] += args_frag
    except Exception as e:
        print(f"[llm] 流式解析失败（已优雅降级）: {e}", file=sys.stderr)
    # think 阶段流结束后补一个换行，避免与后续结果横幅粘连
    if stage == "think" and content:
        print()
    msg: dict = {"role": role, "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def call_llm(messages: list, system: str = "", model: str | None = None,
             tools: list | None = None, max_tool_rounds: int = 5,
             tool_log: list | None = None, tool_choice: str = "auto",
             stage: str | None = None, stream: bool = False) -> str:
    """统一的 LLM 调用。

    messages: list of {role, content} 字典，或 list of str（视为 user 消息）
    system:   系统提示词（可选）
    model:    模型 ID（默认 V4_MODEL）
    tools:    可选的工具列表（Function Calling 格式）。提供后若模型返回
              tool_calls，将自动执行并将结果以 tool 角色消息回传，循环最多
              max_tool_rounds 轮，最终返回模型的文字回答。
    tool_log: 可选的出参收集器（list）。若提供，每次工具执行会向其追加一条
              {"name", "arguments", "result", "status"} 记录，供调用方（如 Act
              阶段）汇总后传递给验证层（Prove），避免工具执行结果被丢弃。
    返回:     模型生成的文本；失败 / 未配置 key 时返回 ""
    """
    if not get_api_key():
        print("[llm] 未设置 API Key（环境变量 V4_API_KEY 或 config.yaml 的 api_key），无法调用模型（优雅降级返回空串）。", file=sys.stderr)
        return ""
    payload_msgs = []
    if system:
        payload_msgs.append({"role": "system", "content": system})
    for m in messages:
        if isinstance(m, dict):
            payload_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        else:
            payload_msgs.append({"role": "user", "content": str(m)})
    if not payload_msgs:
        return ""
    payload = {"model": model or V4_MODEL, "messages": payload_msgs,
               "temperature": 0.2, "stream": bool(stream)}
    if tools:
        payload["tools"] = tools
        # 显式声明 tool_choice=auto：允许模型自主选择是否调用工具。
        # 若不设置，部分 OpenAI 兼容端点（含 DeepSeek 类）默认 tool_choice=none，
        # 模型永远不会返回 tool_calls，导致 Function Calling 始终不执行。
        payload["tool_choice"] = tool_choice

    # ── 第一轮请求（stream=True 时走流式字段流）──
    if stream:
        msg = _post_stream(payload, stage=stage)
    else:
        msg = _post_full(payload)
    if msg is None:
        return ""

    # ── Function Calling 循环：解析 tool_calls -> 执行 -> 回填 tool 消息 ──
    rounds = 0
    while tools and msg.get("tool_calls") and rounds < max_tool_rounds:
        # 把模型的「含 tool_calls 的 assistant 消息」原样加入对话
        payload_msgs.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": msg["tool_calls"],
        })
        # 逐个执行工具，并以 tool 角色消息回传结果
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = execute_tool(name, args)
            if tool_log is not None:
                # 错误判定兼容旧 [错误] 前缀与新结构化 error JSON（任务一：工具失败结构化处理）
                _is_err = (result.startswith("[错误]") or result.startswith("[拦截]")
                           or '"status": "error"' in result)
                tool_log.append({
                    "name": name,
                    "arguments": args,
                    "result": result,
                    "status": "error" if _is_err else "ok",
                })
            payload_msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })
        # ── 带工具结果再请求一次（后续轮非流式，避免重复字段流打印）──
        payload["stream"] = False
        payload["messages"] = payload_msgs
        msg = _post_full(payload)
        if msg is None:
            return ""
        rounds += 1

    return msg.get("content", "") or ""


def _extract_json(text: str) -> dict | None:
    """从模型文本中提取第一个 JSON 对象；失败返回 None。"""
    if not text:
        return None
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s:e + 1])
    except Exception:
        pass
    return None


def _indent(text: str, prefix: str = "    ") -> str:
    """把多行文本每行加前缀（与 main.py 同名工具保持一致）。"""
    return "\n".join(prefix + line for line in str(text).splitlines())


def _format_tool_summary(tool_log: list) -> str:
    """把工具调用记录格式化为结构化摘要：名称 / 输入参数 / 输出内容 / 执行状态。

    该摘要会被 Act 阶段并入返回 dict，并传递给 Prove 阶段与记忆层，确保工具
    执行结果不会在 Function Calling 循环里被丢弃。
    """
    if not tool_log:
        return ""
    lines = [f"[工具执行摘要] 共 {len(tool_log)} 次工具调用："]
    for i, rec in enumerate(tool_log, 1):
        name = rec.get("name", "?")
        args = rec.get("arguments", {}) or {}
        arg_str = ", ".join(f"{k}={args[k]!r}" for k in args) if args else "(无参数)"
        status = rec.get("status", "ok")
        result = rec.get("result", "")
        # 输出内容过长则首尾截断，避免 context 爆炸
        if len(result) > 2000:
            result = result[:1500] + "\n...[输出过长，已截断]...\n" + result[-400:]
        lines.append(f"{i}. {name}({arg_str}) -> {status}")
        lines.append(f"   输出内容：\n{_indent(result)}")
    return "\n".join(lines)


def _stage_system(stage_sys: str, extra: str = "") -> str:
    """把阶段专属指令拼接在通用系统提示词之后，作为本轮 call_llm 的 system。

    通用系统提示词（src/prompts/system_prompt_merged.md）提供跨模型的行为准则；
    阶段指令提供 think/act/prove 的 JSON 格式与角色要求。extra 用于追加其他
    静态上下文（如一次性环境探测快照 format_env_block 的结果），三者组合后整体
    兼容 Function Calling：工具仍通过 tools= 参数下发，system 中不包含工具
    schema，因此不会与模型返回 tool_calls 的逻辑冲突。
    """
    base = load_system_prompt()
    parts = [base, stage_sys] if base else [stage_sys]
    if extra:
        parts.append(extra)
    return "\n\n---\n\n".join(parts)


# ── 动态链式思考：共享工作记忆（working_memory）与执行摘要生成 ──
class WorkingMemory:
    """think() 与 act() 之间共享的内存态工作记忆，每次 act() 后就地更新。

    解决三类漏洞：
      - 状态不一致(Issue 1)：act 的结果显式写回，think 下一轮读取，不再依赖隐式上下文。
      - 上下文膨胀(Issue 3)：think 只读本对象的【摘要】，不读完整工具输出。
      - 模型循环(Issue 6)：迭代推进配合『计划须不同』约束；固定迭代上限已移除，
        循环终止改由 main.run_turn 的「无进展检测」负责（详见 §31）。
    """
    def __init__(self) -> None:
        self.completed_actions: list[str] = []          # 已完成的操作列表（每条为简明摘要）
        self.last_result: dict = {"success": False, "summary": "", "details": ""}
        self.current_step: int = 0                       # 当前迭代步数
        self.iteration_count: int = 0                    # 循环计数器（仅供展示 / 快照，不再作为固定上限）
        self.prev_plan: str = ""                         # 上一轮计划（供『计划须不同』判断）
        self.status: str = "normal"                      # normal | 需要人工介入

    def record_action(self, summary_text: str, success: bool, details: str = "") -> None:
        """act() 执行后调用：写回最后结果、推进步数，并把「成功完成」的动作计入 completed_actions。

        注意：completed_actions 只收录 success=True 的动作（失败 / 被拦截不计入），
        这样上层（main.run_turn 的「无进展检测」）才能用 completed_actions 是否新增
        来可靠判断「本轮是否取得进展」——否则失败动作也计入会让检测器永远不触发。
        """
        if success:
            self.completed_actions.append(summary_text)
        self.last_result = {"success": success, "summary": summary_text, "details": details}
        self.current_step = self.iteration_count

    def snapshot(self) -> str:
        """生成注入 think 提示词的文本块（仅含摘要，控制上下文体积）。"""
        if not self.completed_actions and not self.last_result.get("summary"):
            return "[工作记忆 working_memory]（首轮，尚无已完成动作）"
        lines = ["[工作记忆 working_memory]"]
        lines.append(f"当前步数 current_step: {self.current_step}")
        lines.append(f"迭代计数 iteration_count: {self.iteration_count}")
        lines.append("已完成动作 completed_actions:")
        for a in self.completed_actions:
            lines.append(f"  - {a}")
        lines.append("上一次结果 last_result:")
        for sl in (self.last_result.get("summary", "") or "(无)").splitlines():
            lines.append(f"  {sl}")
        if self.prev_plan:
            lines.append(f"上一轮计划 prev_plan: {self.prev_plan}")
        return "\n".join(lines)


def _build_act_summary(act_result: dict) -> dict:
    """把 act 阶段的执行结果压缩成一段简明摘要（Issue 3：控制上下文体积）。

    返回 {"success": bool, "summary": str}。summary 形如：
      「已创建文件：test.md
        操作状态：成功」
    think() 下一轮读取的即是该摘要，而非完整 tool_execution_summary。
    """
    tool_calls = act_result.get("tool_calls") or []
    lines: list[str] = []
    ok = True
    if tool_calls:
        for tc in tool_calls:
            name = tc.get("name", "?")
            args = tc.get("arguments", {}) or {}
            status = tc.get("status", "ok")
            if status != "ok":
                ok = False
            if name == "write_file":
                p = args.get("path", "?")
                n = len(args.get("content", "") or "")
                lines.append(f"已创建/写入文件：{p}（{n} 字符）")
            elif name == "read_file":
                lines.append(f"已读取文件：{args.get('path', '?')}")
            elif name == "run_command":
                lines.append(f"已执行命令：{args.get('command', '?')}")
            else:
                lines.append(f"已调用工具：{name}")
    else:
        changes = act_result.get("changes", []) or []
        if changes:
            lines.append("已规划改动：" + "；".join(str(c) for c in changes))
        else:
            intent = act_result.get("intent_line", "") or ""
            lines.append(intent or "(无具体改动)")
            ok = bool(intent)
    status_line = "操作状态：成功" if ok else "操作状态：失败"
    text = ("\n".join(lines) + "\n" + status_line) if lines else status_line
    return {"success": ok, "summary": text}


# ── fable-method 三阶段系统提示词（精简版，忠于原版 Step 0-6）──
_THINK_SYS = (
    "你是 Fable Method 的规划阶段。给定一个任务，严格按 JSON 输出："
    '{"classification": "task|question|plan-first|done", '
    '"reasoning": "思考链：先分析用户真实意图，再判断复杂度与是否需要额外规划", '
    '"plan": "最终计划 / 建议方案（一句话描述要做什么，如需工具调用请写明调用哪个工具）", '
    '"done": false, '
    '"complexity": "simple|medium|complex", '
    '"definition_of_done": "完成标准", '
    '"evidence": ["证据1", "证据2"], '
    '"scope": ["受影响文件/模块"], '
    '"subtasks": "可选：仅当任务可拆解为多个相互独立的子任务时输出，数组的每个元素是一条独立的自然语言子任务描述（如 [\\"创建文件 a.txt\\", \\"创建文件 b.txt\\", \\"创建文件 c.txt\\"]）。典型场景：任务要求创建/处理多个独立文件或对象（如 \\"创建 a.txt、b.txt、c.txt 三个文件\\"）、或要求完成多项互不依赖的操作。若任务不可拆解或各子任务之间存在依赖，则省略该字段"}。'
    "请先认真输出 reasoning（你的完整思考过程），再输出 plan（最终要执行的计划）。"
    "只输出 JSON，不要任何额外文字。"
    "\n\n[任务拆解判断]\n"
    "1. 判断任务是否可拆解为多个**相互独立**的子任务（各子任务无先后依赖、可并行完成）。\n"
    "2. 若可拆解：必须在 JSON 中输出 subtasks 数组，每个元素是一条独立、可直接执行的子任务描述"
    "（如 \\\"创建文件 a.txt\\\"），并让 plan 简要说明整体计划。\n"
    "3. 若不可拆解（单目标 / 子任务有依赖 / 无法拆分）：省略 subtasks 字段，只输出 plan。\n"
    "\n[动态链式思考约束]\n"
    "1. 在生成计划前，必须先读取下方【工作记忆 working_memory】中的「已完成动作」与"
    "「上一次结果」，并在 reasoning 中显式引用它们（例如：基于上一步已创建的 X，现在应…）。\n"
    "2. 新增字段 \"done\"：若任务已无需更多动作（或已无可行动作），输出 true；否则 false。"
    "当你认为任务已完成时，请置 done=true 并让 plan 为空或总结性说明。\n"
    "3. 你的计划必须与上一轮计划不同。如果发现上一轮计划与当前计划相似，请优先考虑"
    "「任务已完成」（done=true）或「需要人工介入」。"
)
_ACT_SYS = (
    "你是 Fable Method 的执行阶段。给定任务和建议方案，严格按 JSON 输出："
    '{"changes": ["改动1", "改动2"], '
    '"intent_line": "INTENT: 代码做<X>；期望<Y>；规范说<Z>"}。只输出 JSON，不要任何额外文字。'
    "如果落实方案需要读取文件、写入文件或在项目内运行命令，请调用提供的工具"
    "（read_file / write_file / run_command）；工具执行结果会作为后续上下文返回给你。"
)
_PROVE_SYS = (
    "你是 Fable Method 的验证阶段。给定任务与执行结果，严格按 JSON 输出："
    '{"done_criterion_met": true, '
    '"system_healthy": true, '
    '"observed": "观察证据", '
    '"verdict": "VERIFIED|REFUTED|UNVERIFIABLE"}。只输出 JSON，不要任何额外文字。'
)


# ── 本地路由层：DeepSeek Flash / Pro 二选一（轻量本地实现）──
# 模型路由逻辑集中在 src/integrations/routing/router.py（LightweightRouter），
# 不再依赖外部路由服务。
try:
    from src.integrations.routing.router import get_router
except ImportError:
    def get_router():
        return None


class RealModel:
    """接入真实模型的 fable_cycle 模型接口实现（替换原来的 MockModel）。"""

    def __init__(self) -> None:
        # 共享工作记忆：在 think() 与 act() 之间传递，每次 act 后更新。
        # 单次任务开始时由调用方 reset_working_memory()，避免跨任务污染。
        self.working_memory = WorkingMemory()
        # 一次性环境探测快照格式化的「环境信息」块；由 main 在启动时生成并注入，
        # 使模型按当前平台生成命令（跨平台命令适配）。空串表示未注入。
        self.env_block = ""

    def reset_working_memory(self) -> None:
        """开始新任务时重置工作记忆（防止上一任务的 state 泄漏到本轮）。"""
        self.working_memory = WorkingMemory()

    def think(self, task: str, memory_context: str = "", working_memory=None,
              skill_context: str = "") -> dict:
        print(_c(C_BLUE, "\n[思考] 正在分析任务..."))
        prompt = f"任务：{task}"
        if memory_context:
            prompt += f"\n\n参考历史记忆（如与任务相关，请借鉴）：\n{memory_context}"
        # Issue 1/3：注入工作记忆（仅摘要），让 think 基于「已完成动作 + 上一次结果」规划，
        # 而非读取完整工具输出，避免上下文逐步膨胀。
        if working_memory is not None:
            prompt += "\n\n" + working_memory.snapshot()
        prompt += "\n\n请先输出 reasoning（思考链），再输出 plan（最终计划）。"
        # §42：技能树上下文（来自 skill_manager 的匹配结果）追加进系统提示词，
        # 置于用户输入之前，使模型在规划时参考相关技能分类的操作指引。
        extra = self.env_block
        if skill_context:
            extra = (extra + "\n\n" + skill_context) if extra else skill_context
        raw = call_llm(
            [prompt],
            system=_stage_system(_THINK_SYS, extra=extra), model=get_router().decide(task, "think"),
            stage="think", stream=True,
        )
        data = _extract_json(raw)
        if data:
            data.setdefault("evidence", [])
            data.setdefault("scope", [])
            data.setdefault("reasoning", "")
            data.setdefault("plan", "")
            data.setdefault("done", False)
            # §54：任务拆解——模型可选输出 subtasks（可拆解任务）；未输出则置 None
            data.setdefault("subtasks", None)
            # 兼容旧字段：plan 作为建议方案，同时保留 decision 供 Act 阶段使用
            if not data.get("decision"):
                data["decision"] = data.get("plan", "")
            # 规范化 complexity（来自思考层）；非法值置空，路由会回退到启发式判断
            c = data.get("complexity")
            if c not in ("simple", "medium", "complex"):
                c = None
            data["complexity"] = c
            return data
        # 优雅降级：没有结构化返回也不中断循环
        return {
            "classification": "task",
            "reasoning": "",
            "plan": raw or "(模型未返回内容)",
            "done": False,
            "complexity": None,
            "definition_of_done": raw or "(模型未返回内容)",
            "evidence": [],
            "decision": raw or "(模型未返回内容)",
            "scope": [],
        }

    def act(self, task: str, decision: str, complexity: str | None = None,
            working_memory=None, skill_context: str = "") -> dict:
        print(_c(C_GREEN, "\n[执行] 正在执行操作..."))
        tool_log: list = []  # 收集本轮所有工具调用，供 Prove / 记忆层使用
        # 叠加 Act 阶段补充规则（src/prompts/act.md）：直接执行、不要先列举目录，
        # 需要读写文件时立即调用 write_file / read_file。该规则放在阶段指令末尾、
        # 贴近工具调用决策，用以抵消通用系统提示词里「先探索环境」的倾向。
        act_sys = _ACT_SYS
        act_rules = load_stage_prompt("act")
        if act_rules:
            act_sys = act_sys + "\n\n" + act_rules
        # §42：技能树上下文同样注入 Act 阶段系统提示词（简单直接任务会跳过 think，
        # 直接进 act，此时技能参考仅靠 act 的系统提示词承载）。
        extra = self.env_block
        if skill_context:
            extra = (extra + "\n\n" + skill_context) if extra else skill_context
        raw = call_llm(
            [f"任务：{task}\n建议方案：{decision}\n\n请执行（给出具体改动与意图行）。"],
            system=_stage_system(act_sys, extra=extra), model=get_router().decide(task, "act", complexity=complexity),
            tools=TOOLS, tool_log=tool_log, tool_choice="auto",
            stage="act", stream=True,
        )
        data = _extract_json(raw)
        if data:
            data.setdefault("changes", [])
            data.setdefault("intent_line", "")
            # 汇总工具执行结果，作为 Prove 阶段输入的一部分（修复「结果丢失」问题）
            data["tool_calls"] = tool_log
            data["tool_execution_summary"] = _format_tool_summary(tool_log)
        else:
            data = {
                "changes": [raw or "(模型未返回内容)"],
                "intent_line": "",
                "tool_calls": tool_log,
                "tool_execution_summary": _format_tool_summary(tool_log),
            }
        # Issue 1/3：act 执行后把结果写回工作记忆（仅存摘要，控制上下文体积）；
        # 下一轮 think 读取的即是这份摘要，而非完整工具输出。
        if working_memory is not None:
            summ = _build_act_summary(data)
            working_memory.record_action(summ["summary"], summ["success"])
        return data

    def prove(self, task: str, changes, complexity: str | None = None) -> dict:
        print(_c(C_YELLOW, "\n[验证] 正在验证结果..."))
        if isinstance(changes, list):
            changes_text = "\n".join(str(c) for c in changes)
        else:
            changes_text = str(changes)
        raw = call_llm(
            [f"任务：{task}\n执行结果：{changes_text}\n\n请验证是否通过。"],
            system=_stage_system(_PROVE_SYS, extra=self.env_block), model=get_router().decide(task, "prove", complexity=complexity),
            stage="prove", stream=True,
        )
        data = _extract_json(raw)
        if data:
            return data
        return {
            "done_criterion_met": False,
            "system_healthy": False,
            "observed": raw or "(模型未返回内容)",
        }
