"""联网搜索 — 融合 ddg-search-mcp + duckduckgo-mcp-server 思路

- 多源搜索：ddgs 聚合 + 区域回退 + 新闻模式
- 正文提取链：trafilatura → ddgs extract → 摘要兜底（同 ddg-search-mcp）
- 独立精读：fetch_web_page 支持分页（同 duckduckgo-mcp-server fetch_content）
- 限流、并发抓页、难抓站点跳过
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .browser_search import normalize_search_engine

logger = logging.getLogger(__name__)

SKIP_HOST_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "reddit.com",
    "linkedin.com",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
)

NEWS_HINTS = (
    "新闻",
    "最新消息",
    "今日",
    "头条",
    "热搜",
    "快讯",
    "news",
    "latest",
    "breaking",
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class WebResearchConfig:
    search_engine: str = "bing"
    region: str = "cn-zh"
    fallback_region: str = "wt-wt"
    max_results_default: int = 5
    max_page_chars: int = 2500
    fetch_max_length: int = 8000
    extract_timeout_sec: float = 8.0
    concurrent_fetch: int = 5
    search_rate_per_min: int = 30
    fetch_rate_per_min: int = 20
    use_news: bool = True
    fetch_pages: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WebResearchConfig:
        if not data:
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        if "search_engine" in kwargs:
            kwargs["search_engine"] = normalize_search_engine(
                str(kwargs["search_engine"])
            )
        return cls(**kwargs)


def load_web_research_config(config_dir: Path | None = None) -> WebResearchConfig:
    base = config_dir or Path(__file__).resolve().parent.parent
    yaml_path = base / "config.yaml"
    if not yaml_path.exists():
        return WebResearchConfig()
    try:
        import yaml

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        block = data.get("web_research") or {}
        if isinstance(block, dict):
            return WebResearchConfig.from_dict(block)
    except Exception as e:
        logger.warning("读取 web_research 配置失败: %s", e)
    return WebResearchConfig()


class _RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self._per_minute = max(1, per_minute)
        self._lock = threading.Lock()
        self._times: list[float] = []

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < 60.0]
            if len(self._times) >= self._per_minute:
                sleep_for = 60.0 - (now - self._times[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
            self._times.append(time.monotonic())


@dataclass
class _ResearchRuntime:
    config: WebResearchConfig = field(default_factory=WebResearchConfig)
    search_limiter: _RateLimiter = field(default_factory=lambda: _RateLimiter(30))
    fetch_limiter: _RateLimiter = field(default_factory=lambda: _RateLimiter(20))

    def apply_config(self, cfg: WebResearchConfig) -> None:
        self.config = cfg
        self.search_limiter = _RateLimiter(cfg.search_rate_per_min)
        self.fetch_limiter = _RateLimiter(cfg.fetch_rate_per_min)


_RUNTIME = _ResearchRuntime()


def configure_web_research(cfg: WebResearchConfig | None = None) -> WebResearchConfig:
    cfg = cfg or load_web_research_config()
    _RUNTIME.apply_config(cfg)
    return cfg


configure_web_research()


def _host_skipped(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return any(host == s or host.endswith("." + s) for s in SKIP_HOST_SUFFIXES)
    except Exception:
        return False


def _norm_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower()
        path = (p.path or "").rstrip("/") or ""
        return f"{p.scheme or 'https'}://{host}{path}"
    except Exception:
        return url.strip()


def _try_import_ddgs():
    try:
        from ddgs import DDGS

        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS

            return DDGS
        except ImportError:
            return None


def _looks_like_news(query: str) -> bool:
    q = query.lower()
    return any(h in query or h in q for h in NEWS_HINTS)


def _unwrap_bing_url(raw_url: str) -> str | None:
    """解析必应跳转链接，还原真实网址"""
    try:
        parsed = urlparse(raw_url)
        u_vals = parse_qs(parsed.query).get("u", [])
        if not u_vals:
            return None
        u = u_vals[0]
        if len(u) <= 2:
            return None
        b64_part = u[2:]
        padding = "=" * (-len(b64_part) % 4)
        decoded = base64.urlsafe_b64decode(b64_part + padding)
        return decoded.decode()
    except Exception:
        return None


def _bing_host_for_region(region: str) -> str:
    r = (region or "cn-zh").lower()
    if r.startswith("cn") or r.endswith("-zh") or "zh" in r:
        return "https://cn.bing.com"
    return "https://www.bing.com"


def _bing_search(query: str, max_results: int, region: str) -> list[dict]:
    """直接请求必应 HTML 结果（国内 cn.bing.com）"""
    from lxml import html as lxml_html

    _RUNTIME.search_limiter.wait()
    max_results = max(1, min(int(max_results), 10))
    host = _bing_host_for_region(region)
    search_url = f"{host}/search?q={quote(query)}&ensearch=0"

    page_html = _fetch_html(search_url, _RUNTIME.config.extract_timeout_sec)
    tree = lxml_html.fromstring(page_html)
    items = tree.xpath("//li[contains(@class, 'b_algo')]")
    rows: list[dict] = []

    for item in items:
        if len(rows) >= max_results:
            break
        title_parts = item.xpath(".//h2/a//text()")
        hrefs = item.xpath(".//h2/a/@href")
        body_parts = item.xpath(".//p//text()")
        if not hrefs:
            continue
        href = str(hrefs[0]).strip()
        if "/aclick?" in href:
            continue
        if "/ck/a?" in href:
            href = _unwrap_bing_url(href) or href
        if not href.startswith(("http://", "https://")):
            continue
        rows.append(
            {
                "title": "".join(title_parts).strip(),
                "href": href,
                "body": " ".join(body_parts).strip(),
            }
        )
    if not rows:
        raise RuntimeError("必应未返回有效结果")
    return rows


def _ddgs_search(
    query: str,
    max_results: int,
    region: str,
    category: str = "text",
) -> list[dict]:
    DDGS = _try_import_ddgs()
    if DDGS is None:
        raise RuntimeError("未安装 ddgs，请执行: python -m pip install ddgs")

    _RUNTIME.search_limiter.wait()
    max_results = max(1, min(int(max_results), 10))
    with DDGS() as ddgs:
        kwargs: dict[str, Any] = {
            "max_results": max_results,
            "region": region,
        }
        engine = normalize_search_engine(_RUNTIME.config.search_engine)
        if engine == "bing":
            kwargs["backend"] = "duckduckgo"
        if category == "news":
            return list(ddgs.news(query, **kwargs))
        return list(ddgs.text(query, **kwargs))


def _merge_results(rows: list[dict]) -> list[dict]:
    """按 URL 去重，保留更长摘要"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        href = (row.get("href") or row.get("url") or "").strip()
        if not href:
            continue
        key = _norm_url(href)
        body = (row.get("body") or row.get("snippet") or "").strip()
        if key not in merged:
            merged[key] = dict(row)
            merged[key]["href"] = href
            order.append(key)
            continue
        old_body = (merged[key].get("body") or merged[key].get("snippet") or "").strip()
        if len(body) > len(old_body):
            merged[key]["body"] = body
        old_title = (merged[key].get("title") or "").strip()
        new_title = (row.get("title") or "").strip()
        if len(new_title) > len(old_title):
            merged[key]["title"] = new_title
    return [merged[k] for k in order]


def _multi_source_search(query: str, max_results: int, cfg: WebResearchConfig) -> list[dict]:
    collected: list[dict] = []
    per_region = max(3, max_results)
    engine = normalize_search_engine(cfg.search_engine)

    if engine.startswith("bing"):
        try:
            collected.extend(_bing_search(query, per_region, cfg.region))
        except Exception as e:
            logger.info("必应搜索失败: %s", e)
        if len(collected) < max_results and cfg.fallback_region:
            try:
                extra = _bing_search(query, per_region, cfg.fallback_region)
                collected.extend(extra)
            except Exception as e:
                logger.info("必应回退区域搜索失败: %s", e)
    else:
        try:
            collected.extend(_ddgs_search(query, per_region, cfg.region, "text"))
        except Exception as e:
            logger.info("区域 %s 搜索失败: %s", cfg.region, e)

        if cfg.fallback_region and cfg.fallback_region != cfg.region:
            try:
                collected.extend(
                    _ddgs_search(query, per_region, cfg.fallback_region, "text")
                )
            except Exception as e:
                logger.info("回退区域 %s 搜索失败: %s", cfg.fallback_region, e)

    if cfg.use_news and _looks_like_news(query):
        try:
            if engine.startswith("bing"):
                collected.extend(_bing_search(query, per_region, cfg.region))
            else:
                collected.extend(_ddgs_search(query, per_region, cfg.region, "news"))
        except Exception as e:
            logger.info("新闻搜索失败: %s", e)

    merged = _merge_results(collected)
    return merged[:max_results]


def _clean_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _fetch_html(url: str, timeout: float) -> str:
    import httpx

    _RUNTIME.fetch_limiter.wait()
    with httpx.Client(
        headers=HTTP_HEADERS,
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        resp = client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text


def _extract_trafilatura(html: str) -> str:
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            include_links=False,
            include_images=False,
        )
        return (text or "").strip()
    except ImportError:
        return ""
    except Exception as e:
        logger.debug("trafilatura 提取失败: %s", e)
        return ""


def _extract_ddgs(url: str) -> str:
    DDGS = _try_import_ddgs()
    if DDGS is None:
        return ""
    try:
        with DDGS() as ddgs:
            data = ddgs.extract(url, fmt="text_markdown")
            return str(data.get("content") or "").strip()
    except Exception as e:
        logger.debug("ddgs extract 失败 %s: %s", url, e)
        return ""


def _extract_page_content(
    url: str,
    max_chars: int,
    timeout: float,
) -> tuple[str, str]:
    """返回 (正文, 来源标记: full|snippet)"""
    if not url or _host_skipped(url):
        return "", "skip"

    timeout = max(3.0, float(timeout))

    try:
        html = _fetch_html(url, timeout)
        text = _extract_trafilatura(html)
        if len(text) >= 80:
            return _clean_text(text, max_chars), "trafilatura"
    except Exception as e:
        logger.debug("httpx+trafilatura %s: %s", url, e)

    text = _extract_ddgs(url)
    if len(text) >= 80:
        return _clean_text(text, max_chars), "ddgs"

    return "", "snippet"


def fetch_web_page(
    url: str,
    start_index: int = 0,
    max_length: int | None = None,
) -> tuple[bool, str]:
    """精读单个网页，支持分页（duckduckgo-mcp-server fetch_content 模式）。"""
    url = (url or "").strip()
    if not url:
        return False, "请提供网址"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    cfg = _RUNTIME.config
    max_length = int(max_length or cfg.fetch_max_length)
    max_length = max(500, min(max_length, 20000))
    start_index = max(0, int(start_index))

    text, source = _extract_page_content(
        url,
        max_chars=max_length + start_index + 500,
        timeout=cfg.extract_timeout_sec,
    )
    if not text:
        return False, f"无法读取该页面正文（可能被反爬或需登录）: {url}"

    total = len(text)
    if start_index >= total:
        return True, f"已读完（共 {total} 字）。来源: {source}"

    chunk = text[start_index : start_index + max_length]
    header = f"网址: {url}\n提取方式: {source}\n总字数约 {total}，本次 {start_index}–{start_index + len(chunk)}\n\n"
    if start_index + len(chunk) < total:
        header += f"（还有后续内容，可用 start_index={start_index + len(chunk)} 继续读）\n\n"
    return True, header + chunk


def _format_result(
    idx: int,
    row: dict,
    page_text: str,
    extract_kind: str,
) -> str:
    title = (row.get("title") or "").strip()
    href = (row.get("href") or row.get("url") or "").strip()
    snippet = (row.get("body") or row.get("snippet") or "").strip()

    parts = [f"### [{idx}] {title or '无标题'}"]
    if href:
        parts.append(f"来源: {href}")
    if page_text:
        label = {
            "trafilatura": "正文(高质量提取)",
            "ddgs": "正文(备用提取)",
        }.get(extract_kind, "正文")
        parts.append(f"{label}:\n{page_text}")
    elif snippet:
        parts.append(f"摘要: {snippet}")
    else:
        parts.append("摘要: （无）")
    return "\n".join(parts)


def search_web(
    query: str,
    max_results: int | None = None,
    fetch_pages: bool | None = None,
) -> tuple[bool, str]:
    """搜索互联网并汇总多条结果，供 LLM 综合回答。"""
    query = (query or "").strip()
    if not query:
        return False, "请告诉我要搜索什么"

    cfg = _RUNTIME.config
    max_results = max_results or cfg.max_results_default
    fetch_pages = cfg.fetch_pages if fetch_pages is None else fetch_pages

    try:
        rows = _multi_source_search(query, int(max_results), cfg)
    except Exception as e:
        logger.warning("联网搜索失败: %s", e)
        return False, f"联网搜索失败: {e}"

    if not rows:
        return False, f"没有找到与「{query}」相关的结果，请换个关键词试试"

    page_map: dict[str, tuple[str, str]] = {}
    if fetch_pages:
        urls = [
            (row.get("href") or row.get("url") or "").strip()
            for row in rows
        ]
        urls = [u for u in urls if u and not _host_skipped(u)]
        workers = min(cfg.concurrent_fetch, len(urls) or 1)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _extract_page_content,
                    url,
                    cfg.max_page_chars,
                    cfg.extract_timeout_sec,
                ): url
                for url in urls
            }
            try:
                for fut in as_completed(
                    futures,
                    timeout=cfg.extract_timeout_sec + 4,
                ):
                    url = futures[fut]
                    try:
                        text, kind = fut.result(timeout=1)
                        page_map[url] = (text, kind)
                    except Exception:
                        page_map[url] = ("", "snippet")
            except Exception as e:
                logger.info("部分页面抓取超时: %s", e)

    full_count = 0
    snippet_count = 0
    formatted_rows: list[tuple[dict, str, str]] = []

    for row in rows:
        href = (row.get("href") or row.get("url") or "").strip()
        page_text, kind = page_map.get(href, ("", "snippet"))
        if page_text and kind in ("trafilatura", "ddgs"):
            full_count += 1
        else:
            snippet_count += 1
        formatted_rows.append((row, page_text, kind))

    # 全文优先排序（同 ddg-search-mcp）
    formatted_rows.sort(
        key=lambda x: 0 if x[1] and x[2] in ("trafilatura", "ddgs") else 1
    )

    engine = normalize_search_engine(cfg.search_engine)
    engine_label = "必应 cn.bing.com" if engine.startswith("bing") else engine
    blocks: list[str] = [
        f"搜索词: {query}",
        f"搜索引擎: {engine_label}",
        f"区域: {cfg.region}（回退 {cfg.fallback_region}）",
        f"共 {len(rows)} 条：{full_count} 条全文 / {snippet_count} 条仅摘要",
        "请综合以下信息口语化回答用户，不要念网址；信息不足可调用 fetch_web_page 精读某链接。",
        "",
    ]
    for i, (row, page_text, kind) in enumerate(formatted_rows, 1):
        blocks.append(_format_result(i, row, page_text, kind))
        blocks.append("")

    text = "\n".join(blocks).strip()
    if len(text) > 14000:
        text = text[:14000] + "\n…(结果已截断，可用 fetch_web_page 精读单页)"
    return True, text
