"""浏览器网页搜索 — 默认必应（国内 cn.bing.com）"""

from __future__ import annotations

import re
import subprocess
from urllib.parse import quote

# 必应优先；国内用 cn.bing.com，ensearch=0 优先中文结果
_SEARCH_ENGINES = {
    "bing": "https://cn.bing.com/search?q={}&ensearch=0",
    "bing-intl": "https://www.bing.com/search?q={}&setlang=zh-Hans&mkt=zh-CN",
    "google": "https://www.google.com/search?q={}",
    "baidu": "https://www.baidu.com/s?wd={}",
}

_ALIASES = {
    "必应": "bing",
    "微软": "bing",
    "cn": "bing",
}

_BROWSER_WORD = r"(?:浏览器|网页|microsoft\s*edge|edge|chrome|谷歌浏览器|微软|必应)"
_SEARCH_VERB = r"(?:搜索|搜|查(?:一下|下)?)"
_BROWSER_CONNECTOR = r"(?:并|然后|再)?"

_BROWSER_SEARCH_RES = (
    re.compile(
        rf"(?:请|帮我)?(?:打开|用|在)(?:一下\s*)?(?:默认\s*)?(?:的\s*)?"
        rf"\s*{_BROWSER_WORD}\s*(?:里)?{_BROWSER_CONNECTOR}(?:来)?{_SEARCH_VERB}"
        rf"(?:一下|下)?[：:\s]*(.+)",
        re.I,
    ),
    re.compile(
        rf"(?:打开|用)\s*{_BROWSER_WORD}\s*{_BROWSER_CONNECTOR}{_SEARCH_VERB}"
        rf"(?:一下|下)?[：:\s]*(.+)",
        re.I,
    ),
    re.compile(
        rf"\s*{_BROWSER_WORD}(?:里)?{_BROWSER_CONNECTOR}{_SEARCH_VERB}"
        rf"(?:一下|下)?[：:\s]*(.+)",
        re.I,
    ),
    re.compile(
        r"(?:打开|用)(?:搜索页|搜索)(?:来)?(?:搜索|搜)(?:一下|下)?[：:\s]*(.+)",
        re.I,
    ),
)

_LOCAL_SEARCH_MARKERS = re.compile(
    r"文件|文件夹|本地|电脑|磁盘|目录|桌面|下载|文档",
    re.I,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def normalize_search_engine(engine: str) -> str:
    key = (engine or "bing").strip().lower()
    return _ALIASES.get(key, key)


def _clean_search_query(raw: str) -> str:
    q = (raw or "").strip().strip("。，,.!?；;、：:")
    # 循环去尾部语气词，去完一个后重新检查（"搜索一下吧" → "搜索一下" → "搜索"）
    suffixes = ("一下", "下", "吧", "了", "呢", "哈")
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if q.endswith(suffix) and len(q) > len(suffix):
                q = q[: -len(suffix)].strip()
                changed = True
                break
    return q


def parse_browser_search_query(text: str) -> str | None:
    """从用户话里抽出要在浏览器里搜的关键词；非浏览器搜索场景返回 None。"""
    utterance = (text or "").strip()
    if not utterance or _LOCAL_SEARCH_MARKERS.search(utterance):
        return None
    for pat in _BROWSER_SEARCH_RES:
        m = pat.search(utterance)
        if not m:
            continue
        query = _clean_search_query(m.group(1))
        if query and query not in ("浏览器", "网页", "edge", "chrome"):
            return query
    return None


def is_browser_search_utterance(text: str) -> bool:
    return parse_browser_search_query(text) is not None


def open_web_search(query: str, engine: str = "bing") -> tuple[bool, str]:
    """用默认浏览器打开搜索引擎结果页（Start-Process，与副本版一致）。"""
    if not isinstance(query, str):
        query = str(query) if query is not None else ""
    query = query.strip()
    if not query:
        return False, "请告诉我要搜索什么内容"

    engine = normalize_search_engine(engine)
    template = _SEARCH_ENGINES.get(engine, _SEARCH_ENGINES["bing"])
    url = template.format(quote(query))

    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'Start-Process "{url}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        label = "必应" if engine.startswith("bing") else engine
        return True, f"正在用{label}在浏览器搜索：{query}"
    except Exception as e:
        return False, f"浏览器搜索失败: {e}"
