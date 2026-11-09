"""意图路由共用工具 — 触发词归一化、动作 payload 解析、相似度匹配"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

_STRIP_PUNCT = re.compile(r"[\s。，,.!?；;、：:\"'「」【】（）()]+")
_FILLER_PREFIX = re.compile(r"^(帮我|请|麻烦)")
_OPEN_VERB = re.compile(r"^(打开|启动|运行|开)")
_CLOSE_VERB = re.compile(r"^(关闭|关掉|退出|关)")
_SUFFIX = re.compile(r"(一下|下|吧|呢|哈)$")

# 动词识别（全库唯一来源；其它模块勿再写一套）
OPEN_VERB_ANYWHERE_RE = re.compile(r"(?:打开|启动|运行|open|launch)", re.I)
CLOSE_VERB_ANYWHERE_RE = re.compile(r"(?:关闭|关掉|退出|close|quit)", re.I)
OBJECT_AFTER_VERB_RE = re.compile(
    r"(?:打开|启动|运行|关闭|关掉|open|launch|close|quit)\s*([^\s，,。！？]{2,20})",
    re.I,
)
# 行为模式学习用（观察用户说法结构，非路由表）
OPEN_APP_OBSERVE_RE = re.compile(r"打开(.{1,20})")
CLOSE_APP_OBSERVE_RE = re.compile(r"关(掉|闭)(.{1,20})")

# 学习时：说法相似度 ≥ 此值则合并到已有 trigger，不新建条目
ACTION_TRIGGER_MERGE_SIM = 0.82

# 微信联系人：从用户原话提取 + 搜索框 2～4 字模糊匹配
_IM_CONTACT_RE = re.compile(
    r"给\s*([^\s，,。！？：:]{2,20}?)\s*(?:发|回|说|聊|消息|微信)",
)
_CJK_DIGIT_RUN = re.compile(r"[\u4e00-\u9fff0-9]+")


def normalize_trigger(text: str) -> str:
    return _STRIP_PUNCT.sub("", (text or "").strip().lower())


def extract_action_core(text: str) -> tuple[str | None, str]:
    """拆出操作方向（o=开 / c=关）与对象名，便于「开XX」≈「打开XX」。"""
    s = normalize_trigger(text)
    if not s:
        return None, ""
    s = _FILLER_PREFIX.sub("", s)
    op: str | None = None
    m_close = _CLOSE_VERB.match(s)
    if m_close:
        op = "c"
        s = s[m_close.end():]
    else:
        m_open = _OPEN_VERB.match(s)
        if m_open:
            op = "o"
            s = s[m_open.end():]
    s = _SUFFIX.sub("", s)
    return op, s


def parse_action_payload(payload: str) -> tuple[str, dict] | None:
    raw = (payload or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tool = str(data.get("tool") or "").strip()
    if not tool:
        return None
    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    return tool, args


def trigger_similarity(user_text: str, trigger: str) -> float:
    """0~1：用户说法与已存 trigger 的相近度（支持换说法，不要求原话一致）。"""
    q = (user_text or "").strip()
    t = (trigger or "").strip()
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0

    nq, nt = normalize_trigger(q), normalize_trigger(t)
    if nq == nt:
        return 0.98

    op_q, core_q = extract_action_core(q)
    op_t, core_t = extract_action_core(t)

    # 开 vs 关 不互相匹配
    if op_q and op_t and op_q != op_t:
        return 0.0

    # 只有动词、对象为空（如裸「打开」）不应匹配带具体对象的长 trigger
    if (not core_q or len(core_q) < 2) and core_t and len(core_t) >= 2:
        if op_q and op_t and op_q == op_t:
            return 0.35
        return 0.0
    if (not core_t or len(core_t) < 2) and core_q and len(core_q) >= 2:
        return 0.0

    # 对象名一致（开XX ≈ 打开XX ≈ 帮我打开XX）
    if core_q and core_t and core_q == core_t:
        return 0.94 if op_q and op_t else 0.92

    if core_t and core_t in core_q:
        ratio = len(core_t) / max(len(core_q), 1)
        return 0.86 + 0.1 * min(ratio, 1.0)
    if core_q and core_q in core_t:
        ratio = len(core_q) / max(len(core_t), 1)
        return 0.84 + 0.1 * min(ratio, 1.0)

    if nt and nt in nq:
        return 0.9
    if nq and nq in nt:
        return 0.88
    if t in q or q in t:
        ratio = min(len(t), len(q)) / max(len(t), len(q), 1)
        return 0.75 + 0.15 * ratio

    # 模糊字符相似（错别字、少字）
    compare_a = f"{op_q or ''}:{core_q}" if core_q else nq
    compare_b = f"{op_t or ''}:{core_t}" if core_t else nt
    if compare_a and compare_b:
        sm = SequenceMatcher(None, compare_a, compare_b).ratio()
        if sm >= 0.55:
            return 0.52 + 0.45 * sm

    return 0.0


def wechat_contact_query(name: str) -> str:
    """微信搜索框用的联系人关键词：2～4 字模糊匹配，不必全名。"""
    raw = (name or "").strip()
    if not raw:
        return ""
    chunks = _CJK_DIGIT_RUN.findall(raw)
    core = "".join(chunks) if chunks else _STRIP_PUNCT.sub("", raw)
    if not core:
        return raw[:4]
    if len(core) <= 4:
        return core
    return core[:4]


def extract_im_contact(user_text: str) -> str:
    """从「给某某发消息」类原话提取联系人搜索关键词。"""
    m = _IM_CONTACT_RE.search(user_text or "")
    if not m:
        return ""
    return wechat_contact_query(m.group(1))
