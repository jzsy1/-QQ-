"""Agent Loop 工具候选 — BM25 元数据检索 + 渐进披露。

检索原则（对齐 MCP 渐进披露模式）：
- 工具的检索面（关键词/别名）全部进 registry._TOOL_KEYWORDS 元数据，
  BM25 从全池按用户话检索排序；漏检 → 补元数据，禁止加意图正则分支。
- 仅保留两类非检索逻辑：
  1) 媒体播放策略 _apply_media_policy（歌名/剧名直达无法静态枚举 + VIDEO HIJACK 事故防护）
  2) 安全/弃用过滤 filter_deferred_tool_names、_drop_legacy_wechat_tools
"""
from __future__ import annotations

import logging
import re

from ..contract import DiscoveryLimits
from ..intent_utils import extract_action_core
from ..tools_policy import filter_deferred_tool_names
from .media_intent import is_non_video_look
from .registry import get_tool_names, register_builtin_tools
from .tool_bm25 import rank_tools_by_text

_log = logging.getLogger(__name__)
_LIMITS = DiscoveryLimits()

# tier0 渐进披露第 1 轮的检索补位数（核心清单之外，BM25 命中才并入）
_TIER0_SEARCH_TOP_K = _LIMITS.tier0_search_top_k

# 旧版微信专用脚本，已弃用（读屏组合链替代），任何候选池都不再放行
_LEGACY_WECHAT_TOOLS = frozenset({
    "wechat_send_message",
    "wechat_read_messages",
    "wechat_reply_current",
})

_IM_INTENT_RE = re.compile(
    r"微信|wechat|qq(?!音乐)|钉钉|dingtalk|飞书|发个?消息|回个?(?:消息|微信)"
    r"|回复.{0,6}(?:消息|他|她|一下)|回复一下"
    r"|聊天|联系人|未读|谁找我|私信|群里|发给",
    re.I,
)

# 听歌：内置 QQ 音乐网页播放器（歌名直达兜底判定，见 _apply_media_policy）
_MUSIC_INTENT_RE = re.compile(
    r"听歌|放歌|点歌|来一首|放一?首|唱的|QQ音乐|我想听|我要听|想听|要听|帮我放|帮我听|"
    r"播放(?!.*(?:剧|电影|视频|综艺|动漫|纪录片|第\s*\d+\s*集|第.+集))|"
    r"播放.{0,12}(?:歌|曲|音乐|专辑)|听.{0,12}(?:歌|音乐|专辑|电台)|"
    r"(?:请|帮我|给我)?(?:听|(?<![播])放)\s*[\u4e00-\u9fffA-Za-z0-9·《》]{1,40}|"
    r"暂停音乐|下一首|上一首",
    re.I,
)

_MUSIC_WORKFLOW_TOOLS = (
    "play_music",
    "qqmusic_login",
    "qqmusic_status",
    "control_music",
    "set_volume",
)

# 看剧：科科斯视频空间网页播放器（对齐听歌 play_music，剧名直达兜底判定）
_MEDIA_INTENT_RE = re.compile(
    r"看.{0,20}(?:剧|电影|视频|动漫|综艺|纪录片|最新|第.+集)|追剧|第.{1,5}集|"
    r"最新一?集|更新到|关闭视频|暂停视频|腾讯视频|爱奇艺|哔哩|B\s*站|"
    r"(?:请|帮我|给我)?看\s*[\u4e00-\u9fffA-Za-z0-9·《》]{1,40}|"
    r"播放.{0,12}(?:剧|电影|视频|综艺|动漫)|"
    # 影视词必选：「好看的壁纸/头像/背景」不再触发视频劫持（曾误劫持「换一张好看的壁纸」）
    r"(?:推荐|热播|新出|有什么好看).{0,8}(?:剧|电影|电视剧|视频|动漫)|"
    r"好看的(?:剧|电影|电视剧|视频|动漫)|"
    r"随便(?:看|来|推荐).{0,6}(?:一部|一部剧|个剧|电影)",
    re.I,
)

_MEDIA_WORKFLOW_TOOLS = (
    "play_video",
    "video_login",
    "video_status",
    "close_video",
    "set_volume",
)

_LOOP_CORE_TOOLS = frozenset({
    "get_current_time",
    "search_tools",
    "open_app",
    "close_app",
    "read_screen",
    "recall_screenshot",
    "list_screenshots",
    "take_screenshot",
    "gui_action",
    "remember",
    "upsert_core_memory",
    "search_memory",
    "search_web",
    "acquire_skill",
    "list_open_windows",
    "list_workflows",
    "run_workflow",
    "run_sequence",
    "inspect_window_som",
    "execute_gui_macro",
    "scan_desktop",
})

# 元工具（发现 / 扩展能力）：任何 tier 都必须常驻可见。
# 实测教训：acquire_skill 只靠 BM25 命中才进候选，绝大多数"缺能力"话术
# 检索不到它 → 模型清单里看不到 → 提示词点名也调不了，自我扩展永不触发。
_ALWAYS_VISIBLE_TOOLS = (
    "search_tools",
    "acquire_skill",
    "uninstall_skill",
)

# 第 1 轮渐进披露：贾维斯核心能力（观察 / 执行 / 回忆 / 检索），不含写记忆与 MCP
_JARVIS_TIER0_TOOLS = (
    "get_current_time",
    "search_tools",
    "set_reminder",
    "open_app",
    "close_app",
    "read_screen",
    "recall_screenshot",
    "list_screenshots",
    "ocr_screen",
    "gui_action",
    "list_open_windows",
    "list_desktop_files",
    "list_desktop_icons",
    "show_desktop",
    "click_desktop_icon",
    "open_path",
    "search_files",
    "delete_file",
    "move_to_trash",
    "search_memory",
    "search_web",
    "list_workflows",
    "run_workflow",
    "run_sequence",
    "inspect_window_som",
    "execute_gui_macro",
    "scan_desktop",
)

_TYPE_AFTER_OPEN_RE = re.compile(r"输入|打字|写入|写")
_NEEDS_TOOL_HINT_RE = re.compile(
    r"无法|不能|没有(?:合适|可用)?工具|做不到|unsupported|no tool",
    re.I,
)
# tier0 未覆盖的信息查询意图——命中时触发 tier0→tier1 展开（兜底）
# 仅用于展开判断，工具选择始终由 BM25 全池检索完成
_INFO_SEEK_EXPAND_RE = re.compile(
    r"查|搜|搜索|怎么|什么|多少|为什么|是不是|有没有|能不能|"
    r"天气|温度|IP|蓝牙|网络|WiFi|音量|电量|内存|CPU|磁盘|"
    r"文件|文件夹|目录|复制|移动|重命名|删除|回收站|"
    r"服务|注册表|环境变量|剪贴板|进程|"
    r"系统|状态|安装|卸载|软件",
    re.I,
)


def _is_im_intent(text: str) -> bool:
    return bool(_IM_INTENT_RE.search(text or ""))


# 文档创建意图：agent_loop 承诺纠正时用于点名 create_document（提示文案，非检索）
_DOC_INTENT_RE = re.compile(
    r"Word|WORD|word|docx|文档|写成|写入|导出|生成.{0,6}(?:文件|文档)|"
    r"创建.{0,6}(?:文件|文档|Word|WORD)|对话记录|会议记录|备忘录",
    re.I,
)
# 删/清文档时不要把 create_document 顶到前面
_DOC_DELETE_INTENT_RE = re.compile(
    r"删|删除|清掉|清空|扔掉|移到回收站|进回收站|干掉",
    re.I,
)
_DOC_CREATE_HINT_RE = re.compile(
    r"写成|写入|导出|生成.{0,6}(?:文件|文档)|"
    r"创建.{0,6}(?:文件|文档|Word|WORD)|保存.{0,6}(?:文件|文档)|"
    r"对话记录|会议记录|备忘录",
    re.I,
)


def _is_doc_create_intent(user_text: str) -> bool:
    t = user_text or ""
    if _DOC_DELETE_INTENT_RE.search(t):
        return False
    return bool(_DOC_CREATE_HINT_RE.search(t) or (
        _DOC_INTENT_RE.search(t) and re.search(r"创建|写|生成|导出|保存", t)
    ))


def _merge_always_visible(names: list[str]) -> list[str]:
    """元工具置顶保底：任何 tier 的候选清单都不得裁掉 _ALWAYS_VISIBLE_TOOLS。

    注意必须是「移到头部」而不是「仅缺失时补入」：实测元工具本就在全池里，
    只是被 BM25 排到尾部（不在前若干名），模型只看清单前几行就看不到它。
    """
    valid = set(get_tool_names())
    head = [n for n in _ALWAYS_VISIBLE_TOOLS if n in valid]
    head_set = set(head)
    return head + [n for n in names if n not in head_set]


def _drop_legacy_wechat_tools(names: list[str]) -> list[str]:
    """剔除旧版微信脚本（已弃用）。始终剔除：其描述含"发送消息/联系人"，
    会按语义被 BM25 召回，须在候选池层面挡住。"""
    filtered = [n for n in names if n not in _LEGACY_WECHAT_TOOLS]
    if len(filtered) != len(names):
        _log.info(
            "[policy] 剔除旧版微信脚本: %s",
            [n for n in names if n in _LEGACY_WECHAT_TOOLS],
        )
    return filtered


def _apply_doc_delete_policy(names: list[str], user_text: str) -> list[str]:
    """删除文档场景剔除 create_document — 事故防护，非检索补丁。

    「删掉桌面上所有Word文档」里 create_document 会因描述含 word token
    被检索召回并置顶，小模型可能误调（历史事故：删 Word 跑去创建/搜索）。
    删除意图命中且非创建意图时不给 create_document（对齐旧 _inject_doc_workflow_tools）。
    """
    t = user_text or ""
    if _DOC_DELETE_INTENT_RE.search(t) and not _is_doc_create_intent(t):
        if "create_document" in names:
            _log.info("[policy] 删除文档场景剔除 create_document: '%s'", t)
            return [n for n in names if n != "create_document"]
    return names


def _apply_media_policy(names: list[str], user_text: str) -> list[str]:
    """媒体（听歌/看剧）播放策略 — 事故防护，非检索补丁。

    1) 歌名/剧名直达（"听稻香""看庆余年"）无法靠静态关键词元数据命中，
       命中媒体意图时保底把体内播放器工具链放进候选池；
    2) 看剧意图丢弃易误开桌面客户端的 GUI 工具（VIDEO HIJACK 事故防护），
       用户明说"打开桌面版/客户端"时放行；
    3) is_non_video_look 防止"看一下网络信息"类观察句被误劫持。
    """
    text = user_text or ""
    valid = set(get_tool_names())
    music_hit = bool(_MUSIC_INTENT_RE.search(text))
    video_hit = bool(_MEDIA_INTENT_RE.search(text))
    # 排除「看一下网络信息/看天气」等非视频观察句被 _MEDIA_INTENT_RE 误劫持
    # 对齐 media_intent 弱路由的 is_non_video_look，避免两套逻辑漂移
    if video_hit and is_non_video_look(text):
        _log.info(
            "[media_policy] '%s' 命中 is_non_video_look → 取消 video_hit（非视频观察句）",
            text,
        )
        video_hit = False
    if not music_hit and not video_hit:
        return names
    priority: list[str] = []
    if music_hit:
        priority.extend(n for n in _MUSIC_WORKFLOW_TOOLS if n in valid)
    if video_hit:
        priority.extend(n for n in _MEDIA_WORKFLOW_TOOLS if n in valid and n not in priority)
    seen = set(priority)
    rest = [n for n in names if n not in seen]
    # 看剧：候选中去掉 open_app / GUI，逼 LLM 用 play_video
    if video_hit and not re.search(
        r"(?:打开|启动).{0,6}(?:桌面版|客户端|软件|应用)|桌面(?:版|客户端)",
        text,
        re.I,
    ):
        _drop = {
            "open_app",
            "open_app_and_type",
            "open_url",
            "gui_action",
            "click_at",
            "type_text",
            "send_keys",
            "mouse_scroll",
            "read_screen",
            "ocr_screen",
            "ocr_find_text",
        }
        dropped = [n for n in rest if n in _drop]
        rest = [n for n in rest if n not in _drop]
        if dropped:
            _log.warning(
                "[media_policy] VIDEO HIJACK: '%s' → 置顶 %s, 丢弃 %s",
                text, priority, dropped,
            )
    result = priority + rest
    _log.info("[media_policy] final top-5=%s (total=%d)", result[:5], len(result))
    return result


def _narrow_tools_for_app_intent(text: str, names: list[str]) -> list[str]:
    """打开/关闭应用时收窄工具集（与 intent_utils 共用动词解析，避免多套正则）。"""
    t = (text or "").strip()
    if not t:
        return names
    valid = set(names)
    op, _core = extract_action_core(t)
    is_close = op == "c"
    is_open = op == "o"

    if is_close:
        priority = [n for n in ("close_app", "close_window") if n in valid]
        if priority:
            return priority

    if is_open:
        if _TYPE_AFTER_OPEN_RE.search(t):
            priority = [
                n for n in ("open_app_and_type", "open_app", "focus_window") if n in valid
            ]
        else:
            priority = [n for n in ("open_app", "focus_window") if n in valid]
        if priority:
            return priority

    return names


def tools_jarvis_tier0(user_text: str) -> list[str]:
    """渐进披露第 1 轮：BM25 检索命中置顶 + 贾维斯核心工具。

    核心清单覆盖观察/执行/回忆/检索；壁纸、音量、防火墙、电源、文档等
    能力由 BM25 按 registry._TOOL_KEYWORDS 元数据从全池捞取并置顶
    （对齐旧 _inject_* 注入器的置顶语义），替代手写意图正则。
    """
    register_builtin_tools()
    valid = set(get_tool_names())
    names = [n for n in _JARVIS_TIER0_TOOLS if n in valid]
    names = _narrow_tools_for_app_intent(user_text, names)
    names = _apply_media_policy(names, user_text)
    core = set(names)
    pool = [n for n in get_tool_names() if n not in core]
    extra = rank_tools_by_text(
        user_text, pool, top_k=_TIER0_SEARCH_TOP_K, positive_only=True
    )
    if extra:
        _log.info("[tier0] 检索置顶 %d: %s", len(extra), extra[:8])
    merged = _drop_legacy_wechat_tools(extra + names)
    merged = _apply_doc_delete_policy(merged, user_text)
    return _merge_always_visible(merged)


def _collect_full_tool_candidates(
    user_text: str,
    *,
    mcp_tools: list[str] | None = None,
    skill_tools: list[str] | None = None,
    top_k: int = _LIMITS.retriever_top_k,
) -> list[str]:
    """完整工具候选池 — 全工具池入池，BM25 元数据检索排序 + 媒体播放策略。"""
    text = (user_text or "").strip()
    register_builtin_tools()
    valid = set(get_tool_names())
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name in valid and name not in seen:
            seen.add(name)
            names.append(name)

    # 全工具池入池 — 不再走关键字门卫，让 BM25 从全池中选
    for n in _LOOP_CORE_TOOLS:
        _add(n)
    for n in get_tool_names():
        _add(n)
    _log.info("[retriever] query='%s' pool_size=%d (全池入池)", text, len(names))

    # MCP / Skill 工具
    mcp_picked: list[str] = []
    for mcp_name in mcp_tools or []:
        if isinstance(mcp_name, str) and mcp_name.startswith("mcp__"):
            if mcp_name not in seen:
                seen.add(mcp_name)
                mcp_picked.append(mcp_name)

    skill_picked: list[str] = []
    for skill_name in skill_tools or []:
        if isinstance(skill_name, str) and skill_name.startswith("skill__"):
            if skill_name not in seen:
                seen.add(skill_name)
                skill_picked.append(skill_name)

    # 安全过滤 + 应用意图收窄（仍保留，非关键字逻辑）
    pre_narrow = len(names)
    names = _narrow_tools_for_app_intent(text, names)
    if len(names) != pre_narrow:
        _log.info("[retriever] app_narrow: %d → %d: %s", pre_narrow, len(names), names[:5])
    pre_filter = len(names)
    names = filter_deferred_tool_names(names, text)
    if len(names) != pre_filter:
        _log.info("[retriever] deferred_filter: %d → %d", pre_filter, len(names))
    # BM25 从全池中选 top_k（语料含 registry._TOOL_KEYWORDS 检索元数据）；
    # 混合检索可用时按 RRF 主排序，会话内不做动态裁剪（保持清单稳定）
    names = rank_tools_by_text(text, names, top_k=max(top_k, len(_LOOP_CORE_TOOLS)))
    _log.info("[retriever] BM25 top-%d: %s", len(names), names[:8])
    # 媒体播放策略（歌名/剧名直达保底 + VIDEO HIJACK 事故防护）+ 弃用脚本剔除
    names = _apply_media_policy(names, text)
    names = _drop_legacy_wechat_tools(names)
    names = _apply_doc_delete_policy(names, text)
    # MCP / Skill 置后但保证进池（不被 top_k 裁掉）
    tail = [n for n in names if n not in set(mcp_picked) | set(skill_picked)]
    if mcp_picked:
        tail.extend(mcp_picked)
    if skill_picked:
        tail.extend(skill_picked)
    names = tail
    limit = max(top_k, len(_LOOP_CORE_TOOLS)) + min(
        len(mcp_picked) + len(skill_picked), _LIMITS.retriever_extra_cap
    )
    result = names[:limit]
    result = _merge_always_visible(result)
    _log.info("[retriever] final=%d: %s", len(result), result[:10])
    return result


def tools_full_pool(
    user_text: str,
    *,
    mcp_tools: list[str] | None = None,
    skill_tools: list[str] | None = None,
    top_k: int = _LIMITS.retriever_top_k,
) -> list[str]:
    """完整工具候选池（第 2 轮展开用）。"""
    return _collect_full_tool_candidates(
        user_text, mcp_tools=mcp_tools, skill_tools=skill_tools, top_k=top_k
    )


def tools_for_agent_loop(
    user_text: str,
    *,
    mcp_tools: list[str] | None = None,
    skill_tools: list[str] | None = None,
    top_k: int = _LIMITS.retriever_top_k,
    tier: int = 1,
) -> list[str]:
    """Agent Loop 工具列表；tier=0 仅核心，tier>=1 为完整池。"""
    if tier <= 0:
        merged = tools_jarvis_tier0(user_text)
        # MCP/Skill 工具不在 BM25 registry（_TOOL_KEYWORDS 不含它们），
        # 话术命中（mcp_bridge.tools_matching）后必须直接进 tier0 置顶；
        # 否则第一轮清单看不到浏览器/抓取等扩展工具，模型只能拿内置工具凑合
        seen = set(merged)
        hits = [
            n
            for n in list(mcp_tools or []) + list(skill_tools or [])
            if n and n not in seen and not seen.add(n)
        ]
        if hits:
            _log.info("[tier0] MCP/Skill 话术命中置顶 %d: %s", len(hits), hits[:8])
        return hits + merged
    return tools_full_pool(
        user_text, mcp_tools=mcp_tools, skill_tools=skill_tools, top_k=top_k
    )


def should_expand_tool_tier(
    user_text: str,
    *,
    tool_tier: int,
    had_tool_calls: bool,
    assistant_content: str = "",
    round_had_failure: bool = False,
    round_missing_app_name: bool = False,
) -> bool:
    """判断是否从 tier0 展开到完整工具集（ToolRegistry 渐进披露同款思路）。"""
    if tool_tier > 0:
        return False
    if round_had_failure or round_missing_app_name:
        return True
    if had_tool_calls:
        return False
    text = (user_text or "").strip()
    if not text:
        return False
    if _NEEDS_TOOL_HINT_RE.search(assistant_content or ""):
        return True
    if _is_im_intent(text):
        return True
    op, _ = extract_action_core(text)
    if op in ("o", "c"):
        return True
    # 信息查询意图 — tier0 核心工具覆盖不到，展开到全池让 BM25 检索
    if _INFO_SEEK_EXPAND_RE.search(text):
        return True
    return False
