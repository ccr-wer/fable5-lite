"""fable-judge — 对抗式验证层。

提供两种裁决入口：

1. judge(task, result, evidence, tool_evidence=None) -> dict  【规则版本，用于主循环 prove()】
   纯关键词规则，零依赖、可离线运行。核心改进：区分「过程中的失败」与「最终的失败」——
      - result/evidence 既含失败标记又含最终成功的 write_file 操作（或成功标记）
        -> VERIFIED（过程中虽失败但已恢复并完成最终写入）
      - 只有失败标记、没有任何成功的工具调用
        -> REFUTED（最终仍未成功）
      - 其余：含成功标记 -> VERIFIED；都无 -> UNVERIFIABLE
   可选 tool_evidence={"completed_actions": [...]}（来自 working_memory，仅含成功动作，
   §31/§32）作为更可靠的「是否有成功写文件」结构化信号。返回 {"verdict","reason","suggestions"}。

2. Judge 类【保留，兼容 fable_cycle.FableCycle.run() 的旧式 claims 接口】
   基于声明表(claims)与欺诈排查给出 VERIFIED / VERIFIED WITH CAVEATS / REFUTED。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


# 失败标记：出现在结果/证据中即视为「存在失败」（可能是过程中的失败，也可能是最终失败）。
# 注：结构化的工具错误以小写 "error" 返回（见 §33 tools.py），故必须包含小写 "error"。
_REFUTED_MARKERS = ("error", "failed", "未找到")

# 成功标记：出现在结果/证据中即视为「存在成功信号」。
_VERIFIED_MARKERS = ("成功", "完成", "已重命名", "success", "已写入", "[write_file] 已写入")

# 最终写文件成功的明确标记（用于区分「过程失败但最终恢复」与「彻底失败」）。
# 成功的 write_file 在工具层返回 "[write_file] 已写入 N 字符到 <path>"（§33），
# 在 working_memory.completed_actions 中记录为 "已创建/写入文件：<path>（N 字符）"（§31/§32）。
_WRITE_SUCCESS_MARKERS = ("[write_file] 已写入", "已创建/写入文件", "写入文件", "write_file")

# ── §43 关键操作（写文件 / 移动 / 建目录）识别与显式成功检查 ──
# 任务若明确要求这些操作，必须存在其「成功」证据才允许判 VERIFIED，
# 避免因「无文件可移动」等空操作（命令返回成功但什么都没做）被误判为完成。
_WRITE_KEYWORDS = ("写文件", "写入", "创建文件", "新建文件", "建文件", "文件内容",
                   "文件，内容", "文件内容为", "write", "create")
_MOVE_KEYWORDS = ("移动", "移到", "移动到", "移入", "搬移", "move", "mv")
_MKDIR_KEYWORDS = ("创建目录", "新建目录", "建目录", "创建文件夹", "新建文件夹",
                   "建文件夹", "mkdir")


def _required_key_ops(task: str) -> set:
    """从任务文本识别所需的关键操作集合（write_file / move / mkdir）。"""
    t = task or ""
    ops = set()
    if any(k in t for k in _WRITE_KEYWORDS):
        ops.add("write_file")
    if any(k in t for k in _MOVE_KEYWORDS):
        ops.add("move")
    if any(k in t for k in _MKDIR_KEYWORDS):
        ops.add("mkdir")
    return ops


def _key_op_succeeded(op: str, completed: list, blob: str) -> bool:
    """判断某个关键操作是否真正执行成功（基于结构化 completed_actions + 结果/证据文本）。

    仅成功的动作才进入 completed_actions（§31/§32），故 run_command 形式的
    move/mkdir 只有真正成功（exit 0）才会留下 "已执行命令：move/mkdir ..." 记录；
    空操作（如无文件可移动导致命令失败）不会被记录，从而被本检查识别为「未完成」。
    """
    ctext = " ".join(str(a) for a in (completed or []))
    low = blob.lower()
    if op == "write_file":
        if (("[write_file] 已写入" in blob)
                or ("已创建/写入文件" in ctext)
                or ("写入文件" in ctext)
                or ("write_file" in ctext)):
            return True
        # 兼容通过 run_command 写入文件（echo/printf/type/cat 重定向、python open() 等）：
        # completed_actions 中存在带写重定向或文件写入语义的命令即视为写文件成功。
        for a in (completed or []):
            t = str(a).lower()
            if "已执行命令" in t and (">" in t or ">>" in t or "echo" in t
                                       or "printf" in t or "write" in t or "type" in t
                                       or "cat" in t or "open(" in t or "with open" in t):
                return True
        return False
    if op == "move":
        # 成功移动会在 completed_actions 留下 "已执行命令：move/mv ..."，
        # 或在结果/证据中显式出现「已移动/已重命名/移动了」等成功信号。
        return (("已执行命令：move" in ctext.lower())
                or ("已执行命令：mv" in ctext.lower())
                or ("移动" in ctext)
                or ("已移动" in ctext)
                or ("已重命名" in blob)
                or ("moved" in low))
    if op == "mkdir":
        return (("已执行命令：mkdir" in ctext.lower())
                or ("创建目录" in ctext)
                or ("已创建目录" in blob)
                or ("创建文件夹" in ctext)
                or ("mkdir" in low))
    return True


# ══════════════════════════════════════════════════════════════════════
# §56 语义匹配 + 子任务依赖关系验证（增强校验，不改变既有 REFUTED/UNVERIFIABLE 路径）
# ══════════════════════════════════════════════════════════════════════
_FILE_EXT_RE = re.compile(r"[\w\-]+\.(?:md|txt|py|js|ts|json|yaml|yml|csv|log|html|css|xml|ini|cfg|sh|bat|ps1|java|go|rs|cpp|c|h|jsx|tsx|sql|toml|env)")
# 命令执行类任务意图词（“运行/执行/列出/查看”等）
_CMD_TASK_RE = re.compile(r"(运行|执行|列出|查看|run|exec|list|dir|ls\b|检查\s*输出|检查\s*结果)", re.IGNORECASE)
# 代码编辑类任务意图词（修改/编辑/重构/编写代码等）
_CODE_TASK_RE = re.compile(
    r"(修改|编辑|重构|编写|实现|修复|优化|添加|删除|更新|改).{0,20}(代码|函数|方法|模块|脚本|class|def|import|print)",
    re.IGNORECASE,
)


def _extract_file_targets(task: str) -> list:
    """从任务描述中提取目标文件名（带扩展名的 token，如 a.txt、hello.py）。"""
    return _FILE_EXT_RE.findall(task or "")


def _check_file_semantics(task: str, blob: str, completed: list) -> tuple[bool, str]:
    """文件操作类任务：任务目标文件名必须出现在执行结果/证据中（路径一致 / 文件已处理）。"""
    files = _extract_file_targets(task)
    if not files:
        return True, ""
    ctext = " ".join(str(a) for a in (completed or []))
    missing = [f for f in files if f not in blob and f not in ctext]
    if missing:
        return False, (f"结果与任务目标语义不匹配：任务目标文件 {missing} "
                       f"未在结果/证据中出现（路径可能不一致或文件未创建/处理）")
    return True, ""


def _check_command_semantics(task: str, blob: str) -> tuple[bool, str]:
    """命令执行类任务：结果中应存在命令执行痕迹（[run_command]/[read_file]/输出）。"""
    if not _CMD_TASK_RE.search(task or ""):
        return True, ""
    if _extract_file_targets(task):
        return True, ""  # 文件类任务优先走文件语义，不重复判定
    if ("[run_command]" in blob or "[read_file]" in blob or "EXIT=" in blob
            or "命令执行" in blob or "输出" in blob or "结果" in blob):
        return True, ""
    return False, "结果与任务目标语义不匹配：命令执行类任务未找到命令执行痕迹（[run_command]/输出），无法确认输出符合预期"


def _check_code_semantics(task: str, blob: str, completed: list) -> tuple[bool, str]:
    """代码编辑类任务：应存在写入/修改代码的证据，且目标文件（若有）出现在结果中。"""
    if not _CODE_TASK_RE.search(task or ""):
        return True, ""
    wrote = ("已写入" in blob or "write_file" in blob or "修改" in blob
             or "已修改" in blob or "[write_file]" in blob)
    files = _extract_file_targets(task)
    if files:
        ctext = " ".join(str(a) for a in (completed or []))
        fok = all(f in blob or f in ctext for f in files)
        if not fok:
            return False, "结果与任务目标语义不匹配：代码编辑任务的目标文件未在结果中出现"
    if not wrote:
        return False, "结果与任务目标语义不匹配：代码编辑类任务未找到写入/修改代码的证据"
    return True, ""


def _check_dependencies(task: str, completed: list) -> tuple[bool, str]:
    """子任务/操作间依赖关系验证。

    任务含「在 <目录> 中创建（文件）」「先创建目录，再…」等依赖模式时，
    要求结果中存在该目录被触及的痕迹（mkdir / 创建目录 / 写入到该目录下的文件），
    否则即使操作“看起来成功”也判 REFUTED（依赖不满足）。
    """
    t = task or ""
    # 目录名限定 ASCII 字符集（\w 会匹配中文导致贪婪跨字，regex 失效）；
    # 只匹配明确的「创建 <目录>」或「在 <X> 目录中创建」依赖模式（X 与「目录」紧凑相邻，
    # 避免把「在 sandbox 当前工作目录下…」这类工作区描述误判为依赖目标）。
    m1 = re.search(r"创建\s*(?:一个|个)?\s*(?:目录|文件夹)\s*([A-Za-z0-9_\-./\\]+)", t)  # 创建目录 docs
    if not m1:
        m1 = re.search(r"创建\s*(?:一个|个)?\s*([A-Za-z0-9_\-./\\]+)\s*(?:目录|文件夹)", t)  # 创建 docs 目录
    m2 = re.search(r"在\s*([A-Za-z0-9_\-./\\]+)\s*(?:目录\s*)?(?:中|里|下)\s*创建", t)  # 在 docs(目录)中创建
    m = m1 or m2
    if not m:
        return True, ""
    dirname = (m.group(1) or "").strip("/\\")
    if not dirname:
        return True, ""
    seq = [str(a) for a in (completed or [])]
    if not seq:
        return True, ""  # 无序列可查，放行（避免误杀）
    dir_touched = any(dirname in a for a in seq)
    if not dir_touched:
        return False, (f"子任务依赖关系不满足：任务要求先在 {dirname} 目录中创建文件，"
                       f"但结果中未出现该目录相关的任何操作（目录未创建或文件未写入该目录）")
    return True, ""


def _check_subtask_consistency(task: str, tool_evidence: dict) -> tuple[bool, str]:
    """拆解场景（§54/§55）：任务中的每个目标文件必须出现在某个子任务结果中（路径一致）。"""
    subtasks = tool_evidence.get("subtasks") if isinstance(tool_evidence, dict) else None
    if not isinstance(subtasks, list) or not subtasks:
        return True, ""
    files = _extract_file_targets(task)
    if not files:
        return True, ""
    blob = "\n".join(
        f"{str(s.get('changes') or '')}\n{str(s.get('tool_execution_summary') or '')}\n{str(s.get('subtask') or '')}"
        for s in subtasks
    )
    missing = [f for f in files if f not in blob]
    if missing:
        return False, (f"子任务结果与任务目标不一致：文件 {missing} 未在任何子任务结果中出现"
                       f"（路径可能不一致或未创建）")
    return True, ""


def _extended_checks_fail(task: str, blob: str, tool_evidence: dict) -> tuple[bool, str]:
    """§56 综合增强校验：语义匹配（文件/命令/代码）+ 依赖关系 + 子任务一致性。

    返回 (是否通过, 失败原因)。仅在原本会判 VERIFIED 的路径上调用，
    任一不通过则降级为 REFUTED（不改变既有 REFUTED/UNVERIFIABLE 判定）。
    """
    te = tool_evidence or {}
    completed = te.get("completed_actions") or []
    for check in (
        lambda: _check_file_semantics(task, blob, completed),
        lambda: _check_command_semantics(task, blob),
        lambda: _check_code_semantics(task, blob, completed),
        lambda: _check_dependencies(task, completed),
        lambda: _check_subtask_consistency(task, te),
    ):
        ok, reason = check()
        if not ok:
            return False, reason
    return True, ""


def judge(task: str, result: str, evidence: str, tool_evidence: Optional[Dict] = None) -> Dict[str, str]:
    """规则版裁决：区分「过程中的失败」与「最终的失败」。

    判定优先级：
      1. 证据中既含失败标记、又含最终成功的 write_file 操作（或成功标记）
         -> VERIFIED（过程中虽失败但已恢复并完成最终写入，如「读取缺失文件后创建 recovery.txt」）。
      2. 证据中只有失败标记、没有任何成功的工具调用
         -> REFUTED（最终仍未成功）。
      3. 其余：含成功标记 -> VERIFIED；都无 -> UNVERIFIABLE（维持原规则）。

    Args:
        task: 原始任务描述（保留参数位，便于后续接入上下文相关规则）。
        result: 行动阶段产生的「结果」文本（如变更摘要、改动列表、工具执行摘要）。
        evidence: 验证阶段收集的「证据」文本（如模型观察输出、运行日志）。
        tool_evidence: 可选，结构化工具证据，形如 {"completed_actions": [...]}。
            取自 working_memory.completed_actions（§31/§32 仅收录成功动作），提供时
            「是否有成功的写文件操作」以结构化信号为准，比纯关键词更可靠。

    Returns:
        {"verdict": "VERIFIED" | "REFUTED" | "UNVERIFIABLE",
         "reason": "简要说明判断理由",
         "suggestions": "如果是 REFUTED，给出修复建议；否则为空串"}
    """
    task = task or ""
    result = result or ""
    evidence = evidence or ""
    # 结果和证据一起扫描（task 暂不参与关键词匹配，仅作上下文保留）
    blob = f"{result}\n{evidence}"

    # ── 失败标记检测 ──
    has_error = any(m in blob for m in _REFUTED_MARKERS)

    # ── 成功信号检测（优先用结构化证据，否则退化为关键词）──
    te = tool_evidence or {}
    completed = te.get("completed_actions") or []
    # 结构化：completed_actions 仅收录成功动作（§31/§32），含「写入」即最终有成功的写文件
    final_write_success = any(
        ("写入" in str(a) or "write_file" in str(a)) for a in completed
    )
    has_completed = bool(completed)
    # 退化路径：从 blob 关键词推断（兼容未传 tool_evidence 的情况）
    if not final_write_success:
        final_write_success = any(m in blob for m in _WRITE_SUCCESS_MARKERS)
    if not has_completed:
        has_completed = final_write_success or any(m in blob for m in _VERIFIED_MARKERS)

    # ── 区分「过程中的失败」与「最终的失败」──
    if has_error and final_write_success:
        # §56：语义/依赖增强校验——通过才允许判 VERIFIED
        _ok, _fail_reason = _extended_checks_fail(task, blob, te)
        if not _ok:
            return {
                "verdict": "REFUTED",
                "reason": _fail_reason,
                "suggestions": "请核对任务目标与执行结果的一致性（语义匹配 / 依赖关系校验未通过）。",
            }
        return {
            "verdict": "VERIFIED",
            "reason": "过程中虽出现过失败标记，但最终有成功的 write_file 操作，判定为已验证（过程性失败已恢复）。",
            "suggestions": "",
        }
    if has_error and not has_completed:
        return {
            "verdict": "REFUTED",
            "reason": "证据中仅有失败标记，没有任何成功的工具调用，判定为未通过（最终失败）。",
            "suggestions": (
                "定位该失败标记对应的执行步骤并修正；若标记来自无关日志噪声，"
                "请补充更明确的成功证据（如变更后的文件内容或测试通过输出）后再验证。"
            ),
        }

    # ── §43 关键操作显式检查 ──
    # 任务若要求 write_file / move / mkdir 等关键操作，必须存在其「成功」证据，
    # 否则即便结果/证据中含通用完成标记（如「成功」「完成」）也不能判 VERIFIED，
    # 避免「无文件可移动」等空操作被误判为任务完成。
    required_ops = _required_key_ops(task)
    if required_ops:
        missing_ops = [op for op in required_ops
                       if not _key_op_succeeded(op, completed, blob)]
        if missing_ops:
            # §56：依赖关系不满足优先判 REFUTED（即使关键操作缺失也先给依赖结论）
            _dep_ok, _dep_reason = _check_dependencies(task, completed)
            if not _dep_ok:
                return {
                    "verdict": "REFUTED",
                    "reason": _dep_reason,
                    "suggestions": "请先完成依赖的目录创建/前置步骤，再执行后续操作后重新验证。",
                }
            if has_error and not has_completed:
                return {
                    "verdict": "REFUTED",
                    "reason": (f"任务要求的关键操作 {missing_ops} 未执行成功，"
                               f"且证据中存在失败标记，判定为未通过。"),
                    "suggestions": (
                        "请确认相关源文件/目录确实存在，并完成对应操作"
                        "（如移动前先创建源文件、建目录后再写入），再重新验证。"
                    ),
                }
            # 无明确失败标记，但关键操作缺失/未真正成功：不能算完成，
            # 降级为 UNVERIFIABLE（而非误判 VERIFIED），并给出补充证据建议。
            return {
                "verdict": "UNVERIFIABLE",
                "reason": (f"任务要求的关键操作 {missing_ops} 未找到成功执行证据"
                           f"（可能为「无文件可移动」等空操作），无法判定为完成。"),
                "suggestions": (
                    "请补充可观察的关键操作成功证据"
                    "（如移动后的文件列表、目录创建结果、写入后的文件内容）后再验证。"
                ),
            }

    # ── 其余：维持原规则（成功标记 -> VERIFIED；否则 UNVERIFIABLE）──
    for marker in _VERIFIED_MARKERS:
        if marker in blob:
            # §56：语义/依赖增强校验——通过才允许判 VERIFIED
            _ok, _fail_reason = _extended_checks_fail(task, blob, te)
            if not _ok:
                return {
                    "verdict": "REFUTED",
                    "reason": _fail_reason,
                    "suggestions": "请核对任务目标与执行结果的一致性（语义匹配 / 依赖关系校验未通过）。",
                }
            return {
                "verdict": "VERIFIED",
                "reason": f"在结果/证据中检测到完成标记 '{marker}'。",
                "suggestions": "",
            }

    return {
        "verdict": "UNVERIFIABLE",
        "reason": "结果/证据中既无明确失败标记，也无明确完成标记，无法判定。",
        "suggestions": (
            "补充可观察的完成证据（如变更后的文件内容、测试输出或运行日志）后再验证。"
        ),
    }


@dataclass
class Claim:
    claim: str
    observed: str
    verified: bool = True


class Judge:
    """对一个已完成的「工作」给出对抗式裁决（兼容旧式 claims 接口）。"""

    def judge(self, claims: List[Dict], frauds: Optional[List[str]] = None) -> Dict:
        rows = [
            Claim(**c) if isinstance(c, dict) and "claim" in c else Claim(str(c), str(c))
            for c in claims
        ]
        frauds = frauds or []

        unverified = [r for r in rows if not r.verified]
        if unverified:
            verdict = "REFUTED" if frauds else "VERIFIED WITH CAVEATS"
        elif frauds:
            verdict = "REFUTED"
        else:
            verdict = "VERIFIED"

        return {
            "verdict": verdict,
            "claims": [
                {"claim": r.claim, "observed": r.observed, "verified": r.verified}
                for r in rows
            ],
            "frauds_found": frauds,
            "recommended_action": self._action(verdict),
        }

    @staticmethod
    def _action(verdict: str) -> str:
        return {
            "VERIFIED": "可以交付",
            "VERIFIED WITH CAVEATS": "标注已知 caveat 后交付",
            "REFUTED": "修复失败声明后再交付",
        }[verdict]
