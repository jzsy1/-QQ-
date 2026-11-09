"""Session 工作记忆 — 唤醒期间保留多轮 user/assistant 原文 + 工具事实。"""

from __future__ import annotations

import threading

# 约 8 轮对话（user+assistant 各一条），支撑指代/追问/确认
_MAX_MESSAGES = 16
_MAX_TOOL_FACTS = 8
_MAX_SESSIONS = 32
# 普通工具事实上限；列表类事实（带绝对路径）用更长上限保住指代依据
_TOOL_FACT_CHARS = 180
_TOOL_FACT_LIST_CHARS = 480


class SessionStore:
    def __init__(
        self,
        max_messages: int = _MAX_MESSAGES,
        max_sessions: int = _MAX_SESSIONS,
    ) -> None:
        self._max = max(2, max_messages)
        self._max_sessions = max(1, max_sessions)
        self._lock = threading.Lock()
        self._histories: dict[str, list[dict]] = {}
        self._tool_facts: dict[str, list[str]] = {}
        self._slots: dict[str, dict] = {}
        self._access_order: list[str] = []

    def _touch(self, sid: str) -> None:
        if sid in self._access_order:
            self._access_order.remove(sid)
        self._access_order.append(sid)
        while len(self._access_order) > self._max_sessions:
            old = self._access_order.pop(0)
            self._histories.pop(old, None)
            self._tool_facts.pop(old, None)
            self._slots.pop(old, None)

    # ── 结构化任务状态槽位（Anthropic 结构化笔记思路：系统维护固定槽位，
    #    而非依赖自然语言 fact 的截断残片）──

    def set_slot(self, session_id: str, key: str, value) -> None:
        """记录结构化任务状态：last_files / last_app / now_playing 等。"""
        if value is None or value == "" or value == []:
            return
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            self._touch(sid)
            self._slots.setdefault(sid, {})[key] = value

    def get_slots(self, session_id: str) -> dict:
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            return dict(self._slots.get(sid, {}))

    def get_slots_prompt(self, session_id: str) -> str:
        """渲染为注入 system 的任务状态块；无槽位时返回空串。"""
        slots = self.get_slots(session_id)
        if not slots:
            return ""
        lines = ["【当前任务状态】"]
        last_files = slots.get("last_files")
        if isinstance(last_files, list) and last_files:
            lines.append("- 最近列出的文件: " + "、".join(str(p) for p in last_files))
        elif isinstance(last_files, str) and last_files:
            lines.append(f"- 最近列出的文件: {last_files}")
        if slots.get("last_app"):
            lines.append(f"- 最近操作的应用: {slots['last_app']}")
        if slots.get("now_playing"):
            lines.append(f"- 正在播放: {slots['now_playing']}")
        lines.append("用户说「它/那个/刚才的」时优先复用上述状态。")
        return "\n".join(lines)

    def get_messages(self, session_id: str) -> list[dict]:
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            return list(self._histories.get(sid, []))

    def append_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        user = (user_text or "").strip()
        assistant = (assistant_text or "").strip()
        if not user:
            return
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            self._touch(sid)
            hist = self._histories.setdefault(sid, [])
            hist.append({"role": "user", "content": user})
            if assistant:
                hist.append({"role": "assistant", "content": assistant})
            if len(hist) > self._max:
                self._histories[sid] = hist[-self._max :]

    def append_tool_fact(
        self,
        session_id: str,
        summary: str,
        *,
        max_len: int = _TOOL_FACT_CHARS,
    ) -> None:
        """记下本会话最近工具事实（短摘要，供下轮指代/确认）。

        列表类结果（list_desktop_files/search_files）传 max_len=_TOOL_FACT_LIST_CHARS，
        保住绝对路径供「打开刚才那个文件」等指代复用。
        """
        s = (summary or "").strip()
        if not s:
            return
        limit = max(80, int(max_len))
        if len(s) > limit:
            s = s[: limit - 1] + "…"
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            self._touch(sid)
            facts = self._tool_facts.setdefault(sid, [])
            facts.append(s)
            if len(facts) > _MAX_TOOL_FACTS:
                self._tool_facts[sid] = facts[-_MAX_TOOL_FACTS:]

    def get_tool_facts(self, session_id: str) -> list[str]:
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            return list(self._tool_facts.get(sid, []))

    def get_tool_facts_prompt(self, session_id: str) -> str:
        facts = self.get_tool_facts(session_id)
        if not facts:
            return ""
        lines = "\n".join(f"- {f}" for f in facts)
        return (
            "【本会话最近工具事实·默认接续依据】\n"
            f"{lines}\n"
            "上一轮执行过工具时，本轮默认视为接续：理解指代（他/它/那个/这些/刚才）"
            "与省略（「删掉」指上一步列出的文件、「发给他」指上一步定位的联系人、"
            "「继续」指上一步未完流程）时，优先复用这里的绝对路径与 pending_delete，"
            "勿重猜文件名或后缀；口头答应≠已执行。"
        )

    def clear(self, session_id: str) -> None:
        sid = (session_id or "default").strip() or "default"
        with self._lock:
            self._histories.pop(sid, None)
            self._tool_facts.pop(sid, None)
            self._slots.pop(sid, None)
            if sid in self._access_order:
                self._access_order.remove(sid)

    def prune_idle(self, *, max_sessions: int | None = None) -> int:
        """淘汰最久未访问的会话，返回清除数量。"""
        limit = max_sessions if max_sessions is not None else self._max_sessions
        removed = 0
        with self._lock:
            while len(self._access_order) > limit:
                old = self._access_order.pop(0)
                if old in self._histories or old in self._tool_facts:
                    removed += 1
                self._histories.pop(old, None)
                self._tool_facts.pop(old, None)
                self._slots.pop(old, None)
        return removed
