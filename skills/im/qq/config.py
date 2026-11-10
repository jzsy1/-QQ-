"""QQ 插件配置：只读取 kekesi/config.yaml 的 qq 段。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def kekesi_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").is_file():
            return parent
    return here.parents[2]


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value, default, low=0, high=10_000):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _str_tuple(value, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(items) if items else default


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


@dataclass(frozen=True)
class QQConfig:
    enabled: bool = True
    # NapCat 正向 WebSocket，如 ws://127.0.0.1:3001
    ws_url: str = ""
    access_token: str = ""
    reconnect_sec: float = 5.0
    # 白名单：空 = 全部群/全部私聊；填 QQ 群号 / QQ 号
    groups: tuple[str, ...] = ()
    privates: tuple[str, ...] = ()
    # 超管（可用 /bind）；不在名单里发命令一律沉默忽略
    super_users: tuple[str, ...] = ()
    # 主人 QQ（群里不 @ 也接话，回复可用主人称呼）
    owner_qqs: tuple[str, ...] = ()
    # 人设：QQ 只读自己的 qq 段，不再回落 live.agent_id
    agent_id: str = "keks"
    persona_id: str = ""
    system_prompt: str = ""
    # 独立记忆命名空间：QQ 用户数据只写这里，不碰直播观众记忆
    memory_dir: str = ""
    memory_namespace: str = "qq"
    # 回复清洗参数
    wake_words: tuple[str, ...] = ("小金鱼",)
    sensitive_words: tuple[str, ...] = ()
    owner_names: tuple[str, ...] = ("鱼片",)
    # 群聊被 @/引用后的唤醒窗口，窗口内不 @ 也接话
    wake_window_sec: float = 120.0
    # 攒消息：第一条进批后等 batch_ms 再一起回，最多 batch_size 条
    batch_ms: float = 3000.0
    batch_size: int = 3
    pending_max: int = 6
    # 两条回复之间最小间隔
    reply_gap_ms: float = 2500.0
    max_reply_chars: int = 120
    request_timeout_sec: float = 30.0
    # 同一聊天里完全相同的消息在这个窗口内不重复触发
    duplicate_sec: float = 5.0
    # 会话内滚动记忆：每个聊天保留最近 N 轮问答直接进上下文，0=关
    history_turns: int = 4
    # 记忆维护线程间隔（秒）：补向量 / TTL 清理 / 画像事实批量抽取 / 7天日记
    maintain_interval_sec: float = 1800.0
    # -- 表情包 ----------------------------------------------------------
    # 库在插件 stickers/ 下：账号收藏表情同步进来 + 搜到的 + 群友发的，index.json 记关键词
    sticker_enabled: bool = True
    # 无关键词命中时随机发图概率（0~1，0=只在命中关键词时发）
    sticker_chance: float = 0.25
    # 同一聊天两次表情包最小间隔（秒），刷屏保护
    sticker_cooldown_sec: float = 120.0
    # 本地库没命中关键词时，去 QQ 官方表情搜索现搜一张并入库
    sticker_search: bool = True
    # 群友发来的 QQ 表情（带公开地址+表情名的那种）顺手收进库
    sticker_harvest: bool = True
    # -- 收图理解 ----------------------------------------------------------
    # 收到图片时取字节交视觉模型转成文字描述再进大脑；
    # key/base_url 默认复用 live.embedding 的 siliconflow 配置
    vision_enabled: bool = True
    vision_model: str = "Qwen/Qwen3-VL-30B-A3B-Instruct"
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_timeout_sec: float = 20.0
    # 单条消息最多看几张图（其余忽略）
    vision_max_images: int = 2
    # 送模型前先压到 this 大小/边长以内（base64 还会再胀 1/3）
    vision_max_bytes: int = 800_000
    vision_max_edge: int = 1024
    # -- 独立记忆向量与切片 ----------------------------------------------------
    # 只读 qq.embedding，其次顶层 embedding（通用配置），绝不读 live.embedding
    embedding_enabled: bool = True
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_timeout_sec: float = 10.0
    vector_chunk_chars: int = 300
    vector_chunk_overlap: int = 60
    vector_top_k: int = 3
    vector_min_score: float = 0.45


def load_config() -> QQConfig:
    path = kekesi_root() / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("读取 qq 配置失败，使用默认值", exc_info=True)
        raw = {}
    section = raw.get("qq") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        section = {}

    ws_url = str(section.get("ws_url") or "").strip()
    agent_id = str(section.get("agent_id") or "").strip()

    # QQ 自己的 embedding 段优先；其次用通用顶层 embedding；不读 live 段。
    top_emb = raw.get("embedding") if isinstance(raw, dict) else None
    top_emb = top_emb if isinstance(top_emb, dict) else {}
    qq_emb = section.get("embedding")
    qq_emb = qq_emb if isinstance(qq_emb, dict) else {}
    emb = qq_emb if (isinstance(qq_emb, dict) and qq_emb) else top_emb

    vision_base = str(section.get("vision_base_url") or "").strip()
    vision_key = str(section.get("vision_api_key") or "").strip()
    emb_model = str(qq_emb.get("model") or top_emb.get("model") or "").strip()
    emb_base = str(qq_emb.get("base_url") or top_emb.get("base_url") or "").strip()
    emb_key = str(qq_emb.get("api_key") or top_emb.get("api_key") or "").strip()
    emb_enabled = _bool(
        qq_emb.get("enabled", top_emb.get("enabled", True)), True
    )

    vec = section.get("vectors") if isinstance(section.get("vectors"), dict) else (
        raw.get("memory", {}).get("vectors", {}) if isinstance(raw.get("memory"), dict) else {}
    )

    return QQConfig(
        enabled=bool(section.get("enabled", True)),
        ws_url=ws_url,
        access_token=str(section.get("access_token") or "").strip(),
        reconnect_sec=max(1.0, _number(section.get("reconnect_sec"), 5.0)),
        groups=_str_tuple(section.get("groups"), ()),
        privates=_str_tuple(section.get("privates"), ()),
        super_users=_str_tuple(section.get("super_users"), ()),
        owner_qqs=_str_tuple(section.get("owner_qqs"), ()),
        agent_id=agent_id,
        persona_id=str(section.get("persona_id") or "").strip(),
        system_prompt=str(section.get("system_prompt") or "").strip(),
        memory_dir=str(section.get("memory_dir") or "").strip(),
        memory_namespace=str(section.get("memory_namespace") or "qq").strip(),
        wake_words=_str_tuple(section.get("wake_words"), ()),
        sensitive_words=_str_tuple(section.get("sensitive_words"), ()),
        owner_names=_str_tuple(section.get("owner_names"), ()),
        wake_window_sec=max(0.0, _number(section.get("wake_window_sec"), 120.0)),
        batch_ms=max(200.0, _number(section.get("batch_ms"), 3000.0)),
        batch_size=_integer(section.get("batch_size"), 3, 1, 10),
        pending_max=_integer(section.get("pending_max"), 6, 1, 20),
        reply_gap_ms=max(0.0, _number(section.get("reply_gap_ms"), 2500.0)),
        max_reply_chars=_integer(section.get("max_reply_chars"), 120, 20, 500),
        request_timeout_sec=max(5.0, _number(section.get("request_timeout_sec"), 30.0)),
        duplicate_sec=max(0.0, _number(section.get("duplicate_sec"), 5.0)),
        history_turns=_integer(section.get("history_turns"), 4, 0, 10),
        maintain_interval_sec=max(300.0, _number(
            section.get("maintain_interval_sec"), 1800.0)),
        sticker_enabled=_bool(section.get("sticker_enabled"), True),
        sticker_chance=min(1.0, max(0.0, _number(
            section.get("sticker_chance"), 0.25))),
        sticker_cooldown_sec=max(0.0, _number(
            section.get("sticker_cooldown_sec"), 120.0)),
        sticker_search=_bool(section.get("sticker_search"), True),
        sticker_harvest=_bool(section.get("sticker_harvest"), True),
        vision_enabled=_bool(section.get("vision_enabled"), True),
        vision_model=str(section.get("vision_model") or "").strip(),
        vision_base_url=vision_base or str(emb.get("base_url") or "").strip(),
        vision_api_key=vision_key or str(emb.get("api_key") or "").strip(),
        vision_timeout_sec=max(3.0, _number(
            section.get("vision_timeout_sec"), 20.0)),
        vision_max_images=_integer(section.get("vision_max_images"), 2, 1, 4),
        vision_max_bytes=_integer(section.get("vision_max_bytes"),
                                  800_000, 50_000, 4_000_000),
        vision_max_edge=_integer(section.get("vision_max_edge"), 1024, 256, 2048),
        embedding_enabled=emb_enabled,
        embedding_model=emb_model,
        embedding_base_url=emb_base,
        embedding_api_key=emb_key,
        embedding_timeout_sec=max(1.0, _number(
            qq_emb.get("timeout_sec", top_emb.get("timeout_sec")), 10.0)),
        vector_chunk_chars=_integer(vec.get("chunk_chars"), 300, 50, 2000),
        vector_chunk_overlap=_integer(vec.get("chunk_overlap"), 60, 0, 500),
        vector_top_k=_integer(vec.get("top_k"), 3, 1, 20),
        vector_min_score=min(1.0, max(0.0, _number(vec.get("min_score"), 0.45))),
    )
