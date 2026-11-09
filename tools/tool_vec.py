"""混合检索向量层 — BM25 语义补捞（sqlite-vec 持久化 + numpy 内存兜底）。

铁律对齐：
- 嵌入服务只读 config.yaml 顶层 embedding 段（后台「语义检索 embedding」面板，
  与记忆切片同一处配置）：地址/模型/密钥/超时全由该段决定，这里不做任何
  端点推断。检索行为参数（开关/阈值/裁剪/库路径）走 brain 段 hybrid_* 键
- 稳定性优先：服务不可达/依赖缺失/维度漂移等任何失败都返回 None，
  tool_bm25 侧回退纯词面检索零回归；连续 3 次失败熔断 60s
- 主流程不阻塞：scores_for 默认走快路径，只读缓存查询向量；未命中就入队
  交给后台预热线程（建索引 / 算查询向量），本轮直接 None 回退纯 BM25。
  需要确定性结果的探针/脚本用 blocking=True 显式阻塞
- 语料与 tool_bm25 同源（registry 的 name+description+keywords），
  不引入意图正则分支，检索元数据仍归 registry._TOOL_KEYWORDS
"""

from __future__ import annotations

import hashlib
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..llm_config import BrainConfig, load_brain_config, load_embedding_config

_log = logging.getLogger(__name__)

# 熔断/缓存参数（事故防护，非模型配置，不属于硬编码模型参数）
_FAILS_TO_TRIP = 3        # 连续失败 3 次触发熔断
_TRIP_SECONDS = 60.0      # 熔断 60s 后半开重试
_QUERY_CACHE_MAX = 200    # 查询向量 LRU 上限
_QUERY_CACHE_TTL = 600.0  # 查询向量缓存 10 分钟
_EMBED_BATCH = 32         # 建索引分批大小（云端 embedding 单次条数限制）
_WARM_QUEUE_MAX = 64      # 后台预热队列上限（超出丢弃，下轮自然再入队）
_WARM_IDLE_EXIT_SEC = 600.0  # 预热线程空闲退出时间（有新查询会自动再起）


@dataclass(frozen=True)
class EmbedTarget:
    """工具检索实际使用的嵌入服务（URL / 模型 / 超时 / 向量库路径）。"""

    base_url: str
    api_key: str
    model: str
    timeout_sec: float
    db_path: str


def _kekesi_base() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------- 配置读取（mtime 缓存，支持热更新） ----------

_cfg_cache: dict = {"mtime": -1.0, "cfg": None, "target": None}
_cfg_lock = threading.Lock()


def _config_mtime(base: Path) -> float:
    """与 llm_config.load_brain_config 的读取优先级一致：config.json 优先。"""
    for name in ("config.json", "config.yaml"):
        try:
            p = base / name
            if p.is_file():
                return p.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _resolve_target(emb: dict, db_path: str) -> EmbedTarget | None:
    """从 embedding 段解析嵌入目标；该段未启用或信息不全返回 None。"""
    url = str(emb.get("base_url") or "").strip().rstrip("/")
    model = str(emb.get("model") or "").strip()
    if not (emb.get("enabled") and url and model):
        return None
    try:
        timeout = float(emb.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    return EmbedTarget(
        base_url=url,
        api_key=str(emb.get("api_key") or ""),
        model=model,
        timeout_sec=timeout,
        db_path=db_path,
    )


def _reload_if_stale() -> None:
    """配置文件 mtime 变化时重读 brain + embedding 段（调用方持锁）。"""
    base = _kekesi_base()
    try:
        mtime = _config_mtime(base)
    except OSError:
        mtime = 0.0
    if _cfg_cache["cfg"] is not None and _cfg_cache["mtime"] == mtime:
        return
    try:
        cfg = load_brain_config(base)
        emb = load_embedding_config(base)
    except Exception as e:
        _log.warning("[vec] 配置读取失败，混合检索不可用: %s", e)
        _cfg_cache["target"] = None
        return
    _cfg_cache["mtime"] = mtime
    _cfg_cache["cfg"] = cfg
    _cfg_cache["target"] = _resolve_target(emb, cfg.hybrid_db_path)


def current_config() -> BrainConfig | None:
    """brain 配置（mtime 缓存）；混合检索不可用（未启用/无嵌入服务）返回 None。"""
    with _cfg_lock:
        _reload_if_stale()
        cfg = _cfg_cache["cfg"]
        if cfg is None or not cfg.hybrid_enabled or _cfg_cache["target"] is None:
            return None
        return cfg


def embed_target() -> EmbedTarget | None:
    """当前生效的嵌入服务；混合检索不可用时 None。"""
    return _cfg_cache["target"] if current_config() is not None else None


def hybrid_limits() -> tuple[float, int] | None:
    """(min_score, top_k)；混合检索未启用返回 None。"""
    cfg = current_config()
    if cfg is None:
        return None
    return cfg.hybrid_min_score, cfg.hybrid_top_k


# ---------- 嵌入服务调用（OpenAI 兼容 /v1/embeddings） ----------

def _embed_texts(texts: list[str], target: EmbedTarget) -> list[list[float]] | None:
    if not texts:
        return []
    if not target.base_url:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        _log.warning("[vec] openai SDK 缺失，混合检索不可用")
        return None
    try:
        client = OpenAI(
            api_key=target.api_key or "local-no-key",
            base_url=target.base_url,
            timeout=float(target.timeout_sec) or None,
        )
        resp = client.embeddings.create(model=target.model, input=texts)
        data = [list(d.embedding) for d in (resp.data or [])]
        if len(data) != len(texts) or not any(any(x != 0.0 for x in v) for v in data):
            return None
        return data
    except Exception as e:
        _log.info("[vec] 嵌入服务调用失败: %s", e)
        return None


def _embed_all(texts: list[str], target: EmbedTarget) -> list[list[float]] | None:
    """全量建索引时分批嵌入；任一批失败即整体失败（调用方回退纯 BM25）。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        chunk = texts[i : i + _EMBED_BATCH]
        part = _embed_texts(chunk, target)
        if part is None or len(part) != len(chunk):
            return None
        out.extend(part)
    return out


def _normalize(vec) -> "list":
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr


def _serialize_vec(vec) -> bytes:
    """float32 向量 → 二进制 blob（兼容 sqlite-vec 各版本命名，缺失时回退 numpy）。"""
    try:
        import sqlite_vec  # type: ignore

        fn = getattr(sqlite_vec, "serialize_float32", None) or getattr(
            sqlite_vec, "serialize_float", None
        )
        if fn is not None:
            return fn(list(map(float, vec)))
    except Exception:
        pass
    import numpy as np

    return np.asarray(vec, dtype="<f4").tobytes()


def _deserialize_vec(blob) -> list:
    """二进制 blob → float32 列表（兼容 sqlite-vec 各版本命名，缺失时回退 numpy）。"""
    try:
        import sqlite_vec  # type: ignore

        fn = getattr(sqlite_vec, "deserialize_float32", None) or getattr(
            sqlite_vec, "deserialize_float", None
        )
        if fn is not None:
            return [float(x) for x in fn(blob)]
    except Exception:
        pass
    import numpy as np

    return np.frombuffer(bytes(blob), dtype="<f4").tolist()


# ---------- 向量索引（sqlite-vec 主引擎 + NumPy 备用引擎自动降级兜底） ----------

class _VecIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()          # 只保护内存字典的短临界区
        self._build_lock = threading.Lock()    # 建索引串行化（含网络，慢）
        self._warm_lock = threading.Lock()     # 预热队列 / 线程生命周期
        self._store: dict = {}        # name -> 归一化向量（numpy 数组）
        self._fingerprint = ""         # 语料指纹|嵌入模型
        self._fails = 0
        self._tripped_until = 0.0
        self._qvec_cache: dict = {}    # 查询文本 -> (时间戳, 归一化向量)
        self._db_state = ""            # ""=未探, "sqlite_vec", "numpy"
        self.active_engine = "sqlite_vec"
        self.degrade_reason = ""
        self._force_numpy = False      # 仅测试/故障演练开关
        self._warm_q: "queue.Queue[tuple[str, EmbedTarget]]" = queue.Queue(
            maxsize=_WARM_QUEUE_MAX
        )
        self._warm_pending: set[str] = set()   # 已入队未处理完的查询文本
        self._warm_thread: threading.Thread | None = None

    def engine_status(self) -> dict:
        return {
            "active_engine": self.active_engine,
            "degrade_reason": self.degrade_reason,
            "db_state": self._db_state,
            "indexed_tools": len(self._store),
        }

    def _mark_degraded(self, reason: str) -> None:
        prev = self.active_engine
        self.active_engine = "numpy"
        self._db_state = "numpy"
        self.degrade_reason = str(reason or "sqlite-vec unavailable")
        if prev != "numpy":
            _log.warning(
                "[vec] sqlite-vec 不可用 (%s)，已自动降级至 NumPy 备用引擎",
                self.degrade_reason,
            )

    def _mark_recovered(self) -> None:
        was_degraded = self.active_engine == "numpy" or bool(self.degrade_reason)
        self.active_engine = "sqlite_vec"
        self._db_state = "sqlite_vec"
        self.degrade_reason = ""
        if was_degraded:
            _log.info("[vec] sqlite-vec 已恢复正常，重新切回 sqlite-vec 主引擎")

    # ---- sqlite-vec 持久化 & shadow 表复用 ----

    def _resolve_db_path(self, target: EmbedTarget) -> Path | None:
        if not str(target.db_path or "").strip():
            return None
        db_path = Path(target.db_path)
        if not db_path.is_absolute():
            db_path = _kekesi_base() / db_path
        return db_path

    def _open_db(self, target: EmbedTarget):
        if self._force_numpy:
            self._mark_degraded("forced numpy fallback")
            return None
        try:
            import sqlite_vec  # type: ignore
        except ImportError as e:
            self._mark_degraded(f"ImportError: {e}")
            return None
        db_path = self._resolve_db_path(target)
        if db_path is None:
            self._mark_degraded("hybrid_db_path 未配置")
            return None
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=15000")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            self._mark_recovered()
            return conn
        except Exception as e:
            self._mark_degraded(f"load_extension failed: {e}")
            return None

    def _load_from_shadow_db(self, target: EmbedTarget, fingerprint: str):
        """NumPy 备用引擎：直接复用现有 tool_vec.db 的 vec0 底层存储表，不建独立副本。"""
        db_path = self._resolve_db_path(target)
        if db_path is None or not db_path.is_file():
            return None
        try:
            import numpy as np

            with sqlite3.connect(str(db_path), timeout=15) as conn:
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='tool_vec_meta'"
                ).fetchone()
                if not has_meta:
                    return None
                row = conn.execute(
                    "SELECT value FROM tool_vec_meta WHERE key='fingerprint'"
                ).fetchone()
                if not row or row[0] != fingerprint:
                    return None
                dim_row = conn.execute(
                    "SELECT value FROM tool_vec_meta WHERE key='dim'"
                ).fetchone()
                if not dim_row or int(dim_row[0]) <= 0:
                    return None
                dim = int(dim_row[0])
                blocks = dict(
                    conn.execute(
                        "SELECT rowid, vectors FROM tool_vec_vector_chunks00"
                    ).fetchall()
                )
                rrows = conn.execute(
                    "SELECT rowid, id, chunk_id, chunk_offset FROM tool_vec_rowids"
                ).fetchall()
                out = {}
                for rowid, text_id, blk_id, offset in rrows:
                    name = str(text_id if text_id is not None else rowid)
                    blob = blocks.get(blk_id)
                    if blob is None:
                        continue
                    byte_off = int(offset) * dim * 4
                    if byte_off + dim * 4 > len(blob):
                        continue
                    vec = np.frombuffer(blob, dtype="<f4", count=dim, offset=byte_off)
                    out[name] = _normalize(vec)
                return out if out else None
        except Exception as e:
            _log.warning("[vec] NumPy 备用引擎读取现有向量表失败: %s", e)
            return None

    def _load_from_db(self, conn, fingerprint: str):
        """指纹一致时载入全量向量；否则返回 None（触发重建）。"""
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tool_vec_meta("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            row = conn.execute(
                "SELECT value FROM tool_vec_meta WHERE key='fingerprint'"
            ).fetchone()
            if not row or row[0] != fingerprint:
                return None
            dim_row = conn.execute(
                "SELECT value FROM tool_vec_meta WHERE key='dim'"
            ).fetchone()
            if not dim_row:
                return None
            dim = int(dim_row[0])
            if dim <= 0:
                return None
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS tool_vec USING vec0("
                f"tool_name TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
            )
            rows = conn.execute("SELECT tool_name, embedding FROM tool_vec").fetchall()
            out = {name: _normalize(_deserialize_vec(blob)) for name, blob in rows}
            return out if out else None
        except Exception as e:
            _log.warning("[vec] 向量库读取失败: %s", e)
            return None

    def _save_to_db(self, conn, fingerprint: str, dim: int, vectors: dict) -> bool:
        try:
            conn.execute("DROP TABLE IF EXISTS tool_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE tool_vec USING vec0("
                f"tool_name TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
            )
            for name, vec in vectors.items():
                conn.execute(
                    "INSERT INTO tool_vec(tool_name, embedding) VALUES(?, ?)",
                    (name, _serialize_vec(vec)),
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tool_vec_meta("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute("DELETE FROM tool_vec_meta")
            conn.execute(
                "INSERT INTO tool_vec_meta(key, value) VALUES('fingerprint', ?)",
                (fingerprint,),
            )
            conn.execute(
                "INSERT INTO tool_vec_meta(key, value) VALUES('dim', ?)",
                (str(dim),),
            )
            conn.commit()
            return True
        except Exception as e:
            _log.warning("[vec] 向量库写入失败（下次启动将重建）: %s", e)
            return False

    # ---- 语料与索引 ----

    def _corpus(self) -> tuple[dict[str, str], str]:
        """与 tool_bm25._tool_corpus 同源：name+description+keywords。"""
        from .registry import get_tool, get_tool_names, register_builtin_tools

        register_builtin_tools()
        texts: dict[str, str] = {}
        for name in get_tool_names():
            tool = get_tool(name) or {}
            blob = " ".join(
                part
                for part in (name, tool.get("description", ""), tool.get("keywords", ""))
                if part
            )
            texts[name] = blob
        joined = "|".join(f"{n}:{texts[n]}" for n in sorted(texts))
        sha = hashlib.sha1(joined.encode("utf-8")).hexdigest()
        return texts, sha

    def _ensure(self, target: EmbedTarget, *, force: bool = False) -> bool:
        """确保索引就绪；失败返回 False（调用方回退纯 BM25）。

        force=True 忽略指纹与向量库缓存，全部重新嵌入并回写（会话结束重建用）。
        网络/磁盘部分全部在 _build_lock 内串行完成，提交只替换内存字典，
        快路径读到的要么是旧索引要么是新索引，不会看到半成品。
        """
        with self._build_lock:
            texts, sha = self._corpus()
            fingerprint = f"{sha}|{target.model}"
            if not force and self._store and self._fingerprint == fingerprint:
                if self.active_engine == "numpy" and not self._force_numpy:
                    probe = self._open_db(target)
                    if probe is not None:
                        probe.close()
                return True
            conn = self._open_db(target)
            vectors = None
            try:
                if not force:
                    if conn is not None:
                        vectors = self._load_from_db(conn, fingerprint)
                    if vectors is None:
                        vectors = self._load_from_shadow_db(target, fingerprint)
                rebuilt = vectors is None
                if rebuilt:
                    names = sorted(texts)
                    data = _embed_all([texts[n] for n in names], target)
                    if data is None:
                        return False
                    vectors = dict(zip(names, data))
                    dim = len(next(iter(vectors.values())))
                    if conn is not None:
                        self._save_to_db(conn, fingerprint, dim, vectors)
                self._commit_index(fingerprint, vectors)
                if rebuilt:
                    _log.info(
                        "[vec] 工具向量索引已重建：%d 工具 dim=%d 模型=%s 引擎=%s",
                        len(vectors), dim, target.model, self.active_engine,
                    )
                return True
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _commit_index(self, fingerprint: str, vectors: dict) -> None:
        """提交索引（归一化在锁外做完，锁内只换引用与失效缓存）。"""
        store = {n: _normalize(v) for n, v in vectors.items()}
        with self._lock:
            self._store = store
            self._fingerprint = fingerprint
            # 嵌入模型换了，旧查询向量作废
            self._qvec_cache.clear()

    def _cached_query(self, key: str):
        """只读查询向量缓存；过期/未命中返回 None（不发网络请求）。"""
        with self._lock:
            cached = self._qvec_cache.get(key)
            if cached and time.time() - cached[0] <= _QUERY_CACHE_TTL:
                return cached[1]
        return None

    def _embed_query(self, key: str, target: EmbedTarget):
        """查询文本嵌入（缓存命中免网络）。"""
        cached = self._cached_query(key)
        if cached is not None:
            return cached
        data = _embed_texts([key], target)
        if data is None:
            return None
        qvec = _normalize(data[0])
        with self._lock:
            if len(self._qvec_cache) >= _QUERY_CACHE_MAX:
                oldest = min(self._qvec_cache, key=lambda k: self._qvec_cache[k][0])
                self._qvec_cache.pop(oldest, None)
            self._qvec_cache[key] = (time.time(), qvec)
        return qvec

    # ---- 后台预热（建索引 / 算查询向量，主流程不等） ----

    def _schedule_warm(self, key: str, target: EmbedTarget) -> None:
        """把查询文本交给后台预热线程；同文本在途不重复入队。"""
        with self._warm_lock:
            if key in self._warm_pending:
                return
            thread = self._warm_thread
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._warm_loop, name="tool-vec-warm", daemon=True
                )
                self._warm_thread = thread
                thread.start()
            try:
                self._warm_q.put_nowait((key, target))
            except queue.Full:
                return
            self._warm_pending.add(key)

    def _warm_loop(self) -> None:
        while True:
            try:
                key, target = self._warm_q.get(timeout=_WARM_IDLE_EXIT_SEC)
            except queue.Empty:
                # 空闲超时自己退出，下次有查询会重新拉起，避免常驻空转
                with self._warm_lock:
                    if self._warm_q.empty():
                        self._warm_thread = None
                        return
                continue
            try:
                self._warm_one(key, target)
            except Exception as e:
                _log.info("[vec] 后台预热异常: %s", e)
            finally:
                with self._warm_lock:
                    self._warm_pending.discard(key)
                self._warm_q.task_done()

    def _warm_one(self, key: str, target: EmbedTarget) -> None:
        """预热一轮：索引未就绪先建索引，再缓存该查询的向量。"""
        if time.time() < self._tripped_until:
            return
        if not self._ensure(target):
            self._record_fail()
            return
        if self._embed_query(key, target) is None:
            self._record_fail()
            return
        with self._lock:
            self._fails = 0

    def rebuild(self, target: EmbedTarget) -> None:
        """强制重建索引（会话结束时调用，后台线程执行）。"""
        try:
            if self._ensure(target, force=True):
                _log.info("[vec] 会话结束：工具向量索引已重建（%d 工具）", len(self._store))
            else:
                _log.warning("[vec] 会话结束重建失败（下轮按需再建）")
        except Exception as e:
            _log.warning("[vec] 会话结束重建异常: %s", e)

    # ---- 相似度 ----

    def _similarities(self, qvec, tool_names: list) -> dict | None:
        """用内存索引算余弦相似度（无网络、无阻塞）。"""
        store = self._store
        names = [n for n in tool_names if n in store]
        if not names:
            return None
        import numpy as np

        mat = np.stack([store[n] for n in names])
        sims = mat @ np.asarray(qvec, dtype=np.float32)
        return {n: float(s) for n, s in zip(names, sims)}

    def scores_for(
        self, user_text: str, tool_names: list, *, blocking: bool = False
    ) -> dict | None:
        """返回 {tool_name: 余弦相似度}；不可用/失败返回 None（回退纯 BM25）。

        blocking=False（默认，主流程）：只读缓存，未命中入队后台预热并返回 None，
        调用方本轮走纯 BM25，下一轮/下一阶段同名查询即可命中缓存。
        blocking=True（探针/脚本）：同步建索引 + 同步算查询向量。
        """
        if current_config() is None:
            return None
        # 持 self._lock 期间不得再取配置锁，target 先在锁外解析
        target = embed_target()
        if target is None:
            return None
        if time.time() < self._tripped_until:
            return None
        key = (user_text or "").strip()
        if not key:
            return None
        if blocking:
            return self._scores_blocking(key, tool_names, target)
        return self._scores_fast(key, tool_names, target)

    def _scores_fast(self, key: str, tool_names: list, target: EmbedTarget) -> dict | None:
        qvec = self._cached_query(key)
        if qvec is not None:
            try:
                sims = self._similarities(qvec, tool_names)
            except Exception as e:
                _log.warning("[vec] 相似度计算异常（回退纯 BM25）: %s", e)
                return None
            if sims is not None:
                return sims
        self._schedule_warm(key, target)
        return None

    def _scores_blocking(
        self, key: str, tool_names: list, target: EmbedTarget
    ) -> dict | None:
        if not self._ensure(target):
            return self._record_fail()
        qvec = self._embed_query(key, target)
        if qvec is None:
            return self._record_fail()
        with self._lock:
            self._fails = 0
        try:
            return self._similarities(qvec, tool_names)
        except Exception as e:
            _log.warning("[vec] 相似度计算异常（回退纯 BM25）: %s", e)
            return None

    def _record_fail(self):
        with self._lock:
            self._fails += 1
            if self._fails >= _FAILS_TO_TRIP:
                self._tripped_until = time.time() + _TRIP_SECONDS
                self._fails = 0
                _log.warning(
                    "[vec] 嵌入服务连续失败 %d 次，熔断 %.0fs（期间纯 BM25）",
                    _FAILS_TO_TRIP, _TRIP_SECONDS,
                )
        return None


_index: _VecIndex | None = None
_index_lock = threading.Lock()


def _get_index() -> _VecIndex:
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = _VecIndex()
    return _index


def vec_scores_for(user_text: str, tool_names: list, *, blocking: bool = False) -> dict | None:
    """模块级入口：{tool_name: 相似度}；不可用时 None（调用方回退纯 BM25）。"""
    return _get_index().scores_for(user_text, tool_names, blocking=blocking)


def rebuild_index_async() -> bool:
    """会话结束（唤醒→再见）后强制重建工具向量索引，供下次唤醒直接用。

    后台线程执行、不阻塞调用方；混合检索未启用或嵌入服务不可用时返回 False。
    """
    target = embed_target()
    if target is None:
        return False
    idx = _get_index()
    threading.Thread(
        target=idx.rebuild, args=(target,), name="tool-vec-rebuild", daemon=True
    ).start()
    return True
