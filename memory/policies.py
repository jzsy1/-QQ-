"""记忆保留策略 — 每层的 TTL / 容量 / 重要度 / 衰减。

规范 PART VII：
    记忆分层：Working / Session / User Profile / Preference / Task History / Experience
    每层有独立的保留策略（TTL、容量、重要度阈值、衰减率）。

定位：
    policies.py 是纯策略定义 + 应用工具，不直接读写数据库。
    MemoryRetrieval / MemoryService 在清理时调用 apply_layer_policy()。

设计原则：
1. 零硬编码：所有阈值通过 LayerPolicy 字段配置，可被 config.yaml 覆盖。
2. 稳定优先：策略应用失败 fail-soft，返回 0 而非抛出。
3. 向后兼容：DEFAULT_POLICIES 提供合理默认值，不强制要求调用方传策略。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ================================================================
# 层级常量（统一命名，避免字符串散落）
# ================================================================

LAYER_WORKING = "working"
LAYER_SESSION = "session"
LAYER_PROFILE = "profile"
LAYER_PREFERENCE = "preference"
LAYER_TASK_HISTORY = "task_history"
LAYER_EXPERIENCE = "experience"

ALL_LAYERS = (
    LAYER_WORKING,
    LAYER_SESSION,
    LAYER_PROFILE,
    LAYER_PREFERENCE,
    LAYER_TASK_HISTORY,
    LAYER_EXPERIENCE,
)


# ================================================================
# 策略数据类
# ================================================================


@dataclass
class LayerPolicy:
    """单层记忆的保留策略。

    Fields:
        ttl_sec: 生存时间（秒）。None 表示永久保留；0 表示即时（不持久化）。
        max_items: 最大条数。None 表示无限。超过时按 oldest/lowest-importance 清理。
        importance_threshold: 重要度下限。低于此值的条目在清理时优先删除。
        decay_rate_per_day: 每天重要度衰减比例（0.0-1.0）。0 表示不衰减。
        cleanup_table: 应用策略时操作的数据库表名（None 表示该层不自动清理）。
        cleanup_order_by: 清理时排序字段（asc 表示先删旧的）。
    """

    ttl_sec: float | None = None
    max_items: int | None = None
    importance_threshold: float = 0.0
    decay_rate_per_day: float = 0.0
    cleanup_table: str | None = None
    cleanup_order_by: str = "id ASC"

    def is_permanent(self) -> bool:
        """是否永久保留（无 TTL）。"""
        return self.ttl_sec is None

    def is_volatile(self) -> bool:
        """是否即时（不持久化）。"""
        return self.ttl_sec == 0

    def ttl_datetime(self, now: datetime | None = None) -> datetime | None:
        """返回 TTL 对应的截止时间（早于此时间的应清理）。None 表示无截止。"""
        if self.ttl_sec is None or self.ttl_sec <= 0:
            return None
        base = now or datetime.now()
        return base - timedelta(seconds=float(self.ttl_sec))

    def decayed_importance(self, original: float, age_days: float) -> float:
        """按天数计算衰减后的重要度。"""
        if self.decay_rate_per_day <= 0:
            return max(0.0, float(original))
        decayed = float(original) * ((1.0 - float(self.decay_rate_per_day)) ** max(0.0, age_days))
        return max(0.0, decayed)


# ================================================================
# 默认策略（可被 config 覆盖）
# ================================================================

DEFAULT_POLICIES: dict[str, LayerPolicy] = {
    # Working Memory：当前对话上下文，即时，最多 20 条
    LAYER_WORKING: LayerPolicy(
        ttl_sec=0,
        max_items=20,
        importance_threshold=0.0,
        decay_rate_per_day=0.0,
        cleanup_table="conversation_digest",
        cleanup_order_by="id ASC",
    ),
    # Session Memory：会话级，1 天后清理，每会话最多 100 条
    LAYER_SESSION: LayerPolicy(
        ttl_sec=86400,
        max_items=100,
        importance_threshold=0.2,
        decay_rate_per_day=0.1,
        cleanup_table="conversation_digest",
        cleanup_order_by="id ASC",
    ),
    # User Profile：永久，不衰减
    LAYER_PROFILE: LayerPolicy(
        ttl_sec=None,
        max_items=None,
        importance_threshold=0.0,
        decay_rate_per_day=0.0,
        cleanup_table="user_profile",
        cleanup_order_by="updated_at ASC",
    ),
    # Preference：永久，最多 200 条，低重要度时清理
    LAYER_PREFERENCE: LayerPolicy(
        ttl_sec=None,
        max_items=200,
        importance_threshold=0.3,
        decay_rate_per_day=0.0,
        cleanup_table="inferred_preferences",
        cleanup_order_by="confidence ASC",
    ),
    # Task History：30 天，最多 2000 条
    LAYER_TASK_HISTORY: LayerPolicy(
        ttl_sec=2592000,
        max_items=2000,
        importance_threshold=0.0,
        decay_rate_per_day=0.0,
        cleanup_table="task_history",
        cleanup_order_by="id ASC",
    ),
    # Experience：永久但衰减，最多 5000 条
    LAYER_EXPERIENCE: LayerPolicy(
        ttl_sec=None,
        max_items=5000,
        importance_threshold=0.15,
        decay_rate_per_day=0.05,
        cleanup_table=None,  # Experience 跨多表，由 MemoryService.cleanup 统一处理
        cleanup_order_by="confidence ASC",
    ),
}


def get_policy(layer: str) -> LayerPolicy:
    """获取指定层的默认策略。未知层返回永久策略（安全默认）。"""
    return DEFAULT_POLICIES.get((layer or "").strip().lower()) or LayerPolicy()


def policies_from_config(config: dict) -> dict[str, LayerPolicy]:
    """从 config.yaml 的 memory.policies 段构造策略字典。

    config 格式：
        memory:
          policies:
            working:
              max_items: 30
            task_history:
              ttl_sec: 604800  # 7 天
    """
    if not isinstance(config, dict):
        return dict(DEFAULT_POLICIES)

    raw = config.get("memory", {}).get("policies", {})
    if not isinstance(raw, dict):
        return dict(DEFAULT_POLICIES)

    result: dict[str, LayerPolicy] = dict(DEFAULT_POLICIES)
    for layer_name, overrides in raw.items():
        if not isinstance(overrides, dict):
            continue
        key = (layer_name or "").strip().lower()
        if key not in DEFAULT_POLICIES:
            continue
        base = DEFAULT_POLICIES[key]
        try:
            result[key] = LayerPolicy(
                ttl_sec=_parse_optional_float(overrides.get("ttl_sec"), base.ttl_sec),
                max_items=_parse_optional_int(overrides.get("max_items"), base.max_items),
                importance_threshold=float(
                    overrides.get("importance_threshold", base.importance_threshold)
                ),
                decay_rate_per_day=float(
                    overrides.get("decay_rate_per_day", base.decay_rate_per_day)
                ),
                cleanup_table=base.cleanup_table,
                cleanup_order_by=base.cleanup_order_by,
            )
        except (TypeError, ValueError):
            logger.warning("策略 %s 配置无效，保留默认", key)
    return result


def _parse_optional_float(val: Any, default: float | None) -> float | None:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_optional_int(val: Any, default: int | None) -> int | None:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ================================================================
# 策略应用工具
# ================================================================


def apply_layer_policy(
    conn,
    policy: LayerPolicy,
    *,
    table: str | None = None,
    timestamp_col: str = "created_at",
    importance_col: str | None = None,
    where_guard: str = "",
    now: datetime | None = None,
) -> int:
    """在给定数据库连接上应用单层策略，返回删除条数。

    Args:
        conn: sqlite3.Connection（调用方负责 commit）
        policy: 该层的策略
        table: 实际表名（默认用 policy.cleanup_table）
        timestamp_col: 时间戳列名（用于 TTL 判断）
        importance_col: 重要度列名（用于阈值过滤，None 表示不按重要度清理）
        where_guard: 额外安全保护条件（如 'summarized = 1 AND important = 0'）
        now: 当前时间（测试可注入）

    Returns: 删除的行数
    """
    target_table = table or policy.cleanup_table
    if not target_table:
        return 0

    base_now = now or datetime.now()
    deleted = 0
    extra_sql = f" AND ({where_guard})" if where_guard else ""

    # 1. TTL 清理：删除早于截止时间的行
    cutoff = policy.ttl_datetime(base_now)
    if cutoff is not None:
        try:
            cur = conn.execute(
                f"DELETE FROM {target_table} WHERE {timestamp_col} < ?{extra_sql}",
                (cutoff.isoformat(),),
            )
            deleted += cur.rowcount if cur.rowcount > 0 else 0
        except Exception:
            logger.debug("TTL 清理 %s 失败", target_table, exc_info=True)

    # 2. 重要度阈值清理（可选）
    if importance_col and policy.importance_threshold > 0:
        try:
            cur = conn.execute(
                f"DELETE FROM {target_table} WHERE {importance_col} < ?{extra_sql}",
                (float(policy.importance_threshold),),
            )
            deleted += cur.rowcount if cur.rowcount > 0 else 0
        except Exception:
            logger.debug("重要度清理 %s 失败", target_table, exc_info=True)

    # 3. 容量清理：保留最新 N 条
    if policy.max_items is not None and policy.max_items > 0:
        try:
            # 反向查询保留的 id 范围
            rows = conn.execute(
                f"SELECT id FROM {target_table} ORDER BY id DESC LIMIT ?",
                (int(policy.max_items),),
            ).fetchall()
            if rows:
                min_keep_id = min(int(r["id"]) for r in rows) if rows else 0
                if min_keep_id > 0:
                    cur = conn.execute(
                        f"DELETE FROM {target_table} WHERE id < ?{extra_sql}",
                        (min_keep_id,),
                    )
                    deleted += cur.rowcount if cur.rowcount > 0 else 0
        except Exception:
            logger.debug("容量清理 %s 失败", target_table, exc_info=True)

    return deleted


def compute_importance(
    base: float,
    *,
    age_days: float = 0.0,
    hit_count: int = 0,
    verified: bool = False,
    decay_rate: float = 0.0,
) -> float:
    """综合计算一条记忆的当前重要度。

    公式：decayed_base + hit_bonus + verified_bonus - age_penalty

    Args:
        base: 原始 confidence（0.0-1.0）
        age_days: 距离创建/上次使用的天数
        hit_count: 被命中/使用次数
        verified: 是否已验证
        decay_rate: 每天衰减比例

    Returns: 0.0-1.0 的重要度分数
    """
    if decay_rate > 0:
        decayed = base * ((1.0 - decay_rate) ** max(0.0, age_days))
    else:
        decayed = base

    hit_bonus = min(0.2, float(hit_count) * 0.02)
    verified_bonus = 0.15 if verified else 0.0

    score = decayed + hit_bonus + verified_bonus
    return max(0.0, min(1.0, score))
