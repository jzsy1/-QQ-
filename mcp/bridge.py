"""MCP 桥接 — 把本地 MCP 服务器工具接入科科斯 Agent。

读取 kekesi/mcp.json，启动 stdio MCP 服务器，工具名前缀 mcp__<服务器>__<工具>。
MCP SDK 跑在独立 asyncio 线程，对外暴露同步接口；启动失败不影响主程序。

迁移自 kekesi/mcp_bridge.py（v2.0 目录结构调整）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract import DiscoveryLimits, NameRegistry, Timeouts, mcp_tool_name
from ..mcp_errors import McpErrorCode, format_mcp_error

logger = logging.getLogger(__name__)

_LIMITS = DiscoveryLimits()
_TIMEOUTS = Timeouts()


@dataclass
class McpCallResult:
    """MCP 工具调用结果 — 保留服务端 isError 与 structuredContent。"""

    ok: bool
    text: str
    is_error: bool = False
    structured_content: Any = None
    error_code: str = ""

_IS_WINDOWS = sys.platform.startswith("win")

# 包根目录（kekesi/），用于定位 mcp.json 等配置文件
_PKG_ROOT = Path(__file__).resolve().parent.parent

_FILESYSTEM_PKG = "@modelcontextprotocol/server-filesystem"
_DEFAULT_INIT_TIMEOUT = _TIMEOUTS.mcp_init_sec
_NPX_INIT_TIMEOUT = _TIMEOUTS.mcp_npx_init_sec

# 服务器别名 → 意图；意图 → 触发正则。两处映射只在这里维护。
_SERVER_INTENTS: dict[str, str] = {
    "filesystem": "fs",
    "fs": "fs",
    "files": "fs",
    "playwright": "browser",
    "puppeteer": "browser",
    "browser": "browser",
    "chrome": "browser",
    "fetch": "web",
    "http": "web",
    "github": "git",
    "git": "git",
    "gitlab": "git",
    "brave": "search",
    "search": "search",
    "ddg": "search",
    "duckduckgo": "search",
    "tavily": "search",
    "exa": "search",
    "context7": "docs",
    "docs": "docs",
    "memory": "memory",
}

_INTENT_PATTERNS: dict[str, re.Pattern] = {
    "fs": re.compile(
        r"文件|文件夹|目录|路径|桌面|下载|文档|读写|列表.*文件|Documents|Downloads|Desktop",
        re.I,
    ),
    "browser": re.compile(
        r"浏览器|网页|网站|网址|抓取|自动化|playwright|打开.*http|访问.*http|"
        r"截图.*页|填表|点击.*按钮",
        re.I,
    ),
    "web": re.compile(r"抓取|fetch|网页内容|读一下.*(?:网|页|站)|http[s]?://", re.I),
    "git": re.compile(r"github|gitlab|仓库|pull\s*request|\bpr\b|issue", re.I),
    "search": re.compile(r"搜索|查一下|联网|brave|duckduckgo|\bddg\b|搜一下", re.I),
    "docs": re.compile(r"context7|库文档|api文档|框架文档|怎么用", re.I),
    "memory": re.compile(r"知识图谱|长期记忆|记住实体", re.I),
}

# MCP 工具中文关键词元数据（对齐 registry._TOOL_KEYWORDS 先例）：
# 工具描述是英文，中文话术对不上词面；服务器命中后工具怎么排（谁该进
# per_server 限额）靠这张表打分。漏检 → 补这里，禁止在调用方加意图正则。
_MCP_TOOL_KEYWORDS: dict[str, str] = {
    # playwright
    "browser_navigate": "打开 网址 网页 访问 跳转 goto",
    "browser_take_screenshot": "截图 截屏",
    "browser_click": "点击 按钮 链接",
    "browser_type": "输入 打字 填写",
    "browser_fill_form": "填表 表单",
    "browser_press_key": "按键 回车",
    "browser_snapshot": "页面元素 读页面 内容",
    "browser_select_option": "下拉 选择",
    "browser_hover": "悬停",
    "browser_tabs": "标签页 切换窗口",
    "browser_wait_for": "等待 加载",
    "browser_evaluate": "脚本 js",
    "browser_console_messages": "控制台 报错",
    "browser_network_requests": "网络请求 接口",
    # filesystem
    "read_file": "读文件 查看文件",
    "read_text_file": "读文本",
    "read_media_file": "看图片 音频",
    "read_multiple_files": "多文件",
    "write_file": "写文件 新建 保存",
    "edit_file": "编辑文件 修改文件",
    "create_directory": "建目录 新建文件夹",
    "list_directory": "列目录 文件列表 文件夹里",
    "list_directory_with_sizes": "文件大小 列目录",
    "directory_tree": "目录树",
    "move_file": "移动文件 重命名",
    "search_files": "搜文件 找文件 搜索文件",
    "get_file_info": "文件信息 大小",
    "list_allowed_directories": "允许目录",
    # fetch
    "fetch": "抓取 网页内容 读网页 正文 链接",
}


def _tool_keyword_score(
    raw_name: str, text_lower: str, extra: dict[str, str] | None = None
) -> int:
    """工具中文关键词与话术的命中数（无表/无命中 = 0，保持 MCP 原序兜底）。"""
    kws = (extra or {}).get(raw_name) or _MCP_TOOL_KEYWORDS.get(raw_name)
    if not kws:
        return 0
    return sum(1 for kw in kws.split() if kw and kw in text_lower)


_TRIGGER_CACHE: dict[str, re.Pattern] = {}


def _compiled_trigger(intent: str, source: str) -> re.Pattern | None:
    """编译 Bridge 声明的意图触发正则；无效正则忽略并告警，不影响内置意图。"""
    if not source:
        return None
    cached = _TRIGGER_CACHE.get(source)
    if cached is not None:
        return cached
    try:
        compiled = re.compile(source, re.I)
    except re.error as exc:
        logger.warning("Bridge 意图 %s 触发正则无效，已忽略: %s", intent, exc)
        return None
    _TRIGGER_CACHE[source] = compiled
    return compiled


def _resolve_command(cmd: str) -> str:
    if not _IS_WINDOWS or os.path.splitext(cmd)[1]:
        return cmd
    for cand in (cmd, cmd + ".cmd", cmd + ".exe", cmd + ".bat"):
        found = shutil.which(cand)
        if found:
            return found
    return cmd


def _npm_cache_dir(config_root: Path) -> Path:
    cache = config_root / ".npm-cache"
    cache.mkdir(exist_ok=True)
    return cache


def _clean_npm_env(env: dict) -> dict:
    """去掉无效 npm 配置，避免 npx 警告拖慢启动。"""
    out = dict(env)
    for key in list(out.keys()):
        low = key.lower()
        if low in ("npm_config_devdir", "npm_config_dev_dir"):
            out.pop(key, None)
    return out


def _normalize_path_arg(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw or raw.startswith("-") or raw.startswith("@"):
        return None
    try:
        path = Path(os.path.expanduser(raw)).resolve()
    except OSError:
        return None
    if path.is_dir():
        return str(path)
    logger.warning("MCP 目录不存在，已跳过: %s", raw)
    return None


def _default_user_dirs() -> list[str]:
    home = Path.home()
    seen: set[str] = set()
    out: list[str] = []
    for name in ("Desktop", "Downloads", "Documents", "桌面", "下载", "文档"):
        path = home / name
        if path.is_dir():
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
    if not out:
        out.append(str(home.resolve()))
    return out


def _split_launcher_and_dirs(raw_args: list) -> tuple[list[str], list[str]]:
    launcher: list[str] = []
    dirs: list[str] = []
    for item in raw_args:
        text = str(item).strip()
        if text.startswith("-") or text.startswith("@"):
            launcher.append(text)
            continue
        norm = _normalize_path_arg(text)
        if norm:
            dirs.append(norm)
    return launcher, dirs


def _find_cached_script(config_root: Path, rel: str) -> Path | None:
    cache = _npm_cache_dir(config_root)
    pattern = f"_npx/*/node_modules/{rel}"
    hits = sorted(
        cache.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in hits:
        if path.is_file():
            return path.resolve()
    return None


def _resolve_node() -> str:
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    return "node"


def _exception_summary(exc: BaseException, depth: int = 4) -> str:
    """展开 ExceptionGroup，取最内层原因，避免只报 “unhandled errors in a TaskGroup”。"""
    while depth and isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
        depth -= 1
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _normalize_mcp_config(config: dict) -> dict:
    """mcpServers 包裹解包：顶层 mcpServers 是字典时取内层，否则原样返回。"""
    if isinstance(config, dict) and isinstance(config.get("mcpServers"), dict):
        return config["mcpServers"]
    return config


def _env_names(value) -> list[str]:
    """列出值里引用的 $ENV:NAME / ${ENV:NAME} 变量名。"""
    text = str(value or "")
    names: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("${ENV:", i):
            end = text.find("}", i + 6)
            if end == -1:
                break
            names.append(text[i + 6:end])
            i = end + 1
            continue
        if text.startswith("$ENV:", i):
            j = i + 5
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            if j > i + 5:
                names.append(text[i + 5:j])
            i = j
            continue
        i += 1
    return names


def _expand_env_value(value) -> str:
    """展开 $ENV:NAME / ${ENV:NAME}；缺失变量展开为空串，由调用方丢弃空 header。"""
    text = str(value or "")
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("${ENV:", i):
            end = text.find("}", i + 6)
            if end == -1:
                out.append(text[i:])
                break
            out.append(os.environ.get(text[i + 6:end], ""))
            i = end + 1
            continue
        if text.startswith("$ENV:", i):
            j = i + 5
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            name = text[i + 5:j]
            if not name:
                out.append(text[i:j])
                i = j
                continue
            out.append(os.environ.get(name, ""))
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _mcp_headers(conf: dict) -> dict[str, str]:
    """HTTP headers：展开环境变量，空值整条丢弃；token_env 仅在无 Authorization 时补 Bearer。"""
    raw = (conf or {}).get("headers") or {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            if any(not os.environ.get(name) for name in _env_names(val)):
                continue
            expanded = _expand_env_value(val)
            if expanded.strip():
                out[key] = expanded
    token_env = (conf or {}).get("token_env") or (conf or {}).get("auth_token_env")
    if token_env and not any(str(k).lower() == "authorization" for k in out):
        token = os.environ.get(str(token_env), "")
        if token:
            out["Authorization"] = "Bearer " + token
    return out


def _http_transport_kind(conf: dict) -> str | None:
    """返回 streamable / sse；None 表示走 stdio（transport 明确写 stdio 时优先 stdio）。"""
    conf = conf or {}
    transport = str(conf.get("transport") or "").strip().lower()
    if transport == "stdio":
        return None
    if transport == "sse":
        return "sse"
    if transport in ("streamable", "streamable-http", "streamable_http", "http"):
        return "streamable"
    if conf.get("url"):
        return "streamable"
    return None


def _prepare_launch(
    name: str,
    conf: dict,
    config_root: Path,
) -> tuple[str, list[str], dict, float] | None:
    command = (conf.get("command") or "").strip()
    if not command:
        return None

    raw_args = list(conf.get("args") or [])
    env = _clean_npm_env({**os.environ, **conf.get("env", {})})
    env.setdefault("npm_config_cache", str(_npm_cache_dir(config_root)))

    init_timeout = float(conf.get("init_timeout_sec") or 0)
    cmd_lower = command.lower()

    if cmd_lower.endswith("python.exe") or cmd_lower.endswith("python"):
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        timeout = init_timeout or _DEFAULT_INIT_TIMEOUT.get(name, 45)
        return _resolve_command(command), [str(a) for a in raw_args], env, timeout

    if name == "filesystem" or _FILESYSTEM_PKG in " ".join(map(str, raw_args)):
        launcher, dirs = _split_launcher_and_dirs(raw_args)
        if not dirs:
            dirs = _default_user_dirs()
        script = _find_cached_script(
            config_root,
            "@modelcontextprotocol/server-filesystem/dist/index.js",
        )
        if script:
            timeout = init_timeout or _DEFAULT_INIT_TIMEOUT.get(name, 60)
            logger.info("MCP filesystem 使用本地缓存: %s", script)
            return _resolve_command(_resolve_node()), [str(script), *dirs], env, timeout
        npx_args = launcher or ["-y", _FILESYSTEM_PKG]
        timeout = init_timeout or _NPX_INIT_TIMEOUT
        logger.info("MCP filesystem 使用 npx（超时 %ss）", int(timeout))
        return _resolve_command("npx"), [*npx_args, *dirs], env, timeout

    if "npx" in cmd_lower:
        # 仅 filesystem 需要目录展开（上方专属分支已处理）；其余 npx 服务器
        # 原样传参，避免 flag 值对（如 playwright 的 --browser chromium）被
        # 当作目录路径校验丢弃
        args = [os.path.expanduser(str(a)) for a in raw_args]
        timeout = init_timeout or _DEFAULT_INIT_TIMEOUT.get(name, _NPX_INIT_TIMEOUT)
        return _resolve_command(command), args, env, timeout

    timeout = init_timeout or _DEFAULT_INIT_TIMEOUT.get(name, 30)
    args = [os.path.expanduser(str(a)) for a in raw_args]
    return _resolve_command(command), args, env, timeout


def default_config_path(base_dir: Path | None = None) -> Path:
    base = base_dir or _PKG_ROOT
    return base / "mcp.json"


def load_config(base_dir: Path | None = None) -> dict:
    path = default_config_path(base_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 MCP 配置失败 %s: %s", path, e)
        return {}


class McpBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stack: AsyncExitStack | None = None
        self._schemas: list[dict] = []
        self._dispatch: dict[str, tuple] = {}
        self._ready = threading.Event()
        self._call_timeout = 90.0
        self._shutdown = threading.Event()
        self.names: list[str] = []
        self._last_error: str = ""
        self._errors: list[str] = []
        # 由 integrations.ToolGateway 从 Bridge Manifest 注入；空 = 沿用内置硬编码表
        self.intent_overrides: dict[str, str] = {}
        self.trigger_overrides: dict[str, str] = {}
        self.keyword_overrides: dict[str, str] = {}

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(
        self,
        config: dict,
        log=None,
        timeout: float = 60,
        config_dir: Path | None = None,
        *,
        call_timeout: float | None = None,
    ) -> None:
        if not config:
            return
        if call_timeout is not None:
            self._call_timeout = max(5.0, float(call_timeout))
        if log is None:
            log = logger.info
        try:
            import mcp  # noqa: F401
        except ImportError:
            log("未安装 mcp 库，跳过 MCP（pip install mcp 可启用）")
            return
        self._config_dir = config_dir or _PKG_ROOT
        threading.Thread(
            target=self._run,
            args=(config, log),
            daemon=True,
        ).start()
        self._ready.wait(timeout=timeout)

    def _run(self, config: dict, log) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._setup(config, log))
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _setup(self, config: dict, log) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        config_root = getattr(self, "_config_dir", _PKG_ROOT)
        self._errors = []
        name_registry = NameRegistry()

        new_schemas: list[dict] = []
        new_dispatch: dict[str, tuple] = {}
        new_names: list[str] = []

        async def _close_quietly(stack: AsyncExitStack) -> None:
            try:
                await stack.aclose()
            except BaseException:
                pass

        async def _close_server_stack(
            stack: AsyncExitStack, err: BaseException
        ) -> BaseException:
            try:
                await stack.aclose()
            except BaseException as close_exc:
                return close_exc
            return err

        for name, conf in _normalize_mcp_config(config).items():
            if name.startswith("_"):
                continue
            if not isinstance(conf, dict):
                continue
            if not conf.get("enabled", True):
                continue
            server_schemas: list[dict] = []
            server_dispatch: dict[str, tuple] = {}
            # 每服务器独立资源栈：单个服务器失败时立刻回收，
            # 不污染后续服务器（HTTP 鉴权失败会以 CancelledError 形式冒出）
            server_stack = AsyncExitStack()
            try:
                http_kind = _http_transport_kind(conf)
                if http_kind is not None:
                    init_timeout = float(
                        conf.get("init_timeout_sec") or _TIMEOUTS.mcp_startup_sec
                    )
                    command, args, env = "", [], {}
                else:
                    prepared = _prepare_launch(name, conf, config_root)
                    if not prepared:
                        continue
                    command, args, env, init_timeout = prepared
                if http_kind is not None:
                    http_url = (conf.get("url") or "").strip()
                    if not http_url:
                        msg = f"MCP 服务器 {name} 缺少 url，已跳过"
                        self._errors.append(msg)
                        logger.warning(msg)
                        continue
                    http_headers = _mcp_headers(conf)
                    if http_kind == "sse":
                        from mcp.client.sse import sse_client

                        read, write = await server_stack.enter_async_context(
                            sse_client(http_url, headers=http_headers)
                        )
                    else:
                        from mcp.client.streamable_http import (
                            streamablehttp_client,
                        )

                        http_result = await server_stack.enter_async_context(
                            streamablehttp_client(http_url, headers=http_headers)
                        )
                        read, write = http_result[0], http_result[1]
                else:
                    params = StdioServerParameters(
                        command=command,
                        args=args,
                        env=env,
                    )
                    read, write = await server_stack.enter_async_context(
                        stdio_client(params)
                    )
                session = await server_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await asyncio.wait_for(session.initialize(), timeout=init_timeout)
                resp = await asyncio.wait_for(
                    session.list_tools(), timeout=init_timeout
                )
                for t in resp.tools:
                    full = mcp_tool_name(name, t.name)
                    if not name_registry.claim(full, (name, t.name)):
                        msg = (
                            f"MCP 工具名冲突：{name}/{t.name} -> {full}"
                            f"（已有 {name_registry.owner_of(full)}）"
                        )
                        logger.warning(msg)
                        self._errors.append(msg)
                        continue
                    # mcp SDK 2.x 用 snake_case（input_schema），1.x 是 camelCase（inputSchema）
                    schema = getattr(t, "input_schema", None) or getattr(
                        t, "inputSchema", None
                    )
                    entry = {
                        "name": full,
                        "description": (t.description or t.name)[:1000],
                        "input_schema": schema
                        or {"type": "object", "properties": {}},
                    }
                    server_schemas.append(entry)
                    server_dispatch[full] = (session, t.name)
                new_schemas.extend(server_schemas)
                new_dispatch.update(server_dispatch)
                new_names.append(name)
                self._stack.push_async_callback(server_stack.aclose)
                log(f"MCP「{name}」已接入（{len(resp.tools)} 个工具）")
            except asyncio.TimeoutError:
                await _close_quietly(server_stack)
                msg = f"MCP「{name}」启动超时（{init_timeout}s）"
                self._errors.append(msg)
                log(format_mcp_error(McpErrorCode.SERVER_START_FAILED, msg))
                logger.warning("MCP 服务器 %s 初始化超时", name)
            except BaseException as e:
                shutdown = self._shutdown.is_set()
                is_control = isinstance(e, (KeyboardInterrupt, SystemExit))
                e = await _close_server_stack(server_stack, e)
                if shutdown or is_control:
                    raise
                detail = _exception_summary(e)
                msg = f"MCP「{name}」启动失败：{detail}"
                self._errors.append(msg)
                log(format_mcp_error(McpErrorCode.SERVER_START_FAILED, msg))
                logger.warning("MCP 服务器 %s 启动异常：%s", name, detail)

        # 原子替换 schema，避免半加载状态
        self._schemas = new_schemas
        self._dispatch = new_dispatch
        self.names = new_names

    def tool_schemas(self) -> list[dict]:
        return list(self._schemas)

    @property
    def tool_names(self) -> list[str]:
        """已接入的完整工具名（mcp__服务器__工具），供 Agent 候选池使用。"""
        return [t["name"] for t in self._schemas if t.get("name")]

    def tools_matching(self, user_text: str, *, max_n: int | None = None) -> list[str]:
        """按用户话术挑相关 MCP 工具，避免每轮塞满。"""
        if max_n is None:
            max_n = _LIMITS.mcp_max
        names = self.tool_names
        if not names:
            return []
        t = (user_text or "").strip()
        tl = t.lower()
        explicit = any(k in tl for k in ("mcp", "扩展", "插件", "mcp工具"))
        if explicit:
            return names[:max_n]

        patterns = dict(_INTENT_PATTERNS)
        for intent, source in (self.trigger_overrides or {}).items():
            compiled = _compiled_trigger(intent, source)
            if compiled is not None:
                patterns[intent] = compiled
        hits = {intent: bool(pat.search(t)) for intent, pat in patterns.items()}
        # fetch/http 服务器对“浏览器/网页”话术同样放行
        hits["web"] = hits.get("web", False) or hits.get("browser", False)

        def _server_of(full: str) -> str:
            parts = full.split("__")
            return parts[1] if len(parts) >= 3 else ""

        intents = self.intent_overrides or {}
        keywords = self.keyword_overrides or {}
        candidates: list[tuple[int, int, str]] = []
        for idx, full in enumerate(names):
            server = _server_of(full).lower()
            intent = intents.get(server) or _SERVER_INTENTS.get(server)
            if not intent or not hits.get(intent):
                continue
            raw = full.split("__", 2)[-1]
            # (-分数, 原序) 双键稳定排序：关键词命中的工具排前，同分保持 MCP 原序
            candidates.append((-_tool_keyword_score(raw, tl, keywords), idx, full))
        candidates.sort()

        picked: list[str] = []
        per_server: dict[str, int] = {}
        for _, _, full in candidates:
            server = _server_of(full).lower()
            if per_server.get(server, 0) >= _LIMITS.mcp_per_server_max:
                continue
            per_server[server] = per_server.get(server, 0) + 1
            picked.append(full)
            if len(picked) >= max_n:
                break
        return picked

    def to_ollama_tools(self) -> list[dict]:
        result = []
        for t in self._schemas:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return result

    def has(self, name: str) -> bool:
        return name in self._dispatch

    def call(self, full_name: str, args: dict, *, timeout: float | None = None) -> tuple[bool, str]:
        res = self.call_detail(full_name, args, timeout=timeout)
        return res.ok, res.text

    def call_detail(
        self, full_name: str, args: dict, *, timeout: float | None = None
    ) -> McpCallResult:
        """同 call()，但保留 isError / structuredContent / 错误码。"""
        if self._shutdown.is_set():
            return McpCallResult(
                ok=False,
                text=format_mcp_error(McpErrorCode.SHUTDOWN, "MCP 已关闭"),
                error_code=McpErrorCode.SHUTDOWN,
            )
        if self._loop is None or full_name not in self._dispatch:
            return McpCallResult(
                ok=False,
                text=format_mcp_error(
                    McpErrorCode.NOT_FOUND, f"未知 MCP 工具：{full_name}"
                ),
                error_code=McpErrorCode.NOT_FOUND,
            )
        session, tool_name = self._dispatch[full_name]
        deadline = timeout if timeout is not None else self._call_timeout
        fut = None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call(session, tool_name, args or {}),
                self._loop,
            )
            return fut.result(timeout=deadline)
        except TimeoutError:
            logger.warning("MCP 调用超时 %s (%.0fs)", full_name, deadline)
            if fut is not None:
                fut.cancel()
            return McpCallResult(
                ok=False,
                text=format_mcp_error(
                    McpErrorCode.CALL_TIMEOUT,
                    f"MCP 调用超时（{int(deadline)}秒）",
                ),
                error_code=McpErrorCode.CALL_TIMEOUT,
            )
        except Exception as e:
            logger.exception("MCP 调用失败 %s", full_name)
            code = McpErrorCode.SERVER_CRASHED if self._shutdown.is_set() else McpErrorCode.CALL_FAILED
            return McpCallResult(
                ok=False,
                text=format_mcp_error(code, f"调用 MCP 工具出错：{e}"),
                error_code=code,
            )

    def health(self) -> dict:
        """MCP 桥健康状态（供监控/自检）。"""
        return {
            "running": not self._shutdown.is_set() and self._loop is not None,
            "ready": self._ready.is_set(),
            "tool_count": len(self._dispatch),
            "schema_count": len(self._schemas),
            "servers": list(self.names),
            "call_timeout_sec": self._call_timeout,
            "errors": list(self._errors),
        }

    def shutdown(self) -> None:
        """关闭 MCP 会话并释放子进程。"""
        self._shutdown.set()
        loop = self._loop
        if loop is None:
            return
        if self._stack is not None:

            async def _close():
                await self._stack.aclose()

            try:
                fut = asyncio.run_coroutine_threadsafe(_close(), loop)
                fut.result(timeout=10)
            except Exception:
                logger.debug("MCP stack close failed", exc_info=True)
        self._schemas.clear()
        self._dispatch.clear()
        self.names.clear()
        self._stack = None
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except Exception:
            logger.debug("MCP loop stop failed", exc_info=True)
        self._loop = None

    async def _call(self, session, tool_name: str, args: dict) -> McpCallResult:
        res = await session.call_tool(tool_name, args)
        parts = []
        for c in getattr(res, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        out = "\n".join(parts) or "（已执行，无文本输出）"
        if len(out) > _LIMITS.mcp_output_max:
            out = out[:_LIMITS.mcp_output_max] + "\n…(输出已截断)"
        # MCP SDK 1.x 是 camelCase（isError/structuredContent），2.x 是 snake_case
        is_error = bool(
            getattr(res, "isError", None) or getattr(res, "is_error", False)
        )
        structured = getattr(res, "structuredContent", None)
        if structured is None:
            structured = getattr(res, "structured_content", None)
        if is_error:
            return McpCallResult(
                ok=False,
                text=format_mcp_error(McpErrorCode.TOOL_ERROR, out),
                is_error=True,
                structured_content=structured,
                error_code=McpErrorCode.TOOL_ERROR,
            )
        return McpCallResult(ok=True, text=out, structured_content=structured)
