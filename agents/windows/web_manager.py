"""WebManager — 网页搜索与抓取。"""

from __future__ import annotations

from typing import Any

from ...tools.execution_result import ExecutionResult, from_tuple
from ...tools.web_research import (
    WebResearchConfig,
    configure_web_research,
    fetch_web_page,
    search_web,
)


class WebManager:
    """网络检索与页面抓取。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def _apply_research_cfg(self) -> None:
        cfg = getattr(self._ctx, "web_research_cfg", None) if self._ctx else None
        if cfg:
            configure_web_research(WebResearchConfig.from_dict(cfg))

    def search_web(self, query: str, *, max_results: int = 5) -> ExecutionResult:
        q = (query or "").strip()
        if not q:
            return ExecutionResult(
                success=False,
                message="请告诉我要搜索什么",
                verified=False,
                action="search_web",
            )
        self._apply_research_cfg()
        ok, msg = search_web(q, max_results=max_results, fetch_pages=True)
        return from_tuple(ok, msg, verified=ok, action="search_web", target=q[:48])

    def fetch_web_page(
        self,
        url: str,
        *,
        start_index: int = 0,
        max_length: int | None = None,
    ) -> ExecutionResult:
        raw = (url or "").strip()
        if not raw:
            return ExecutionResult(
                success=False,
                message="请提供 URL",
                verified=False,
                action="fetch_web_page",
            )
        ok, msg = fetch_web_page(
            raw,
            start_index=start_index,
            max_length=max_length,
        )
        return from_tuple(ok, msg, verified=ok, action="fetch_web_page", target=raw[:48])

    def web_search(self, query: str, *, engine: str = "bing") -> ExecutionResult:
        from ...tools.browser_search import open_web_search

        q = (query or "").strip()
        if not q:
            return ExecutionResult(
                success=False,
                message="请告诉我要搜索什么",
                verified=False,
                action="web_search",
            )
        ok, msg = open_web_search(q, engine=engine)
        return from_tuple(ok, msg, verified=ok, action="web_search", target=q[:48])
