"""Agent Loop — 龙虾式 LLM ↔ 工具原生循环；记忆仅作参考，不抢先路由。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Callable

from .brain import Brain
from .context_builder import build_memory_context
from .llm_config import is_local_llm_url
from .execution_critic import execute_with_critic
from .contract import DiscoveryLimits, is_tool_visible
from .r1_executor import (
    STATUS_EXECUTING,
    STATUS_FUNC_NOT_FOUND,
    STATUS_SUCCESS,
    execute_call,
    autonomous_tool_decision,
    autonomy_policy_violated,
    mark_autonomy_policy_violation,
    r1_to_tool_output,
    tool_call_budget_exceeded,
)
from .personas import resolve_system_prompt
from .r2_persona import progress_hint, resolve_persona
from .safety.guard import check_tool_call
from .safety.activity_guard import clear_agent_control
from .session_store import SessionStore, _TOOL_FACT_LIST_CHARS
from .tools.executor import ToolContext
from .tools.param_normalize import normalize_tool_params, remap_kill_to_close_app
from .tools.registry import get_tool_names, get_tool, register_builtin_tools
from .tools_policy import is_tool_enabled
from .tools.tool_retriever import (
    _NEEDS_TOOL_HINT_RE,
    should_expand_tool_tier,
    tools_for_agent_loop,
)
from .tools.tool_bm25 import rank_tools_by_text
from .utils import parse_json_dict
from .intent_utils import extract_action_core

logger = logging.getLogger(__name__)

_LIMITS = DiscoveryLimits()

_MAX_ITERATIONS = 200  # GOAL_CTRL_V1: safety fuse only (was 12); normal stop is goal/stagnation/budget via GoalController
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…])")
# 零宽/不可见字符过滤：gemma 等模型在工具循环后常吐出 U+200B 等字符，
# 若不滤掉会被当成"有效 content"，导致 final_reply 为不可见字符，
# _stream_text 内部 strip 后为空不调 on_chunk，TTS 收不到任何文本。
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\u00ad\ufeff\u2060]")
_DESKTOP_GUI_WORKFLOW = (
    "【桌面 GUI 操作标准流程】在已安装软件（微信、QQ 等）里发消息、回消息、点界面时，"
    "必须分步调用基础工具，禁止跳步或只用口头描述："
    "1) open_app 打开目标程序；"
    "2) read_screen 或 ocr_screen 观察当前界面状态，根据截图决定下一步；"
    "3) 微信找联系人：focus_window 确保前台 → 点左栏搜索区(gui_action/click_at) 或 send_keys(^f)，"
    "type_text 输入 2～4 字关键词，send_keys({DOWN}{ENTER}) 选中下拉第一项；"
    "禁止仅在侧边栏 partial 点击联系人名（易误触、易留在文件传输助手）；"
    "4) 发消息前 ocr_find_text 限定聊天标题区(region) 确认已是目标联系人；"
    "5) gui_action 或 click_at 点输入框 → type_text → send_keys({ENTER}) 发送；"
    "6) minimize_window 最小化窗口。"
    "需要快速连续操作时，可先调 inspect_window_som 获取窗口内 [#编号] 控件清单，"
    "再用 execute_gui_macro 按编号一次性完成「点击+输入+回车」（内部会自动借还用户焦点与鼠标）；"
    "单步精细操作则执行后再 read_screen 确认界面变化。"
    "禁止在未看屏的情况下盲目点击；禁止重复 open_app。"
    "【查未读/回消息流程】用户问「谁找我」「有没有新消息」或要回消息时："
    "1) open_app 打开 IM → read_screen 看左侧会话列表，找带未读角标/红点的会话；"
    "2) gui_action 点开该会话 → read_screen 读最近几条消息；"
    "3) 先口头向用户汇报是谁、说了什么，再问是否回复、回什么；"
    "4) 用户给出内容后按发消息流程输入并发送；"
    "5) 多个未读会话就逐个处理，处理完 minimize_window。"
    "【发送内容确认纪律】用户逐字口述的内容可直接发送；"
    "若回复内容是你代拟的（用户只说「帮我回一下」「随便回个」），"
    "发送前必须先把拟好的内容念给用户，等用户同意后再 type_text 发送。"
)
_MUSIC_WORKFLOW = (
    "【听歌流程·已由系统写死】用户要听歌时优先走体内网页播放器，禁止 search_web，"
    "禁止 open_app 打开 QQ/网易云/酷狗客户端："
    "1) 点歌：play_music 的 query 填歌名，source 可选 qq/netease/kugou（用户点名时传入）；"
    "2) 「我喜欢」易歧义：说「我喜欢列表/歌单/我喜欢的歌/我的音乐里的我喜欢/红心/收藏」→ "
    "play_music(playlist=liked)；说「我想听我喜欢」「播放《我喜欢》」→ query=我喜欢 按歌名搜；"
    "仅「播放我喜欢」说不清时先问一句是歌单还是歌名；"
    "3) 未指定平台时用用户默认播放器；首次未选会由系统询问；"
    "4) 暂停/下一首用 control_music；登录用 qqmusic_login（可带 source）；"
    "5) 仅当用户明确要求打开桌面客户端时才 open_app。"
)
_VIDEO_WORKFLOW = (
    "【看剧流程·已由系统写死】用户要看剧/电影时优先走科科斯视频空间网页播放器，"
    "禁止 open_app 打开腾讯视频/爱奇艺/B站桌面客户端："
    "1) 具体剧名 → play_video(query=剧名)；明确「第N集」传 episode=N；"
    "说「最新一集/更新到哪了」设 latest=true，禁止瞎猜集数；"
    "2) 模糊需求（新出来的/热播/推荐/好看的电视剧等）：可先 search_web 查近期热播具体剧名，"
    "再 play_video(query=具体剧名)；禁止把「新出来的电视剧」整句当 query；"
    "3) source 可选 tencent/bilibili/iqiyi；未登录用 video_login；关掉用 close_video；"
    "4) 仅当用户明确要求打开桌面客户端时才 open_app。"
)
_LOOP_SYSTEM_BASE = (
    "\n\n【Agent 模式】你是自主 Agent，通过 function calling 调用工具完成任务。"
    "用户说的是要你去执行的指令，不是让你照原话复述或口头应付；"
    "能调工具就立刻调工具，禁止只回复「好的/这就帮你/正在播放」却不调用工具。"
    "【系统预取】如果上下文中有 [系统预取·工具名] 的消息，说明该工具已由系统预先执行，"
    "你必须直接使用该结果生成口语化回复，禁止再次调用同一工具。"
    "「记忆参考」只供偏好检索与上下文参考，与当前问题无关必须忽略；"
    "禁止根据记忆/历史偏好发起用户本轮未点名的默认动作"
    "（例如曾说不喜欢弹幕，本轮只点播时不要自行关弹幕，也不要口头假装已关）。"
    "要把偏好做成自动执行，须走已有工具参数或 Sentinel 规则，禁止只靠推理口头落实。"
    "【桌面感知】桌面文件/图标标准流程："
    "1) 开/删文件：list_desktop_files 取完整路径 → 打开用 open_path(path)，删除用 delete_file(path)；"
    "2) 点桌面图标：优先 click_desktop_icon(name)（内部会先露出桌面再双击）；"
    "   或 show_desktop → list_desktop_icons → click_at(x,y,double=true)；"
    "3) 窗口操作：list_open_windows 看清标题/句柄后再 focus_window/close_window；"
    "禁止瞎猜路径与坐标。"
    "【工具发现】当可用工具中没有你需要的功能时（如查IP、蓝牙、天气、系统状态等），"
    "先调用 search_tools(query=\"自然语言描述\") 搜索匹配工具，再调用返回的工具。"
    "不要用 search_web 替代专用工具。"
    "确认库里确实没有合适工具时，可调用 acquire_skill(need=\"要补的能力，自然语言说清\") "
    "上网找现成的技能/插件并自动装好，然后直接调用返回的新工具完成任务；"
    "这是补能力的正路，禁止只回一句「我做不了」就收场。"
    "用户想卸载/删除已装的技能或插件时，调用 uninstall_skill(skill_ref=\"技能 id 或名字\")，"
    "拿不准名字就先留空调用一次拿到已装清单再卸。"
    "注意：gui_action/click_text/type_text 只是通用鼠标键盘，不代表你拥有某个平台的专用能力。"
    "当用户要的是某个 App/平台/服务的既有功能（发动态、发帖、下单、抢购、签到、改资料等），"
    "而工具清单里没有对应的专用工具时，就属于「缺能力」，必须先 acquire_skill 找现成实现，"
    "禁止拿通用点击工具硬拼碰运气。"
    "看屏幕用 read_screen（历史截图用 recall_screenshot）；精确文字坐标用 ocr_screen。"
    "点击优先 gui_action。闲聊直接回复，不要多余调工具。"
    "每次优先只调一个工具；打开/关闭软件必须调 open_app/close_app，禁止口头假装已执行。"
    "每轮最多调用 3 个工具，不要一次性吐出长计划；先完成当前步骤，再根据结果继续下一步。"
    "需要「打开应用后立即输入内容」时用 open_app_and_type 一体化工具"
    "（内部已等窗口聚焦），不要分开 open_app 再 type_text，避免输错窗口。"
    "工具成功后用一句口语概括，不要念工具返回全文。参数数字用阿拉伯数字。"
    "破坏性操作须用户明确说「确认执行」。"
    "禁止执行/生成：格式化磁盘、清空系统盘、修改启动配置(diskpart/bcdedit)、"
    "直接删除系统注册表项、关闭或绕过系统安全防护(Defender/UAC/防火墙)、"
    "导出或读取系统凭据。用户要求这类动作时，直接告知需人工处理，不要用工具尝试。"
    "连续多轮【默认接续】：上一轮执行过工具时（【本会话最近工具事实】非空），"
    "本轮默认视为同一任务接续，结合会话历史与工具事实理解指代（他/它/那个/这个/这些/刚才）"
    "与省略（「发个你好」→发给上一步定位的联系人、「删掉」→指上一步列出的文件、"
    "「继续/下一个」→接上一步未完的流程）；"
    "优先复用已解析的绝对路径、窗口句柄、联系人，勿重新瞎猜文件名或后缀。"
    "仅当本轮明显转向与上一轮对象/目标无关的新主题时才独立处理；"
    "接续同一任务不算翻旧账，禁止的只是无故翻无关旧账。"
    "禁止「请稍等」「我这就去查」——当场做完或直接答。"
)
# 辅助/只读类工具：执行失败「跳过不中断」——不触发 tier 展开，
# 避免截图、查时间等无害失败把流程带偏（AnovaX optional/core 分级思想）
_OPTIONAL_TOOLS = frozenset({
    "take_screenshot",
    "read_screen",
    "ocr_screen",
    "inspect_window_som",
    "recall_screenshot",
    "get_current_time",
    "get_network_info",
    "get_system_status",
    "get_cpu_usage",
    "get_memory_usage",
    "get_battery_status",
    "get_disk_usage",
    "list_open_windows",
})
_IM_INTENT_RE = re.compile(
    r"微信|QQ|钉钉|飞书|企业微信|发消息|回消息|回个?(?:消息|微信)|"
    r"回复|未读|谁找我|有没有人找|聊天|联系人"
)
_MUSIC_INTENT_RE = re.compile(
    r"QQ音乐|听歌|放歌|点歌|来一首|放一?首|暂停音乐|下一首|上一首|"
    r"我想听|我要听|想听|要听|帮我放|帮我听|"
    r"我喜欢(?:的)?(?:歌单|列表)|我喜欢的歌|红心歌单|我的收藏|"
    r"播放(?!.*(?:剧|电影|视频|综艺|动漫|纪录片|第\s*\d+\s*集|第.+集))|"
    r"播放.{0,12}(?:歌|曲|音乐)|听.{0,12}(?:歌|音乐|专辑)|"
    r"(?:请|帮我|给我)?(?:听|(?<![播])放)\s*[\u4e00-\u9fffA-Za-z0-9·《》]{1,40}|"
    r"(?:放|听|播)(?:一下)?(?:刚才|刚刚|那首|这首)"
)
_MEDIA_INTENT_RE = re.compile(
    r"腾讯视频|爱奇艺|优酷|哔哩|B\s*站|芒果|看剧|追剧|看电影|看视频|"
    r"我想看|想看|我要看|要看|帮我看|给我看|"
    r"第\d+集|最新一?集|更新到|下一集|上一集|"
    r"^(?:请|帮我|给我)?看\s*[\u4e00-\u9fffA-Za-z0-9·《》]{2,40}$|"
    r"看.{0,20}(?:第.+集|最新|电影|剧|动漫|综艺)|"
    r"播放.{0,12}(?:剧|电影|视频|综艺|动漫)|"
    r"(?:推荐|有什么好看|好看的|热播|新出).{0,8}(?:剧|电影|电视剧)?"
)
_GUI_INTENT_RE = re.compile(
    r"点|点击|屏幕|界面|窗口|桌面|输入|打字|滚动|OCR|识别"
)


def _build_loop_system_addendum(user_text: str) -> str:
    """按意图裁剪纪律：闲聊只带短底座，IM/影音才附长流程。"""
    t = user_text or ""
    parts = [_LOOP_SYSTEM_BASE]
    if _IM_INTENT_RE.search(t) or (
        _GUI_INTENT_RE.search(t)
        and any(k in t for k in ("微信", "QQ", "消息", "聊天"))
    ):
        parts.append(_DESKTOP_GUI_WORKFLOW)
    if _MUSIC_INTENT_RE.search(t):
        parts.append(_MUSIC_WORKFLOW)
    if _MEDIA_INTENT_RE.search(t):
        parts.append(_VIDEO_WORKFLOW)
    return "".join(parts)


# 兼容旧引用
_LOOP_SYSTEM_ADDENDUM = _LOOP_SYSTEM_BASE
_TOOL_RESULT_MAX_CHARS = _LIMITS.tool_result_max
_TOOL_RESULT_LIST_MAX_CHARS = _LIMITS.tool_result_list_max
# LLM 上下文窗口保守限制（字符数）兜底值：brain.context_max_chars 配置 >0 时覆盖，
# 0/缺省用此保守值（system prompt + 工具清单已占大量空间，搜索结果回注后极易溢出）。
# 超过预算时分层裁剪：旧 tool 消息占位化 → 旧历史轮摘要化 → 兜底截断。
_LLM_CONTEXT_MAX_CHARS = 12000
# 渐进披露阈值兜底：全池工具清单占上下文预算的比例超过它则 tier0 起步，
# 否则直接全池。brain.tool_briefing_max_ratio 配置 >0 时覆盖。
_TOOL_BRIEFING_MAX_RATIO = 0.45


def _summarize_history_turns(history_msgs: list[dict]) -> str:
    """把较早的对话轮次压成结构化摘要（轮次意图 + 绝对路径），规则式、零 LLM 调用。

    LangGraph SummarizationMiddleware 思路：旧轮次不裸丢也不裸留，
    压成「意图线 + 关键实体」保住指代依据。
    """
    lines: list[str] = []
    paths: list[str] = []
    seen_paths: set[str] = set()
    for m in history_msgs:
        role = m.get("role")
        content = str(m.get("content") or "").replace("\n", " ").strip()
        if not content:
            continue
        for p in re.findall(r"[A-Za-z]:[/\\][^\s，。；、\"'）)]{2,}", content):
            p = p.rstrip("\\/")
            if p not in seen_paths:
                seen_paths.add(p)
                paths.append(p)
        tag = "用户" if role == "user" else "助手"
        lines.append(f"{tag}: {content[:40]}")
    if not lines:
        return ""
    digest = " | ".join(lines[:12])
    parts = [f"【早期对话摘要】{digest}"]
    if paths:
        parts.append("历史涉及路径: " + "、".join(paths[:6]))
    return "\n".join(parts)


def _trim_messages_for_context(
    messages: list[dict],
    history_end: int | None = None,
    budget: int | None = None,
) -> list[dict]:
    """分层上下文预算（LangGraph 分层裁剪思路），窗口超限时依次：

    1. 旧 tool 消息占位化（保留最近 2 条完整）
    2. 旧 user/assistant 历史轮压缩为一条【早期对话摘要】（规则式摘要，
       保留最近 2 条历史原文与当前用户消息不动）
    3. 仍超限则截断摘要与保留的 tool 消息

    system 消息永不截断（工作流纪律/接续说明全在里面）。
    history_end: 真实历史切片右边界（不含），压缩只动历史区，
    不碰本轮注入的推测执行/系统预取消息。history_end=None 时不做历史压缩。
    budget: 上下文字符预算（brain.context_max_chars）；None/非正数用模块兜底值。
    """
    limit = budget if budget and budget > 0 else _LLM_CONTEXT_MAX_CHARS
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= limit:
        return messages

    # ── 第1层：旧 tool 消息占位化 ──
    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
    ]
    keep_tool: set[int] = set()
    if len(tool_indices) > 2:
        keep_tool = set(tool_indices[-2:])
        for i in tool_indices[:-2]:
            if i not in keep_tool:
                old = str(messages[i].get("content") or "")
                messages[i]["content"] = (
                    f"(已执行，结果过长已省略，关键词: {old[:60]}…)"
                )
        total = sum(len(str(m.get("content") or "")) for m in messages)
        if total <= limit:
            return messages

    # ── 第2层：旧历史轮 → 结构化摘要 ──
    if history_end is not None and history_end > 2:
        # 上界钳制：不越过当前用户消息（历史已压缩后 history_end 会失效，
        # 且 digest 消息以【早期对话摘要】开头时由 already 守卫跳过）
        _upper = max(1, min(history_end - 2, len(messages) - 1))
        zone = list(range(1, _upper))  # 保留最近 2 条历史原文
        zone_msgs = [messages[i] for i in zone]
        already = any(
            str(m.get("content") or "").startswith("【早期对话摘要】")
            for m in zone_msgs
        )
        if zone_msgs and not already:
            digest = _summarize_history_turns(zone_msgs)
            if digest:
                for i in reversed(zone):
                    del messages[i]
                messages.insert(1, {"role": "user", "content": digest})
                total = sum(len(str(m.get("content") or "")) for m in messages)
                if total <= limit:
                    return messages
                # 压缩后摘要本身超长则截断
                for m in messages:
                    c = str(m.get("content") or "")
                    if c.startswith("【早期对话摘要】") and len(c) > 500:
                        m["content"] = c[:500] + "…(摘要已截断)"
                total = sum(len(str(m.get("content") or "")) for m in messages)
                if total <= limit:
                    return messages

    # ── 第3层：兜底截断（摘要/保留的 tool/助手消息），用户消息永不截 ──
    for idx, m in enumerate(messages):
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool" and len(content) > 800:
            messages[idx]["content"] = content[:800] + "…(已截断)"
        elif role == "assistant" and len(content) > 300:
            messages[idx]["content"] = content[:300] + "…(已截断)"
        elif (
            role == "user"
            and content.startswith("【早期对话摘要】")
            and len(content) > 500
        ):
            messages[idx]["content"] = content[:500] + "…(摘要已截断)"

    return messages


# ──────────────────────────────────────────────────────────
# 推测执行：LLM 调用前预运行只读工具，省掉一轮 LLM 往返
# ──────────────────────────────────────────────────────────

# 关键词 → 只读工具映射（只放无副作用的查询类工具）
_SPECULATIVE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"几点|几点了|现在时间|什么时间|现在几点|目前时间|几号|今天.*号|日期"), "get_current_time"),
    (re.compile(r"^.{0,6}(?:IP|ip地址|网络信息|本机.*IP|查.*IP|网速|联网)"), "get_network_info"),
    (re.compile(r"^.{0,6}(?:系统状态|系统信息|系统概况|电脑状态|系统占用)"), "get_system_status"),
]


def _try_speculative_exec(
    user_text: str, tool_ctx: "ToolContext"
) -> str | None:
    """推测执行：在 LLM 调用前，预运行只读工具，隐藏 LLM 延迟。

    正常流程：LLM→tool_call→execute→LLM→reply（两轮 LLM 调用）
    推测执行：execute→LLM→reply（一轮 LLM 调用），省掉一轮 LLM 延迟。

    仅对短查询（≤15 字）且关键词明确匹配时触发，避免误触发复杂查询。
    """
    text = (user_text or "").strip()
    if not text or len(text) > 15:
        return None

    for pattern, tool_name in _SPECULATIVE_MAP:
        if not pattern.search(text):
            continue
        try:
            from .r1_executor import execute_call, r1_to_tool_output, STATUS_SUCCESS

            resp = execute_call(tool_name, {}, tool_ctx)
            if resp.status != STATUS_SUCCESS:
                logger.debug("推测执行 %s 状态非成功: %s", tool_name, resp.status)
                return None
            output = r1_to_tool_output(resp)
            if not output.ok or not output.text:
                return None
            return f"[系统预取·{tool_name}]\n{output.text}"
        except Exception as e:
            logger.debug("推测执行 %s 异常: %s", tool_name, e)
            return None
    return None


# 失败工具 → 预跑只读状态查询（自适应恢复：核心工具失败后，下一轮决策前
# 先拿到最新系统状态，避免模型再调查询工具浪费一轮往返）
_FAILURE_SPECULATIVE_MAP: dict[str, str] = {
    "open_app": "list_open_windows",
    "focus_window": "list_open_windows",
    "close_app": "list_open_windows",
    "get_network_info": "get_system_status",
    "search_web": "get_network_info",
}


def _try_failure_speculative_exec(
    executed_tools: list[dict], tool_ctx: "ToolContext"
) -> str | None:
    """失败后自适应恢复：上一轮核心工具失败时，预跑相关只读状态查询注入上下文。

    借鉴 AnovaX 恢复循环思想：静态计划失败后，在 LLM 重新决策前预跑只读工具，
    模型直接基于最新状态修正策略，而不是再调一轮查询工具。
    """
    if not executed_tools:
        return None
    for t in reversed(executed_tools):
        if t.get("ok"):
            continue
        func = str(t.get("func") or "")
        pre = _FAILURE_SPECULATIVE_MAP.get(func)
        if not pre:
            continue
        try:
            from .r1_executor import execute_call, r1_to_tool_output, STATUS_SUCCESS

            resp = execute_call(pre, {}, tool_ctx)
            if resp.status != STATUS_SUCCESS:
                logger.debug("失败恢复预跑 %s 非成功: %s", pre, resp.status)
                return None
            output = r1_to_tool_output(resp)
            if not output.ok or not output.text:
                return None
            return f"[系统预取·{pre}]\n{output.text}"
        except Exception as e:
            logger.debug("失败恢复预跑 %s 异常: %s", pre, e)
            return None
    return None


# 有界计划：限制每轮工具调用数量，防 LLM 一次性吐超长计划（AnovaX 有界计划思想）
_MAX_TOOL_CALLS_PER_ROUND = 3


# 承诺型回复：模型口头答应「这就去做」却不调工具，必须强制其继续执行
# 动词组：覆盖查询/操作/通讯/提醒/媒体/系统等各类动作
_PROMISE_VERBS = (
    r"查|搜|看|办|做|处理|执行|打开|关闭|整理|分析|创建|写入|生成|导出|保存|制作|写|"
    r"删|删除|清|清空|列|列出|移到回收站|发|发送|回复|回|提醒|定|设|设置|播|播放|截|截屏|截图|调|记|记下"
)
_PROMISE_RE = re.compile(
    # 即时承诺：这就/马上/立刻/立即/正在/接下来/现在 + 动作动词
    rf"(?:这就|马上|立刻|立即|正在|接下来|现在)(?:为您|给您|帮您|去)?[^。！？\n]{{0,24}}"
    rf"(?:{_PROMISE_VERBS})"
    # 应允型承诺：好的/没问题/行 + (我帮您/我给您/我来)? + 动作动词（"好的，我帮您发一下"）
    rf"|(?:好的?|没问题|行|嗯)[，,。]?\s*(?:我(?:帮|给)您?|我来)?[^。！？\n]{{0,12}}"
    rf"(?:{_PROMISE_VERBS})"
    # 虚假完成：已/已经 + 动作动词 + 了/好/完/过（无工具却声称做完）
    rf"|(?:已经|已)[^。！？\n]{{0,12}}(?:{_PROMISE_VERBS})(?:了|好|完|过)"
    # 简短拖延与即时词
    rf"|请?稍等|稍候|马上就好|我来(?:查|看|办|处理|创建|写|删|列)"
    rf"|正在为您|这就为您|马上为您|好的?[，,]?(?:我)?(?:这就|马上|去)"
)
# 承诺重试上限：超过仍只口头回复、且未执行任何工具 → 诚实兜底，不再接受假承诺
_MAX_PROMISE_RETRIES = 2


def _in_system_trigger(user_text: str, tool_calls: list[dict], trig: dict) -> bool:
    """系统代触判据：模型本轮「没用到任何专用工具」+ 用户要的是平台专用功能。

    两个条件都成立才代触：
    - 只看平台词会误伤（若库里/技能池有该平台专用实现，模型会调它，
      那它就不落在 non_dedicated 集里）；
    - 只看工具也误伤（「打开记事本写便签」是正常 GUI 流程，不该去装技能）。
    non_dedicated_tools = 通用兜底工具 + 能力发现类元工具 + 已弃用脚本，
    即「用了这些不算解决了平台专用能力」。配置见 config.yaml
    skill_acquisition.system_trigger（有内置默认）。
    """
    non_dedicated = {str(x) for x in (trig.get("non_dedicated_tools") or [])}
    words = [str(x).lower() for x in (trig.get("platform_words") or []) if str(x)]
    if not non_dedicated or not words:
        return False
    names = [
        str((tc.get("function") or {}).get("name") or "").strip() for tc in tool_calls
    ]
    names = [n for n in names if n]
    if not names or any(n not in non_dedicated for n in names):
        return False
    text = (user_text or "").lower()
    return any(w in text for w in words)


def _looks_like_promise(content: str) -> bool:
    text = (content or "").strip()
    if not text or len(text) > 160:
        return False
    return bool(_PROMISE_RE.search(text))


_ACTION_NEED_TOOL_RE = re.compile(
    r"(?:创建|生成|导出|写入|写成|保存).{0,12}(?:Word|WORD|word|docx|文档|文件)|"
    r"(?:打开|关闭|关掉|启动).{0,12}\S|"
    r"(?:删|删除|删掉|清掉|清空|扔掉|移到回收站|进回收站).{0,20}"
    r"(?:文件|文档|Word|WORD|word|docx|桌面|文件夹)?|"
    r"(?:列出|看看|有哪些|有什么).{0,12}(?:桌面|文件|文档|Word)|"
    r"(?:调|设置).{0,6}音量|关机|重启|截屏|截图|ocr|识别屏幕",
    re.I,
)

# 删除/列桌面：本轮若完全没碰这些工具，视为池子/选型跑偏
_DELETE_OR_LIST_FUNCS = frozenset({
    "list_desktop_files",
    "list_directory",
    "search_files",
    "delete_file",
    "move_to_trash",
})


def _user_request_needs_tool(user_text: str) -> bool:
    """用户话明显是要动手做事时，禁止纯口头收尾。"""
    return bool(_ACTION_NEED_TOOL_RE.search(user_text or ""))


def _user_wants_file_delete_or_list(user_text: str) -> bool:
    t = user_text or ""
    return bool(
        re.search(
            r"(?:删|删除|删掉|清掉|清空|扔掉|移到回收站)|"
            r"(?:列出|看看).{0,8}(?:桌面|Word|文档|文件)",
            t,
            re.I,
        )
    )


def _missed_file_delete_tools(user_text: str, executed_tools: list[dict]) -> bool:
    """删/列桌面意图下，本轮没调到关键文件工具 → 需要纠偏再跑。"""
    if not _user_wants_file_delete_or_list(user_text):
        return False
    funcs = {str(t.get("func") or t.get("name") or "") for t in (executed_tools or [])}
    return not bool(funcs & _DELETE_OR_LIST_FUNCS)


_LOCAL_TOOL_TOP_K = _LIMITS.local_model_top_k
_APP_TOOL_FUNCS = frozenset({"open_app", "close_app", "open_app_and_type"})
_TIER_EXPAND_USER_NOTE = (
    "[系统] 已为你展开完整工具列表（含文件、窗口、MCP 等），请选用合适工具继续完成任务。"
)
_MISSED_DELETE_HINT = (
    "[系统] 用户要处理桌面/文档文件，但你还没调用 list_desktop_files / "
    "delete_file / move_to_trash。请先 list_desktop_files(extensions=\"docx,doc\") "
    "看【分类摘要】，再删除；禁止空口声称没有 Word 或已删除。"
)


def _should_require_app_tool(user_text: str, tool_names: list[str]) -> bool:
    """打开/关闭且工具集已收窄时，强制模型必须调工具（防口头答应）。"""
    op, core = extract_action_core(user_text)
    if not core or op not in ("o", "c"):
        return False
    names = list(tool_names or [])
    if op == "o":
        return names == ["open_app"] or (
            len(names) <= 2 and "open_app" in names
        )
    if op == "c":
        allowed = {"close_app", "close_window"}
        return "close_app" in names and set(names).issubset(allowed)
    return False


def _should_require_action_tool(user_text: str, tool_names: list[str]) -> bool:
    """删除/列文件等动作：池里已有对应工具时强制 function calling。"""
    if not _user_request_needs_tool(user_text):
        return False
    names = set(tool_names or [])
    if _user_wants_file_delete_or_list(user_text):
        return bool(names & _DELETE_OR_LIST_FUNCS)
    return bool(names)


# ---------------------------------------------------------------------------
# 无 schema 模式：精简清单写进 system prompt，模型用 <tool_call>JSON</tool_call> 发起调用
# ---------------------------------------------------------------------------

_TOOL_CALL_TAG_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)
# 本地模型实测会漏掉闭合标签（只发 <tool_call>{"name":...}），需要单独兜底
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>", re.IGNORECASE)

# 模型自造乱标记（如 gemma 偶发输出 < | DSML | | invoke name="tool_call">…）
_MALFORMED_TOOL_MARKUP_RE = re.compile(r"DSML|\binvoke\b", re.IGNORECASE)
# 分隔符可能是半角 | 或全角 ｜（实测全角更常见，漏了它标记就剥不掉、直接漏给用户）
_TOOL_MARKUP_JUNK_RE = re.compile(r"<[^>\n]*[|｜][^>\n]*>")

# DeepSeek 原生 DSML 调用格式（实测：模型常不按我们给的 <tool_call> 走，而是输出这个）：
#   <｜｜DSML｜｜ invoke name="run_shell">
#   <｜｜DSML｜｜ parameter name="arguments">{"command": "..."}</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
# 工具名在 invoke 的 name 属性上、参数在 parameter 标签体内，必须专门解析，
# 否则调用丢失、标签被剥离后残留 JSON 直接漏给用户（实测事故）。
_DSML_INVOKE_OPEN_RE = re.compile(
    r"<[^>\n]*\binvoke\b[^>\n]*\bname\s*=\s*[\"']([^\"']+)[\"'][^>\n]*>",
    re.IGNORECASE,
)
_DSML_INVOKE_CLOSE_RE = re.compile(r"</[^>\n]*\binvoke\b[^>\n]*>", re.IGNORECASE)
_DSML_PARAM_RE = re.compile(
    r"<[^>\n]*\bparameter\b[^>\n]*\bname\s*=\s*[\"']([^\"']+)[\"'][^>\n]*>(.*?)"
    r"</[^>\n]*\bparameter\b[^>\n]*>",
    re.IGNORECASE | re.DOTALL,
)


def _iter_dsml_blocks(text: str) -> list[tuple[int, int, str, str]]:
    """产出 DSML invoke 块 (start, end, 工具名, 标签体)。end 为块结束的后一位。"""
    out: list[tuple[int, int, str, str]] = []
    if not text or ("DSML" not in text and "invoke" not in text.lower()):
        return out
    pos = 0
    while True:
        m = _DSML_INVOKE_OPEN_RE.search(text, pos)
        if not m:
            break
        close = _DSML_INVOKE_CLOSE_RE.search(text, m.end())
        if not close:
            break
        out.append(
            (m.start(), close.end(), m.group(1).strip(), text[m.end(): close.start()])
        )
        pos = close.end()
    return out


def _parse_dsml_tool_calls(text: str) -> list[dict]:
    """从 DSML invoke 块解析工具调用。"""
    out: list[dict] = []
    for i, (_start, _end, name, inner) in enumerate(_iter_dsml_blocks(text)):
        if not name:
            continue
        args: dict = {}
        for pm in _DSML_PARAM_RE.finditer(inner):
            key = pm.group(1).strip()
            if not key:
                continue
            raw = pm.group(2).strip()
            try:
                args[key] = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                args[key] = raw
        # 模型常把整个参数对象塞进名为 arguments/args 的单个 parameter
        if len(args) == 1:
            for alias in ("arguments", "args", "parameters"):
                if alias in args and isinstance(args[alias], dict):
                    args = args[alias]
                    break
        out.append(
            {
                "id": f"text_tc_dsml_{i}_{uuid.uuid4().hex[:8]}",
                "function": {"name": name, "arguments": args},
            }
        )
    return out


def _iter_json_objects(text: str):
    """产出 (start, end, parsed)：text 中所有顶层完整 JSON 对象（括号匹配）。

    parsed 解析失败时为 None；end 为闭区间后一位，可直接切片移除。
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        j = i
        end = -1
        while j < n:
            c = text[j]
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = not in_string
            elif not in_string:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            j += 1
        if end < 0:
            break
        try:
            data = json.loads(text[i : end + 1])
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
        yield i, end + 1, data
        i = end + 1


def _looks_like_tool_json(data: object) -> bool:
    """形如工具调用的 JSON：有字符串 name，且带 arguments/args/parameters 任一键。"""
    if not isinstance(data, dict):
        return False
    name = str(data.get("name") or "").strip()
    if not name:
        return False
    return any(k in data for k in ("arguments", "args", "parameters"))


def _extract_first_json(text: str) -> dict | None:
    """从文本中提取第一个完整的 JSON 对象（括号匹配，支持嵌套）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError, TypeError):
                    return None
    return None


def _build_tool_briefing(
    tool_names: list[str],
    mcp_bridge=None,
    skill_bridge=None,
) -> str:
    """构建精简工具清单，注入 system prompt（替代传 tools schema）。

    格式：- tool_name(param1, param2?): 一句话描述
    """
    lines: list[str] = []
    seen: set[str] = set()
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        entry = _format_tool_brief_line(name, mcp_bridge, skill_bridge)
        if entry:
            lines.append(entry)
    if not lines:
        return ""
    briefing = "\n".join(lines)
    return (
        "\n\n【可用工具清单】\n"
        f"{briefing}\n\n"
        "【调用方式】在回复中用以下格式发起工具调用（可连续多个）：\n"
        '<tool_call>{"name":"工具名","arguments":{"参数名":"值"}}</tool_call>\n'
        "调用后等工具结果返回再继续。闲聊直接回复文字，不要调工具。"
        "每次优先只调一个工具。参数用 JSON 对象，字符串值用双引号。"
    )


def _format_tool_brief_line(
    name: str, mcp_bridge=None, skill_bridge=None
) -> str | None:
    """渲染单个工具的精简描述行。"""
    # 内置工具
    t = get_tool(name)
    if t:
        desc = (t.get("description") or "").split("。")[0].split("；")[0][
            : _LIMITS.briefing_description_max
        ]
        params = (t.get("parameters") or {}).get("properties", {}) or {}
        required = set((t.get("parameters") or {}).get("required", []) or [])
        param_str = ", ".join(
            f"{p}{'?' if p not in required else ''}" for p in params
        )
        return f"- {name}({param_str}): {desc}"
    # MCP 工具
    if name.startswith("mcp__") and mcp_bridge:
        for schema in mcp_bridge.to_ollama_tools():
            fn = schema.get("function") or {}
            if fn.get("name") == name:
                desc = (fn.get("description") or "")[: _LIMITS.briefing_description_max]
                params = (fn.get("parameters") or {}).get("properties", {}) or {}
                required = set((fn.get("parameters") or {}).get("required", []) or [])
                param_str = ", ".join(
                    f"{p}{'?' if p not in required else ''}" for p in params
                )
                return f"- {name}({param_str}): {desc}"
    # Skill 工具
    if name.startswith("skill__") and skill_bridge:
        for schema in skill_bridge.to_ollama_tools():
            fn = schema.get("function") or {}
            if fn.get("name") == name:
                desc = (fn.get("description") or "")[: _LIMITS.briefing_description_max]
                params = (fn.get("parameters") or {}).get("properties", {}) or {}
                required = set((fn.get("parameters") or {}).get("required", []) or [])
                param_str = ", ".join(
                    f"{p}{'?' if p not in required else ''}" for p in params
                )
                return f"- {name}({param_str}): {desc}"
    return None


def _parse_text_tool_calls(content: str) -> list[dict]:
    """从模型回复中解析 <tool_call>JSON</tool_call> 块。

    用括号匹配提取完整 JSON，正确处理嵌套对象（如 arguments: {...}）。
    正规格式解析不到时兜底：从模型自造乱标记（DSML/invoke）中提取夹带的
    {"name":..., "arguments":...} JSON，避免调用漏执行、标记漏显示。
    """
    if not content:
        return []
    out: list[dict] = []
    for i, m in enumerate(_TOOL_CALL_TAG_RE.finditer(content)):
        raw = m.group(1).strip()
        if not raw:
            continue
        # 先尝试直接解析，失败则用括号匹配（嵌套 JSON 场景）
        data = None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            data = _extract_first_json(raw)
        if not data or not isinstance(data, dict):
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        args = data.get("arguments")
        if args is None:
            args = data.get("args") or data.get("parameters") or {}
        if isinstance(args, str):
            args = parse_json_dict(args)
        if not isinstance(args, dict):
            args = {}
        out.append(
            {
                "id": f"text_tc_{i}_{uuid.uuid4().hex[:8]}",
                "function": {"name": name, "arguments": args},
            }
        )
    if out:
        return out
    # 漏了闭合标签：本地 qwen 实测只发 <tool_call>{"name":...}，
    # 旧解析要求 </tool_call>，结果调用漏执行、原文直接漏给用户。
    m = _TOOL_CALL_OPEN_RE.search(content)
    if m:
        data = _extract_first_json(content[m.end():])
        if _looks_like_tool_json(data):
            name = str(data.get("name") or "").strip()
            args = data.get("arguments")
            if args is None:
                args = data.get("args") or data.get("parameters") or {}
            if isinstance(args, str):
                args = parse_json_dict(args)
            if not isinstance(args, dict):
                args = {}
            return [
                {
                    "id": f"text_tc_open_{uuid.uuid4().hex[:8]}",
                    "function": {"name": name, "arguments": args},
                }
            ]
    dsml = _parse_dsml_tool_calls(content)
    if dsml:
        return dsml
    return _parse_embedded_tool_json(content)


def _parse_embedded_tool_json(content: str) -> list[dict]:
    """兜底：从乱标记文本中提取所有形如工具调用的 JSON 对象。

    仅当正文出现 DSML/invoke 等调用标记痕迹才启用，避免把闲聊里的
    普通 JSON 示例误当工具调用。
    """
    if not content or not _MALFORMED_TOOL_MARKUP_RE.search(content):
        return []
    out: list[dict] = []
    for i, (_start, _end, data) in enumerate(_iter_json_objects(content)):
        if not _looks_like_tool_json(data):
            continue
        name = str(data.get("name") or "").strip()
        args = data.get("arguments")
        if args is None:
            args = data.get("args") or data.get("parameters") or {}
        if isinstance(args, str):
            args = parse_json_dict(args)
        if not isinstance(args, dict):
            args = {}
        out.append(
            {
                "id": f"text_tc_emb_{i}_{uuid.uuid4().hex[:8]}",
                "function": {"name": name, "arguments": args},
            }
        )
    return out


def _strip_text_tool_call_blocks(content: str) -> str:
    """从 content 中剥离 <tool_call>...</tool_call> 块与乱标记片段，保留纯文本回复。"""
    if not content:
        return ""
    t = _TOOL_CALL_TAG_RE.sub("", content)
    # 漏闭合标签的 <tool_call>：连同紧随其后的工具 JSON 一起剥掉
    while True:
        m = _TOOL_CALL_OPEN_RE.search(t)
        if not m:
            break
        after = t[m.end():]
        first = next(_iter_json_objects(after), None)
        if first is not None and _looks_like_tool_json(first[2]):
            t = t[: m.start()] + after[first[1]:]
        else:
            t = t[: m.start()] + after
    # DSML invoke 块整体剥离（含标签体内的参数 JSON），否则参数残渣会漏给用户
    blocks = _iter_dsml_blocks(t)
    if blocks:
        pieces: list[str] = []
        pos = 0
        for start, end, _name, _inner in blocks:
            pieces.append(t[pos:start])
            pos = end
        pieces.append(t[pos:])
        t = "".join(pieces)
    if _MALFORMED_TOOL_MARKUP_RE.search(t):
        # 剥离 < | DSML | | ...> 类自造标记段
        t = _TOOL_MARKUP_JUNK_RE.sub("", t)
        # 剥离夹带的工具调用 JSON（仅移除形如 {"name":...,"arguments":...} 的对象）
        pieces: list[str] = []
        pos = 0
        for start, end, data in _iter_json_objects(t):
            if _looks_like_tool_json(data):
                pieces.append(t[pos:start])
                pos = end
        if pieces:
            pieces.append(t[pos:])
            t = "".join(pieces)
    return t.strip()


def _stream_text(
    text: str,
    on_chunk: Callable[[str], None] | None,
    on_start: Callable[[], None] | None,
) -> None:
    if not text:
        return
    if on_start:
        on_start()
    if not on_chunk:
        return
    parts = _SENTENCE_SPLIT_RE.split(text)
    buf = ""
    chunks_sent = 0
    for part in parts:
        buf += part
        if part and part[-1] in "。！？!?…":
            if buf.strip():
                on_chunk(buf)
                chunks_sent += 1
            buf = ""
    if buf.strip():
        on_chunk(buf)
        chunks_sent += 1


def _make_tool_result_msg(tool_call: dict, content: str) -> dict:
    """无 schema 模式下，工具结果用 user role 代替 tool role。

    gemma 等模型通过 <tool_call> 文本发起调用（无 OpenAI tool_calls 字段），
    {"role": "tool"} 消息会被 API/模型忽略，导致模型看不到工具结果、重复调用。
    用 {"role": "user"} 确保模型能看到。
    """
    fn = (tool_call.get("function") or {})
    func = str(fn.get("name") or "")
    return {"role": "user", "content": f"[工具 {func} 执行结果]\n{content}"}


def _build_reply_from_executed_tools(executed_tools: list[dict]) -> str:
    """连续重复工具调用达阈值时，直接基于已执行工具结果生成回复。

    模型陷入 tool_call 死循环时，不再调 LLM，而是把最后一个成功工具的
    结果直接作为回复给用户，保证至少有可读内容送达 TTS。
    """
    for t in reversed(executed_tools):
        if t.get("ok"):
            text = str(t.get("text") or t.get("summary") or "").strip()
            if text:
                return text
    return "先生，这件事我重复尝试了几次都没能完成，您可以换个说法试试。"


class AgentLoop:
    """OpenClaw 风格：LLM 选工具 → 执行 → 结果回注 → 再推理，直到给出最终回复。"""

    def __init__(
        self,
        brain: Brain,
        memory,
        tool_ctx: ToolContext,
        mcp_bridge=None,
        mcp_call: Callable | None = None,
        skill_bridge=None,
        *,
        session_store: SessionStore | None = None,
        qs_store=None,
        extra_context_fn: Callable[[str], str] | None = None,
        proactor=None,
        max_iterations: int = _MAX_ITERATIONS,
        tool_top_k: int = _LIMITS.retriever_top_k,
        static_replay_cfg: dict | None = None,
    ):
        self.brain = brain
        self.memory = memory
        self.tool_ctx = tool_ctx
        self.mcp_bridge = mcp_bridge
        self.mcp_call = mcp_call
        self.skill_bridge = skill_bridge
        self.session_store = session_store or SessionStore()
        self.qs_store = qs_store
        self.extra_context_fn = extra_context_fn
        self.proactor = proactor
        self.max_iterations = max_iterations
        self.tool_top_k = tool_top_k
        self.static_replay_cfg = static_replay_cfg or {}
        # 上下文预算与渐进披露阈值（brain 配置，0/缺省 → 模块兜底值）
        _cfg = getattr(brain, "config", None)
        _cmc = int(getattr(_cfg, "context_max_chars", 0) or 0)
        self._context_max_chars = _cmc if _cmc > 0 else _LLM_CONTEXT_MAX_CHARS
        _ratio = float(getattr(_cfg, "tool_briefing_max_ratio", 0) or 0)
        self._tool_briefing_max_ratio = (
            _ratio if _ratio > 0 else _TOOL_BRIEFING_MAX_RATIO
        )

    # ─── 候选池检索（含后台工具开关过滤）─────────────────────────

    def _tool_visible(self, name: str) -> bool:
        """统一可见谓词：分组开关 + registry DISABLED，所有分支共用。"""
        bridge = self.skill_bridge
        disabled = bool(
            name.startswith("skill__")
            and bridge is not None
            and hasattr(bridge, "is_disabled")
            and bridge.is_disabled(name)
        )
        return is_tool_visible(
            name,
            lambda n: is_tool_enabled(n, self.tool_ctx.tools_cfg),
            registry_disabled=disabled,
        )

    def _retrieve_tool_names(
        self,
        user_text: str,
        *,
        mcp_tools: list[str] | None = None,
        skill_tools: list[str] | None = None,
        top_k: int = _LIMITS.retriever_top_k,
        tier: int = 1,
    ) -> list[str]:
        """检索候选池，并按后台「工具开关」剔除被关闭分组里的工具。

        与执行期 is_tool_enabled 同一套判定：关闭后模型看不到这些工具，
        不会再出现「选了才被拒」的空转。
        """
        names = tools_for_agent_loop(
            user_text,
            mcp_tools=mcp_tools,
            skill_tools=skill_tools,
            top_k=top_k,
            tier=tier,
        )
        visible = [n for n in names if self._tool_visible(n)]
        candidates = [n for n in visible if n.startswith(("skill__", "mcp__"))]
        try:
            from .memory.cognition import query_capability_health_many

            health = query_capability_health_many(candidates)
        except Exception:
            health = {}
        # BROKEN requires objective failure evidence; unknown capabilities stay
        # selectable so the existing execution verifier can probe them.
        return [n for n in visible if health.get(n) != "BROKEN"]

    # ─── 静态指令（语音指令记忆训练流）配置 ─────────────────────────

    def _static_replay_enabled(self) -> bool:
        return bool(self.static_replay_cfg.get("enabled", False))

    def _replay_threshold(self) -> float:
        rep = self.static_replay_cfg.get("replay") or {}
        return float(rep.get("threshold", 0.42))

    def _min_steps(self) -> int:
        return max(2, int(self.static_replay_cfg.get("min_steps", 2)))

    def _max_replay_fail(self) -> int:
        return max(1, int(self.static_replay_cfg.get("max_replay_fail", 2)))

    def run(
        self,
        text: str,
        agent_id: str = "keks",
        session_id: str = "default",
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        on_tool_event: Callable[[str, bool, str], None] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        memory_text: str | None = None,
        execution_policy_context: str | None = None,
        goal_controller: object | None = None,  # GOAL_CTRL_V1
    ) -> tuple[str | None, str, list[dict]]:
        mem_text = (memory_text or text).strip()
        if not mem_text:
            clear_agent_control()
            return None, "empty", []
        # CANCEL_TOKEN_V1: entry check — fast paths below must not run if already cancelled.
        if cancel_check and cancel_check():
            clear_agent_control()
            return "好的，已停下。", "cancelled", []

        if self.proactor and self.proactor.note_user_text(mem_text):
            reply = "好的，以后少打扰您。需要时再说「确认执行」或直接吩咐即可。"
            _stream_text(reply, on_chunk, on_start)
            clear_agent_control()
            return reply, "ok", []

        # 听歌 / 看剧：硬正则 + LLM 轻分类定域后再进快路径工具
        from .tools.media_arbiter import resolve_media_fastpath

        # 快路径分类器注入最近几轮对话摘要（接续话语的指代消解依据）
        _recent_ctx_parts: list[str] = []
        for _m in self.session_store.get_messages(session_id)[-6:]:
            _r = "用户" if _m.get("role") == "user" else "助手"
            _c = str(_m.get("content") or "").replace("\n", " ").strip()[:60]
            if _c:
                _recent_ctx_parts.append(f"{_r}: {_c}")
        media_hit = resolve_media_fastpath(
            mem_text,
            self.memory,
            brain=self.brain,
            session_ctx=" | ".join(_recent_ctx_parts[-6:]),
        )
        if media_hit is not None:
            layer, reply, tool_results = media_hit
            # 分类原文绝不能当回复（路由 JSON / 模型偷答）
            from .tools.media_intent import looks_like_route_json

            if looks_like_route_json(reply or ""):
                logger.warning("fastpath reply looks like route json, drop to agent: %r", reply[:80])
                # 落到下方 Agent Loop，不保存这条脏回复
            else:
                if on_start:
                    on_start()
                _stream_text(reply, on_chunk, None)
                if on_tool_event:
                    for tr in tool_results:
                        on_tool_event(
                            str(tr.get("func") or ""),
                            bool(tr.get("ok")),
                            str(tr.get("text") or "")[:120],
                        )
                # 快路径也写入 Session，保证与后续桌面多轮连续（指代/追问能接上）
                for tr in tool_results:
                    self.session_store.append_tool_fact(
                        session_id,
                        f"{tr.get('func')} {'ok' if tr.get('ok') else 'fail'}: "
                        f"{str(tr.get('text') or '')[:120]}",
                    )
                # 快路径成功同样更新任务槽位（正在播放）
                try:
                    for tr in tool_results:
                        if not tr.get("ok"):
                            continue
                        _f = str(tr.get("func") or "")
                        _a = tr.get("args") if isinstance(tr.get("args"), dict) else {}
                        if _f == "play_music" and str(_a.get("query") or "").strip():
                            self.session_store.set_slot(
                                session_id,
                                "now_playing",
                                "音乐「%s」（%s）"
                                % (_a.get("query"), _a.get("source") or "qq"),
                            )
                        elif _f == "play_video" and str(_a.get("query") or "").strip():
                            self.session_store.set_slot(
                                session_id,
                                "now_playing",
                                "视频「%s」（%s）"
                                % (_a.get("query"), _a.get("source") or "tencent"),
                            )
                except Exception as _slot_err:
                    logger.debug("fastpath 任务槽位更新失败: %s", _slot_err)
                self.session_store.append_turn(session_id, mem_text, reply)
                if self.qs_store:
                    try:
                        turn_id = f"{layer}_{uuid.uuid4().hex[:12]}"
                        for tr in tool_results:
                            self.qs_store.append_tool_result(
                                str(tr.get("func") or ""),
                                tr.get("args") if isinstance(tr.get("args"), dict) else {},
                                ok=bool(tr.get("ok")),
                                text=str(tr.get("text") or ""),
                                r1_status=layer,
                                session_id=session_id,
                                turn_id=turn_id,
                            )
                    except Exception:
                        pass
                clear_agent_control()
                return reply, layer, tool_results

        # 跨轮待删：用户确认后直接执行，不靠模型再猜路径/后缀
        from .safety.pending_delete import try_consume_pending_delete

        pending_hit = try_consume_pending_delete(
            mem_text, self.memory, self.tool_ctx
        )
        if pending_hit is not None:
            reply, tool_results = pending_hit
            if on_start:
                on_start()
            _stream_text(reply, on_chunk, None)
            if on_tool_event:
                for tr in tool_results:
                    on_tool_event(
                        str(tr.get("func") or ""),
                        bool(tr.get("ok")),
                        str(tr.get("text") or "")[:120],
                    )
            for tr in tool_results:
                self.session_store.append_tool_fact(
                    session_id,
                    f"{tr.get('func')} {'ok' if tr.get('ok') else 'fail'}: "
                    f"{str(tr.get('text') or '')[:120]}",
                )
            if self.session_store:
                self.session_store.append_turn(session_id, mem_text, reply)
            clear_agent_control()
            return reply, "pending_delete", tool_results

        # ─── 静态指令（语音指令记忆训练流）快路径 ────────────────
        if self._static_replay_enabled():
            # ① 用户确认固化上轮动作（"已完成/记下来"→ 生成静态指令）
            from .safety.static_compile import try_consume_pending_compile

            _compiled = try_consume_pending_compile(
                mem_text, self.memory, self.memory.db
            )
            if _compiled is not None:
                compiled_reply, compiled_tools = _compiled
                if on_start:
                    on_start()
                _stream_text(compiled_reply, on_chunk, None)
                if on_tool_event:
                    for tr in compiled_tools or []:
                        on_tool_event(
                            str(tr.get("func") or ""),
                            bool(tr.get("ok")),
                            str(tr.get("text") or "")[:120],
                        )
                if self.session_store:
                    self.session_store.append_turn(
                        session_id, mem_text, compiled_reply
                    )
                clear_agent_control()
                return compiled_reply, "static_compile", compiled_tools or []

            # ② 模糊命中静态指令 → 直接回放，绕过 LLM 决策
            from .tools.static_instruction import (
                match_static_instruction,
                replay_static_instruction,
            )

            _hit = match_static_instruction(
                self.memory.db, mem_text, threshold=self._replay_threshold()
            )
            if _hit:
                name, _score = _hit
                replayed_tools, replay_text = replay_static_instruction(
                    self.memory.db,
                    name,
                    self.tool_ctx,
                    max_replay_fail=self._max_replay_fail(),
                )
                if replayed_tools:
                    reply_text = replay_text or "已按记忆执行。"
                    if on_start:
                        on_start()
                    _stream_text(reply_text, on_chunk, None)
                    if on_tool_event:
                        for tr in replayed_tools:
                            on_tool_event(
                                str(tr.get("func") or ""),
                                bool(tr.get("ok")),
                                str(tr.get("text") or "")[:120],
                            )
                    for tr in replayed_tools:
                        self.session_store.append_tool_fact(
                            session_id,
                            f"{tr.get('func')} {'ok' if tr.get('ok') else 'fail'}: "
                            f"{str(tr.get('text') or '')[:120]}",
                        )
                    self.session_store.append_turn(session_id, mem_text, reply_text)
                    if self.qs_store:
                        try:
                            turn_id = f"static_fastpath_{uuid.uuid4().hex[:12]}"
                            for tr in replayed_tools:
                                self.qs_store.append_tool_result(
                                    str(tr.get("func") or ""),
                                    tr.get("args")
                                    if isinstance(tr.get("args"), dict)
                                    else {},
                                    ok=bool(tr.get("ok")),
                                    text=str(tr.get("text") or ""),
                                    r1_status="static_fastpath",
                                    session_id=session_id,
                                    turn_id=turn_id,
                                )
                        except Exception:
                            pass
                    clear_agent_control()
                    return reply_text, "static_fastpath", replayed_tools

        if not self.brain.is_available():
            if is_local_llm_url(self.brain.config.base_url):
                logger.warning(
                    "本地 LLM 预检未通过，仍尝试对话（model=%s base=%s）",
                    self.brain.model,
                    self.brain.config.base_url,
                )
            else:
                clear_agent_control()
                return None, "error", []

        persona = resolve_persona(self.memory, agent_id)
        if on_start:
            on_start()
        if on_chunk:
            on_chunk(progress_hint("working", persona=persona))

        system = resolve_system_prompt(self.memory, agent_id, self.brain.config) + _build_loop_system_addendum(
            mem_text
        )
        from .safety.pending_delete import pending_hint

        _pd_hint = pending_hint(self.memory)
        if _pd_hint:
            system += f"\n\n{_pd_hint}"
        _tf_prompt = self.session_store.get_tool_facts_prompt(session_id)
        if _tf_prompt:
            system += f"\n\n{_tf_prompt}"
        # 结构化任务状态槽位（最近文件/应用/播放），指代复用的可靠依据
        _slots_prompt = self.session_store.get_slots_prompt(session_id)
        if _slots_prompt:
            system += f"\n\n{_slots_prompt}"
        raw_user_text = (text or "").strip()
        query_for_mem = (
            f"{raw_user_text} {mem_text}".strip()
            if (raw_user_text and raw_user_text != mem_text)
            else mem_text
        )
        # 第二操作者 Core RAM：人脉/路径/画像/踩坑教训每轮常驻，不依赖关键词门控
        try:
            from .memory.core_memory import build_core_ram

            _core_ram = build_core_ram(self.memory.db, query_for_mem)
        except Exception:  # noqa: BLE001
            logger.debug("Core RAM 组装失败", exc_info=True)
            _core_ram = ""
        if _core_ram:
            system += f"\n\n{_core_ram}"
        # 自我记忆 L0：她自己刷到的见闻，每轮带一点。相关性优先，只做本地 SQLite 读
        try:
            from .memory import self_memory

            _self_block = self_memory.build_l0_block(self.memory.db, query_for_mem)
        except Exception:  # noqa: BLE001
            logger.debug("自我记忆 L0 组装失败", exc_info=True)
            _self_block = ""
        if _self_block:
            system += f"\n\n{_self_block}"
        # 会话历史已在 messages 里，记忆侧不再重复塞「最近对话」
        mem_ctx = build_memory_context(
            self.memory, query_for_mem, session_id, include_digests=False
        )
        if memory_text and memory_text.strip() and memory_text.strip() != raw_user_text:
            extra_mem = memory_text.strip()
            if extra_mem not in (mem_ctx or ""):
                mem_ctx = f"{extra_mem}\n\n{mem_ctx}".strip() if mem_ctx else extra_mem
        if mem_ctx:
            system += (
                "\n\n【记忆参考·仅供检索/上下文】与当前问题无关必须忽略；"
                "禁止据此主动执行未请求动作；口头答应≠已执行。"
                "自动执行偏好请用 Sentinel/显式工具，不要自行脑补落实。\n"
                f"{mem_ctx}\n【记忆参考结束】"
            )

        if self.extra_context_fn:
            extra = (self.extra_context_fn(mem_text) or "").strip()
            if extra:
                system += f"\n\n{extra}"

        if self.proactor:
            spoken = None
            if hasattr(self.proactor, "recent_spoken"):
                spoken = self.proactor.recent_spoken()
            if spoken:
                system += (
                    f"\n\n【你刚主动对用户说】{spoken}"
                    "（用户可能在回应；同意则直接执行）"
                )
            else:
                suggestion = self.proactor.take_suggestion()
                if suggestion:
                    system += f"\n\n【主动提示】{suggestion}"

        history = self.session_store.get_messages(session_id)
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": raw_user_text or mem_text})

        # 推测执行：预运行只读工具，结果注入上下文，省掉一轮 LLM 往返
        spec_result = _try_speculative_exec(mem_text, self.tool_ctx)
        if spec_result:
            messages.append({"role": "user", "content": spec_result})
            _spec_name = re.match(r"\[系统预取·(\w+)\]", spec_result)
            if _spec_name:
                injected_prefetch.add(_spec_name.group(1))
            logger.info("推测执行命中: %s", spec_result[:80])

        # 注意：必须传 mcp__* 工具全名；服务器名（filesystem）进不了候选池
        mcp_tool_names = (
            self.mcp_bridge.tools_matching(mem_text)
            if self.mcp_bridge
            else []
        )
        skill_tool_names = (
            self.skill_bridge.tools_matching(mem_text)
            if self.skill_bridge
            else []
        )
        effective_top_k = (
            min(self.tool_top_k, _LOCAL_TOOL_TOP_K)
            if is_local_llm_url(self.brain.base_url)
            else self.tool_top_k
        )
        # 渐进披露阈值化：先构建全池清单，占上下文预算比例 ≤ 阈值 → 直接全池；
        # 超过 → tier0 核心清单起步，模型没调工具时再展开（should_expand_tool_tier）。
        # 换模型只调 brain.context_max_chars / tool_briefing_max_ratio，不改代码。
        full_names = self._retrieve_tool_names(
            mem_text,
            mcp_tools=mcp_tool_names,
            skill_tools=skill_tool_names,
            top_k=effective_top_k,
            tier=1,
        )
        adopted_policy_ids: dict[str, list[int]] = {}
        adopted_policy_versions: dict[str, dict[str, int]] = {}
        policy_context = str(execution_policy_context or "").strip()
        learning = getattr(self.tool_ctx, "learning", None)
        if learning is not None and hasattr(learning, "get_execution_policies"):
            try:
                policies = learning.get_execution_policies(mem_text, full_names, limit=2)
            except Exception:
                logger.debug("执行策略检索失败", exc_info=True)
                policies = []
            if policies:
                existing_ids = set(re.findall(r"\[(?:policy|candidate)\s+(\d+)", policy_context))
                missing = [p for p in policies if str(p["id"]) not in existing_ids]
                extra_context = learning.format_execution_policy_context(missing) if missing else ""
                if extra_context:
                    policy_context = "\n".join(x for x in (policy_context, extra_context) if x)
                for policy in policies:
                    if policy.get("is_candidate_reference") or str(policy.get("policy_state") or "active") == "candidate":
                        continue
                    func = str(policy.get("correction_func") or "")
                    if func:
                        policy_id = int(policy["id"])
                        adopted_policy_ids.setdefault(func, []).append(policy_id)
                        adopted_policy_versions.setdefault(func, {})[str(policy_id)] = int(policy.get("policy_version") or 1)
                if messages:
                    messages[0]["content"] = (messages[0].get("content") or "") + "\n" + policy_context
        _recorded_corrections: set[tuple[str, str, str]] = set()
        full_briefing = _build_tool_briefing(
            full_names, self.mcp_bridge, self.skill_bridge
        )
        if len(full_briefing) <= self._context_max_chars * self._tool_briefing_max_ratio:
            tool_tier = 1
            tool_names = full_names
            briefing = full_briefing
        else:
            tool_tier = 0
            tool_names = self._retrieve_tool_names(
                mem_text,
                mcp_tools=mcp_tool_names,
                skill_tools=skill_tool_names,
                top_k=effective_top_k,
                tier=0,
            )
            briefing = _build_tool_briefing(
                tool_names, self.mcp_bridge, self.skill_bridge
            )
        logger.info(
            "工具披露 tier=%d（%d 个，清单 %d 字符 / 预算 %d×%.2f）",
            tool_tier, len(tool_names), len(briefing),
            self._context_max_chars, self._tool_briefing_max_ratio,
        )
        # 无 schema 模式：精简清单写进 system prompt，模型按 <tool_call> 格式发起调用
        if briefing and messages:
            messages[0]["content"] = (messages[0].get("content") or "") + briefing

        executed_tools: list[dict] = []
        executed_sigs: set[tuple[str, str]] = set()
        _verified_retry_sigs: set[tuple[str, str]] = set()  # CLOSED_LOOP_P3_20261030
        final_reply: str | None = None
        llm_degraded = False
        loop_exhausted = False  # Q2_TERMINAL_V1: 12轮耗尽标记
        # 脏回复（取消/降级/迭代耗尽兜底话术）不写入会话历史，避免污染下一轮上下文
        turn_recordable = True
        # 已注入过的 [系统预取·工具名] 集合：失败恢复预跑每轮都可能触发，需去重
        injected_prefetch: set[str] = set()
        # 历史切片右边界（messages = [system] + history + 当前用户 + 本轮注入）：
        # 分层裁剪只压缩历史区，不碰本轮注入消息
        history_end = 1 + len(history)
        turn_id = f"loop_{uuid.uuid4().hex[:12]}"
        verbal_tool_retry = False
        # 自我扩展守卫：同一轮最多自动触发一次（避免反复联网装技能）
        skill_acq_tried = False
        promise_retry_count = 0
        missed_tools_retry = False
        force_tool_required = False
        consecutive_dup_count = 0
        last_assistant = ""
        controller_fate = None  # GOAL_CTRL_V1: controller verdict
        streamed = False
        if history:
            for m in reversed(history):
                if m.get("role") == "assistant":
                    last_assistant = str(m.get("content") or "")
                    break

        round_had_failure = False
        round_missing_app_name = False

        for _ in range(self.max_iterations):
            if cancel_check and cancel_check():
                final_reply = "好的，已停下。"
                turn_recordable = False
                break
            if tool_call_budget_exceeded():
                final_reply = "自主任务已达到工具调用预算上限，剩余步骤已停止。"
                llm_degraded = True
                turn_recordable = False
                break

            # GOAL_CTRL_V1: controller stop check — once per round, after cancel.
            if goal_controller is not None:
                _sc_stop, _sc_fate, _sc_reason = goal_controller.should_stop()
                if _sc_stop:
                    controller_fate = _sc_fate
                    final_reply = _sc_reason
                    turn_recordable = False
                    break

            # 连续重复调用超过阈值时，模型已陷入 tool_call 死循环，
            # 直接基于已执行工具结果生成回复，不再调 LLM（避免其吐出零宽空格等无效 content）
            if consecutive_dup_count >= 3:
                final_reply = _build_reply_from_executed_tools(executed_tools)
                if on_chunk and final_reply:
                    on_chunk(final_reply)
                streamed = True
                break
            force_text_reply = False

            try:
                # 动作强制：仅在尚未命中关键文件工具时要求必调，避免挂起确认后无法口头收尾
                action_force = _should_require_action_tool(
                    mem_text, tool_names
                ) and not any(
                    (t.get("func") or t.get("name")) in _DELETE_OR_LIST_FUNCS
                    for t in executed_tools
                )
                need_tool = (
                    (
                        tool_names
                        and (
                            _should_require_app_tool(mem_text, tool_names)
                            or action_force
                        )
                    )
                    or force_tool_required
                )
                force_tool_required = False
                # 无 schema 模式：不传 tools，靠 system prompt 清单 + <tool_call> 文本解析
                # force_text_reply 时不传清单（已在上方 messages 中控制）
                _trim_messages_for_context(
                    messages, history_end=history_end, budget=self._context_max_chars
                )
                # 失败后自适应恢复：上轮核心工具失败 → 预跑相关只读状态查询注入上下文
                # （同一工具只注入一次，避免多轮失败时重复膨胀）
                if round_had_failure:
                    fail_spec = _try_failure_speculative_exec(
                        executed_tools, self.tool_ctx
                    )
                    if fail_spec:
                        _fail_m = re.match(r"\[系统预取·(\w+)\]", fail_spec)
                        _fail_name = _fail_m.group(1) if _fail_m else ""
                        if _fail_name and _fail_name in injected_prefetch:
                            pass  # 已注入过，跳过
                        else:
                            if _fail_name:
                                injected_prefetch.add(_fail_name)
                            messages.append({"role": "user", "content": fail_spec})
                msg = self.brain.chat_once(
                    messages,
                    tools=None,
                    tool_choice=None,
                )
            except Exception as e:
                logger.exception("Agent Loop LLM 异常: %s", e)
                final_reply = "抱歉，推理服务暂时不可用。"
                llm_degraded = True
                turn_recordable = False
                break

            if not msg:
                final_reply = "抱歉，模型暂时无法响应，请稍后再试。"
                llm_degraded = True
                turn_recordable = False
                break
            # CANCEL_TOKEN_V1: LLM generation finished — check before starting tools.
            if cancel_check and cancel_check():
                final_reply = "好的，已停下。"
                turn_recordable = False
                break

            tool_calls = msg.get("tool_calls") or []
            content = _ZERO_WIDTH_RE.sub("", msg.get("content") or "").strip()

            # 无 schema 模式：模型把调用写在 content 的 <tool_call>JSON</tool_call> 块中
            if not tool_calls and content:
                parsed = _parse_text_tool_calls(content)
                if parsed:
                    tool_calls = parsed
                    # 保留原始 content（含 <tool_call>）在 assistant 消息中，
                    # 让模型能看到自己之前的调用记录；
                    # content 变量用剥离后的纯文本，用于 final_reply
                    stripped = _strip_text_tool_call_blocks(content)
                    content = stripped
                    logger.info(
                        "从 content 解析到 %d 个文本 tool_call", len(parsed)
                    )

            # 诊断：模型像是想调工具（含 JSON/调用标记）却没解析出来，
            # 必须留原文，否则整轮变成"哑火 + 把调用片段漏给用户"（实测事故）。
            # 判据只认调用标记 / 整段就是个裸 JSON，避免正常回答里夹个 { 就刷日志。
            _probe = content.strip()
            if not tool_calls and content and (
                "DSML" in content
                or "invoke" in content.lower()
                or "<tool" in content
                or (_probe.startswith("{") and _probe.endswith("}"))
            ):
                logger.info("疑似工具调用未解析，原文前 400 字：%s", content[:400])

            # 系统代触自我扩展：tier0→tier1 已展开，模型却仍只能拿「通用兜底工具」
            # 硬拼某个平台/服务的专用功能（历史：拿 gui_action 硬点微信朋友圈发动态）。
            # 这类需求通用点击无法可靠完成 → 丢掉这些瞎点的调用，转由下面的
            # 自我扩展逻辑联网找现成技能（全自动，装完再告知）。
            force_self_extend = False
            _acq = getattr(self.tool_ctx, "_skill_acquirer", None)
            if tool_calls and not skill_acq_tried and tool_tier >= 1 and _acq is not None:
                _trig = (getattr(_acq, "cfg", None) or {}).get("system_trigger") or {}
                if _trig.get("enabled", True) and _in_system_trigger(
                    mem_text, tool_calls, _trig
                ):
                    force_self_extend = True
                    logger.info(
                        "系统代触自我扩展：丢弃通用工具硬拼调用 %s",
                        [
                            str((tc.get("function") or {}).get("name") or "")
                            for tc in tool_calls
                        ],
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[系统] 你用通用点击工具硬拼该平台功能的做法不可靠，已拦下；"
                                "正在联网找现成的专用技能并安装。"
                            ),
                        }
                    )
                    tool_calls = []

            if not tool_calls:
                if should_expand_tool_tier(
                    mem_text,
                    tool_tier=tool_tier,
                    had_tool_calls=False,
                    assistant_content=content,
                ):
                    tool_tier = 1
                    tool_names = self._retrieve_tool_names(
                        mem_text,
                        mcp_tools=mcp_tool_names,
                        skill_tools=skill_tool_names,
                        top_k=effective_top_k,
                        tier=tool_tier,
                    )
                    # 无 schema 模式：tier 展开后追加新工具清单到 messages
                    expanded_briefing = _build_tool_briefing(
                        tool_names, self.mcp_bridge, self.skill_bridge
                    )
                    if expanded_briefing:
                        messages.append({"role": "system", "content": expanded_briefing})
                    messages.append({"role": "user", "content": _TIER_EXPAND_USER_NOTE})
                    logger.info("工具集 tier0→tier1 展开（模型未调工具）: %d 个", len(tool_names))
                    continue

                # 自我扩展：模型自陈「没合适工具/做不到」时，自动上网找现成技能并安装，
                # 装好后把新工具并进候选池、重跑本轮（全自动，装完再告知）。
                # force_self_extend=系统代触（上面判定模型在拿通用工具硬拼平台功能）。
                if (
                    not skill_acq_tried
                    and tool_tier >= 1
                    and (force_self_extend or _NEEDS_TOOL_HINT_RE.search(content or ""))
                    and getattr(self.tool_ctx, "_skill_acquirer", None) is not None
                ):
                    skill_acq_tried = True
                    need = (mem_text or content or "").strip()
                    before_skills = (
                        set(self.skill_bridge.tool_names()) if self.skill_bridge else set()
                    )
                    _acq_args = {"need": need}
                    _acq_allowed, _acq_risk, _acq_reason = autonomous_tool_decision("acquire_skill", _acq_args)
                    allowed, deny_reason = (False, _acq_reason) if not _acq_allowed else check_tool_call(
                        "acquire_skill", _acq_args, user_text=mem_text,
                        recent_assistant=last_assistant,
                        pre_authorized=_acq_risk in {"R1", "R2", "R3"},
                        pre_authorized_reason="exact one-use goal action authorization" if _acq_risk in {"R1", "R2", "R3"} else "",
                    )
                    if not allowed:
                        if _acq_allowed:
                            mark_autonomy_policy_violation("acquire_skill", _acq_args, _acq_risk, deny_reason)
                        logger.info("自我扩展被安全策略拦截: %s", deny_reason)
                    else:
                        if on_tool_start:
                            try:
                                on_tool_start("acquire_skill")
                            except Exception:
                                logger.debug("on_tool_start 回调失败", exc_info=True)
                        resp, _sc, _cok, _csum = execute_with_critic(
                            "acquire_skill",
                            {"need": need},
                            self.tool_ctx,
                            execute_call=execute_call,
                            mcp_bridge=self.mcp_bridge,
                            mcp_call=self.mcp_call,
                            cancel_check=cancel_check,
                        )
                        out = r1_to_tool_output(resp)
                        acq_ok = resp.status == STATUS_SUCCESS and out.ok
                        acq_text = (out.text or "").strip() or "没能装上现成技能。"
                        executed_tools.append(
                            {
                                "name": "acquire_skill",
                                "func": "acquire_skill",
                                "ok": acq_ok,
                                "args": {"need": need},
                                "summary": acq_text[:120],
                            }
                        )
                        if on_tool_event:
                            on_tool_event("acquire_skill", acq_ok, acq_text[:120])
                        if acq_ok:
                            # 热重载已由 SkillAcquirer 内部完成（原地刷新），
                            # 这里只把新装的 skill__* 工具强制并入本轮候选池
                            after_skills = (
                                set(self.skill_bridge.tool_names())
                                if self.skill_bridge
                                else set()
                            )
                            fresh = [
                                n for n in sorted(after_skills - before_skills)
                                if self._tool_visible(n)
                            ]
                            if fresh:
                                if self.skill_bridge:
                                    skill_tool_names = [
                                        n for n in self.skill_bridge.tools_matching(mem_text)
                                        if self._tool_visible(n)
                                    ]
                                tool_names = list(tool_names) + [
                                    n for n in fresh if n not in tool_names
                                ]
                                new_brief = _build_tool_briefing(
                                    fresh, self.mcp_bridge, self.skill_bridge
                                )
                                if new_brief:
                                    messages.append({"role": "system", "content": new_brief})
                                logger.info("自我扩展成功，新增技能工具: %s", fresh)
                            else:
                                logger.info("自我扩展成功（未新增 skill 工具）: %s", acq_text[:120])
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        f"[系统] 自我扩展已完成：{acq_text}"
                                        "请立即用刚装上的工具完成用户请求，"
                                        "完成后一句口语汇报，不要再说「做不到」。"
                                    ),
                                }
                            )
                            continue
                        logger.info("自我扩展未成功: %s", acq_text[:120])
                        # 如实告知（含 on_not_found=ask 的追问），不编造已完成
                        final_reply = acq_text
                        _stream_text(final_reply, on_chunk, None)
                        streamed = True
                        break

                if (
                    (
                        _should_require_app_tool(mem_text, tool_names)
                        or _should_require_action_tool(mem_text, tool_names)
                    )
                    and not verbal_tool_retry
                ):
                    verbal_tool_retry = True
                    force_tool_required = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[系统] 你必须通过 function calling 调用工具完成用户请求，"
                                "不要用纯文字假装已经执行。"
                            ),
                        }
                    )
                    logger.info("模型口头回复未调工具，注入纠正后重试推理")
                    continue

                # 承诺型回复，或明显要动手却不调工具 → 强制继续执行
                # 注意：本轮已真正执行过工具（executed_tools 非空）时的收尾汇报不算承诺，
                # 哪怕措辞像"好的，已经打开"也不重试，避免把已完成任务的汇报误判成假承诺
                if (
                    (
                        _looks_like_promise(content)
                        or _user_request_needs_tool(mem_text)
                    )
                    and not executed_tools
                    and promise_retry_count < _MAX_PROMISE_RETRIES
                    and not force_text_reply
                ):
                    promise_retry_count += 1
                    force_tool_required = True
                    if tool_tier == 0:
                        tool_tier = 1
                        tool_names = self._retrieve_tool_names(
                            mem_text,
                            mcp_tools=mcp_tool_names,
                            skill_tools=skill_tool_names,
                            top_k=effective_top_k,
                            tier=tool_tier,
                        )
                    # create_document / 删除链由 BM25 按 registry._TOOL_KEYWORDS
                    # 检索元数据拉进候选池（写文档/导出/删除等词已配置）
                    messages.append(msg)
                    # 仅「创建文档」才点名 create_document；删除意图走列表+删除
                    from .tools.tool_retriever import (
                        _DOC_DELETE_INTENT_RE,
                        _is_doc_create_intent,
                    )

                    if _is_doc_create_intent(mem_text):
                        inject_hint = (
                            "[系统] 你刚才只是口头承诺，没有实际执行。"
                            "现在立即通过 function calling 调用工具完成用户请求"
                            "（创建文档用 create_document；filename 用用户说的名字，"
                            "未指定则用「未命名文档」，禁止无故写成「对话记录」），"
                            "完成后直接汇报结果，禁止再说「正在创建」「请稍等」。"
                        )
                    elif _DOC_DELETE_INTENT_RE.search(mem_text):
                        inject_hint = (
                            "[系统] 你刚才只是口头承诺，没有实际执行。"
                            "现在立即调用 list_desktop_files(extensions=\"docx,doc\") "
                            "查看【分类摘要】，再 delete_file / move_to_trash；"
                            "禁止声称桌面没有 Word；禁止 create_document。"
                        )
                    else:
                        inject_hint = (
                            "[系统] 你刚才只是口头承诺，没有实际执行。"
                            "现在立即通过 function calling 调用工具完成用户请求，"
                            "完成后直接汇报结果，禁止再说「请稍等」。"
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": inject_hint,
                        }
                    )
                    logger.info("检测到未执行的动作承诺，注入纠正后重试: %s", content[:40])
                    continue

                # 诚实兜底：已重试到上限、本轮仍只口头承诺、且没执行任何工具，
                # 不再把假承诺当最终回复，明确告诉用户没落实
                if (
                    promise_retry_count >= _MAX_PROMISE_RETRIES
                    and not executed_tools
                    and _looks_like_promise(content)
                    and not force_text_reply
                ):
                    final_reply = (
                        "先生，这件事我没能用工具落实，可能缺少对应能力。"
                        "您换个说法，或拆成更具体的一步试试？"
                    )
                    if on_chunk:
                        on_chunk(final_reply)
                    streamed = True
                    break

                # 播报前先剥工具标记：本地模型漏闭合标签时这里会流式播报原文
                _safe_content = _strip_text_tool_call_blocks(content)
                final_reply = _safe_content or (
                    _build_reply_from_executed_tools(executed_tools)
                    if executed_tools
                    else "好的。"
                )
                from .safety.pending_delete import scrub_fake_deleted_reply

                deleted_ok = any(
                    t.get("func") in ("delete_file", "move_to_trash") and t.get("ok")
                    for t in executed_tools
                )
                final_reply = scrub_fake_deleted_reply(
                    final_reply, self.memory, deleted_ok=deleted_ok
                )
                _stream_text(final_reply, on_chunk, None)
                streamed = True
                break

            messages.append(msg)
            inject_batch: list[dict] = []
            round_had_failure = False
            round_missing_app_name = False

            # 有界计划：单轮工具调用数超限 → 截断并提示分批，防 LLM 一次性吐超长计划
            if len(tool_calls) > _MAX_TOOL_CALLS_PER_ROUND:
                messages.append(
                    _make_tool_result_msg(
                        {"function": {"name": "plan"}, "arguments": "{}"},
                        f"本轮工具调用过多（{len(tool_calls)} 个），"
                        f"最多 {_MAX_TOOL_CALLS_PER_ROUND} 个，请分步执行，"
                        "先做完这步再继续下一步。",
                    )
                )
                tool_calls = tool_calls[:_MAX_TOOL_CALLS_PER_ROUND]

            for tc in tool_calls:
                if tool_call_budget_exceeded():
                    final_reply = "自主任务已达到工具调用预算上限，剩余步骤已停止。"
                    llm_degraded = True
                    turn_recordable = False
                    break
                if cancel_check and cancel_check():
                    break
                fn = (tc.get("function") or {})
                func = str(fn.get("name") or "").strip()
                args = parse_json_dict(fn.get("arguments"))
                if not isinstance(args, dict):
                    args = {}
                if not func:
                    messages.append(
                        _make_tool_result_msg(tc, "工具名为空，请换用正确工具。")
                    )
                    continue

                sig = (
                    func,
                    json.dumps(args, sort_keys=True, ensure_ascii=False),
                )
                if sig in executed_sigs and not (sig not in _verified_retry_sigs and (_lt := next((t for t in reversed(executed_tools) if t.get("func") == func and isinstance(t.get("args"), dict) and json.dumps(t.get("args"), sort_keys=True, ensure_ascii=False) == sig[1]), None)) is not None and "未能确认" in str(_lt.get("text") or "") and (_verified_retry_sigs.add(sig) is None)):  # CLOSED_LOOP_P3_V2_20261030
                    consecutive_dup_count += 1
                    logger.debug("跳过重复工具调用: %s (连续%d次)", func, consecutive_dup_count)
                    messages.append(
                        _make_tool_result_msg(
                            tc, "相同操作已执行过，请根据上方结果给出最终回复。"
                        )
                    )
                    continue
                executed_sigs.add(sig)
                consecutive_dup_count = 0

                # search_tools 元工具：LLM 语义搜索工具表，动态扩展可用工具
                if func == "search_tools":
                    query = str(args.get("query", "")).strip()
                    if not query:
                        messages.append(
                            _make_tool_result_msg(tc, "请提供搜索关键词。")
                        )
                        continue
                    register_builtin_tools()
                    all_tools = [n for n in get_tool_names() if n != "search_tools"]
                    matched = rank_tools_by_text(query, all_tools, top_k=10)
                    # 将匹配的工具加入可用列表
                    added = [n for n in matched if n not in tool_names]
                    if added:
                        tool_names.extend(added)
                        logger.info(
                            "search_tools('%s') → 添加 %d 个工具: %s",
                            query, len(added), added[:5],
                        )
                    # 返回工具描述供 LLM 选择
                    lines = []
                    for name in matched:
                        t_def = get_tool(name) or {}
                        desc = (t_def.get("description") or "")[:100]
                        lines.append(f"  {name}: {desc}")
                    result_text = (
                        "匹配的工具:\n" + "\n".join(lines)
                        if lines else "未找到匹配工具。"
                    )
                    messages.append(_make_tool_result_msg(tc, result_text))
                    continue

                func, args = remap_kill_to_close_app(func, args, mem_text)
                args = normalize_tool_params(func, args, user_text=mem_text)

                autonomy_allowed, autonomy_risk, autonomy_reason = autonomous_tool_decision(func, args)
                if not autonomy_allowed:
                    round_had_failure = True
                    executed_tools.append({
                        "name": func, "func": func, "ok": False, "args": args,
                        "summary": autonomy_reason[:160], "failure_class": "authorization",
                        "autonomy_risk": autonomy_risk,
                    })
                    if on_tool_event:
                        on_tool_event(func, False, autonomy_reason[:160])
                    messages.append(_make_tool_result_msg(tc, autonomy_reason))
                    break

                if func in _APP_TOOL_FUNCS and not str(args.get("app_name") or "").strip():
                    round_missing_app_name = True
                    messages.append(
                        _make_tool_result_msg(
                            tc,
                            "缺少 app_name。请从用户原话提取软件名，用 open_app/close_app 的 app_name 参数重试。",
                        )
                    )
                    continue

                if func not in tool_names and tool_tier == 0:
                    round_had_failure = True
                    messages.append(
                        _make_tool_result_msg(
                            tc,
                            f"工具「{func}」当前不可用，将为你展开更多工具后请重试。",
                        )
                    )
                    continue

                allowed, deny_reason = check_tool_call(
                    func,
                    args,
                    user_text=mem_text,
                    recent_assistant=last_assistant,
                    pre_authorized=autonomy_risk in {"R1", "R2", "R3"},
                    pre_authorized_reason="exact one-use goal action authorization" if autonomy_risk in {"R1", "R2", "R3"} else "",
                )
                if not allowed:
                    mark_autonomy_policy_violation(func, args, autonomy_risk, deny_reason)
                    if func in ("delete_file", "move_to_trash"):
                        from .safety.pending_delete import set_pending

                        resolved = set_pending(
                            self.memory,
                            str(args.get("path") or ""),
                            func,
                        )
                        path_show = resolved or str(args.get("path") or "").strip()
                        if path_show:
                            deny_reason = (
                                f"已记下待删文件：{path_show}。"
                                f"请说「确认执行」或「确认」后再删；"
                                f"未确认前不要声称已删除。"
                            )
                            self.session_store.append_tool_fact(
                                session_id, f"pending_delete: {path_show}"
                            )
                    round_had_failure = True
                    executed_tools.append(
                        {
                            "name": func,
                            "func": func,
                            "ok": False,
                            "args": args,
                            "summary": deny_reason[:120],
                        }
                    )
                    if on_tool_event:
                        on_tool_event(func, False, deny_reason[:120])
                    if goal_controller is not None:  # GOAL_CTRL_V1
                        goal_controller.on_denied(func, args, deny_reason)
                    messages.append(
                        _make_tool_result_msg(tc, deny_reason)
                    )
                    continue

                # GOAL_CTRL_V1: controller veto — destructive calls need
                # goal-verb or destructive-acceptance coverage; vetoed
                # calls never execute. Lock is fail-closed afterwards.
                if goal_controller is not None:
                    _vetoed, _veto_reason = goal_controller.veto(func, args)
                    if _vetoed:
                        round_had_failure = True
                        executed_tools.append(
                            {
                                "name": func,
                                "func": func,
                                "ok": False,
                                "args": args,
                                "summary": _veto_reason[:120],
                            }
                        )
                        if on_tool_event:
                            on_tool_event(func, False, _veto_reason[:120])
                        messages.append(
                            _make_tool_result_msg(tc, _veto_reason)
                        )
                        continue

                if func in ("delete_file", "move_to_trash"):
                    from .safety.pending_delete import clear_pending

                    # 本轮已真正执行删除，清掉 pending
                    clear_pending(self.memory)

                # 执行前反馈：让用户知道“开始干活了”（语音壳播 execute 音）
                if on_tool_start:
                    try:
                        on_tool_start(func)
                    except Exception:
                        logger.debug("on_tool_start 回调失败", exc_info=True)
                resp, selfcheck_extra, _c_ok, _c_summary = execute_with_critic(
                    func,
                    args,
                    self.tool_ctx,
                    execute_call=execute_call,
                    mcp_bridge=self.mcp_bridge,
                    mcp_call=self.mcp_call,
                    cancel_check=cancel_check,
                )
                if cancel_check and cancel_check():
                    break
                out = r1_to_tool_output(resp)
                # 执行器忙（上一个工具还没完）：不当失败，提示稍候再试
                if resp.status == STATUS_EXECUTING:
                    summary = "上一个操作还在进行，稍候再试。"
                    executed_tools.append(
                        {
                            "name": func,
                            "func": func,
                            "ok": False,
                            "args": args,
                            "summary": summary,
                            "text": summary,
                        }
                    )
                    if on_tool_event:
                        on_tool_event(func, False, summary)
                    messages.append(_make_tool_result_msg(tc, summary))
                    continue
                ok = resp.status == STATUS_SUCCESS and out.ok
                if resp.status == STATUS_FUNC_NOT_FOUND:
                    round_had_failure = True
                elif not ok and func not in _OPTIONAL_TOOLS:
                    # optional 工具（截图/只读查询）失败视为无害，跳过不中断
                    round_had_failure = True
                summary = (out.text or "")[:120]
                _explicit_verified = (resp.data or {}).get("verified")
                _tool_verified = bool(ok and _c_ok and (_explicit_verified is not False))
                executed_tools.append(
                    {
                        "name": func,
                        "func": func,
                        "ok": ok,
                        "verified": _tool_verified,
                        "critic_summary": str(_c_summary or "")[:120],
                        "args": args,
                        "summary": summary,
                        "text": summary,
                        "adopted_policy_ids": list(adopted_policy_ids.get(func) or []),
                        "adopted_policy_versions": dict(adopted_policy_versions.get(func) or {}),
                    }
                )
                if ok and func not in _OPTIONAL_TOOLS and learning is not None and hasattr(learning, "record_correction_outcome"):
                    try:
                        for _prev in executed_tools[:-1]:
                            if _prev.get("ok") or _prev.get("via") == "selfcheck":
                                continue
                            _orig_func = str(_prev.get("func") or _prev.get("name") or "").strip()
                            if not _orig_func or _orig_func in _OPTIONAL_TOOLS:
                                continue
                            _orig_args = _prev.get("args") if isinstance(_prev.get("args"), dict) else {}
                            if func == _orig_func and (args or {}) == _orig_args:
                                continue
                            _sig = learning._signature(_orig_func, _orig_args) if hasattr(learning, "_signature") else _orig_func
                            _ckey = (_orig_func, _sig, func)
                            if _ckey in _recorded_corrections:
                                continue
                            _recorded_corrections.add(_ckey)
                            learning.record_correction_outcome(
                                original_func=_orig_func,
                                original_args=_orig_args,
                                failure_reason=str(_prev.get("summary") or _prev.get("text") or "执行失败"),
                                correction_func=func,
                                correction_args=args if isinstance(args, dict) else {},
                                correction_strategy="",
                                success=True,
                                verified=_tool_verified,
                                verifier_evidence=str(_c_summary or summary or "verified")[:120],
                                user_text=mem_text,
                                task_id=turn_id,
                            )
                    except Exception:
                        logger.debug("纠错回填 record_correction_outcome 失败（不影响主流程）", exc_info=True)
                if on_tool_event:
                    on_tool_event(func, ok, summary)
                if goal_controller is not None:  # GOAL_CTRL_V1
                    goal_controller.on_executed(func, args, ok)

                for sc in selfcheck_extra:
                    executed_tools.append({
                        "name": sc.get("name", "selfcheck"),
                        "func": sc.get("name", "selfcheck"),
                        "ok": bool(sc.get("ok")),
                        "args": sc.get("args") or {},
                        "summary": f"selfcheck {sc.get('r1_status', '')}",
                        "text": f"selfcheck {sc.get('r1_status', '')}",
                        "via": sc.get("via", "selfcheck"),
                    })

                # 跨轮工具事实（短摘要；列表类尽量带绝对路径便于指代）
                fact = f"{func} {'ok' if ok else 'fail'}: {summary}"
                if ok and func in ("list_desktop_files", "search_files"):
                    from .safety.pending_delete import (
                        extract_paths_from_tool_text,
                        maybe_set_pending_from_list_result,
                    )

                    paths = extract_paths_from_tool_text(out.text or "")
                    if paths:
                        # 保全路径（正斜杠缩字符），供「打开刚才那个Word」复用
                        shown = [p.replace("\\", "/") for p in paths[:6]]
                        short = "、".join(shown)
                        fact = (
                            f"{func} ok: {len(paths)}项 {short}"
                            + ("…" if len(paths) > 6 else "")
                        )
                        # 结构化槽位：最近文件（前5个完整路径）
                        self.session_store.set_slot(
                            session_id,
                            "last_files",
                            [p.replace("\\", "/") for p in paths[:5]],
                        )
                    self.session_store.append_tool_fact(
                        session_id, fact, max_len=_TOOL_FACT_LIST_CHARS
                    )
                    early = maybe_set_pending_from_list_result(
                        self.memory, mem_text, func, out.text or ""
                    )
                    if early:
                        # early 可能是多行批量路径
                        n = len([x for x in early.splitlines() if x.strip()])
                        self.session_store.append_tool_fact(
                            session_id,
                            f"pending_delete×{n}: "
                            + early.replace("\n", " | ")[:160],
                        )
                else:
                    self.session_store.append_tool_fact(session_id, fact)

                # 结构化任务状态槽位（应用/播放），供下轮「打开它/刚才的歌」指代
                try:
                    if ok and func == "open_app":
                        _app = str(args.get("app_name") or "").strip()
                        if _app:
                            self.session_store.set_slot(session_id, "last_app", _app)
                    elif ok and func == "play_music":
                        _q = str(args.get("query") or "").strip()
                        if _q:
                            self.session_store.set_slot(
                                session_id,
                                "now_playing",
                                f"音乐「{_q}」（{args.get('source') or 'qq'}）",
                            )
                    elif ok and func == "play_video":
                        _q = str(args.get("query") or "").strip()
                        if _q:
                            self.session_store.set_slot(
                                session_id,
                                "now_playing",
                                f"视频「{_q}」（{args.get('source') or 'tencent'}）",
                            )
                except Exception as _slot_err:
                    logger.debug("任务槽位更新失败: %s", _slot_err)

                if self.qs_store:
                    try:
                        self.qs_store.append_tool_result(
                            func,
                            args,
                            ok=ok,
                            text=out.text or "",
                            r1_status=resp.status,
                            session_id=session_id,
                            turn_id=turn_id,
                        )
                    except Exception as e:
                        logger.debug("QS 写入失败: %s", e)

                tool_text = self._format_tool_result(resp)
                messages.append(_make_tool_result_msg(tc, tool_text))

                inject = (resp.data or {}).get("inject_messages") or []
                if isinstance(inject, list):
                    for item in inject:
                        if isinstance(item, dict) and item.get("role"):
                            inject_batch.append(item)

            if cancel_check and cancel_check():
                final_reply = "好的，已停下。"
                turn_recordable = False
                break

            if tool_call_budget_exceeded():
                final_reply = "自主任务已达到工具调用预算上限，剩余步骤已停止。"
                llm_degraded = True
                turn_recordable = False
                break

            if autonomy_policy_violated():
                final_reply = "自主目标包含当前未获准的操作，任务已暂停并等待人工逐项审查。"
                llm_degraded = True
                turn_recordable = False
                break

            if inject_batch:
                # 截图只保留最新一张：旧图已落盘，需要时用 recall_screenshot 取回
                from .tools.screen_ops import message_has_screenshot, strip_screenshot_messages

                if any(message_has_screenshot(m) for m in inject_batch):
                    removed = strip_screenshot_messages(messages)
                    if removed:
                        logger.info("踢掉 messages 中旧截图 %d 条，只保留当前注入", removed)
                messages.extend(inject_batch)

            if tool_tier == 0 and should_expand_tool_tier(
                mem_text,
                tool_tier=tool_tier,
                had_tool_calls=True,
                round_had_failure=round_had_failure,
                round_missing_app_name=round_missing_app_name,
            ):
                tool_tier = 1
                tool_names = self._retrieve_tool_names(
                    mem_text,
                    mcp_tools=mcp_tool_names,
                    skill_tools=skill_tool_names,
                    top_k=effective_top_k,
                    tier=tool_tier,
                )
                messages.append({"role": "user", "content": _TIER_EXPAND_USER_NOTE})
                logger.info("工具集 tier0→tier1 展开（执行未达预期）: %d 个", len(tool_names))

            # 痛点2：已调工具但完全跑偏（删 Word 却去 search_web）→ 纠偏再跑一轮
            if (
                _missed_file_delete_tools(mem_text, executed_tools)
                and not missed_tools_retry
                and not force_text_reply
            ):
                missed_tools_retry = True
                force_tool_required = True
                if tool_tier == 0:
                    tool_tier = 1
                tool_names = self._retrieve_tool_names(
                    mem_text,
                    mcp_tools=mcp_tool_names,
                    skill_tools=skill_tool_names,
                    top_k=max(effective_top_k, 16),
                    tier=1,
                )
                messages.append({"role": "user", "content": _MISSED_DELETE_HINT})
                logger.info("删除/列文件意图未命中关键工具，纠偏重试")
                continue

        # CANCEL_TOKEN_V1: cancelled return — override exhaustion fallback, keep executed_tools.
        _cancelled_now = bool(cancel_check and cancel_check())
        if _cancelled_now:
            final_reply = "好的，已停下。"
            turn_recordable = False
        if final_reply is None and controller_fate is None:  # GOAL_CTRL_V1
            final_reply = "这个任务步骤较多，我先停在这里，您可以换个说法或拆成几步再说。"
            turn_recordable = False
            loop_exhausted = True  # Q2_TERMINAL_V1
        # ─── 静态指令：动态多步执行成功后 offer 固化（已学会则不打扰）──
        if final_reply and not _cancelled_now and self._static_replay_enabled():
            from .safety.static_compile import set_pending_compile, should_offer_compile
            from .tools.static_instruction import (
                _normalize_trigger,
                match_static_instruction,
                sanitize_steps_for_static,
            )

            if should_offer_compile(executed_tools, min_steps=self._min_steps()):
                trig = _normalize_trigger(mem_text)
                # 同请求已固化过 → 不重复 offer，避免打扰
                if trig and match_static_instruction(
                    self.memory.db, trig, threshold=0.6
                ) is None:
                    steps = sanitize_steps_for_static(
                        executed_tools, min_steps=self._min_steps()
                    )
                    if steps:
                        set_pending_compile(self.memory, steps, trig, session_id)
                        final_reply += (
                            "（要不要记住这套操作？回复「已完成」或「记下来」，"
                            "下次就能直接照做。）"
                        )
        # 播报前兜底：最终回复里若还残留自造工具标记（DSML/invoke 等）或夹带的调用 JSON，
        # 一律剥离，绝不把原始标记漏给用户（历史事故：回复直接吐 <｜｜DSML｜｜ calls>）
        if final_reply:
            cleaned = _strip_text_tool_call_blocks(final_reply)
            if cleaned != final_reply:
                logger.info("最终回复含残留工具标记，已剥离")
                final_reply = cleaned
        if final_reply and not streamed:
            _stream_text(final_reply, on_chunk, None)

        if final_reply:
            from .safety.pending_delete import scrub_fake_deleted_reply

            deleted_ok = any(
                t.get("func") in ("delete_file", "move_to_trash") and t.get("ok")
                for t in executed_tools
            )
            scrubbed = scrub_fake_deleted_reply(
                final_reply, self.memory, deleted_ok=deleted_ok
            )
            if scrubbed != final_reply:
                logger.info("scrubbed fake-deleted reply")
                final_reply = scrubbed
            if turn_recordable and not llm_degraded:
                self.session_store.append_turn(session_id, mem_text, final_reply)

        clear_agent_control()
        _fate = "cancelled" if _cancelled_now else ("loop_degraded" if llm_degraded else ("loop_exhausted" if loop_exhausted else "loop"))
        if controller_fate is not None and not _cancelled_now:  # GOAL_CTRL_V1
            _fate = controller_fate
        return final_reply, _fate, executed_tools  # Q2_TERMINAL_V1 + GOAL_CTRL_V1

    @staticmethod
    def _format_tool_result(resp) -> str:
        data = resp.data or {}
        if data.get("reason") == "cancelled":
            return "用户已打断，操作已取消。"
        text = str(data.get("text") or "").strip()
        _verified = data.get("verified")
        if _verified is not None:  # CLOSED_LOOP_P1_20261030
            _ev = f"[外部核验：verified={_verified}]"
            text = f"{text} {_ev}".strip() if text else _ev
        if resp.status == STATUS_SUCCESS:
            out = text or "执行成功"
        elif text:
            out = f"[{resp.status}] {text}"
        else:
            out = f"执行未成功，状态：{resp.status}"
        # 列表类：优先保住【分类摘要】与文档行，避免 .lnk 挤掉 Word
        is_list = (
            "桌面目录：" in out
            or "【分类摘要】" in out
            or "【已过滤后缀】" in out
            or out.startswith("找到")
            or "搜索结果" in out[:80]
        )
        max_chars = _TOOL_RESULT_LIST_MAX_CHARS if is_list else _TOOL_RESULT_MAX_CHARS
        if len(out) <= max_chars:
            return out
        if is_list:
            lines = out.splitlines()
            head: list[str] = []
            docs: list[str] = []
            rest: list[str] = []
            for i, line in enumerate(lines):
                low = line.lower()
                if (
                    i < 6
                    or "【" in line
                    or "桌面目录" in line
                    or line.startswith("名称 |")
                    or "共 " in line[:20]
                ):
                    head.append(line)
                elif any(
                    ext in low
                    for ext in (
                        ".docx",
                        ".doc",
                        ".xlsx",
                        ".xls",
                        ".txt",
                        ".pdf",
                        ".md",
                    )
                ):
                    docs.append(line)
                else:
                    rest.append(line)
            packed = "\n".join(head + docs)
            if len(packed) < max_chars - 80:
                budget = max_chars - len(packed) - 40
                extra: list[str] = []
                size = 0
                for line in rest:
                    if size + len(line) + 1 > budget:
                        break
                    extra.append(line)
                    size += len(line) + 1
                packed = "\n".join(head + docs + extra)
            if len(packed) > max_chars:
                packed = packed[: max_chars - 1] + "…"
            elif len(lines) > len(packed.splitlines()):
                packed += "\n…(其余快捷方式等已省略，请看上方【分类摘要】)"
            return packed
        return out[: max_chars - 1] + "…"
