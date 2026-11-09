"""自习专用抓取层 — 纯 HTTP (httpx) 访问 arXiv 与 GitHub，零浏览器依赖。

分层约束：
    - 与 tools/web_research.py 平级但互不依赖（web_research 继续服务对话，自习只走本模块）
    - 不 import 任何 skills 插件内部实现，不起浏览器，禁拉 tarball (codeload)
    - 每次实际抓取均登记 fetched_urls，供上层入库核验 source_refs 真实性
    - 内置每日与单场 HTTP 预算护栏，配额持久化至 data/study_state.json（本地日期日切）
"""

from __future__ import annotations

import contextlib
import html as std_html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

# ─── 常量与默认配额 ────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "KekesiStudyAgent/1.0 (+https://arxiv.org; +https://api.github.com)"
)

_ARXIV_API_URL = "https://export.arxiv.org/api/query"
_ARXIV_MIN_INTERVAL_SEC = 1.05  # 官方礼貌策略：请求间隔 ≥1s
_ARXIV_HTML_MAX_BYTES = 1_048_576  # >1MB 先截断再抽取

_RAW_FILE_MAX_BYTES = 61_440  # 单文件 raw 硬上限 60KB
_SESSION_RAW_MAX_BYTES = 409_600  # 每场累计 raw/正文 ≤400KB
_SESSION_GH_SEARCH_MAX = 2
_SESSION_GH_TREES_MAX = 2
_SESSION_GH_RAW_MAX = 12

_DEFAULT_DAILY_BUDGET: dict[str, int] = {
    "github_search_per_day": 20,
    "github_api_per_day": 40,
    "raw_files_per_day": 60,
    "http_bytes_per_day": 4_194_304,  # 4MB
}

_DEFAULT_STATE_REL = "data/study_state.json"
_CONNECT_TIMEOUT = 8.0
_READ_TIMEOUT = 15.0

_ALLOWED_TREE_EXTS = (".py", ".md", ".toml", ".yaml", ".yml")
_ALLOWED_TREE_NAMES = {"readme", "dockerfile", "makefile", "license"}
_SKIP_TREE_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    "vendor",
    "third_party",
    "site-packages",
}
_TREE_MAX_DEPTH = 4
_TREE_MAX_ITEMS = 80

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# 进程内轻量缓存：减少重复请求
_arxiv_lock = threading.Lock()
_last_arxiv_ts: float = 0.0
_paper_cache: dict[str, PaperRef] = {}
_repo_branch_cache: dict[str, str] = {}
_module_fetched_urls: set[str] = set()


# ─── 数据结构 ──────────────────────────────────────────────────


@dataclass
class PaperRef:
    arxiv_id: str
    title: str
    summary: str
    authors: list[str] = field(default_factory=list)
    published: str = ""
    categories: list[str] = field(default_factory=list)
    abs_url: str = ""
    html_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepoRef:
    full_name: str
    owner: str
    repo: str
    description: str = ""
    stars: int = 0
    language: str = ""
    license: str = ""
    default_branch: str = "main"
    html_url: str = ""
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_source_url(url: str) -> str:
    """规范化来源 URL，统一协议、去掉末尾斜杠与锚点，便于核验。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw
        host = parsed.netloc.lower()
        if host == "export.arxiv.org":
            host = "arxiv.org"
        path = re.sub(r"/+$", "", parsed.path or "")
        # arxiv 链接统一不带版本号也能对齐
        return f"https://{host}{path}"
    except Exception:
        return raw.rstrip("/")


def strip_arxiv_version(arxiv_id: str) -> str:
    """去掉 arXiv ID 末尾的 vN 版本号（如 2401.12345v2 -> 2401.12345）。"""
    return re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.IGNORECASE)


def clean_arxiv_id(raw: str) -> str:
    """从输入字符串或 URL 中提取干净的 arXiv ID。"""
    s = str(raw or "").strip()
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.IGNORECASE)
    if "arxiv.org/" in s or "ar5iv" in s:
        m = re.search(r"/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+/[0-9]{7}(?:v\d+)?)", s, re.I)
        if m:
            return m.group(1)
    m = re.search(r"([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+/[0-9]{7}(?:v\d+)?)", s, re.I)
    return m.group(1) if m else s


# ─── 预算与状态持久化 ──────────────────────────────────────────


class HttpBudget:
    """单场 + 每日 HTTP 抓取预算与真实 URL 账本。"""

    def __init__(
        self,
        daily_limits: dict[str, Any] | None = None,
        state_file: Path | str | None = None,
        *,
        session_gh_search_max: int = _SESSION_GH_SEARCH_MAX,
        session_gh_trees_max: int = _SESSION_GH_TREES_MAX,
        session_gh_raw_max: int = _SESSION_GH_RAW_MAX,
        session_raw_max_bytes: int = _SESSION_RAW_MAX_BYTES,
    ) -> None:
        merged = dict(_DEFAULT_DAILY_BUDGET)
        for k, v in (daily_limits or {}).items():
            if k in merged and v is not None:
                try:
                    merged[k] = max(1, int(v))
                except (TypeError, ValueError):
                    pass
        self.daily_limits = merged

        if state_file is None:
            base = Path(__file__).resolve().parent.parent
            self.state_path = base / _DEFAULT_STATE_REL
        else:
            p = Path(state_file)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            self.state_path = p

        self.session_gh_search_max = max(1, int(session_gh_search_max))
        self.session_gh_trees_max = max(1, int(session_gh_trees_max))
        self.session_gh_raw_max = max(1, int(session_gh_raw_max))
        self.session_raw_max_bytes = max(1024, int(session_raw_max_bytes))

        self.session_gh_search = 0
        self.session_gh_trees = 0
        self.session_gh_raw = 0
        self.session_raw_bytes = 0
        self.session_http_bytes = 0

        self.fetched_urls: set[str] = set()
        self.sources_used: set[str] = set()  # "arxiv_html" | "arxiv_abstract" | "gh_readme_code" | "gh_meta"
        self.exhausted_reason: str = ""
        self._lock = threading.RLock()
        self._lock_depth = 0
        self._lock_fd: int | None = None

    # ── 跨进程排他文件锁与状态文件读写（按本地日期日切） ──

    @contextlib.contextmanager
    def _file_lock(self, timeout: float = 15.0):
        """跨进程 + 进程内可重入排他锁。

        在 Windows 下使用 msvcrt.locking 锁定 .lock 文件首字节，在 POSIX 下使用 fcntl.flock。
        由操作系统内核管理句柄级字节锁：若持锁进程崩溃或被强杀，OS 内核关闭句柄时会自动释放锁，不会永久死锁。
        """
        with self._lock:
            if self._lock_depth > 0:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return

            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            locked = False
            deadline = time.monotonic() + max(0.5, float(timeout))
            try:
                if os.name == "nt":
                    import msvcrt

                    while True:
                        try:
                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"等待跨进程预算锁超时: {lock_path}")
                            time.sleep(0.003)
                else:
                    import fcntl

                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"等待跨进程预算锁超时: {lock_path}")
                            time.sleep(0.003)

                self._lock_fd = fd
                self._lock_depth = 1
                yield
            finally:
                self._lock_depth = 0
                self._lock_fd = None
                if locked:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _today_str(self) -> str:
        return date.today().isoformat()

    def _load_daily(self) -> dict[str, Any]:
        with self._file_lock():
            today = self._today_str()
            default_state: dict[str, Any] = {
                "date": today,
                "github_search": 0,
                "github_api": 0,
                "raw_files": 0,
                "http_bytes": 0,
                "history": [],
            }
            try:
                if self.state_path.is_file():
                    raw_text = ""
                    for attempt in range(3):
                        try:
                            raw_text = self.state_path.read_text(encoding="utf-8")
                            break
                        except OSError:
                            if attempt == 2:
                                raise
                            time.sleep(0.002)
                    data = json.loads(raw_text) if raw_text.strip() else {}
                    if isinstance(data, dict):
                        prev_history = data.get("history") if isinstance(data.get("history"), list) else []
                        file_date = str(data.get("date") or "").strip()
                        if file_date == today:
                            for k in ("github_search", "github_api", "raw_files", "http_bytes"):
                                try:
                                    val = int(data.get(k) or 0)
                                except (TypeError, ValueError):
                                    val = 0
                                # 异常状态自愈：负数归零，保留真实正数流量记录
                                default_state[k] = max(0, val)
                            default_state["history"] = prev_history[-30:]
                        else:
                            # 跨日自动日切重置：幂等归档前一日真实流量记录，重置今日计数并立即落盘
                            if file_date:
                                archived = {
                                    "date": file_date,
                                    "github_search": max(0, int(data.get("github_search") or 0)),
                                    "github_api": max(0, int(data.get("github_api") or 0)),
                                    "raw_files": max(0, int(data.get("raw_files") or 0)),
                                    "http_bytes": max(0, int(data.get("http_bytes") or 0)),
                                }
                                prev_history = [
                                    h for h in prev_history if isinstance(h, dict) and h.get("date") != file_date
                                ]
                                prev_history.append(archived)
                            default_state["history"] = prev_history[-30:]
                            self.exhausted_reason = ""
                            self._save_daily(default_state)
            except Exception:
                logger.debug("读取 study_state.json 失败，使用今日零值", exc_info=True)
            return default_state

    def _save_daily(self, state: dict[str, Any]) -> None:
        with self._file_lock():
            tmp = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(state, ensure_ascii=False, indent=2)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                for attempt in range(8):
                    try:
                        tmp.replace(self.state_path)
                        break
                    except OSError:
                        if attempt == 7:
                            raise
                        time.sleep(0.002)
            except Exception:
                logger.debug("写入 study_state.json 失败", exc_info=True)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def daily_snapshot(self) -> dict[str, Any]:
        with self._file_lock():
            state = self._load_daily()
            used_bytes = state["http_bytes"]
            limit_bytes = self.daily_limits["http_bytes_per_day"]
            status = "exhausted" if used_bytes >= limit_bytes else "normal"
            return {
                "date": state["date"],
                "status": status,
                "used": {
                    "github_search": state["github_search"],
                    "github_api": state["github_api"],
                    "raw_files": state["raw_files"],
                    "http_bytes": state["http_bytes"],
                },
                "limits": dict(self.daily_limits),
                "history": list(state.get("history") or []),
            }

    # ── 额度检查与扣减 ──

    def check_and_consume(
        self,
        kind: str,
        *,
        est_bytes: int = 0,
    ) -> tuple[bool, str]:
        """检查额度并预扣调用次数（字节数在响应到达后由 add_bytes 记账）。

        kind: 'arxiv_api' | 'arxiv_html' | 'github_search' | 'github_tree' | 'github_raw'
        """
        with self._file_lock():
            state = self._load_daily()
            if (
                state["http_bytes"] >= self.daily_limits["http_bytes_per_day"]
                or state["http_bytes"] + max(0, est_bytes) > self.daily_limits["http_bytes_per_day"]
            ):
                self.exhausted_reason = (
                    f"每日 HTTP 流量预算已满 ({state['http_bytes']}/{self.daily_limits['http_bytes_per_day']} bytes)"
                )
                return False, self.exhausted_reason

            if kind == "github_search":
                if self.session_gh_search >= self.session_gh_search_max:
                    return False, f"本场 github_search 已达上限 ({self.session_gh_search_max})"
                if state["github_search"] >= self.daily_limits["github_search_per_day"]:
                    self.exhausted_reason = (
                        f"每日 github_search 预算已满 ({state['github_search']}/{self.daily_limits['github_search_per_day']})"
                    )
                    return False, self.exhausted_reason
                if state["github_api"] >= self.daily_limits["github_api_per_day"]:
                    self.exhausted_reason = (
                        f"每日 github_api 预算已满 ({state['github_api']}/{self.daily_limits['github_api_per_day']})"
                    )
                    return False, self.exhausted_reason
                self.session_gh_search += 1
                state["github_search"] += 1
                state["github_api"] += 1
                self._save_daily(state)
                return True, ""

            if kind == "github_tree":
                if self.session_gh_trees >= self.session_gh_trees_max:
                    return False, f"本场 github_repo_files 已达上限 ({self.session_gh_trees_max})"
                if state["github_api"] >= self.daily_limits["github_api_per_day"]:
                    self.exhausted_reason = (
                        f"每日 github_api 预算已满 ({state['github_api']}/{self.daily_limits['github_api_per_day']})"
                    )
                    return False, self.exhausted_reason
                self.session_gh_trees += 1
                state["github_api"] += 1
                self._save_daily(state)
                return True, ""

            if kind == "github_raw":
                if self.session_gh_raw >= self.session_gh_raw_max:
                    return False, f"本场 github_read_file 已达上限 ({self.session_gh_raw_max})"
                if self.session_raw_bytes >= self.session_raw_max_bytes:
                    return False, f"本场源码读取累计体积已达上限 ({self.session_raw_max_bytes} bytes)"
                if state["raw_files"] >= self.daily_limits["raw_files_per_day"]:
                    self.exhausted_reason = (
                        f"每日 raw_files 预算已满 ({state['raw_files']}/{self.daily_limits['raw_files_per_day']})"
                    )
                    return False, self.exhausted_reason
                self.session_gh_raw += 1
                state["raw_files"] += 1
                self._save_daily(state)
                return True, ""

            return True, ""

    def add_bytes(self, nbytes: int, *, is_raw: bool = False) -> None:
        if nbytes <= 0:
            return
        with self._file_lock():
            self.session_http_bytes += nbytes
            if is_raw:
                self.session_raw_bytes += nbytes
            state = self._load_daily()
            state["http_bytes"] += nbytes
            self._save_daily(state)

    def record_url(self, *urls: str) -> None:
        with self._lock:
            for u in urls:
                if not u:
                    continue
                raw = str(u).strip()
                norm = normalize_source_url(raw)
                if raw:
                    self.fetched_urls.add(raw)
                    _module_fetched_urls.add(raw)
                if norm:
                    self.fetched_urls.add(norm)
                    _module_fetched_urls.add(norm)

    def is_url_verified(self, ref_url: str) -> bool:
        """核验笔记引用的 URL 是否属于本轮真实 fetch 过的来源。"""
        raw = str(ref_url or "").strip()
        if not raw:
            return False
        norm = normalize_source_url(raw)
        if raw in self.fetched_urls or norm in self.fetched_urls:
            return True
        # arXiv 版本号容错：如抓了 2401.12345v1，引用写 https://arxiv.org/abs/2401.12345
        if "arxiv.org/" in norm:
            cid = strip_arxiv_version(clean_arxiv_id(norm))
            if cid:
                for u in self.fetched_urls:
                    if "arxiv.org/" in u and strip_arxiv_version(clean_arxiv_id(u)) == cid:
                        return True
        # GitHub 子路径容错：若真实抓过某仓库的 raw 文件，引用写该仓库主页或对应 blob 链接视为有效
        if norm.startswith("https://github.com/"):
            parts = norm[len("https://github.com/") :].split("/")
            if len(parts) >= 2:
                repo_root = f"https://github.com/{parts[0]}/{parts[1]}"
                if repo_root in self.fetched_urls:
                    return True
        return False


def get_module_fetched_urls() -> set[str]:
    return set(_module_fetched_urls)


def clear_module_fetched_urls() -> None:
    _module_fetched_urls.clear()


# ─── HTTP 底层辅助 ─────────────────────────────────────────────


def _resolve_github_token(token_env: str = "GITHUB_TOKEN") -> str:
    env_name = (token_env or "GITHUB_TOKEN").strip()
    if not env_name:
        return ""
    return (os.environ.get(env_name) or "").strip()


def _github_headers(token_env: str = "GITHUB_TOKEN") -> dict[str, str]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _resolve_github_token(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _arxiv_throttle() -> None:
    global _last_arxiv_ts
    with _arxiv_lock:
        now = time.monotonic()
        elapsed = now - _last_arxiv_ts
        if elapsed < _ARXIV_MIN_INTERVAL_SEC and _last_arxiv_ts > 0:
            time.sleep(_ARXIV_MIN_INTERVAL_SEC - elapsed)
        _last_arxiv_ts = time.monotonic()


def _http_get_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = _ARXIV_HTML_MAX_BYTES,
    timeout_sec: float = _READ_TIMEOUT,
) -> tuple[int, bytes, str]:
    """发起 GET 请求并按字节上限截断，禁止 codeload tarball。

    返回 (status_code, body_bytes, final_url)。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "codeload.github.com" in host or url.endswith((".tar.gz", ".tgz", ".zip")):
        raise ValueError(f"禁止下载归档包/tarball: {url}")

    import httpx

    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)

    timeout = httpx.Timeout(timeout_sec, connect=_CONNECT_TIMEOUT)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(
                headers=req_headers,
                follow_redirects=True,
                timeout=timeout,
            ) as client:
                with client.stream("GET", url) as resp:
                    status = resp.status_code
                    final_url = str(resp.url)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        remain = max_bytes - total
                        if remain <= 0:
                            break
                        if len(chunk) > remain:
                            chunks.append(chunk[:remain])
                            total += remain
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    return status, b"".join(chunks), final_url
        except Exception as e:
            last_exc = e
            if attempt == 0:
                time.sleep(0.6)
                continue
            raise
    raise RuntimeError(f"HTTP 请求失败: {last_exc}")


def _extract_html_text(raw_html: str) -> str:
    """从 HTML 提取干净正文：优先 trafilatura，回退 BeautifulSoup，再回退正则。"""
    if not raw_html:
        return ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            raw_html,
            include_comments=False,
            include_tables=True,
            include_links=False,
            include_images=False,
        )
        if extracted and len(extracted.strip()) >= 120:
            return re.sub(r"\n{3,}", "\n\n", extracted.strip())
    except Exception:
        logger.debug("trafilatura 提取失败，尝试 bs4 回退", exc_info=True)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        article = soup.find("article") or soup.find("main") or soup.body or soup
        text = article.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) >= 80:
            return text
    except Exception:
        logger.debug("bs4 提取失败，使用正则回退", exc_info=True)

    stripped = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
    stripped = std_html.unescape(stripped)
    return re.sub(r"\s+", " ", stripped).strip()


# ─── 1. arXiv 检索与全文读取 ───────────────────────────────────


def _parse_arxiv_atom(xml_bytes: bytes) -> list[PaperRef]:
    root = ET.fromstring(xml_bytes)
    papers: list[PaperRef] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        id_text = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        if not id_text:
            continue
        arxiv_id = clean_arxiv_id(id_text)
        base_id = strip_arxiv_version(arxiv_id)
        title = re.sub(
            r"\s+",
            " ",
            (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip(),
        )
        summary = re.sub(
            r"\s+",
            " ",
            (entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").strip(),
        )
        if not title and not summary:
            continue
        authors = [
            (a.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", _ATOM_NS)
            if (a.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").strip()
        ]
        published = (
            entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or ""
        ).strip()[:10]
        categories = [
            str(c.attrib.get("term") or "").strip()
            for c in entry.findall("atom:category", _ATOM_NS)
            if str(c.attrib.get("term") or "").strip()
        ]
        abs_url = f"https://arxiv.org/abs/{base_id}"
        html_url = f"https://arxiv.org/html/{base_id}"
        ref = PaperRef(
            arxiv_id=arxiv_id,
            title=title,
            summary=summary,
            authors=authors[:6],
            published=published,
            categories=categories[:6],
            abs_url=abs_url,
            html_url=html_url,
        )
        papers.append(ref)
        _paper_cache[base_id] = ref
        _paper_cache[arxiv_id] = ref
    return papers


def _build_arxiv_search_query(query: str) -> str:
    q = re.sub(r"^(?:ARXIV|PAPER)\s*:\s*", "", str(query or "").strip(), flags=re.I).strip()
    if not q:
        return ""
    if any(prefix in q for prefix in ("all:", "ti:", "abs:", "cat:", "au:")):
        return q
    # 多词查询拆成 AND 词项，避免短语精确匹配过严返回 0 条
    tokens = [t for t in re.split(r"\s+", q) if t]
    if len(tokens) <= 1:
        return f"all:{q}"
    return " AND ".join(f"all:{t}" for t in tokens[:6])


def arxiv_search(
    query: str,
    max_results: int = 3,
    sort: str = "relevance",
    *,
    budget: HttpBudget | None = None,
) -> tuple[bool, list[PaperRef]]:
    """检索 arXiv 论文（结构化 Atom API）。"""
    search_query = _build_arxiv_search_query(query)
    if not search_query:
        return False, []

    if budget is not None:
        ok, _reason = budget.check_and_consume("arxiv_api")
        if not ok:
            return False, []

    max_results = max(1, min(int(max_results or 3), 10))
    sort_map = {
        "relevance": "relevance",
        "submitteddate": "submittedDate",
        "lastupdateddate": "lastUpdatedDate",
        "date": "submittedDate",
        "recent": "submittedDate",
    }
    sort_by = sort_map.get(str(sort or "relevance").lower(), "relevance")
    url = (
        f"{_ARXIV_API_URL}?search_query={quote(search_query)}"
        f"&start=0&max_results={max_results}&sortBy={sort_by}&sortOrder=descending"
    )

    try:
        _arxiv_throttle()
        status, body, final_url = _http_get_bytes(url, max_bytes=512_000)
        if budget is not None:
            budget.add_bytes(len(body))
        if status != 200 or not body:
            logger.info("arXiv 检索返回 HTTP %s (%s)", status, query)
            return False, []
        papers = _parse_arxiv_atom(body)
        if not papers:
            return False, []
        for p in papers:
            urls = (
                url,
                final_url,
                p.abs_url,
                f"https://arxiv.org/abs/{p.arxiv_id}",
            )
            if budget is not None:
                budget.record_url(*urls)
                budget.sources_used.add("arxiv_abstract")
            else:
                for u in urls:
                    _module_fetched_urls.add(normalize_source_url(u))
        return True, papers
    except Exception as e:
        logger.warning("arXiv 检索异常 (%s): %s", query, e)
        return False, []


def arxiv_fulltext(
    arxiv_id: str,
    max_chars: int = 6000,
    *,
    budget: HttpBudget | None = None,
) -> tuple[bool, str, str]:
    """读取 arXiv 论文正文。

    优先抓 `https://arxiv.org/html/<id>`（>1MB 先截断再由 trafilatura 抽取）；
    若无 HTML 版（404 等），尝试 `ar5iv.labs.arxiv.org/html/<id>`，
    仍不可得则平滑回退到 Atom API 的 Abstract。

    返回: (ok, text, source_tag)  其中 source_tag ∈ {"html", "ar5iv", "abstract"}
    """
    cid = clean_arxiv_id(arxiv_id)
    if not cid:
        return False, "缺少有效的 arxiv_id", ""
    base_id = strip_arxiv_version(cid)
    max_chars = max(500, min(int(max_chars or 6000), 20_000))

    if budget is not None:
        ok, reason = budget.check_and_consume("arxiv_html")
        if not ok:
            return False, reason, ""

    # 1. 尝试官方 HTML 全文 (arxiv.org/html/<id>)
    html_candidates = [
        ("html", f"https://arxiv.org/html/{cid}"),
    ]
    if base_id != cid:
        html_candidates.append(("html", f"https://arxiv.org/html/{base_id}"))
    html_candidates.append(("ar5iv", f"https://ar5iv.labs.arxiv.org/html/{base_id}"))

    for tag, html_url in html_candidates:
        try:
            _arxiv_throttle()
            status, body, final_url = _http_get_bytes(
                html_url,
                max_bytes=_ARXIV_HTML_MAX_BYTES,
                timeout_sec=_READ_TIMEOUT,
            )
            if budget is not None:
                budget.add_bytes(len(body))
            if status == 200 and len(body) >= 500:
                raw_html = body.decode("utf-8", errors="replace")
                extracted = _extract_html_text(raw_html)
                if len(extracted) >= 200:
                    rec_urls = (
                        html_url,
                        final_url,
                        f"https://arxiv.org/html/{base_id}",
                        f"https://arxiv.org/abs/{base_id}",
                        f"https://arxiv.org/abs/{cid}",
                    )
                    if budget is not None:
                        budget.record_url(*rec_urls)
                        budget.sources_used.add("arxiv_html")
                    else:
                        for u in rec_urls:
                            _module_fetched_urls.add(normalize_source_url(u))
                    return True, extracted[:max_chars], tag
        except Exception as e:
            logger.debug("arXiv HTML (%s) 读取未果: %s", html_url, e)

    # 2. 降级回退：使用缓存或查询 Atom API 摘要
    cached = _paper_cache.get(cid) or _paper_cache.get(base_id)
    if cached is None:
        try:
            _arxiv_throttle()
            id_url = f"{_ARXIV_API_URL}?id_list={quote(base_id)}"
            status, body, _ = _http_get_bytes(id_url, max_bytes=256_000)
            if budget is not None:
                budget.add_bytes(len(body))
            if status == 200 and body:
                found = _parse_arxiv_atom(body)
                if found:
                    cached = found[0]
        except Exception as e:
            logger.debug("arXiv abstract 回退查询失败 (%s): %s", base_id, e)

    if cached and cached.summary:
        abs_text = f"Title: {cached.title}\nAuthors: {', '.join(cached.authors)}\nPublished: {cached.published}\n\nAbstract:\n{cached.summary}"
        rec_urls = (
            cached.abs_url or f"https://arxiv.org/abs/{base_id}",
            f"https://arxiv.org/abs/{cid}",
        )
        if budget is not None:
            budget.record_url(*rec_urls)
            budget.sources_used.add("arxiv_abstract")
        else:
            for u in rec_urls:
                _module_fetched_urls.add(normalize_source_url(u))
        return True, abs_text[:max_chars], "abstract"

    return False, f"未找到 arXiv 论文 {cid} 的全文或摘要", ""


# ─── 2. GitHub 检索、文件树与单文件读取 ────────────────────────


def github_search(
    query: str,
    max_results: int = 5,
    sort: str = "stars",
    *,
    budget: HttpBudget | None = None,
    token_env: str = "GITHUB_TOKEN",
) -> tuple[bool, list[RepoRef]]:
    """检索 GitHub 开源仓库。"""
    clean_q = re.sub(r"^(?:REPO|GITHUB)\s*:\s*", "", str(query or "").strip(), flags=re.I).strip()
    if not clean_q:
        return False, []

    if budget is not None:
        ok, _reason = budget.check_and_consume("github_search")
        if not ok:
            return False, []

    max_results = max(1, min(int(max_results or 5), 10))
    sort_param = str(sort or "stars").strip().lower()
    if sort_param not in ("stars", "updated", "forks", "help-wanted-issues"):
        sort_param = "stars"

    url = (
        f"https://api.github.com/search/repositories"
        f"?q={quote(clean_q)}&sort={sort_param}&order=desc&per_page={max_results}"
    )
    try:
        status, body, final_url = _http_get_bytes(
            url,
            headers=_github_headers(token_env),
            max_bytes=512_000,
        )
        if budget is not None:
            budget.add_bytes(len(body))
        if status != 200 or not body:
            logger.info("GitHub search 返回 HTTP %s (%s)", status, clean_q)
            return False, []
        data = json.loads(body.decode("utf-8", errors="replace"))
        items = data.get("items") or []
        repos: list[RepoRef] = []
        for item in items[:max_results]:
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("full_name") or "").strip()
            if not full_name or "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            default_branch = str(item.get("default_branch") or "main").strip()
            lic_obj = item.get("license")
            lic_name = ""
            if isinstance(lic_obj, dict):
                lic_name = str(lic_obj.get("spdx_id") or lic_obj.get("name") or "").strip()
            html_url = str(item.get("html_url") or f"https://github.com/{full_name}").strip()
            ref = RepoRef(
                full_name=full_name,
                owner=owner,
                repo=repo,
                description=str(item.get("description") or "").strip()[:300],
                stars=int(item.get("stargazers_count") or 0),
                language=str(item.get("language") or "").strip(),
                license=lic_name,
                default_branch=default_branch,
                html_url=html_url,
                topics=[str(t) for t in (item.get("topics") or [])[:8] if t],
            )
            repos.append(ref)
            _repo_branch_cache[full_name.lower()] = default_branch
            if budget is not None:
                budget.record_url(html_url, final_url)
                budget.sources_used.add("gh_meta")
            else:
                _module_fetched_urls.add(normalize_source_url(html_url))
        return bool(repos), repos
    except Exception as e:
        logger.warning("GitHub 检索异常 (%s): %s", clean_q, e)
        return False, []


def _filter_and_rank_tree_paths(tree_items: list[dict[str, Any]]) -> list[str]:
    """筛选仓库目录树：只留文档/配置/源码，限制目录深度与总条数。"""
    readme_paths: list[str] = []
    config_paths: list[str] = []
    code_paths: list[str] = []

    for item in tree_items:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        parts = path.split("/")
        if len(parts) > _TREE_MAX_DEPTH:
            continue
        if any(seg.lower() in _SKIP_TREE_DIRS or seg.startswith(".") for seg in parts[:-1]):
            continue
        filename = parts[-1]
        lower_name = filename.lower()
        stem = lower_name.rsplit(".", 1)[0]
        ext = ("." + lower_name.rsplit(".", 1)[1]) if "." in lower_name else ""

        if stem in _ALLOWED_TREE_NAMES or lower_name.startswith("readme"):
            readme_paths.append(path)
        elif ext in (".toml", ".yaml", ".yml") or lower_name in ("requirements.txt", "setup.py"):
            config_paths.append(path)
        elif ext in _ALLOWED_TREE_EXTS:
            code_paths.append(path)

    # 浅层文件优先
    code_paths.sort(key=lambda p: (p.count("/"), len(p)))
    config_paths.sort(key=lambda p: (p.count("/"), len(p)))
    readme_paths.sort(key=lambda p: (p.count("/"), len(p)))

    combined: list[str] = []
    for group in (readme_paths, config_paths, code_paths):
        for p in group:
            if p not in combined:
                combined.append(p)
            if len(combined) >= _TREE_MAX_ITEMS:
                return combined
    return combined


def github_repo_files(
    owner: str,
    repo: str,
    ref: str = "HEAD",
    *,
    budget: HttpBudget | None = None,
    token_env: str = "GITHUB_TOKEN",
) -> tuple[bool, list[str]]:
    """通过 GitHub git/trees?recursive=1 一次获取仓库关键文件清单。"""
    owner = str(owner or "").strip().strip("/")
    repo = str(repo or "").strip().strip("/")
    if not owner or not repo:
        return False, []

    if budget is not None:
        ok, _reason = budget.check_and_consume("github_tree")
        if not ok:
            return False, []

    full_key = f"{owner}/{repo}".lower()
    target_ref = str(ref or "").strip() or _repo_branch_cache.get(full_key) or "HEAD"
    refs_to_try = [target_ref]
    for fallback_ref in ("HEAD", "main", "master"):
        if fallback_ref not in refs_to_try:
            refs_to_try.append(fallback_ref)

    headers = _github_headers(token_env)
    for r in refs_to_try:
        url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{quote(r)}?recursive=1"
        try:
            status, body, final_url = _http_get_bytes(
                url,
                headers=headers,
                max_bytes=1_048_576,
            )
            if budget is not None:
                budget.add_bytes(len(body))
            if status == 404:
                continue
            if status != 200 or not body:
                return False, []
            data = json.loads(body.decode("utf-8", errors="replace"))
            tree_items = data.get("tree") or []
            filtered = _filter_and_rank_tree_paths(tree_items)
            repo_url = f"https://github.com/{owner}/{repo}"
            if budget is not None:
                budget.record_url(repo_url, final_url)
                budget.sources_used.add("gh_meta")
            else:
                _module_fetched_urls.add(normalize_source_url(repo_url))
            return True, filtered
        except Exception as e:
            logger.warning("GitHub tree 获取异常 (%s/%s@%s): %s", owner, repo, r, e)
            return False, []
    return False, []


def github_read_file(
    owner: str,
    repo: str,
    ref: str = "HEAD",
    path: str = "",
    max_bytes: int = _RAW_FILE_MAX_BYTES,
    *,
    budget: HttpBudget | None = None,
    token_env: str = "GITHUB_TOKEN",
) -> tuple[bool, str]:
    """从 raw.githubusercontent.com 读取单个源码或文档文件（严禁 tarball，单文件 ≤60KB）。"""
    owner = str(owner or "").strip().strip("/")
    repo = str(repo or "").strip().strip("/")
    clean_path = str(path or "").strip().lstrip("/")
    if not owner or not repo or not clean_path:
        return False, "owner/repo/path 不能为空"

    hard_max = max(512, min(int(max_bytes or _RAW_FILE_MAX_BYTES), _RAW_FILE_MAX_BYTES))
    if budget is not None:
        ok, reason = budget.check_and_consume("github_raw", est_bytes=min(4096, hard_max))
        if not ok:
            return False, reason
        remain_session = budget.session_raw_max_bytes - budget.session_raw_bytes
        if remain_session <= 0:
            return False, "本场源码读取累计体积已达上限"
        hard_max = min(hard_max, remain_session)

    full_key = f"{owner}/{repo}".lower()
    target_ref = str(ref or "").strip() or _repo_branch_cache.get(full_key) or "HEAD"
    refs_to_try = [target_ref]
    for fallback_ref in ("HEAD", "main", "master"):
        if fallback_ref not in refs_to_try:
            refs_to_try.append(fallback_ref)

    headers = {"User-Agent": _USER_AGENT}
    token = _resolve_github_token(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    encoded_path = "/".join(quote(seg) for seg in clean_path.split("/"))
    for r in refs_to_try:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(r)}/{encoded_path}"
        try:
            status, body, final_url = _http_get_bytes(
                raw_url,
                headers=headers,
                max_bytes=hard_max,
            )
            if budget is not None:
                budget.add_bytes(len(body), is_raw=True)
            if status == 404:
                continue
            if status != 200:
                return False, f"HTTP {status}"
            text = body.decode("utf-8", errors="replace")
            repo_url = f"https://github.com/{owner}/{repo}"
            blob_url = f"https://github.com/{owner}/{repo}/blob/{r}/{clean_path}"
            if budget is not None:
                budget.record_url(raw_url, final_url, repo_url, blob_url)
                budget.sources_used.add("gh_readme_code")
            else:
                for u in (raw_url, repo_url, blob_url):
                    _module_fetched_urls.add(normalize_source_url(u))
            return True, text
        except Exception as e:
            logger.warning("GitHub raw 读取异常 (%s/%s/%s): %s", owner, repo, clean_path, e)
            return False, str(e)
    return False, f"未找到文件: {owner}/{repo}/{clean_path}"
