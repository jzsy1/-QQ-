# -*- coding: utf-8 -*-
"""跨轮待删文件：挡住删除时记下路径，用户确认后自动执行（不靠模型再猜文件名）。"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

PENDING_PATH_KEY = "pending_delete_path"
PENDING_PATHS_KEY = "pending_delete_paths"  # 多文件：用 \n 分隔绝对路径
PENDING_FUNC_KEY = "pending_delete_func"

_HARD_CONFIRM = (
    "确认执行",
    "我确认执行",
    "我确认",
    "确定执行",
    "执行吧",
    "就这样执行",
    "好的执行",
)

# 已有 pending 时：单独「确认 / 删掉」也算同意（禁止无 pending 时用这些误放行）
_SOFT_CONFIRM_RE = re.compile(
    r"^(?:请|帮我|给我)?"
    r"(?:"
    r"确认(?:删除|删掉|执行)?"
    r"|确定(?:删除|删掉|执行)?"
    r"|(?:是的?|好的?)?(?:，|,)?(?:直接)?(?:把它|将它|那个|这些|它们|给我)?(?:都|全部|全)?"
    r"(?:删掉|删除|清掉)"
    r"|删吧|删掉|删除|全部删掉|都删掉|全都删|删了吧|删了它|把它删了"
    r")(?:吧|啊|呀|了)?[.。!！?？]*$",
    re.I,
)

_ALL_TARGETS_RE = re.compile(r"所有|全部|全都|都删|统统", re.I)
_WORD_TARGET_RE = re.compile(r"word|WORD|\.docx|\.doc|文档", re.I)

_DELETE_INTENT_RE = re.compile(r"删除|删掉|清掉|移到回收站|丢掉", re.I)

_FAKE_DELETED_RE = re.compile(
    r"(?:已经|已为您|已给您|已经为您)?(?:删除|删掉)(?:了|掉)?|"
    r"文件已经(?:不在|没了)|已经移到回收站",
    re.I,
)

_PATH_IN_TEXT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\n\"'<>|]{3,240})"
    r"|(?:/(?:Users|home)/[^\s\n\"'<>|]{3,240})",
)


def _resolve_delete_path(path: str) -> str:
    """模糊解析成真实绝对路径；解析失败则退回原串。"""
    p = (path or "").strip()
    if not p:
        return ""
    if os.path.exists(os.path.expanduser(p)):
        return os.path.abspath(os.path.expanduser(p))
    try:
        from kekesi.tools.file_ops import resolve_user_path

        found = resolve_user_path(p)
        if found and os.path.exists(found):
            return found
    except Exception as e:
        logger.debug("resolve pending path failed: %s", e)
    return p


def get_pending_paths(memory) -> list[str]:
    """返回全部待删绝对路径（兼容单文件旧键）。"""
    if memory is None:
        return []
    try:
        multi = (memory.get_pref(PENDING_PATHS_KEY) or "").strip()
        if multi:
            out = []
            seen: set[str] = set()
            for line in multi.splitlines():
                p = line.strip()
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
            if out:
                return out
        path = (memory.get_pref(PENDING_PATH_KEY) or "").strip()
        return [path] if path else []
    except Exception:
        return []


def get_pending(memory) -> tuple[str, str]:
    paths = get_pending_paths(memory)
    path = paths[0] if paths else ""
    try:
        func = (
            (memory.get_pref(PENDING_FUNC_KEY) or "delete_file").strip()
            if memory is not None
            else "delete_file"
        ) or "delete_file"
    except Exception:
        func = "delete_file"
    return path, func


def set_pending(memory, path: str, func: str = "delete_file") -> str:
    """记下待删路径（先模糊解析）。返回实际写入的路径。"""
    if memory is None:
        return ""
    p = _resolve_delete_path(path)
    if not p:
        return ""
    fn = (func or "delete_file").strip() or "delete_file"
    if fn not in ("delete_file", "move_to_trash"):
        fn = "delete_file"
    try:
        memory.set_pref(PENDING_PATH_KEY, p)
        memory.set_pref(PENDING_PATHS_KEY, p)
        memory.set_pref(PENDING_FUNC_KEY, fn)
        logger.info("pending delete set: %s (%s)", p[:120], fn)
    except Exception as e:
        logger.debug("set pending delete failed: %s", e)
        return ""
    return p


def set_pending_batch(
    memory,
    paths: list[str],
    func: str = "delete_file",
) -> list[str]:
    """记下多个待删路径（「删掉所有 Word」）。"""
    if memory is None or not paths:
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        p = _resolve_delete_path(raw)
        if not p or not os.path.exists(p) or p in seen:
            continue
        seen.add(p)
        resolved.append(p)
    if not resolved:
        return []
    fn = (func or "delete_file").strip() or "delete_file"
    if fn not in ("delete_file", "move_to_trash"):
        fn = "delete_file"
    try:
        memory.set_pref(PENDING_PATH_KEY, resolved[0])
        memory.set_pref(PENDING_PATHS_KEY, "\n".join(resolved))
        memory.set_pref(PENDING_FUNC_KEY, fn)
        logger.info("pending delete batch set: %d files (%s)", len(resolved), fn)
    except Exception as e:
        logger.debug("set pending batch failed: %s", e)
        return []
    return resolved


def clear_pending(memory) -> None:
    if memory is None:
        return
    try:
        memory.set_pref(PENDING_PATH_KEY, "")
        memory.set_pref(PENDING_PATHS_KEY, "")
        memory.set_pref(PENDING_FUNC_KEY, "")
    except Exception:
        pass


def user_confirms_pending_delete(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if any(m in t for m in _HARD_CONFIRM):
        return True
    return bool(_SOFT_CONFIRM_RE.match(t))


def user_wants_delete(text: str) -> bool:
    return bool(_DELETE_INTENT_RE.search((text or "").strip()))


def extract_paths_from_tool_text(text: str) -> list[str]:
    """从 list/search 工具输出里抽绝对路径。"""
    raw = text or ""
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_IN_TEXT_RE.finditer(raw):
        p = m.group(0).rstrip(")。,]}")
        if p not in seen and os.path.exists(p):
            seen.add(p)
            found.append(p)
    # 也扫「桌面相对」行：纯文件名
    for line in raw.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or line.startswith("找到") or "未找到" in line:
            continue
        if os.path.isabs(line) and os.path.exists(line) and line not in seen:
            seen.add(line)
            found.append(line)
    return found


def maybe_set_pending_from_list_result(
    memory,
    user_text: str,
    tool_name: str,
    tool_text: str,
) -> str:
    """用户本轮在删，且 list/search 得到路径 → 提前 pending（支持「所有 Word」批量）。"""
    if memory is None or not user_wants_delete(user_text):
        return ""
    if tool_name not in ("list_desktop_files", "search_files"):
        return ""
    paths = extract_paths_from_tool_text(tool_text)
    if not paths:
        return ""
    # 「删掉所有 Word/文档」→ 批量挂起匹配后缀
    if _ALL_TARGETS_RE.search(user_text or "") and _WORD_TARGET_RE.search(
        user_text or ""
    ):
        wordish = [
            p
            for p in paths
            if os.path.splitext(p)[1].lower() in (".docx", ".doc")
        ]
        if wordish:
            got = set_pending_batch(memory, wordish, "delete_file")
            return "\n".join(got) if got else ""
    if len(paths) == 1:
        return set_pending(memory, paths[0], "delete_file")
    # 多条时：用用户话里的关键词模糊收窄到唯一
    if len(paths) > 1:
        try:
            from kekesi.tools.file_ops import _path_basename_query, _score_name_match

            q = _path_basename_query(user_text)
            if q and _DELETE_INTENT_RE.search(q):
                q = _DELETE_INTENT_RE.sub("", q).strip()
            scored = [
                (_score_name_match(q or user_text, os.path.basename(p)), p) for p in paths
            ]
            scored = [x for x in scored if x[0] >= 50]
            scored.sort(key=lambda x: -x[0])
            if len(scored) == 1 or (
                len(scored) >= 2 and scored[0][0] >= scored[1][0] + 15
            ):
                return set_pending(memory, scored[0][1], "delete_file")
        except Exception:
            pass
    return ""


def try_consume_pending_delete(
    user_text: str,
    memory,
    tool_ctx,
) -> tuple[str, list[dict]] | None:
    """若本轮是确认且有待删路径，直接删（支持批量），返回 (reply, tools)。"""
    paths = get_pending_paths(memory)
    if not paths:
        return None
    if not user_confirms_pending_delete(user_text):
        return None

    _, func = get_pending(memory)
    from kekesi.r1_executor import STATUS_SUCCESS, execute_call, r1_to_tool_output
    from kekesi.safety.guard import check_tool_call

    tools: list[dict] = []
    ok_names: list[str] = []
    fail_bits: list[str] = []
    for raw in paths:
        path = _resolve_delete_path(raw) or raw
        name = path.replace("\\", "/").split("/")[-1]
        allowed, deny = check_tool_call(
            func,
            {"path": path},
            user_text="确认执行",
            recent_assistant="",
        )
        if not allowed:
            fail_bits.append(f"「{name}」: {deny}")
            tools.append(
                {"func": func, "args": {"path": path}, "ok": False, "text": deny}
            )
            continue
        resp = execute_call({"func": func, "args": {"path": path}}, tool_ctx)
        out = r1_to_tool_output(resp)
        out_ok = resp.status == STATUS_SUCCESS and out.ok
        text = out.text or ""
        tools.append(
            {
                "func": func,
                "args": {"path": path},
                "ok": out_ok,
                "text": text[:240],
            }
        )
        if out_ok:
            ok_names.append(name)
        else:
            fail_bits.append(f"「{name}」: {text[:80]}")

    clear_pending(memory)
    if ok_names and not fail_bits:
        if len(ok_names) == 1:
            return f"好的，已删除「{ok_names[0]}」。", tools
        return f"好的，已删除 {len(ok_names)} 个文件：" + "、".join(ok_names) + "。", tools
    if ok_names and fail_bits:
        return (
            f"已删除 {len(ok_names)} 个（" + "、".join(ok_names) + "）；"
            f"失败：{'；'.join(fail_bits)}"
        ), tools
    return f"删除没成功：{'；'.join(fail_bits) or '未知原因'}", tools


def pending_hint(memory) -> str:
    paths = get_pending_paths(memory)
    if not paths:
        return ""
    if len(paths) == 1:
        path = paths[0]
        name = path.replace("\\", "/").split("/")[-1]
        return (
            f"[系统] 待确认删除：{path}（文件名「{name}」）。"
            f"用户说确认/确认执行/直接删掉/删了它时，优先 delete_file 该绝对路径，"
            f"勿改名、勿猜后缀。未确认前禁止声称已删除。"
        )
    names = [p.replace("\\", "/").split("/")[-1] for p in paths[:12]]
    return (
        f"[系统] 待确认批量删除共 {len(paths)} 个："
        + "、".join(names)
        + ("…" if len(paths) > 12 else "")
        + "。用户说确认/全部删掉时按下列绝对路径逐个 delete_file，勿改后缀："
        + " | ".join(paths[:8])
        + "。未确认前禁止声称已删除。"
    )


def scrub_fake_deleted_reply(reply: str, memory, *, deleted_ok: bool) -> str:
    """有 pending 且本轮未真正删成时，改掉「已删除」幻觉话术。"""
    if deleted_ok or memory is None:
        return reply or ""
    paths = get_pending_paths(memory)
    if not paths:
        return reply or ""
    t = (reply or "").strip()
    if not t or not _FAKE_DELETED_RE.search(t):
        return t
    if len(paths) == 1:
        name = paths[0].replace("\\", "/").split("/")[-1]
        return (
            f"还没删。已记下待删文件「{name}」（{paths[0]}），"
            f"请说「确认执行」或「确认」后再删。"
        )
    names = [p.replace("\\", "/").split("/")[-1] for p in paths[:8]]
    return (
        f"还没删。已记下待删 {len(paths)} 个文件："
        + "、".join(names)
        + "。请说「确认」或「全部删掉」后再删。"
    )
