"""Secrets Store — API Key / 密码 / token 的安全存储。

规范 PART X：
- 不得继续写入 assistants.json
- 迁移到：环境变量 / Windows Credential Manager / 加密 Secrets Store
- Audit 记录禁止写入密码/token/API Key

优先级：
1. 环境变量（最高优先级，进程级注入）
2. Windows Credential Manager（通过 keyring，可选依赖）
3. DPAPI 加密的本地 SQLite 存储（fail-soft 兜底）

任何一层失败都自动降级到下一层，绝不抛出导致主程序崩溃的异常。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认值集中管理（零硬编码：可被构造参数覆盖）
_DEFAULT_DB_NAME = "secrets.db"
_DEFAULT_DB_PARENT = "data"
_DEFAULT_KEYRING_SERVICE = "kekesi"
_DPAPI_ENTROPY = b"kekesi-secrets-v1"  # DPAPI 附加熵，阻止其他进程解密

# 永不记录到日志/审计的敏感字段名（小写匹配）
_SENSITIVE_KEY_NAMES = frozenset({
    "password", "api_key", "apikey", "token", "secret",
    "access_token", "refresh_token", "key_material", "private_key",
})


def _is_sensitive_key(key: str) -> bool:
    """判断 key 是否属于敏感字段（用于审计脱敏）。"""
    if not isinstance(key, str):
        return False
    return key.strip().lower() in _SENSITIVE_KEY_NAMES


def _mask_key_name(key: str) -> str:
    """对外暴露时仅显示前 2 位 + ***，避免完整 key 进入日志。"""
    if not isinstance(key, str) or not key:
        return ""
    if len(key) <= 4:
        return key[:1] + "***"
    return key[:2] + "***" + key[-1:]


class SecretsStore:
    """统一的 Secrets 访问入口。

    使用方式：
        store = SecretsStore(base_dir=Path("g:/kks/kekesi"))
        store.set("llm_api_key", "sk-xxxxx", source="user_config")
        val = store.get("llm_api_key")  # 自动尝试 env → keyring → dpapi
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        keyring_service: str = _DEFAULT_KEYRING_SERVICE,
        enable_keyring: bool = True,
        enable_dpapi: bool = True,
    ) -> None:
        # 解析 db 路径：优先 db_path 参数，其次 base_dir/data/secrets.db
        if db_path is not None:
            self._db_path = Path(db_path)
        elif base_dir is not None:
            base = Path(base_dir)
            self._db_path = base / _DEFAULT_DB_PARENT / _DEFAULT_DB_NAME
        else:
            self._db_path = Path(_DEFAULT_DB_PARENT) / _DEFAULT_DB_NAME

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._keyring_service = str(keyring_service or _DEFAULT_KEYRING_SERVICE)
        self._enable_keyring = bool(enable_keyring)
        self._enable_dpapi = bool(enable_dpapi)
        self._lock = threading.Lock()
        self._keyring_backend: Any = None  # 懒加载

        if self._enable_dpapi:
            self._ensure_db()

        # keyring 懒加载，失败不阻塞
        if self._enable_keyring:
            self._keyring_backend = self._load_keyring()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def get(self, key: str, *, default: str | None = None) -> str | None:
        """按优先级读取 secret：env → keyring → dpapi。"""
        if not isinstance(key, str) or not key.strip():
            return default
        key = key.strip()

        # 1. 环境变量（最高优先级，进程级注入，最安全）
        env_val = self._read_env(key)
        if env_val:
            logger.debug("secrets.get[%s] hit env", _mask_key_name(key))
            return env_val

        # 2. Windows Credential Manager
        if self._keyring_backend is not None:
            kr_val = self._read_keyring(key)
            if kr_val:
                logger.debug("secrets.get[%s] hit keyring", _mask_key_name(key))
                return kr_val

        # 3. DPAPI 加密本地存储
        if self._enable_dpapi:
            dpapi_val = self._read_dpapi(key)
            if dpapi_val:
                logger.debug("secrets.get[%s] hit dpapi", _mask_key_name(key))
                return dpapi_val

        return default

    def set(
        self,
        key: str,
        value: str,
        *,
        source: str = "manual",
        persist: bool = True,
    ) -> bool:
        """写入 secret。persist=False 时仅写入环境变量会话缓存（不持久化）。

        Returns: True 表示至少一层写入成功。
        """
        if not isinstance(key, str) or not key.strip():
            return False
        if not isinstance(value, str):
            try:
                value = str(value)
            except Exception:
                return False
        key = key.strip()

        ok = False

        # 1. 优先尝试 Windows Credential Manager（跨进程持久化）
        if persist and self._keyring_backend is not None:
            if self._write_keyring(key, value, source):
                ok = True

        # 2. 兜底用 DPAPI 加密本地存储
        if persist and self._enable_dpapi and not ok:
            if self._write_dpapi(key, value, source):
                ok = True

        # 3. 若都不行，仅写入当前进程环境变量（临时方案，进程退出即失效）
        if not ok:
            try:
                os.environ[key] = value
                logger.warning(
                    "secrets.set[%s] persisted to env only (keyring/dpapi unavailable)",
                    _mask_key_name(key),
                )
                ok = True
            except Exception:
                logger.error("secrets.set[%s] all backends failed", _mask_key_name(key), exc_info=True)
                return False

        logger.info("secrets.set[%s] ok source=%s", _mask_key_name(key), source)
        return ok

    def delete(self, key: str) -> bool:
        """删除 secret（从所有层移除）。返回是否至少删过一处。"""
        if not isinstance(key, str) or not key.strip():
            return False
        key = key.strip()
        deleted = False

        # 环境变量不主动删除（可能由系统注入，删除有副作用）
        # 仅清理 keyring 与 dpapi 持久化层

        if self._keyring_backend is not None:
            try:
                self._keyring_backend.delete_password(self._keyring_service, key)
                deleted = True
            except Exception:
                pass  # 不存在视为正常

        if self._enable_dpapi:
            try:
                with self._lock, sqlite3.connect(self._db_path) as conn:
                    conn.execute("DELETE FROM secrets WHERE key = ?", (key,))
                    conn.commit()
                deleted = True
            except Exception:
                logger.debug("secrets.delete[%s] dpapi failed", _mask_key_name(key), exc_info=True)

        if deleted:
            logger.info("secrets.delete[%s] ok", _mask_key_name(key))
        return deleted

    def list_keys(self) -> list[str]:
        """列出所有已持久化的 secret key 名（不返回值）。"""
        keys: set[str] = set()

        if self._enable_dpapi:
            try:
                with self._lock, sqlite3.connect(self._db_path) as conn:
                    rows = conn.execute("SELECT key FROM secrets").fetchall()
                for row in rows:
                    if row and row[0]:
                        keys.add(str(row[0]))
            except Exception:
                logger.debug("secrets.list dpapi failed", exc_info=True)

        if self._keyring_backend is not None:
            try:
                # keyring 没有标准 list 接口，仅尝试已知的可能 key 名
                # 这里不做遍历，依赖 dpapi 的 list 结果即可
                pass
            except Exception:
                pass

        return sorted(keys)

    def has(self, key: str) -> bool:
        """判断 secret 是否存在（不读取值）。"""
        if not isinstance(key, str) or not key.strip():
            return False
        key = key.strip()
        return self.get(key, default=None) is not None

    # ------------------------------------------------------------------
    # 环境变量层
    # ------------------------------------------------------------------

    @staticmethod
    def _read_env(key: str) -> str | None:
        """从环境变量读取，支持若干常见命名变体。"""
        candidates = [
            key,
            key.upper(),
            f"KEKESI_{key.upper()}",
        ]
        # 常见简写映射
        lower = key.lower()
        if "api_key" in lower or "apikey" in lower:
            candidates.extend(["KEKESI_LLM_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"])
        elif "token" in lower:
            candidates.extend(["KEKESI_TOKEN", "API_TOKEN"])

        for name in candidates:
            val = os.environ.get(name, "").strip()
            if val:
                return val
        return None

    # ------------------------------------------------------------------
    # Windows Credential Manager 层
    # ------------------------------------------------------------------

    def _load_keyring(self) -> Any:
        """懒加载 keyring 库。失败返回 None（降级）。"""
        try:
            import keyring  # type: ignore
            # 触发一次 backend 解析，避免运行时崩
            _ = keyring.get_keyring()
            return keyring
        except Exception:
            logger.info("keyring unavailable, fallback to dpapi")
            return None

    def _read_keyring(self, key: str) -> str | None:
        try:
            val = self._keyring_backend.get_password(self._keyring_service, key)
            if val:
                return val
        except Exception:
            logger.debug("keyring.get[%s] failed", _mask_key_name(key), exc_info=True)
        return None

    def _write_keyring(self, key: str, value: str, source: str) -> bool:
        try:
            self._keyring_backend.set_password(self._keyring_service, key, value)
            return True
        except Exception:
            logger.debug("keyring.set[%s] failed", _mask_key_name(key), exc_info=True)
            return False

    # ------------------------------------------------------------------
    # DPAPI 加密本地存储层
    # ------------------------------------------------------------------

    def _ensure_db(self) -> None:
        """初始化 secrets 表。"""
        try:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS secrets (
                        key TEXT PRIMARY KEY,
                        ciphertext BLOB NOT NULL,
                        source TEXT DEFAULT 'manual',
                        updated_at TEXT DEFAULT (datetime('now'))
                    );
                    """
                )
                conn.commit()
        except Exception:
            logger.error("secrets db init failed: %s", self._db_path, exc_info=True)
            self._enable_dpapi = False  # 数据库不可用时禁用本层

    @staticmethod
    def _dpapi_protect(plaintext: str) -> bytes | None:
        """使用 Windows DPAPI 加密。非 Windows 或失败返回 None。"""
        if not isinstance(plaintext, str):
            return None
        try:
            import win32crypt  # type: ignore
            blob = win32crypt.CryptProtectData(
                plaintext.encode("utf-8"),
                None,                       # description
                _DPAPI_ENTROPY,             # entropy
                None,                       # reserved
                None,                       # prompt
                0,                          # flags
            )
            return blob
        except ImportError:
            return None
        except Exception:
            logger.debug("dpapi protect failed", exc_info=True)
            return None

    @staticmethod
    def _dpapi_unprotect(ciphertext: bytes) -> str | None:
        """使用 Windows DPAPI 解密。失败返回 None。"""
        if not isinstance(ciphertext, (bytes, bytearray)):
            return None
        try:
            import win32crypt  # type: ignore
            _desc, plaintext = win32crypt.CryptUnprotectData(
                bytes(ciphertext),
                _DPAPI_ENTROPY,
                None,
                None,
                0,
            )
            return plaintext.decode("utf-8")
        except ImportError:
            return None
        except Exception:
            logger.debug("dpapi unprotect failed", exc_info=True)
            return None

    def _read_dpapi(self, key: str) -> str | None:
        try:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT ciphertext FROM secrets WHERE key = ?",
                    (key,),
                ).fetchone()
            if not row:
                return None
            return self._dpapi_unprotect(row[0])
        except Exception:
            logger.debug("dpapi.read[%s] failed", _mask_key_name(key), exc_info=True)
            return None

    def _write_dpapi(self, key: str, value: str, source: str) -> bool:
        ciphertext = self._dpapi_protect(value)
        if ciphertext is None:
            return False
        try:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO secrets (key, ciphertext, source, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (key, ciphertext, str(source or "manual"), datetime.now().isoformat()),
                )
                conn.commit()
            return True
        except Exception:
            logger.error("dpapi.write[%s] failed", _mask_key_name(key), exc_info=True)
            return False


# ----------------------------------------------------------------------
# 模块级单例（懒加载）
# ----------------------------------------------------------------------

_default_store: SecretsStore | None = None
_default_lock = threading.Lock()


def get_default_store(base_dir: str | Path | None = None) -> SecretsStore:
    """获取模块级默认 SecretsStore 单例。

    首次调用时根据 base_dir 创建；后续调用忽略 base_dir 参数。
    """
    global _default_store
    if _default_store is not None:
        return _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = SecretsStore(base_dir=base_dir)
        return _default_store


def reset_default_store() -> None:
    """重置单例（仅用于测试）。"""
    global _default_store
    with _default_lock:
        _default_store = None


# ----------------------------------------------------------------------
# 兼容现有 _resolve_api_key 的便捷接口
# ----------------------------------------------------------------------

def resolve_secret(key: str, *, default: str | None = None) -> str | None:
    """便捷接口：从默认 store 读取 secret。

    等价于 get_default_store().get(key, default=default)，
    便于 llm_config.py 等模块在不引入 store 实例的情况下使用。
    """
    store = get_default_store()
    return store.get(key, default=default)
