"""工具注册表 — 供 Ollama tool calling 使用"""

from __future__ import annotations

from typing import Any, Callable

ToolHandler = Callable[..., tuple[bool, str]]

_TOOLS: dict[str, dict[str, Any]] = {}


def _register(
    name: str,
    description: str,
    parameters: dict,
    handler: ToolHandler,
    category: str = "",
):
    _TOOLS[name] = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "handler": handler,
        "category": category,
    }


# 检索关键词元数据（name → 空格分隔关键词串）。
# 这是工具目录的检索面：BM25（tool_bm25）把这些词拼进语料，
# 让"换壁纸/查IP/别让XX联网"这类口语查询能命中对应工具。
# 维护原则：漏检 → 补这里的关键词，禁止在检索层加意图正则分支。
_TOOL_KEYWORDS: dict[str, str] = {
    # 天气
    "get_weather": "天气 气温 温度 多少度 下雨 降雨 下雪 雨伞 穿衣服 冷不冷 热不热",
    # 蓝牙
    "get_bluetooth_status": "蓝牙 蓝牙状态 蓝牙开了吗",
    "set_bluetooth": "蓝牙 打开蓝牙 关闭蓝牙 开蓝牙 关蓝牙 关掉蓝牙 蓝牙开关",
    # 音量（系统/按应用/静音/查看）
    "set_volume": "音量 声音 调大 调小 大点声 小点声 调大声 调小声",
    "set_app_volume": "音量 应用音量 调小声 调大声 大点声 小点声 音量调到",
    "mute_app": "静音 消音 小声 闭麦 别出声",
    "list_app_volumes": "音量 查看音量 各应用音量 音量列表",
    # 亮度/显示器
    "set_brightness": "亮度 调亮 调暗 亮一点 暗一点 护眼",
    "get_brightness": "亮度 当前亮度 亮度是多少",
    "list_monitors": "显示器 屏幕 显示器信息 显示器列表",
    # 壁纸
    "set_wallpaper": "壁纸 墙纸 桌面背景 换壁纸 设置壁纸 背景图 换桌面",
    "get_wallpaper": "壁纸 墙纸 桌面背景 当前壁纸",
    # 电源计划/定时（刻意不含裸「睡眠」，立即睡眠走 system_power）
    "set_power_plan": "电源计划 省电模式 节能模式 高性能 卓越性能 平衡模式",
    "set_power_timeout": "自动睡眠 睡眠定时 定时睡眠 定时熄屏 熄屏时间 关屏时间 关闭显示器时间 自动关屏 电源超时",
    "get_power_timeouts": "熄屏时间 查看睡眠时间 电源超时",
    "list_power_plans": "电源计划 列出电源计划 电源计划列表",
    # Windows 更新（刻意不含裸「更新」，「更新到第X集」走 play_video）
    "check_updates": "检查更新 系统更新 检测更新 更新系统 有没有更新 系统升级",
    "install_updates": "安装更新 更新补丁 打补丁 安装补丁 装更新 补丁",
    "get_update_history": "更新历史 更新记录 补丁历史",
    # 防火墙/联网管控（裸「联网/上网」兜底"恢复微信联网/禁止迅雷上网"类隔词语序，
    # 二者进池由 LLM 按描述选择）
    "block_app_network": "联网 上网 禁止联网 屏蔽联网 别让联网 不许联网 拦截联网 阻止联网 禁网",
    "allow_app_network": "联网 上网 允许联网 恢复联网 解除屏蔽 解除禁止 让它联网",
    "get_firewall_status": "防火墙 防火墙状态",
    "list_firewall_rules": "防火墙规则 出站规则 入站规则",
    # 进程
    "list_processes": "进程 列出进程 查看进程 进程列表 正在运行 内存占用",
    "find_process": "进程 找进程 查进程",
    "kill_process": "结束进程 杀进程 杀掉进程",
    # 窗口管理
    "minimize_window": "最小化 收起",
    "focus_window": "聚焦 前置 置顶 切换窗口 切到",
    "close_window": "关闭窗口 关窗口",
    "list_open_windows": "窗口 打开的窗口 窗口列表 有哪些窗口",
    # 文档/文件（create 无裸「文档」：删除场景由 delete「删掉+文档」双命中稳压；
    # 裸「word/创建/新建」兜底"创建一个word文档"类隔词表述）
    "create_document": "写文档 创建文档 生成文档 写成 导出 word 整理成 存成 新建 创建 备忘录 会议记录 纪要",
    "list_directory": "列出文件 目录 文件夹内容 文件列表",
    "search_files": "找文件 搜索文件 文件在哪",
    "delete_file": "删除 删掉 删除文件 删除文档 删掉文档 文档",
    "move_to_trash": "移到回收站 扔进回收站 回收站",
    # 观察查询
    "get_network_info": "网络 IP 查IP 网络信息 IP地址 网卡 wifi",
    "get_temperatures": "温度 硬件温度 CPU温度 显卡温度",
    "system_power": "睡眠 关机 重启 锁屏 注销",
    # IM 组合链使用场景（看屏→点击→输入→最小化）
    "open_app": "打开 启动 打开应用 启动软件",
    "open_app_and_type": "打开 打开应用 输入",
    "open_path": "打开 打开文件 打开路径 打开文件夹",
    "open_url": "打开 打开网址 网站 网址",
    "read_screen": "看屏幕 读屏 屏幕内容 微信 聊天 消息 聊天记录",
    "ocr_screen": "识别屏幕 屏幕文字 聊天记录",
    "ocr_window": "识别窗口 窗口文字",
    "ocr_find_text": "找文字 定位文字 找联系人 聊天记录 消息",
    "gui_action": "点击界面 界面操作 微信 聊天",
    "inspect_window_som": "标注窗口 控件编号 窗口控件 界面控件 看窗口 微信 聊天",
    "execute_gui_macro": "连续点击 连续操作 批量点击 宏动作 点击输入 发送消息 微信",
    "upsert_core_memory": "记住 记下 微信叫 昵称是 路径在 存放在 默认用 偏好 关系",
    "forget_core_memory": "忘掉 忘记 不用记 删除记忆",
    "click_text": "点击文字 点文字 微信 聊天 消息 按钮",
    "find_text_location": "文字位置 定位文字",
    "click_at": "点击坐标 按坐标点击",
    "click_and_type": "点击并输入 搜索框 搜索输入",
    "fill_form": "填表 表单 填写",
    "click_menu_item": "菜单 菜单项 右键菜单",
    "click_image": "点击图片 图标点击",
    "wait_until_found": "等待出现 等待窗口",
    "wait_until_gone": "等待消失",
    "type_text": "打字 输入文字 输入 微信 聊天 消息",
    "send_keys": "按键 快捷键 回车 发送 消息",
    "write_clipboard": "剪贴板 复制 粘贴",
    "check_occlusion": "遮挡 挡住 检查遮挡",
    # 音乐（歌名直达走 fastpath，这里覆盖口语兜底）
    "play_music": "听歌 放歌 点歌 来一首 首歌 播放歌曲 音乐 我想听 想听 要听 下一首 上一首 暂停 电台 QQ音乐",
    "qqmusic_login": "登录 音乐账号 QQ音乐",
    "qqmusic_status": "音乐状态 播放状态",
    "control_music": "控制音乐 音乐暂停 下一首 上一首",
    # 视频（关键词覆盖口语；歌名/剧名直达兜底在 tool_retriever._apply_media_policy）
    "play_video": "看剧 追剧 电影 电视剧 视频 动漫 综艺 纪录片 播放视频 最新一集 第几集 腾讯视频 爱奇艺 哔哩哔哩 B站 在线看",
    "video_login": "登录 视频账号",
    "video_status": "视频状态 播放状态",
    "close_video": "关闭视频 关掉视频",
    # 自习（定时后台学知识 + 手动触发）
    "self_study": "自习 自主学习 学点东西 学东西 涨知识 充电 去学习 学习一下 让她学习 让他学习 补课",
    "acquire_skill": "不会 做不到 没有这个能力 没工具 没有工具 扩展技能 装技能 装插件 下载技能 下载插件 自学 学个新本事 新增能力 缺个功能",
    "uninstall_skill": "卸载插件 删除插件 卸载技能 删除技能 移除插件 删掉插件 卸载掉 不要这个插件 不需要这个技能 拆掉 已装技能 装了哪些插件",
}


def register_builtin_tools():
    """注册内置工具定义（handler 在 executor 中绑定）"""
    if _TOOLS:
        return

    _register(
        "get_current_time",
        "获取当前本地时间，用户问几点、什么时间时使用",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "search_tools",
        "搜索可用工具。当核心工具中没有你需要的功能时，用自然语言描述需求来搜索更多工具。"
        "例如：search_tools(query=\"查看网络IP地址\")、search_tools(query=\"截图\")、"
        "search_tools(query=\"关闭蓝牙\")。返回匹配的工具名称和描述，"
        "然后你可以直接调用返回的工具。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "对需要做的事情的自然语言描述，如\"查看网络IP地址\"、\"打开应用程序\"、\"关闭蓝牙\"",
                },
            },
            "required": ["query"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "open_app",
        "打开或启动本机应用。用户说「打开XX」「启动XX」时必须调用本工具；"
        "参数名必须是 app_name（软件中文名，如「记事本」「微信」），不要用 name。"
        "若已在运行会切换到该应用。"
        "在软件内发消息/点界面时：先 open_app，再 read_screen 看屏，"
        "然后用 gui_action / click_text / type_text / send_keys 分步操作，最后 minimize_window。",
        {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称，如「记事本」「微信」",
                },
                "force_new": {
                    "type": "boolean",
                    "description": "是否强制再开一个新窗口，默认 false",
                },
            },
            "required": ["app_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wechat_send_message",
        "【已弃用，优先用 open_app+read_screen+click_text+type_text 组合】"
        "在 IM 应用里找联系人并发送消息。",
        {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称（如用户所说的 IM 应用名，用于聚焦/启动）",
                },
                "contact": {
                    "type": "string",
                    "description": "联系人昵称或备注名（搜索时 2～4 字模糊匹配即可）",
                },
                "message": {
                    "type": "string",
                    "description": "要发送的文字内容",
                },
            },
            "required": ["app_name", "contact", "message"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wechat_read_messages",
        "【已弃用，优先 read_screen / ocr_screen】读取微信消息。",
        {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称，通常为「微信」",
                },
                "scope": {
                    "type": "string",
                    "enum": ["unread", "current", "contact"],
                    "description": "unread=未读会话（默认），current=当前会话，contact=指定联系人",
                },
                "contact": {
                    "type": "string",
                    "description": "scope=contact 时的联系人昵称或备注（2～4 字模糊匹配即可）",
                },
                "max_chats": {
                    "type": "integer",
                    "description": "未读模式下最多读取几个会话，默认 3",
                },
            },
            "required": ["app_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wechat_reply_current",
        "【已弃用，优先 type_text + send_keys】在当前微信会话里直接回复。",
        {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称，通常为「微信」",
                },
                "message": {
                    "type": "string",
                    "description": "要回复的文字内容",
                },
            },
            "required": ["app_name", "message"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "open_app_and_type",
        "打开应用并在其窗口中自动输入文字，如「打开记事本输入你好」",
        {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "应用名称"},
                "text": {"type": "string", "description": "要输入的文字内容"},
                "press_enter": {
                    "type": "boolean",
                    "description": "输入完成后是否按回车（用于搜索场景），默认 false",
                },
            },
            "required": ["app_name", "text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "search_web",
        "自动在互联网搜索并汇总多条结果（多源搜索+正文提取，类似 Perplexity）。"
        "用户问新闻、天气、知识、实时信息、「帮我查一下」「网上搜」时使用；"
        "返回多条全文/摘要，搜完后综合回答，不要只打开浏览器。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或自然语言问题",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多抓取几条结果，默认 5，范围 1-10",
                },
            },
            "required": ["query"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "fetch_web_page",
        "精读单个网页正文（分页读取）。search_web 结果不够详细、或用户给了具体网址时使用。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要读取的网页地址"},
                "start_index": {
                    "type": "integer",
                    "description": "从第几个字开始读，长文分页用，默认 0",
                },
                "max_length": {
                    "type": "integer",
                    "description": "本次最多返回字数，默认 8000",
                },
            },
            "required": ["url"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "web_search",
        "在默认浏览器打开搜索引擎结果页（必应等），不自动朗读汇总。"
        "用户说「打开浏览器搜索XX」「用浏览器搜XX」「在浏览器里搜索XX」时使用；"
        "仅查资料并由助手回答时用 search_web。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "self_study",
        "让科科斯立刻开始自习：后台自动上网搜索资料并总结成知识笔记存入记忆库，"
        "学到的知识会在之后的对话中被引用。用户说「去自习」「学点东西」「自主学习」时使用；"
        "可指定学习主题，不指定则自动从近期对话里挑。",
        {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "string",
                    "description": "要学习的主题，多个用逗号分隔；不填则自动选主题",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "acquire_skill",
        "自我扩展：确实找不到能完成需求的现成工具时调用。"
        "它会自动上网检索现成的技能/插件（GitHub 技能仓库、MCP server、PyPI 包），"
        "下载并装配成本地技能、热重载，然后把新工具名返回给你。"
        "need 用一句话描述缺的能力；装好后直接调用返回的新工具完成任务。",
        {
            "type": "object",
            "properties": {
                "need": {
                    "type": "string",
                    "description": "缺的能力，一句话描述，例如「把视频里的音频抽成 mp3」",
                },
            },
            "required": ["need"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "uninstall_skill",
        "卸载/删除已装的技能或插件（自我扩展装的能力不再需要时使用）。"
        "skill_ref 可传技能 id（如 im/qq）、工具名（skill__im__qq__qq_start）、"
        "裸技能名或 MCP 插件名；不传则返回当前已装清单。卸载后相关工具立即从工具清单消失。",
        {
            "type": "object",
            "properties": {
                "skill_ref": {
                    "type": "string",
                    "description": "要卸载的技能/插件名，如 im/qq；留空则只列出已装清单",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "type_text",
        "向当前已聚焦的窗口输入文字（不打开新应用）",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文字"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "search_files",
        "在本地电脑搜索文件名，不是网页搜索",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "文件名关键词"},
                "search_dir": {
                    "type": "string",
                    "description": "搜索目录，默认桌面",
                },
            },
            "required": ["keyword"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "create_document",
        "创建 Word/文本文档并写入内容（默认保存到桌面）。"
        "仅当用户明确要求「写成文档/导出Word/保存对话」时调用；"
        "闲聊、删除、扫桌面时禁止调用。"
        "filename 用用户说的名字；未指定则用「未命名文档」，禁止无故命名为「对话记录」。",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "文档正文（完整内容，可含多段对话）",
                },
                "title": {
                    "type": "string",
                    "description": "文档标题，可选",
                },
                "filename": {
                    "type": "string",
                    "description": "文件名，如「会议纪要.docx」；默认「未命名文档.docx」",
                },
                "folder": {
                    "type": "string",
                    "description": "保存目录，默认桌面",
                },
                "format": {
                    "type": "string",
                    "enum": ["docx", "txt", "md"],
                    "description": "格式，默认 docx",
                },
            },
            "required": ["content"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "run_shell",
        "执行系统命令，用于明确的系统底层操作。默认 PowerShell；"
        "需要管理员权限时设 admin=true（会自动提权，权限不足会弹 UAC）。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "shell": {
                    "type": "string",
                    "enum": ["powershell", "cmd"],
                    "description": "解释器，默认 powershell",
                },
                "admin": {
                    "type": "boolean",
                    "description": "是否以管理员权限执行，默认 false；操作系统级资源时设 true",
                },
            },
            "required": ["command"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "remember_app",
        "记住如何打开某个应用，用户教你「XX 是 notepad」时使用",
        {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "应用别名"},
                "command": {
                    "type": "string",
                    "description": "启动命令，如 notepad、msedge、code",
                },
            },
            "required": ["app_name", "command"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "remember_fact",
        "记住用户偏好或事实信息（键值对形式）",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "记忆键名"},
                "value": {"type": "string", "description": "要记住的内容"},
            },
            "required": ["key", "value"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "upsert_core_memory",
        "记住/更新核心实体关系（人脉别名、账号昵称、常用路径、默认方式等），之后每轮自动可用。"
        "用户说「老张微信叫一叶知秋」「周报放在D:\\工作\\周报」「以后听歌用QQ音乐」时使用",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "实体，如 老张、老婆、周报、听歌"},
                "relation": {"type": "string", "description": "关系/属性，如 微信昵称、存放路径、默认平台"},
                "object": {"type": "string", "description": "值，如 一叶知秋、D:\\工作\\周报、QQ音乐"},
            },
            "required": ["subject", "relation", "object"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "forget_core_memory",
        "删除核心实体关系（用户说「忘掉老张的微信」「老张那条不用记了」时使用）",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "实体"},
                "relation": {"type": "string", "description": "关系；留空则删除该实体全部关系"},
            },
            "required": ["subject"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "inspect_window_som",
        "UIA + OCR 融合标注目标窗口内的所有可交互控件与文字，返回 [#1], [#2] 编号列表（Set-of-Mark），配合 execute_gui_macro 一步连续执行",
        {
            "type": "object",
            "properties": {
                "window_title": {"type": "string", "description": "窗口标题关键词（留空则标注当前前台窗口）"},
                "include_ocr": {"type": "boolean", "description": "是否融合局部 OCR 文字，默认 true"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "execute_gui_macro",
        "连续执行多步 GUI 宏动作（支持按 inspect_window_som 的 #id 或文字 target 点击/输入/按键），优先后台无感操作，前台操作自动借用并归还用户原窗口焦点与鼠标位置",
        {
            "type": "object",
            "properties": {
                "window_title": {"type": "string", "description": "目标窗口标题"},
                "borrow_focus": {"type": "boolean", "description": "操作完成后是否自动归还用户原窗口焦点与鼠标位置，默认 true"},
                "steps": {
                    "type": "array",
                    "description": "动作列表，每项如 {'op': 'click', 'id': 1} 或 {'op': 'type', 'text': '内容', 'id': 2, 'enter': true} 或 {'op': 'key', 'keys': '{ENTER}'}",
                    "items": {"type": "object"},
                },
            },
            "required": ["steps"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "search_memory",
        "按关键词检索长期记忆、学会的操作与事实（用户问「上次/之前/记得吗」时使用）",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或自然语言问句"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 5"},
            },
            "required": ["query"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "cleanup_memory",
        "清理历史对话摘要与工具执行日志，保留最近若干条；用户说清理记忆/清日志时使用",
        {
            "type": "object",
            "properties": {
                "digest_keep": {
                    "type": "integer",
                    "description": "保留最近多少条对话摘要，默认 50",
                },
                "task_keep": {
                    "type": "integer",
                    "description": "保留最近多少条任务日志，默认 200",
                },
                "prune_stale_prefs": {
                    "type": "boolean",
                    "description": "是否删除已遗忘/空值的偏好条目",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "open_url",
        "在默认浏览器打开一个网址",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "网址"}},
            "required": ["url"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_weather",
        "查询某个城市的天气，城市名用中文或拼音，如 Beijing、上海",
        {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "set_volume",
        "设置系统音量，范围 0 到 100",
        {
            "type": "object",
            "properties": {"level": {"type": "integer", "description": "音量 0-100", "minimum": 0, "maximum": 100}},
            "required": ["level"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_app_volumes",
        "列出当前所有应用的独立音量（每个正在发声/发过声的应用一条），"
        "返回进程名、音量百分比、静音状态、pid。想按应用调音量前可用它确认应用的确切进程名。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_app_volume",
        "单独设置某个应用的音量（0-100），不影响系统主音量和其他应用。"
        "app 填应用进程名（可先 list_app_volumes 查询），如 QQMusic、chrome、msedge；"
        "不确定进程名时先调用 list_app_volumes。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "应用进程名或名称关键词"},
                "level": {"type": "integer", "description": "音量 0-100", "minimum": 0, "maximum": 100},
            },
            "required": ["app", "level"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "mute_app",
        "单独静音/取消静音某个应用，不影响系统主音量和其他应用。"
        "muted 缺省 true=静音；传 false 为取消静音。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "应用进程名或名称关键词"},
                "muted": {"type": "boolean", "description": "true=静音（默认），false=取消静音"},
            },
            "required": ["app"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_monitors",
        "列出所有显示器及其亮度调节支持情况（物理屏名称、当前亮度、是否虚拟屏）。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "get_brightness",
        "读取显示器当前亮度（0-100）。monitor 可选：显示器序号/名称关键词，缺省读主屏（单屏机器无需传）。",
        {
            "type": "object",
            "properties": {
                "monitor": {"type": "string", "description": "显示器序号或名称关键词，可选"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "set_brightness",
        "设置显示器亮度（0-100）。外接屏走 DDC/CI、笔记本内置屏走 WMI，自动选择通道。"
        "monitor 可选：显示器序号/名称关键词，缺省全部可调屏。",
        {
            "type": "object",
            "properties": {
                "level": {"type": "integer", "description": "亮度 0-100", "minimum": 0, "maximum": 100},
                "monitor": {"type": "string", "description": "显示器序号或名称关键词，可选"},
            },
            "required": ["level"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_wallpaper",
        "读取当前桌面壁纸路径与显示样式（填充/适应/拉伸/居中/跨屏）。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_wallpaper",
        "设置桌面壁纸。image_path 为本地图片绝对路径（支持 bmp/jpg/png，webp 不支持）；"
        "style 可选：填充（默认样式，不动注册表）/适应/拉伸/居中/跨屏。",
        {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "本地图片绝对路径"},
                "style": {
                    "type": "string",
                    "description": "显示样式，可选：填充/适应/拉伸/居中/跨屏",
                    "enum": ["填充", "适应", "拉伸", "居中", "跨屏"],
                },
            },
            "required": ["image_path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_power_plans",
        "列出所有电源计划（省电/平衡/高性能等）并标注当前使用的计划。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_power_plan",
        "切换电源计划。plan 填计划名称关键词，如：省电（节能）/平衡/高性能/卓越性能，"
        "也可填计划 GUID。注意：这是切换性能模式，不是立即睡眠。",
        {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "计划名称关键词或 GUID"},
            },
            "required": ["plan"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_power_timeouts",
        "读取当前电源计划的睡眠定时和熄屏（关闭显示）定时。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_power_timeout",
        "设置睡眠/熄屏定时（分钟），0=从不。kind: sleep=睡眠 / monitor=关闭显示（熄屏）；"
        "on_battery 可选：true=仅用电池时，缺省为接通电源（台式机）。"
        "注意：这是设置自动睡眠的等待时间，不是立即睡眠。",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "sleep=睡眠 / monitor=熄屏", "enum": ["sleep", "monitor"]},
                "minutes": {"type": "integer", "description": "分钟数 0-1440，0=从不", "minimum": 0, "maximum": 1440},
                "on_battery": {"type": "boolean", "description": "true=仅用电池时生效，可选"},
            },
            "required": ["kind", "minutes"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "check_updates",
        "检查 Windows 更新：搜索微软服务器上的可用更新，列出标题/大小/是否需要重启。"
        "需联网，可能需要 1-3 分钟。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "install_updates",
        "下载并安装 Windows 更新（需要管理员权限）。update_filter 可选："
        "安装标题包含该关键词的更新；缺省安装全部可用更新。"
        "安装耗时可能较长，完成后会提示是否需要重启。",
        {
            "type": "object",
            "properties": {
                "update_filter": {"type": "string", "description": "标题关键词过滤，可选"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_update_history",
        "读取最近 20 条 Windows 更新安装历史（本地记录，速度快）。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "get_firewall_status",
        "读取防火墙三个配置文件（域/专用/公用）的开关状态。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "list_firewall_rules",
        "列出科科斯创建的防火墙规则（kks_ 前缀）并统计全部规则数。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "block_app_network",
        "屏蔽应用联网（防火墙出站阻止规则，需要管理员权限）。app 填应用名"
        "（如：微信、chrome、迅雷）或完整 exe 路径；direction 可选 out=出站"
        "（缺省）/in=入站/both。会自动解析应用路径。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "应用名或完整 exe 路径"},
                "direction": {"type": "string", "description": "out/in/both，可选", "enum": ["out", "in", "both"]},
            },
            "required": ["app"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "allow_app_network",
        "恢复应用联网：删除科科斯为该应用创建的屏蔽规则（需要管理员权限，"
        "不会动用户手工创建的规则）。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "应用名"},
            },
            "required": ["app"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "play_music",
        "在科科斯体内空间用网页音乐访客后台点歌（QQ/网易云/酷狗，不打开桌面客户端）。"
        "query 填歌名，可带歌手；source 可选 qq / netease / kugou。"
        "播收藏列表时仅当用户明确说「我喜欢列表/歌单/我喜欢的歌/红心/收藏」才设 playlist=liked；"
        "用户说「我想听我喜欢」是点歌名，query 填「我喜欢」，不要当成歌单。"
        "听歌优先用本工具，禁止 search_web，不要 open_app 音乐客户端。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌名，可含歌手；playlist=liked 时可省略",
                },
                "playlist": {
                    "type": "string",
                    "enum": ["liked"],
                    "description": "liked=播放收藏「我喜欢」列表（需用户明确要列表/歌单，且已登录）",
                },
                "source": {
                    "type": "string",
                    "enum": ["qq", "netease", "kugou"],
                    "description": "播放器：qq=QQ音乐，netease=网易云，kugou=酷狗",
                },
                "show": {
                    "type": "boolean",
                    "description": "是否显示体内空间窗口，默认 false",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "qqmusic_login",
        "在科科斯体内空间打开网页音乐登录页供扫码（QQ/网易云/酷狗）。"
        "source 可选 qq/netease/kugou，默认 qq。",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["qq", "netease", "kugou"],
                }
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "qqmusic_status",
        "查询体内空间是否运行、当前网页音乐访客是否已登录、当前歌曲",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "control_music",
        "控制音乐播放：play 播放, pause 暂停, playpause 切换, next 下一首, previous 上一首。"
        "优先控制体内空间网页播放器；若未在播则回退系统媒体键。",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "playpause", "next", "previous"],
                }
            },
            "required": ["action"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "play_video",
        "在科科斯视频空间用网页看剧（腾讯视频/B站/爱奇艺，独立于听歌空间）。"
        "query 必须填具体剧名（如「完美世界」），禁止填「新出来的电视剧」「热播剧」这类描述。"
        "episode 填明确集数；若用户说「最新一集/更新到哪了」则设 latest=true（不要瞎猜集数）。"
        "模糊推荐时可先 search_web 再对本工具传入具体剧名。"
        "source 可选 tencent / bilibili / iqiyi。"
        "看剧优先用本工具，不要 open_app 视频客户端。",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "剧名/番名，不要带「最新一集」等修饰",
                },
                "episode": {
                    "type": "integer",
                    "description": "明确集数，如 55；与 latest 二选一",
                },
                "latest": {
                    "type": "boolean",
                    "description": "是否播最新一集；用户说最新/更新时用 true",
                },
                "source": {
                    "type": "string",
                    "enum": ["tencent", "bilibili", "iqiyi"],
                    "description": "源站：tencent=腾讯视频，bilibili=哔哩哔哩，iqiyi=爱奇艺",
                },
            },
            "required": ["query"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "video_login",
        "在科科斯视频空间打开网页视频登录页供扫码。"
        "source 可选 tencent/bilibili/iqiyi，默认 tencent。",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["tencent", "bilibili", "iqiyi"],
                }
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "video_status",
        "查询视频空间是否运行、当前源站与剧集",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "close_video",
        "关闭科科斯视频空间（停播并收起看剧窗口）",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )



    _register(
        "set_timer",
        "设置倒计时提醒，到点播放提示音",
        {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "倒计时秒数"},
                "message": {"type": "string", "description": "到点提醒内容"},
            },
            "required": ["seconds"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "take_screenshot",
        "截图：默认全屏并保存到桌面；提供 title/hwnd 时用 PrintWindow 截取指定窗口（被遮挡仍可抓到）。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题关键词（截指定窗口时提供）"},
                "hwnd": {"type": "integer", "description": "窗口句柄（与 title 二选一）"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "system_power",
        "电源控制：lock 锁屏, sleep 休眠, logoff 注销, shutdown 关机, restart 重启。"
        "shutdown/restart 会延迟 60 秒，仅当用户已明确口头下令才可调用。",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["lock", "sleep", "logoff", "shutdown", "restart"],
                },
            },
            "required": ["action"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_directory",
        "列出某个目录下的文件和子文件夹，做多步文件任务时先用它了解现状",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，支持 ~，如 ~/Downloads",
                }
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "move_to_trash",
        "把文件或文件夹移到回收站（用户说删除/删掉文件时用，比永久删除安全）",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径或文件名，如 桌面/test.txt"}},
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "delete_file",
        "删除文件或文件夹（移到回收站，可恢复）。用户说「删除」「删掉」「移除」某个文件时必须调用，不要只回复已删除。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的路径或文件名，如 test.txt、桌面/报告.pdf",
                },
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "empty_recycle_bin",
        "清空 Windows 回收站（永久删除回收站内全部文件，不可恢复）。"
        "用户说「清空回收站」「清空垃圾箱」时必须调用，不要只回复已清空。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "remember",
        "记住用户的事实或偏好，跨重启永久记住。一句话事实直接填 fact；"
        "结构化偏好填 key+value（如 key=昵称, value=小明）。",
        {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "要记住的一句话，如「用户叫小明」「用户喜欢安静」",
                },
                "key": {
                    "type": "string",
                    "description": "记忆键名（与 value 搭配使用，二选一）",
                },
                "value": {
                    "type": "string",
                    "description": "要记住的内容（与 key 搭配使用，二选一）",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "forget",
        "删除长期记忆中含某关键词的条目",
        {
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "关键词"}},
            "required": ["keyword"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "read_screen",
        "读取当前屏幕：截图会落盘存档并注入当前画面供分析。"
        "返回 screenshot_id；messages 里只保留最新一张图，历史图用 recall_screenshot 按 id 取回。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "recall_screenshot",
        "从截图存档按 screenshot_id 取回历史画面并注入（会替换 messages 里当前那张图）。"
        "需要对比更早屏幕、或旧图已被踢出上下文时使用。",
        {
            "type": "object",
            "properties": {
                "screenshot_id": {
                    "type": "string",
                    "description": "read_screen 返回的 id，如 20260722_111500_123",
                },
            },
            "required": ["screenshot_id"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_screenshots",
        "列出最近落盘的截图 id（需要找历史画面时先看这个）",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_windows",
        "列出当前可见窗口（进程名、PID、标题）。需要句柄/是否激活/窗口矩形时改用 list_open_windows。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 40"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_open_windows",
        "【桌面感知】列出可见顶层窗口：标题、句柄、进程、PID、是否前台激活、屏幕矩形。"
        "做激活/关闭/切换窗口前先调用，不要猜窗口标题。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 40"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_desktop_files",
        "【桌面感知】列出桌面文件/文件夹/快捷方式：名称、类型、完整路径，并尽量附上图标坐标。"
        "结果含【分类摘要】（Word/Excel 文件名会优先列出）。"
        "删除 Word 时传 extensions=\"docx,doc\"；打开/删除/移动桌面文件前先调用，不要猜路径。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 80"},
                "extensions": {
                    "type": "string",
                    "description": "后缀过滤，如 docx,doc 或 xlsx；删 Word 时务必传 docx,doc",
                },
                "name_contains": {
                    "type": "string",
                    "description": "文件名包含的关键字，可选",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_desktop_icons",
        "【桌面感知】列出桌面图标名称与屏幕中心坐标 (x,y)。"
        "一般点图标请优先用 click_desktop_icon（会先清桌面）；"
        "仅当需要自定义单击/坐标时再用本工具 + click_at。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 80"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "show_desktop",
        "【桌面感知】露出桌面：最小化当前窗口，便于随后点击桌面图标。"
        "在 click_desktop_icon 之外单独点坐标前应先调用。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "click_desktop_icon",
        "【桌面感知】按名称点击桌面图标：默认先露出桌面再双击。"
        "打开桌面快捷方式/图标时优先用本工具；有完整路径时优先 open_path。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "图标显示名，如 微信、Cursor"},
                "double": {
                    "type": "boolean",
                    "description": "是否双击，默认 true",
                },
                "show_first": {
                    "type": "boolean",
                    "description": "点击前是否先露出桌面，默认 true",
                },
            },
            "required": ["name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "open_path",
        "按完整路径或桌面文件名用系统默认程序打开（文件/文件夹/快捷方式）。"
        "删除前用 list_desktop_files 取路径；打开桌面文件优先本工具，不要猜坐标。"
        "也可传「桌面/xxx.txt」或纯文件名。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径，或 桌面/文件名，或桌面上的文件名",
                },
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "focus_window",
        "按窗口标题关键词聚焦/切换到前台",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题中的关键词"},
            },
            "required": ["title"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "close_app",
        "按应用名称关闭软件。用户说「关闭XX」「关掉XX」「退出XX」时必须调用本工具；"
        "参数名必须是 app_name（如「记事本」「微信」）。"
        "不要用 kill_process、list_processes 或 find_process 代替关闭应用。",
        {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "应用名称，如 微信、记事本、Chrome"},
                "force": {
                    "type": "boolean",
                    "description": "默认 true 直接结束进程（快、可靠）；"
                    "设 false 则先尝试优雅关闭主窗口，未退出再强制结束",
                },
            },
            "required": ["app_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "close_window",
        "按窗口标题关键词关闭窗口（发送关闭消息，非强杀进程）",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题中的关键词"},
            },
            "required": ["title"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "close_all_apps",
        "关闭所有带窗口的用户应用（一次批量完成）。"
        "用户说「关闭所有程序/关掉所有窗口/全部关掉」时使用，不要逐个 kill_process。"
        "自动保护：克克斯自身进程、CosyVoice/MCP 子进程、Cursor/VS Code 窗口。"
        "默认发关闭消息；force=true 强杀进程，仅当用户已明确口头确认后才可设 force。",
        {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "是否强杀进程，默认 false（发关闭消息）",
                },
                "keep_title": {
                    "type": "string",
                    "description": "保留标题含此关键词的窗口，如 Cursor",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "minimize_window",
        "按窗口标题关键词最小化窗口",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题中的关键词"},
            },
            "required": ["title"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_processes",
        "列出运行中的进程（按内存占用排序）",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 40"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "find_process",
        "按 名称/PID/窗口标题/命令行 精准查找进程，返回 PID、路径、命令行。"
        "结束进程前先用它定位，尤其 python、node、Electron 等同名多进程场景。",
        {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "进程名、PID 或命令行关键词，如 chrome、1234、main.py",
                },
                "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
            },
            "required": ["keyword"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "kill_process",
        "结束指定进程。不能结束 explorer 等系统关键进程；批量或有风险时先口头确认。",
        {
            "type": "object",
            "properties": {
                "name_or_pid": {
                    "type": "string",
                    "description": "进程名（如 chrome）或 PID 数字",
                },
                "force": {
                    "type": "boolean",
                    "description": "默认 true 直接强制结束（杀进程语义）；"
                    "设 false 则先尝试优雅关闭，未退出再强制结束",
                },
            },
            "required": ["name_or_pid"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "read_clipboard",
        "读取系统剪贴板文字内容",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "write_clipboard",
        "写入文字到系统剪贴板",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要写入剪贴板的文字"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_system_status",
        "获取 CPU、内存、C 盘空间、系统运行时长",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "get_hardware_info",
        "获取本机硬件详情（类似鲁大师）：CPU/主板/内存条/显卡/磁盘/分区，并附带温度摘要。"
        "用户问配置、硬件信息、显卡型号、几代 U 时使用。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "get_temperatures",
        "读取硬件温度（科科斯体内传感器服务，不打开桌面监控窗）：CPU/GPU/主板/磁盘。"
        "用户问温度、几度、热不热、显卡温度时使用。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_temp_threshold",
        "设定硬件温度报警阈值（℃），并可开启/关闭自动报警。"
        "例如用户说「CPU超过85度就报警」「GPU温度阈值90度并打开报警」。"
        "只改提到的项；alert_enabled=true 后超阈值会语音提醒。",
        {
            "type": "object",
            "properties": {
                "cpu": {"type": "number", "description": "CPU 报警阈值℃，默认85"},
                "gpu": {"type": "number", "description": "GPU 报警阈值℃，默认85"},
                "ssd": {"type": "number", "description": "固态/硬盘报警阈值℃，默认70"},
                "other": {"type": "number", "description": "其它传感器阈值℃，默认80"},
                "alert_enabled": {
                    "type": "boolean",
                    "description": "是否开启自动温度报警",
                },
                "interval_sec": {
                    "type": "integer",
                    "description": "巡检间隔秒，默认30，最小15",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_temp_threshold",
        "查看当前温度报警阈值与是否已开启自动报警",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "setup_temp_sensors",
        "启动科科斯体内硬件传感器服务（无桌面窗口）。完整 CPU 温度首次可能需同意一次 UAC。"
        "用户说启动温度监控、体内传感器时调用。",
        {
            "type": "object",
            "properties": {
                "download": {
                    "type": "boolean",
                    "description": "已忽略，保留兼容",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "set_reminder",
        "设置定时提醒或定时任务（统一入口）。三种用法："
        "①自然语言时间提醒：给 title+time_expr（如\"明天早上8点\"）；"
        "②倒计时：给 seconds（秒数），到点播放提示音；"
        "③定时执行动作：给 time_expr+action_func(+action_args)，到期自动调用指定工具。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "提醒/任务标题"},
                "time_expr": {
                    "type": "string",
                    "description": "时间表达式，如\"明天早上8点\"、\"下午3点\"、\"30分钟后\"",
                },
                "seconds": {
                    "type": "integer",
                    "description": "倒计时秒数（与 time_expr 二选一）",
                    "minimum": 1,
                },
                "message": {"type": "string", "description": "到点提醒内容（倒计时用）"},
                "description": {"type": "string", "description": "提醒详情（可选）"},
                "action_func": {
                    "type": "string",
                    "description": "到期执行的工具名，如 open_app（定时动作用）",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "schedule_action",
        "安排定时执行动作（如\"明天早上9点打开记事本\"）。到期后自动调用指定工具。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题"},
                "time_expr": {"type": "string", "description": "时间表达式，如\"明天早上9点\""},
                "action_func": {"type": "string", "description": "到期执行的工具名，如 open_app"},
                "action_args": {
                    "type": "object",
                    "description": "工具参数，如 {\"app_name\": \"记事本\"}",
                },
                "description": {"type": "string", "description": "备注（可选）"},
            },
            "required": ["title", "time_expr", "action_func"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_tasks",
        "列出所有待执行的任务和提醒",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "add_watch_rule",
        "创建自主监控规则：满足条件时主动询问用户，超时无回应可执行默认动作。"
        "用户说「以后电脑空闲超过X分钟就问我要不要关机，没回应就直接关」这类话时使用。"
        "condition_type 可选：idle_minutes(键鼠空闲N分钟)/cpu_above(CPU超N%)/"
        "memory_above(内存超N%)/disk_free_below_gb(C盘剩余低于N GB)/"
        "cpu_temp_above(CPU温度超N℃)/gpu_temp_above(GPU温度超N℃)/temp_above(任一传感器超N℃)。"
        "纯温度自动语音报警更推荐 set_temp_threshold(alert_enabled=true)。",
        {
            "type": "object",
            "properties": {
                "condition_type": {
                    "type": "string",
                    "enum": [
                        "idle_minutes",
                        "cpu_above",
                        "memory_above",
                        "disk_free_below_gb",
                        "cpu_temp_above",
                        "gpu_temp_above",
                        "temp_above",
                    ],
                    "description": "触发条件类型",
                },
                "threshold": {"type": "number", "description": "阈值（分钟/百分比/GB/℃）"},
                "question": {
                    "type": "string",
                    "description": "触发时向用户播报的询问，如「先生，电脑空闲很久了，要帮您关机吗？」",
                },
                "action_func": {
                    "type": "string",
                    "description": "超时默认执行的工具名（如 system_power）；留空则仅提醒",
                },
                "action_args": {
                    "type": "object",
                    "description": "默认动作参数，如 {\"action\":\"shutdown\"}",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "等待用户回应的秒数，默认60；0表示仅提醒",
                },
                "description": {"type": "string", "description": "规则简述"},
            },
            "required": ["condition_type", "threshold", "question"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_watch_rules",
        "列出所有自主监控规则（用户问「有哪些监控/自动规则」时使用）",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "delete_watch_rule",
        "删除指定的自主监控规则",
        {
            "type": "object",
            "properties": {
                "rule_id": {"type": "integer", "description": "规则ID"},
            },
            "required": ["rule_id"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "complete_task",
        "标记任务为已完成",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务ID"},
            },
            "required": ["task_id"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "delete_task",
        "删除指定的任务或提醒",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务ID"},
            },
            "required": ["task_id"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "detect_emotion",
        "分析用户输入文本中的情感状态（开心/难过/生气/疲惫/焦虑/惊讶），返回情感类型和置信度",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要分析的文本"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "send_keys",
        "向当前焦点窗口发送快捷键（SendKeys 语法）。"
        "示例：^c 复制、^v 粘贴、^s 保存、^f 查找、{ENTER} 回车、%{TAB} Alt+Tab、^+{ESC} Ctrl+Shift+Esc",
        {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "SendKeys 语法快捷键字符串",
                },
            },
            "required": ["keys"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "copy_path",
        "复制文件或文件夹到目标路径",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源路径"},
                "destination": {"type": "string", "description": "目标路径"},
            },
            "required": ["source", "destination"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "move_path",
        "移动文件或文件夹到目标路径",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源路径"},
                "destination": {"type": "string", "description": "目标路径"},
            },
            "required": ["source", "destination"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "rename_path",
        "重命名文件或文件夹（仅改名称，不改目录）",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "原文件/文件夹路径"},
                "new_name": {"type": "string", "description": "新名称（不含路径）"},
            },
            "required": ["path", "new_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "read_registry",
        "读取 Windows 注册表项或值。path 如 HKCU:\\Software\\MyApp，可选 name 读取单个值",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "注册表路径"},
                "name": {"type": "string", "description": "值名称，留空则列出所有值"},
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "write_registry",
        "写入当前用户注册表 HKCU（仅 HKCU）。修改系统设置前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "注册表路径，如 HKCU:\\Software\\MyApp"},
                "name": {"type": "string", "description": "值名称"},
                "value": {"type": "string", "description": "要写入的值"},
                "value_type": {
                    "type": "string",
                    "enum": ["String", "ExpandString", "DWord", "QWord", "MultiString"],
                    "description": "值类型，默认 String",
                },
            },
            "required": ["path", "name", "value"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_services",
        "列出 Windows 服务及运行状态",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "running", "stopped"],
                    "description": "筛选状态，默认 all",
                },
                "limit": {"type": "integer", "description": "最多返回条数"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "control_service",
        "启动/停止/重启 Windows 服务。停止系统关键服务前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "服务名称，如 Spooler"},
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "restart"],
                },
            },
            "required": ["name", "action"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_scheduled_tasks",
        "列出 Windows 计划任务",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 30"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "run_scheduled_task",
        "立即触发运行一个计划任务",
        {
            "type": "object",
            "properties": {
                "task_name": {"type": "string", "description": "计划任务名称"},
            },
            "required": ["task_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "read_env_var",
        "读取环境变量（User/Process/Machine 各作用域）",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "变量名，如 PATH、JAVA_HOME"},
            },
            "required": ["name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "set_env_var",
        "设置当前用户环境变量（User 作用域，永久生效）。修改前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "变量名"},
                "value": {"type": "string", "description": "变量值"},
            },
            "required": ["name", "value"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_env_vars",
        "列出当前用户环境变量，可按前缀筛选",
        {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "名称前缀，如 JAVA、PATH"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 40"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_installed_software",
        "列出本机已安装软件清单（winget，较慢，30~120 秒）。"
        "仅当用户明确要『列出/查看所有已安装软件』时才用；"
        "判断某个软件是否安装或能否打开，请改用 find_process 或 open_app，不要用本工具。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "description": "最多返回条数"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "install_software",
        "用 winget 安装软件。安装前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "软件包 ID（如 Microsoft.VisualStudioCode）或名称",
                },
                "silent": {
                    "type": "boolean",
                    "description": "是否静默安装，默认 true",
                },
            },
            "required": ["package"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "uninstall_software",
        "用 winget 卸载软件。卸载前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "软件包 ID 或名称",
                },
                "silent": {
                    "type": "boolean",
                    "description": "是否静默卸载，默认 true",
                },
            },
            "required": ["package"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_network_info",
        "获取网卡 IP、网关、DNS 等网络信息",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_dns",
        "设置指定网卡的 DNS 服务器。修改前必须先口头确认，可能需要管理员权限。",
        {
            "type": "object",
            "properties": {
                "interface_alias": {
                    "type": "string",
                    "description": "网卡名称，如 以太网、WLAN",
                },
                "primary": {"type": "string", "description": "主 DNS，如 8.8.8.8"},
                "secondary": {"type": "string", "description": "备用 DNS，可选"},
            },
            "required": ["interface_alias", "primary"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_wifi_networks",
        "扫描附近可用 Wi-Fi 网络",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "list_wifi_profiles",
        "列出已保存的 Wi-Fi 配置文件",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "connect_wifi",
        "连接 Wi-Fi。若未保存过该网络，需提供 password。",
        {
            "type": "object",
            "properties": {
                "ssid": {"type": "string", "description": "Wi-Fi 名称"},
                "password": {"type": "string", "description": "密码（新网络必填）"},
            },
            "required": ["ssid"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_bluetooth_status",
        "查询本机蓝牙适配器的开关状态（是否开启/已连接设备）",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "set_bluetooth",
        "开启或关闭本机蓝牙适配器。关闭前必须先口头确认。通常需要管理员权限。",
        {
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true=开启蓝牙，false=关闭蓝牙"},
            },
            "required": ["on"],
        },
        handler=None,  # type: ignore
    )

    # ===== 系统掌控：权限提升与深度集成 =====
    _register(
        "get_privilege_status",
        "查看当前对 Windows 的控制权限级别（标准用户/管理员/SYSTEM）。"
        "需要做系统级操作但不确定权限时先查。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "elevate_self",
        "以管理员身份重启自身（弹 UAC）。提权后整个助手获得管理员权限，"
        "之后所有操作（注册表 HKLM、系统服务、改 DNS 等）都不再受限。"
        "会启动新实例，当前实例需关闭。仅当用户明确要求获取管理员权限/掌控系统时调用。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "run_shell_admin",
        "以管理员权限执行 PowerShell 命令并返回输出。非管理员时会自动弹 UAC 提权。"
        "用于单条需要管理员的系统操作，无需重启整个助手。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell 命令"},
            },
            "required": ["command"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "run_shell_system",
        "以 NT AUTHORITY\\SYSTEM（系统最高权限，比管理员更高）执行 PowerShell 命令。"
        "用于普通管理员都受限的操作（如改受保护注册表、操作系统核心组件）。需先有管理员权限。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell 命令"},
            },
            "required": ["command"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "install_autostart",
        "让助手彻底融入系统：注册开机自启计划任务，开机/登录即以最高权限运行（免每次 UAC）。"
        "scope=admin 登录时以管理员启动（推荐，有界面）；scope=system 开机以 SYSTEM 后台启动。"
        "安装前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["admin", "system"],
                    "description": "权限范围，默认 admin",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "uninstall_autostart",
        "取消开机自启计划任务",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "autostart_status",
        "查询语音助手开机自启计划任务是否已注册及当前配置（只读）。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "install_system_service",
        "把助手注册为真正的 Windows 服务（LocalSystem 账户、开机自启、SYSTEM 权限常驻）。"
        "安装前必须先口头确认。GUI 助手建议改用 install_autostart(scope=system)。",
        {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "服务名，默认 KekesiService"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "uninstall_system_service",
        "卸载之前注册的 Windows 服务",
        {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "服务名，默认 KekesiService"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "take_ownership",
        "夺取文件或目录的所有权并授予管理员完全控制，用于操作受 TrustedInstaller/系统保护的资源。"
        "危险操作，执行前必须先口头确认。需管理员/SYSTEM 权限。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件或目录路径"},
                "recurse": {"type": "boolean", "description": "是否递归子项，默认 true"},
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "grant_full_control",
        "对已有所有权的路径授予指定账户完全控制（不夺取所有权）。"
        "与 take_ownership 不同：仅授权，适合已拥有文件时单独加权限。"
        "危险操作，执行前必须先口头确认。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件或目录路径"},
                "account": {
                    "type": "string",
                    "description": "账户名，默认 Administrators",
                },
                "recurse": {"type": "boolean", "description": "是否递归子项，默认 true"},
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )

    # —— 系统自修复 ——
    _register(
        "check_system_health",
        "全面检查系统健康：关键服务、磁盘空间、内存、系统文件完整性、事件日志。"
        "可自动修复发现的问题。",
        {
            "type": "object",
            "properties": {
                "auto_fix": {
                    "type": "boolean",
                    "description": "是否自动修复发现的问题，默认 false",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "repair_system",
        "一键修复系统：运行健康检查并自动修复所有可修复的问题（服务重启、系统文件修复等）。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )

    # —— 内存与磁盘清理 ——
    _register(
        "clean_memory",
        "清理系统内存：释放各进程工作集内存，立即回收物理内存。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )
    _register(
        "clean_ram",
        "清理系统内存：释放各进程工作集内存，立即回收物理内存（不是清理对话记忆）。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )
    _register(
        "clean_disk",
        "清理磁盘空间：临时文件、Windows更新缓存、缩略图缓存、回收站、DNS缓存、"
        "npm/pip/uv缓存、AMD着色器缓存、崩溃转储等。可先 find_large_files 再清理。",
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要清理的项目（temp_files/windows_temp/windows_update/thumbnails/"
                        "recycle_bin/dns_cache/prefetch/error_reports/delivery_optimization/"
                        "old_logs/package_caches/amd_shader_cache/crash_dumps），留空则清理全部"
                    ),
                },
                "aggressive": {
                    "type": "boolean",
                    "description": "激进模式（清理更多缓存），默认 false",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "find_large_files",
        "扫描磁盘大文件（按体积排序），标注可清缓存/建议确认/勿动，便于后续 clean_disk 或定向删除。",
        {
            "type": "object",
            "properties": {
                "drive": {
                    "type": "string",
                    "description": "盘符，如 C、D，默认 C",
                },
                "min_mb": {
                    "type": "integer",
                    "description": "最小文件体积（MB），默认 100",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数，默认 40，最大 200",
                },
                "junk_only": {
                    "type": "boolean",
                    "description": "只看可清缓存和调试残留，默认 false",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_system_stats",
        "获取系统资源概况：CPU使用率、内存使用、磁盘空间、内存占用前5进程。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )
    _register(
        "kill_memory_hogs",
        "结束占用内存过高的非关键进程，释放内存。",
        {
            "type": "object",
            "properties": {
                "threshold_mb": {
                    "type": "integer",
                    "description": "内存占用阈值(MB)，超过此值的非关键进程将被结束，默认 500",
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "排除的进程名列表",
                },
            },
        },
        handler=None,  # type: ignore
    )

    # —— 鼠标控制 ——
    _register(
        "mouse_move",
        "移动鼠标到屏幕坐标 (x, y)。坐标原点为屏幕左上角，多显示器时可为负数。",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "目标 X 坐标（像素）"},
                "y": {"type": "integer", "description": "目标 Y 坐标（像素）"},
                "smooth": {"type": "boolean", "description": "是否平滑移动，默认 false"},
            },
            "required": ["x", "y"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "mouse_click",
        "在指定位置点击鼠标。不传坐标则在当前位置点击。"
        "virtual=true 时用虚拟鼠标（不挪物理光标）。",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X 坐标（不传则在当前位置点击）"},
                "y": {"type": "integer", "description": "Y 坐标"},
                "button": {"type": "string", "description": "left/right/middle，默认 left"},
                "double": {"type": "boolean", "description": "是否双击，默认 false"},
                "virtual": {
                    "type": "boolean",
                    "description": "虚拟点击（不移动物理光标），默认 false",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "mouse_drag",
        "从 (x1,y1) 拖拽到 (x2,y2)。",
        {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
                "duration": {"type": "number", "description": "拖拽时长（秒），默认 0.4"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "mouse_scroll",
        "滚动鼠标滚轮。正数向上，负数向下。virtual=true 时向指定坐标窗口投递滚轮消息，不移动物理光标。",
        {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "滚动格数，正向上负向下，默认 -3"},
                "x": {"type": "integer", "description": "在指定位置滚动（可选）"},
                "y": {"type": "integer"},
                "virtual": {
                    "type": "boolean",
                    "description": "虚拟滚动（不动物理光标），默认 false",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_cursor_pos",
        "获取当前鼠标位置坐标。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )

    # —— UIA 自动化 ——
    _register(
        "uia_find_element",
        "按名称/自动化ID/控件类型查找 UIA 元素，返回坐标和属性。比 OCR 精确。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "元素名称（支持子串匹配）"},
                "automation_id": {"type": "string", "description": "自动化 ID（精确匹配）"},
                "control_type": {"type": "string", "description": "控件类型：Button/Edit/List 等"},
                "class_name": {"type": "string"},
                "app": {"type": "string", "description": "限定根窗口标题关键词"},
                "scope": {"type": "string", "description": "self/children/descendants，默认 descendants"},
                "limit": {"type": "integer", "description": "最多返回数，默认 20"},
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "uia_click_element",
        "查找并点击 UIA 控件。优先用 InvokePattern（程序化激活），失败回退坐标点击。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "控件名称"},
                "automation_id": {"type": "string"},
                "control_type": {"type": "string"},
                "class_name": {"type": "string"},
                "app": {"type": "string", "description": "限定窗口标题"},
                "button": {"type": "string", "description": "left/right/middle"},
                "double": {"type": "boolean"},
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "uia_get_text",
        "获取 UIA 控件的文本内容。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "automation_id": {"type": "string"},
                "control_type": {"type": "string"},
                "app": {"type": "string"},
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "uia_list_controls",
        "列出窗口下的控件树。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "窗口标题关键词（不传则列桌面顶层）"},
                "scope": {"type": "string", "description": "children/descendants，默认 children"},
                "control_type": {"type": "string", "description": "只列指定类型"},
                "limit": {"type": "integer", "description": "默认 50"},
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "uia_set_value",
        "设置编辑框的值（ValuePattern）。",
        {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "要设置的值"},
                "name": {"type": "string"},
                "automation_id": {"type": "string"},
                "control_type": {"type": "string", "description": "默认 edit"},
                "app": {"type": "string"},
            },
            "required": ["value"],
        },
        handler=None,  # type: ignore
    )

    # —— OCR ——
    _register(
        "ocr_screen",
        "对屏幕（或指定区域）做 OCR 文字识别，返回识别到的文字及坐标。用户需要提取屏幕上的具体文字内容时使用。",
        {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y, width, height] 区域，不传则整屏",
                },
                "language": {"type": "string", "description": "OCR 语言，默认 zh-Hans-CN"},
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "ocr_find_text",
        "在屏幕上查找指定文字，返回中心坐标。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要查找的文字"},
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "限定搜索区域 [x,y,w,h]",
                },
                "language": {"type": "string"},
                "partial": {"type": "boolean", "description": "是否部分匹配，默认 true"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_ocr_languages",
        "列出系统可用的 OCR 语言。",
        {"type": "object", "properties": {}},
        handler=None,  # type: ignore
    )

    # —— 视觉点击（UIA + OCR 融合） ——
    _register(
        "click_text",
        "在屏幕上找到指定文字并点击。策略：UIA 精确查找 → OCR 兜底 → 点击。"
        "适用于「点登录按钮」「点搜索结果中的完美世界」等场景。"
        "优先推荐 gui_action（带 UI 缓存 L0）。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要点击的文字"},
                "app": {"type": "string", "description": "限定应用窗口标题，提高精度"},
                "button": {"type": "string", "description": "left/right/middle"},
                "double": {"type": "boolean"},
                "partial": {"type": "boolean", "description": "部分匹配，默认 true"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "gui_action",
        "桌面 GUI 统一点击/悬停入口（推荐）。分层：L0 UI缓存 → L1 UIA → L2 OCR。"
        "用户说「点登录」「点确定」「点击某某按钮」时优先用本工具。"
        "成功后自动缓存，下次同类操作更快。不含全屋/IoT。",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要点击的文字或控件名，如「登录」「确定」",
                },
                "action": {
                    "type": "string",
                    "enum": ["click", "double_click", "hover"],
                    "description": "动作，默认 click",
                },
                "app": {
                    "type": "string",
                    "description": "限定窗口标题关键词，可选",
                },
                "button": {
                    "type": "string",
                    "description": "left/right/middle，默认 left",
                },
                "double": {
                    "type": "boolean",
                    "description": "双击（与 action=double_click 等效），默认 false",
                },
                "partial": {
                    "type": "boolean",
                    "description": "部分匹配，默认 true",
                },
            },
            "required": ["target"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "list_workflows",
        "列出已学习/已保存的桌面工作流（多步操作一键复用）",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "run_workflow",
        "按名称执行已学习的工作流。用户说「按上次流程」「跑那个自动化」时使用。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工作流名称或标题关键词"},
            },
            "required": ["name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "save_workflow",
        "把多步工具调用保存为命名工作流，供以后 run_workflow 复用",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工作流短名"},
                "title": {"type": "string", "description": "可读标题"},
                "trigger_hint": {"type": "string", "description": "触发说法提示"},
                "steps": {
                    "type": "array",
                    "description": "步骤列表，每项含 func 与 args",
                    "items": {"type": "object"},
                },
            },
            "required": ["name", "steps"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "run_sequence",
        "当场执行临时多步工具序列（不必先保存工作流）。"
        "适合「先点A再输入B再回车」这类已知步骤清单；每项含 func 与 args。"
        "持久复用请改用 save_workflow + run_workflow。",
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "步骤列表，如 [{\"func\":\"gui_action\",\"args\":{\"target\":\"确定\"}}]",
                    "items": {"type": "object"},
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "某步失败是否中断，默认 true",
                },
            },
            "required": ["steps"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "check_occlusion",
        "检测屏幕坐标或窗口是否被其他窗口遮挡。"
        "点击前可先调用；提供 (x,y) 检点，或 title/hwnd 检整窗。",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "屏幕 X（与 y 一起用）"},
                "y": {"type": "integer", "description": "屏幕 Y"},
                "title": {"type": "string", "description": "期望窗口标题关键词"},
                "hwnd": {"type": "integer", "description": "期望窗口句柄"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "virtual_click",
        "虚拟鼠标点击：向目标窗投递消息，不移动物理光标。"
        "用户正在用鼠标、或不想光标乱跳时优先本工具；失败可再 mouse_click/click_at。",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "description": "left/right/middle"},
                "double": {"type": "boolean"},
            },
            "required": ["x", "y"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "virtual_scroll",
        "虚拟滚轮：在指定屏幕坐标投递滚轮消息，不移动物理光标。",
        {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "滚动量，正数向上、负数向下，默认 -3",
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "scan_desktop",
        "一体桌面快照：虚拟屏幕、任务栏、各窗口可见度%、可选控件摘要、桌面空白区。"
        "需要了解当前桌面全貌时优先用本工具（比多次 list/uia 省步骤）。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多窗口数，默认 20"},
                "include_controls": {
                    "type": "boolean",
                    "description": "是否附带可见窗控件摘要，默认 true",
                },
                "include_regions": {
                    "type": "boolean",
                    "description": "是否检测桌面露出空白区，默认 true",
                },
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "screenshot_window",
        "用 PrintWindow 截取指定窗口（被其他窗遮挡时仍可抓到内容）。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题关键词"},
                "hwnd": {"type": "integer"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "ocr_window",
        "对指定窗口 PrintWindow 后 OCR；默认暗色主题增强，适合深色 UI。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "hwnd": {"type": "integer"},
                "language": {"type": "string"},
                "dark_enhance": {"type": "boolean", "description": "暗色增强，默认 true"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wait_until_found",
        "等待屏幕上出现指定文字或控件（UIA→OCR 轮询）。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "control_type": {"type": "string"},
                "app": {"type": "string"},
                "timeout": {"type": "number", "description": "秒，默认 15"},
                "partial": {"type": "boolean"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wait_until_gone",
        "等待指定文字从屏幕消失。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "app": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wait_for_stable_screen",
        "等待画面稳定（连续若干秒无变化），适合动画/加载结束后再操作。",
        {
            "type": "object",
            "properties": {
                "timeout": {"type": "number", "description": "最长等待秒，默认 10"},
                "stable_for": {"type": "number", "description": "需稳定秒数，默认 0.8"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "wait_for_file",
        "等待文件出现且大小稳定（如下载完成）。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "timeout": {"type": "number"},
                "stable_for": {"type": "number"},
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "register_image",
        "注册图像模板别名，供 find_image/click_image 用名称引用。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string", "description": "模板图片路径"},
            },
            "required": ["name", "path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "find_image",
        "在屏幕上按模板图查找位置（无文字的图标按钮用）。可装 opencv 加速。",
        {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "模板路径或 register_image 别名",
                },
                "confidence": {"type": "number", "description": "默认 0.8"},
            },
            "required": ["image"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "click_image",
        "找到模板图并点击（默认虚拟鼠标）。",
        {
            "type": "object",
            "properties": {
                "image": {"type": "string"},
                "confidence": {"type": "number"},
                "button": {"type": "string"},
                "double": {"type": "boolean"},
                "virtual": {"type": "boolean"},
            },
            "required": ["image"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "click_and_type",
        "原子动作：先点击再输入。给 (x,y) 或 target 文字/控件名。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的内容"},
                "target": {"type": "string", "description": "要点的控件/文字"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "app": {"type": "string"},
                "clear_first": {"type": "boolean", "description": "输入前 Ctrl+A 删除"},
                "virtual": {"type": "boolean"},
            },
            "required": [],
        },
        handler=None,  # type: ignore
    )
    _register(
        "focus_and_hotkey",
        "原子动作：先聚焦（点一下）再发快捷键，适合网页/iframe。",
        {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "SendKeys，如 ^a、^v、{ENTER}",
                },
                "target": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "app": {"type": "string"},
            },
            "required": ["keys"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "fill_form",
        "批量填表：fields 为 [{target,value}] 或 [{x,y,value}]，可选 submit 按钮名。",
        {
            "type": "object",
            "properties": {
                "fields": {"type": "array", "items": {"type": "object"}},
                "app": {"type": "string"},
                "submit": {"type": "string", "description": "填完后要点的提交按钮"},
            },
            "required": ["fields"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "click_menu_item",
        "按菜单路径逐级点击，如「文件>打开」或「编辑>首选项」。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "用 > 分隔的菜单路径"},
                "app": {"type": "string"},
                "separator": {"type": "string", "description": "默认 >"},
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "find_text_location",
        "查找屏幕上文字的位置，不点击。返回坐标和使用的策略（uia/ocr）。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "app": {"type": "string"},
                "partial": {"type": "boolean"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "read_screen_text",
        "读取屏幕（或指定窗口）的全部文字。优先 UIA 收集文本控件，失败用 OCR。",
        {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "限定窗口标题"},
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x,y,w,h] 区域",
                },
            },
        },
        handler=None,  # type: ignore
    )
    _register(
        "click_at",
        "在指定坐标点击（视觉模型给出坐标时的执行入口）。"
        "virtual=true 时不移动物理光标。",
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string"},
                "double": {"type": "boolean"},
                "virtual": {"type": "boolean", "description": "虚拟点击，默认 false"},
            },
            "required": ["x", "y"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "hover_text",
        "将鼠标悬停到屏幕上的指定文字（不点击）。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "app": {"type": "string"},
                "partial": {"type": "boolean"},
            },
            "required": ["text"],
        },
        handler=None,  # type: ignore
    )


    # ============================================================
    # 个性设定工具
    # ============================================================
    _register(
        "set_persona",
        "设置助手个性风格（幽默/严肃/俏皮/中性），影响回复语气和表情",
        {
            "type": "object",
            "properties": {
                "persona": {
                    "type": "string",
                    "enum": ["humorous", "serious", "playful", "neutral"],
                    "description": "个性风格：humorous(幽默)、serious(严肃)、playful(俏皮)、neutral(中性)",
                },
            },
            "required": ["persona"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "get_persona",
        "获取当前助手个性风格设置",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "list_personas",
        "列出所有可用的个性风格选项及其描述",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )

    # ============================================================
    # 代码自修复工具（kekesi 自身代码 BUG 修复）
    # ============================================================
    _register(
        "list_kekesi_files",
        "列出 kekesi 自身所有 .py 文件清单（含行数、可写/只读标记）。"
        "用于代码自修复时定位目标文件。仅限 kekesi/ 目录。",
        {"type": "object", "properties": {}, "required": []},
        handler=None,  # type: ignore
    )
    _register(
        "read_code_file",
        "读取 kekesi 自身某个 .py 文件的内容（带行号）。"
        "用于代码自修复时查看工具实现、定位 BUG。"
        "仅限 kekesi/ 目录下的 .py 文件，路径越界会拒绝。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对路径，如 tools/winops.py 或 memory/learning.py；也可简写 winops.py 自动找 tools/ 下",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始），默认 1",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号，默认 0 表示读到末尾",
                },
            },
            "required": ["path"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "write_code_file",
        "修改 kekesi 自身某个 .py 文件的内容（自动备份 .bak + ast 语法校验）。"
        "用于代码自修复：定位到 BUG 后，给出完整新文件内容。"
        "安全约束：仅允许 tools/ 和 memory/ 子目录；禁止核心调度文件；语法错拒绝写入。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对路径，如 tools/winops.py",
                },
                "content": {
                    "type": "string",
                    "description": "完整的文件新内容（不是片段替换，是整文件覆盖）",
                },
                "create": {
                    "type": "boolean",
                    "description": "是否允许新建文件，默认 false（文件必须已存在）",
                },
            },
            "required": ["path", "content"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "reload_module",
        "重载已加载的 Python 模块，让 write_code_file 的修改立即生效。"
        "用于代码自修复：改完代码后调用此工具重载，再重试原失败工具。"
        "module_name 可简写（如 winops 会自动补全为 kekesi.tools.winops）。",
        {
            "type": "object",
            "properties": {
                "module_name": {
                    "type": "string",
                    "description": "模块名，完整如 kekesi.tools.winops 或简写 winops",
                },
            },
            "required": ["module_name"],
        },
        handler=None,  # type: ignore
    )
    _register(
        "find_tool_definition",
        "查找某个工具名在 registry.py 和 executor.py 中的定义/调度位置。"
        "用于代码自修复时快速定位工具实现位置。",
        {
            "type": "object",
            "properties": {
                "func_name": {
                    "type": "string",
                    "description": "工具名，如 get_current_time、open_app",
                },
            },
            "required": ["func_name"],
        },
        handler=None,  # type: ignore
    )

    # 应用检索关键词元数据（未知工具名自动忽略，防止表与注册漂移时报错）
    for _kw_name, _kw_text in _TOOL_KEYWORDS.items():
        _tool = _TOOLS.get(_kw_name)
        if _tool is not None:
            _tool["keywords"] = _kw_text


def get_tool_keywords(name: str) -> str:
    """工具检索关键词（空格分隔），供 BM25 语料使用；未配置返回空串。"""
    register_builtin_tools()
    tool = _TOOLS.get(name)
    if not tool:
        return ""
    return tool.get("keywords") or ""


def get_tool_names() -> list[str]:
    register_builtin_tools()
    return list(_TOOLS.keys())


def get_tool(name: str) -> dict[str, Any] | None:
    register_builtin_tools()
    return _TOOLS.get(name)


_CATEGORY_BY_PREFIX: dict[str, str] = {
    "get_current_time": "时间",
    "set_timer": "时间",
    "set_reminder": "时间",
    "get_weather": "天气",
    "open_app": "应用",
    "close_app": "应用",
    "focus_window": "应用",
    "minimize_window": "应用",
    "list_windows": "应用",
    "list_open_windows": "应用",
    "list_desktop_files": "文件",
    "list_desktop_icons": "输入",
    "show_desktop": "输入",
    "click_desktop_icon": "输入",
    "open_path": "文件",
    "list_directory": "文件",
    "search_files": "文件",
    "create_document": "文件",
    "copy_path": "文件",
    "move_path": "文件",
    "rename_path": "文件",
    "delete_file": "文件",
    "move_to_trash": "文件",
    "empty_recycle_bin": "文件",
    "read_code_file": "代码",
    "write_code_file": "代码",
    "reload_module": "代码",
    "find_tool_definition": "代码",
    "acquire_skill": "系统",
    "uninstall_skill": "系统",
    "system_power": "系统",
    "get_system_status": "系统",
    "list_monitors": "系统",
    "get_brightness": "系统",
    "set_brightness": "系统",
    "get_wallpaper": "系统",
    "set_wallpaper": "系统",
    "list_power_plans": "系统",
    "set_power_plan": "系统",
    "get_power_timeouts": "系统",
    "set_power_timeout": "系统",
    "check_updates": "系统",
    "install_updates": "系统",
    "get_update_history": "系统",
    "get_firewall_status": "系统",
    "list_firewall_rules": "系统",
    "block_app_network": "系统",
    "allow_app_network": "系统",
    "get_hardware_info": "系统",
    "get_temperatures": "系统",
    "set_temp_threshold": "系统",
    "get_temp_threshold": "系统",
    "setup_temp_sensors": "系统",
    "get_system_stats": "系统",
    "check_system_health": "系统",
    "repair_system": "系统",
    "clean_memory": "系统",
    "clean_ram": "系统",
    "clean_disk": "系统",
    "find_large_files": "系统",
    "kill_memory_hogs": "系统",
    "list_processes": "进程",
    "find_process": "进程",
    "kill_process": "进程",
    "run_shell": "进程",
    "get_network_info": "网络",
    "set_dns": "网络",
    "list_wifi_networks": "网络",
    "connect_wifi": "网络",
    "search_web": "网络",
    "web_search": "网络",
    "fetch_web_page": "网络",
    "self_study": "学习",
    "open_url": "网络",
    "play_music": "媒体",
    "qqmusic_login": "媒体",
    "qqmusic_status": "媒体",
    "control_music": "媒体",
    "play_video": "媒体",
    "video_login": "媒体",
    "video_status": "媒体",
    "close_video": "媒体",
    "set_volume": "媒体",
    "list_app_volumes": "媒体",
    "set_app_volume": "媒体",
    "mute_app": "媒体",
    "take_screenshot": "媒体",
    "mouse_move": "输入",
    "mouse_click": "输入",
    "mouse_drag": "输入",
    "mouse_scroll": "输入",
    "send_keys": "输入",
    "type_text": "输入",
    "click_at": "输入",
    "click_text": "输入",
    "gui_action": "输入",
    "hover_text": "输入",
    "read_screen": "屏幕",
    "recall_screenshot": "屏幕",
    "list_screenshots": "屏幕",
    "read_screen_text": "屏幕",
    "ocr_screen": "屏幕",
    "ocr_find_text": "屏幕",
    "find_text_location": "屏幕",
    "uia_find_element": "UIA",
    "uia_click_element": "UIA",
    "uia_get_text": "UIA",
    "uia_list_controls": "UIA",
    "uia_set_value": "UIA",
    "remember": "记忆",
    "remember_app": "记忆",
    "remember_fact": "记忆",
    "search_memory": "记忆",
    "upsert_core_memory": "记忆",
    "forget_core_memory": "记忆",
    "forget": "记忆",
    "cleanup_memory": "记忆",
    "list_workflows": "记忆",
    "run_workflow": "记忆",
    "save_workflow": "记忆",
    "run_sequence": "输入",
    "inspect_window_som": "屏幕",
    "execute_gui_macro": "输入",
    "check_occlusion": "屏幕",
    "virtual_click": "输入",
    "virtual_scroll": "输入",
    "scan_desktop": "屏幕",
    "screenshot_window": "屏幕",
    "ocr_window": "屏幕",
    "wait_until_found": "屏幕",
    "wait_until_gone": "屏幕",
    "wait_for_stable_screen": "屏幕",
    "wait_for_file": "文件",
    "register_image": "屏幕",
    "find_image": "屏幕",
    "click_image": "输入",
    "click_and_type": "输入",
    "focus_and_hotkey": "输入",
    "fill_form": "输入",
    "click_menu_item": "输入",
    "add_watch_rule": "监控",
    "list_watch_rules": "监控",
    "delete_watch_rule": "监控",
    "wechat_send_message": "其他",
    "wechat_read_messages": "其他",
    "wechat_reply_current": "其他",
    "elevate_self": "权限",
    "take_ownership": "权限",
    "grant_full_control": "权限",
    "get_privilege_status": "权限",
    "list_services": "服务",
    "control_service": "服务",
    "list_scheduled_tasks": "服务",
    "run_scheduled_task": "服务",
    "install_system_service": "服务",
    "uninstall_system_service": "服务",
    "install_autostart": "服务",
    "uninstall_autostart": "服务",
    "autostart_status": "服务",
    "read_registry": "注册表",
    "write_registry": "注册表",
    "read_env_var": "环境",
    "set_env_var": "环境",
    "list_env_vars": "环境",
    "list_installed_software": "软件",
    "install_software": "软件",
    "uninstall_software": "软件",
    "read_clipboard": "剪贴板",
    "write_clipboard": "剪贴板",
}


def get_tool_category(name: str) -> str:
    """根据工具名获取类别（自动推断）"""
    return _CATEGORY_BY_PREFIX.get(name, "其他")


def get_categories() -> list[str]:
    """获取所有类别列表"""
    cats = sorted(set(_CATEGORY_BY_PREFIX.values()))
    return cats


def get_tools_by_category(category: str) -> list[str]:
    """获取指定类别的所有工具名"""
    return [name for name, cat in _CATEGORY_BY_PREFIX.items() if cat == category]


def get_openai_tools(names: list[str] | None = None) -> list[dict]:
    """OpenAI / Ollama 兼容 tools  schema；names 为空则返回全部内置工具。"""
    register_builtin_tools()
    result = []
    keys = names if names else list(_TOOLS.keys())
    for name in keys:
        tool = _TOOLS.get(name)
        if not tool:
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
        )
    return result


def to_ollama_tools() -> list[dict]:
    """转换为 Ollama /api/chat 的 tools 格式"""
    return get_openai_tools(None)
