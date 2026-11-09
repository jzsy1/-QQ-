"""QQ 接入编排：NapCat → 白名单/唤醒 → 独立身份 → 独立大脑回复 → 发回。

分层：transport 只管收发，channel 只管解析，identity 只管认人，
brain 只管生成，这里只做白名单、攒批、节流和状态。
记忆使用 QQ 独立命名空间的 CompanionMemory（隔离存储，不污染其他业务）：
先记消息（记不记回复都记），被抽中回应的批再记 reply，session_id 用 qq_ 前缀。
"""

from __future__ import annotations

import atexit
import concurrent.futures
import logging
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path

from . import brain as brain_mod
from . import channel
from . import media as media_mod
from . import memes as memes_mod
from . import music as music_mod
from . import stickers as stickers_mod
from . import vision as vision_mod
from . import weather as weather_mod
from .config import QQConfig, load_config
from .identity import IdentityMap
from .transport import OneBotTransport

logger = logging.getLogger(__name__)

# QQ 单图表情的上限，超过就别发了（上传慢且容易被压糊）；和 stickers._MAX_ADD_BYTES 同值
_MAX_STICKER_BYTES = 4 * 1024 * 1024
# 对方发来的动图收进表情包库的大小上限，再大的更像截图不是表情
_MAX_HARVEST_BYTES = 2 * 1024 * 1024

_SERVICE: "QQService | None" = None
_SERVICE_LOCK = threading.Lock()


def sticker_chance(items, default: float) -> float:
    """对方这轮甩了表情包就必回一张（对等才像朋友斗图），否则按配置概率。"""
    return 1.0 if any(bool(it.get("sticker_in")) for it in items or []) else float(default)


def get_service() -> "QQService":
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = QQService()
            atexit.register(_SERVICE.stop_silent)
        return _SERVICE


def start_qq() -> tuple[bool, str]:
    return get_service().start()


def stop_qq() -> tuple[bool, str]:
    return get_service().stop()


def qq_status() -> tuple[bool, str]:
    return True, get_service().status_text()


def bind_qq(qq_id: str, anchor: str = "") -> tuple[bool, str]:
    return get_service().bind(qq_id, anchor)


def sync_stickers(limit: int = 60) -> tuple[bool, str]:
    return get_service().sync_stickers(limit)


class QQService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._events: "queue.Queue[dict] | None" = None
        self._handler: threading.Thread | None = None
        self._replier: threading.Thread | None = None
        self._transport: OneBotTransport | None = None
        self._cfg: QQConfig | None = None
        self._identity: IdentityMap | None = None
        self._memory = None
        self._self_id = ""
        self._state = "stopped"
        self._started_at = 0.0
        # 群聊唤醒窗口：chat_key -> awake_until
        self._awake: dict[str, float] = {}
        # 攒批：chat_key -> {"msgs": [...], "due": ts}
        self._batches: dict[str, dict] = {}
        self._batch_lock = threading.Lock()
        # 会话内滚动记忆：chat_key -> [(who, text, reply), ...]
        self._history: dict[str, list] = {}
        # 表情包节流：chat_key -> last_ts
        self._sticker_ts: dict[str, float] = {}
        self._maintainer: threading.Thread | None = None
        self._last_sent = 0.0
        # 同聊天相同内容去重：chat_key|uid|text -> ts
        self._seen: dict[str, float] = {}
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._stats = self._empty_stats()
        # 最近一次失败原因（图片取不到 / 视觉接口报错 / 带图发送失败）
        self._last_image_error = ""
        self._last_vision_error = ""
        self._last_sticker_error = ""

    def _note_error(self, slot: str, text: str) -> None:
        text = str(text or "")[:200]
        if not text:
            return
        with self._lock:
            setattr(self, slot, text)

    @staticmethod
    def _bank_text() -> str:
        """表情包库规模：总张数，其中能发的动图几张。"""
        try:
            total = stickers_mod.count()
            gifs = stickers_mod.gif_count()
        except Exception:
            logger.info("表情包库数量读不出", exc_info=True)
            return "库读不出"
        return f"库 {total} 张（动图 {gifs} 张）"

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {
            "received": 0,
            "considered": 0,
            "responded": 0,
            "skipped": 0,
            "failed": 0,
            "batches": 0,
            # 收图理解
            "images_ok": 0,
            "images_fail": 0,
            # 表情包
            "stickers_sent": 0,
            "stickers_fail": 0,
            "sticker_search": 0,
            "stickers_banked": 0,
        }

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + amount

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        cfg = load_config()
        if not cfg.enabled:
            return False, "QQ 插件已禁用（config.yaml 的 qq.enabled 为 false）"
        if not cfg.ws_url:
            return False, "缺少 NapCat 地址：请在 config.yaml 的 qq.ws_url 填 OneBot v11 正向 WebSocket 地址。"

        with self._lock:
            if self._handler is not None and self._handler.is_alive():
                return True, "QQ 接入已在运行。"
            self._stop_event.clear()
            self._cfg = cfg
            self._stats = self._empty_stats()
            self._last_image_error = ""
            self._last_vision_error = ""
            self._last_sticker_error = ""
            self._events = queue.Queue(maxsize=500)
            self._batches = {}
            self._awake = {}
            self._seen = {}
            self._history = {}
            from kekesi_common.companion_memory import CompanionMemory

            qq_dir = self._plugin_dir()
            mem_root = Path(cfg.memory_dir) if cfg.memory_dir else qq_dir / "memory"
            embed_fn = self._build_embed_fn(cfg) if cfg.embedding_enabled else None
            try:
                self._memory = CompanionMemory(
                    root=mem_root,
                    namespace=cfg.memory_namespace or "qq",
                    embed_fn=embed_fn,
                )
                self._memory.chunk_target_chars = int(cfg.vector_chunk_chars or 300)
                self._memory.chunk_overlap_chars = int(cfg.vector_chunk_overlap or 60)
            except Exception as exc:
                self._events = None
                return False, f"初始化 QQ 独立记忆失败：{exc}"

            try:
                self._identity = IdentityMap(
                    data_dir=qq_dir / "data",
                )
            except Exception as exc:
                self._events = None
                return False, f"初始化 QQ 身份管理失败：{exc}"

            self._transport = OneBotTransport(
                cfg.ws_url, cfg.access_token,
                on_event=self._on_event, reconnect_sec=cfg.reconnect_sec,
            )
            self._handler = threading.Thread(
                target=self._handle_loop, name="qq-handler", daemon=True
            )
            self._replier = threading.Thread(
                target=self._reply_loop, name="qq-replier", daemon=True
            )
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="qq-worker"
            )
        self._transport.start()
        self._handler.start()
        self._replier.start()
        # 记忆维护：embedding 开了就拉起补向量后台线程（幂等，重复调不起双份）
        try:
            start_embed = getattr(self._memory, "start_embed_worker", None)
            if callable(start_embed):
                start_embed()
        except Exception:
            logger.info("补向量线程未启动（embedding 未配置时属正常）", exc_info=True)
        self._maintainer = threading.Thread(
            target=self._maintain_loop, name="qq-maintain", daemon=True
        )
        self._maintainer.start()
        with self._lock:
            self._state = "running"
            self._started_at = time.time()
        return (
            True,
            "已开始接 QQ 消息：私聊全响应；群聊被 @ 或引用后接话并保持唤醒窗口。"
            "回复走独立 QQ 大脑与记忆命名空间。",
        )

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self._transport is None:
                return True, "QQ 接入没有在运行。"
        self._shutdown()
        return True, "QQ 接入已停止。"

    def stop_silent(self) -> None:
        try:
            self._shutdown()
        except Exception:
            pass

    def _shutdown(self) -> None:
        with self._lock:
            self._state = "stopped"
            self._stop_event.set()
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False)
        transport = self._transport
        if transport is not None:
            transport.stop()

    @staticmethod
    def _plugin_dir():
        from pathlib import Path

        return Path(__file__).resolve().parent

    # -- 状态 ------------------------------------------------------------
    def status_text(self) -> str:
        with self._lock:
            cfg = self._cfg
            stats = dict(self._stats)
            state = self._state
            started = self._started_at
            transport = self._transport
            identity = self._identity
        if cfg is None:
            return "QQ 接入：未启动过。"
        connected = transport.connected if transport else False
        error = transport.last_error if transport else ""
        parts = [f"QQ 接入：{state}，NapCat {'已连接' if connected else '未连接'}"]
        if started:
            parts.append(f"运行 {int(time.time() - started)} 秒")
        parts.append(
            f"收到 {stats.get('received', 0)}，处理 {stats.get('considered', 0)}，"
            f"回复 {stats.get('responded', 0)}，攒批 {stats.get('batches', 0)}"
        )
        parts.append(
            f"看图 成{stats.get('images_ok', 0)}/败{stats.get('images_fail', 0)}，"
            f"表情包 发{stats.get('stickers_sent', 0)}/搜{stats.get('sticker_search', 0)}"
            f"/败{stats.get('stickers_fail', 0)}，"
            f"收对方表情 {stats.get('stickers_banked', 0)} 张（{self._bank_text()}）"
        )
        with self._lock:
            img_err, vis_err, stk_err = (self._last_image_error,
                                         self._last_vision_error,
                                         self._last_sticker_error)
        for label, text in (("取图", img_err), ("视觉", vis_err), ("表情", stk_err)):
            if text:
                parts.append(f"最近{label}失败：{text}")
        if identity is not None:
            bindings = identity.bindings()
            parts.append(f"绑定 {len(bindings)} 人")
        try:
            from .emotion import get_emotion_engine
            emo_eng = get_emotion_engine()
            mood_parts = [f"{k.replace('qq_', '')}:{v.dominant_mood}(V:{v.valence:+.2f})" for k, v in list(emo_eng._states.items())[:3]]
            if mood_parts:
                parts.append(f"情绪动量: {', '.join(mood_parts)}")
        except Exception:
            pass
        awake = [k for k, v in self._awake.items() if v > time.time()]
        if awake:
            parts.append(f"唤醒中 {len(awake)} 个会话")
        if error:
            parts.append(f"最近错误：{error}")
        return "；".join(parts)

    def bind(self, qq_id: str, anchor: str = "") -> tuple[bool, str]:
        if self._identity is None:
            from .identity import IdentityMap

            self._identity = IdentityMap(
                data_dir=self._plugin_dir() / "data",
            )
        return self._identity.bind(qq_id, anchor)

    # -- 事件流 ----------------------------------------------------------
    def _on_event(self, data: dict) -> None:
        if self._events is None:
            return
        try:
            self._events.put_nowait(data)
        except Exception:
            self._bump("skipped")

    def _handle_loop(self) -> None:
        stop = self._stop_event  # 捕获引用：重启换新事件后旧线程自然退出
        while not stop.is_set():
            try:
                data = self._events.get(timeout=0.5)
            except Exception:
                continue
            self._handle_event(data)

    def _handle_event(self, data: dict) -> None:
        self._bump("received")
        if data.get("post_type") == "meta_event":
            self_id = str((data.get("status") or {}).get("self_id")
                          or data.get("self_id") or "")
            if self_id:
                self._self_id = self_id
            return
        if data.get("post_type") == "notice":
            return
        if not self._self_id:
            self._self_id = str(data.get("self_id") or "")
        cfg = self._cfg
        if cfg is None:
            return
        event = channel.parse_message_event(data, self._self_id)
        if event is None:
            return
        # VIS_IMG_20261102: 收图理解——视觉小模型转一句话再进大脑（异步提交，避免阻塞 _handle_loop）
        # 视觉关了也要下图：对方甩的动图得收进表情包库并标 sticker_in（回表情不靠画面描述）
        if event.get("images") and (cfg.vision_enabled or cfg.sticker_harvest):
            executor = self._executor
            if executor is not None:
                executor.submit(self._async_handle_image_event, event, cfg)
                return
            try:
                event = self._describe_images(event, cfg)
            except Exception:
                logger.warning("同步视觉描述失败", exc_info=True)
        self._finish_handle_event(event, cfg)

    def _async_handle_image_event(self, event: dict, cfg: QQConfig) -> None:
        try:
            event = self._describe_images(event, cfg)
        except Exception:
            logger.warning("异步视觉描述失败", exc_info=True)
        self._finish_handle_event(event, cfg)

    def _finish_handle_event(self, event: dict, cfg: QQConfig) -> None:
        self._bump("considered")
        try:
            self._route(cfg, event)
        except Exception:
            logger.warning("处理 QQ 消息失败", exc_info=True)
            self._bump("skipped")

    def _route(self, cfg: QQConfig, event: dict) -> None:
        # 白名单
        if event["chat_type"] == "group":
            if cfg.groups and event["chat_id"] not in cfg.groups:
                return
        else:
            if cfg.privates and event["user_id"] not in cfg.privates:
                return

        text = event["text"]
        uid = event["user_id"]

        # 超管命令：/bind；非超管发的一律沉默忽略
        if text.startswith("/"):
            self._handle_command(cfg, event)
            return

        # 相同内容去重
        if cfg.duplicate_sec > 0:
            key = f"{event['chat_key']}|{uid}|{text}"
            now = time.time()
            with self._lock:
                last = self._seen.get(key, 0.0)
                self._seen[key] = now
                if len(self._seen) > 500:
                    cutoff = now - max(cfg.duplicate_sec, 60.0) * 2
                    self._seen = {
                        k: v for k, v in self._seen.items() if v > cutoff
                    }
            if now - last < cfg.duplicate_sec:
                return

        is_owner = uid in cfg.owner_qqs
        # 身份合并：认到直播观众就用人家的档案
        person_id = ""
        matched_by = ""
        try:
            person_id, matched_by = self._identity.resolve(
                uid, event.get("nickname") or event["user_name"],
                card=event.get("card") or "",
            )
        except Exception:
            logger.warning("身份解析失败", exc_info=True)
        person_id = person_id or f"qq_{uid}"

        session_id = f"qq_{datetime.now().strftime('%Y-%m-%d')}"
        try:
            self._memory.record_viewer_event(
                person_id, event["user_name"], "chat", text,
                session_id=session_id, message_id=event["message_id"] or None,
            )
        except Exception:
            logger.warning("记 QQ 消息失败", exc_info=True)

        if event["chat_type"] == "group":
            woken_by_name = bool(cfg.wake_words and any(w in text for w in cfg.wake_words))
            respond = bool(event["at_me"] or event["reply_me"] or woken_by_name)
            if respond:
                if cfg.wake_window_sec > 0:
                    self._awake[event["chat_key"]] = (
                        time.time() + cfg.wake_window_sec
                    )
            elif is_owner:
                respond = True
            else:
                respond = time.time() < self._awake.get(event["chat_key"], 0.0)
        else:
            respond = True
        if not respond:
            return

        with self._batch_lock:
            batch = self._batches.setdefault(
                event["chat_key"], {"msgs": [], "due": 0.0}
            )
            batch["msgs"].append({
                "user_id": uid,
                "user_name": event["user_name"],
                "person_id": person_id,
                "text": text,
                "sticker_in": bool(event.get("sticker_in")),
                "message_id": str(event.get("message_id") or ""),
                "is_owner": is_owner,
                "ts": time.time(),
                "session_id": session_id,
                "chat_type": event["chat_type"],
                "chat_id": event["chat_id"],
            })
            if len(batch["msgs"]) == 1:
                batch["due"] = time.time() + cfg.batch_ms / 1000.0
            elif len(batch["msgs"]) > cfg.pending_max:
                batch["msgs"].pop(0)

    # -- 命令 ------------------------------------------------------------
    def _handle_command(self, cfg: QQConfig, event: dict) -> None:
        if event["user_id"] not in cfg.super_users:
            return  # 沉默拒绝
        text = event["text"].strip()
        if text.startswith("/bind"):
            parts = text.split()
            qq_id = parts[1] if len(parts) > 1 else event["user_id"]
            anchor = parts[2] if len(parts) > 2 else ""
            ok, msg = self._identity.bind(qq_id, anchor)
            self._send(event["chat_type"], event["chat_id"], msg)
            return
        if text in ("/qq_status", "/状态"):
            self._send(event["chat_type"], event["chat_id"], self.status_text())

    # -- 攒批回复 --------------------------------------------------------
    def _reply_loop(self) -> None:
        stop = self._stop_event  # 捕获引用：重启换新事件后旧线程自然退出
        while not stop.is_set():
            batch_info = self._take_due_batch()
            if batch_info is None:
                time.sleep(0.1)
                continue
            chat_key, items = batch_info
            try:
                self._respond(chat_key, items, stop)
            except Exception:
                logger.warning("QQ 回复失败", exc_info=True)
                self._bump("failed")

    def _take_due_batch(self) -> tuple[str, list[dict]] | None:
        cfg = self._cfg
        if cfg is None:
            return None
        now = time.time()
        with self._batch_lock:
            for chat_key, batch in list(self._batches.items()):
                if batch["msgs"] and batch["due"] <= now:
                    items = batch["msgs"][: cfg.batch_size]
                    batch["msgs"] = batch["msgs"][cfg.batch_size:]
                    if batch["msgs"]:
                        batch["due"] = now + cfg.batch_ms / 1000.0
                    else:
                        self._batches.pop(chat_key, None)
                    return chat_key, items
        return None

    def _respond(self, chat_key: str, items: list[dict],
                 stop_event=None) -> None:
        cfg = self._cfg
        if cfg is None:
            return
        self._bump("batches")
        first = items[0]
        combined_text = " ".join(str(it.get("text") or "") for it in items).strip()

        # 0. 情绪动力学更新 (基于连续 PAD 空间与动量惯性公式推进心情与好感度)
        is_owner = any(bool(it.get("is_owner")) for it in items)
        sticker_in = any(bool(it.get("sticker_in")) for it in items)
        emo_inst = ""
        emo_tags: list[str] = []
        try:
            from .emotion import get_emotion_engine
            emo_eng = get_emotion_engine()
            emo_state = emo_eng.update(
                first["person_id"],
                combined_text,
                is_owner=is_owner,
                sticker_in=sticker_in,
            )
            emo_inst = emo_eng.build_prompt_instruction(emo_state, is_owner=is_owner)
            emo_tags = emo_eng.get_sticker_intent_tags(emo_state)
        except Exception as exc:
            logger.debug("情绪动力学更新异常: %s", exc)

        # 1. 优先快路径：天气查询
        is_weather, city = weather_mod.is_weather_intent(combined_text)
        if is_weather:
            reply = weather_mod.get_weather_report(city)
            error = ""
        else:
            # 2. 优先快路径：点歌与音乐分享
            is_music, song_name = music_mod.is_music_intent(combined_text)
            if is_music:
                if not song_name:
                    reply = "想听什么歌呢？请告诉我歌名哦，例如：我要听 晴天 / 点歌 七里香 🎵"
                    error = ""
                else:
                    song = music_mod.search_song(song_name)
                    if song:
                        title = song["title"]
                        singer = song["singer"]
                        page_url = song["page_url"]
                        platform = song["platform"]
                        song_id = song["id"]
                        pic_url = song.get("pic_url") or "https://y.gtimg.cn/mediastyle/global/img/album_300.png"
                        audio_url = song.get("audio_url") or page_url

                        card_ok = False
                        card_msg = ""

                        # 优先通道 1: 原生 QQ Ark 结构化卡片 (com.tencent.music.lua)
                        try:
                            ark_json = music_mod.sign_music_card(song)
                            if ark_json:
                                json_seg = {"type": "json", "data": {"data": ark_json}}
                                card_ok, card_msg = self._send_segments(first["chat_type"], first["chat_id"], [json_seg])
                        except Exception as exc:
                            logger.debug("Ark 卡片通道异常: %s", exc)

                        # 优先通道 2: OneBot 自定义音乐卡片 (custom)
                        if not card_ok:
                            custom_music_seg = {
                                "type": "music",
                                "data": {
                                    "type": "custom",
                                    "url": page_url,
                                    "audio": audio_url,
                                    "title": title,
                                    "content": singer,
                                    "image": pic_url,
                                }
                            }
                            card_ok, card_msg = self._send_segments(first["chat_type"], first["chat_id"], [custom_music_seg])

                        # 备用通道 3: OneBot 预设平台卡片 (qq / 163)
                        if not card_ok:
                            preset_music_seg = {"type": "music", "data": {"type": platform, "id": song_id}}
                            card_ok, card_msg = self._send_segments(first["chat_type"], first["chat_id"], [preset_music_seg])

                        if card_ok:
                            # 卡片发送成功：直接完成回复，不再发送冗余的文字链接，保持聊天窗口整洁
                            logger.info("QQ 音乐卡片发送成功：《%s》- %s (%s)", title, singer, platform)
                            self._last_sent = time.time()
                            self._bump("responded")
                            mem_text = f"[音乐分享] 《{title}》- {singer}"
                            if cfg.history_turns > 0:
                                with self._batch_lock:
                                    hist = self._history.setdefault(chat_key, [])
                                    hist.append(
                                        (str(first.get("user_name") or ""),
                                         str(first.get("text") or ""), mem_text)
                                    )
                                    del hist[:-cfg.history_turns]
                            session_id = first.get("session_id") or f"qq_{datetime.now().strftime('%Y-%m-%d')}"
                            try:
                                self._memory.record_viewer_reply(
                                    first["person_id"], first["user_name"], mem_text,
                                    reply_to_ts=first.get("ts"),
                                    session_id=session_id,
                                    message_id=first.get("message_id") or None,
                                )
                            except Exception:
                                logger.warning("记 QQ 回复失败", exc_info=True)
                            return
                        else:
                            # 仅当 QQ 客户端风控卡片发送失败时，才降级发送试听直达链接
                            logger.info("OneBot 音乐卡片发送受阻(%s)，降级为直达试听链接", card_msg)
                            reply = f"已为你找到歌曲：《{title}》- {singer} 🎵\n在线收听地址：{page_url}"
                        error = ""
                    else:
                        reply = f"抱歉呀，没有搜到《{song_name}》呢，换一首歌试试看？"
                        error = ""
            else:
                # 3. 常规 LLM 回复
                history: list[tuple[str, str, str]] = []
                if cfg.history_turns > 0:
                    with self._batch_lock:
                        hist = self._history.get(chat_key) or []
                        history = list(hist[-cfg.history_turns:])
                reply, error = brain_mod.generate_reply(
                    cfg, self._memory, items, stop_event or self._stop_event,
                    history=history, emotion_instruction=emo_inst,
                )
        if not reply:
            logger.info("QQ 批次未产出回复：%s", error)
            return
        # 节流
        gap = cfg.reply_gap_ms / 1000.0
        wait = self._last_sent + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        first = items[0]
        # 表情这一整步不许把文字回复带崩：挑图要读盘、要联网，出错就只发文字
        image_file = ""
        sticker_from = ""
        if cfg.sticker_enabled:
            with self._batch_lock:
                last = self._sticker_ts.get(chat_key, 0.0)
            if time.time() - last >= cfg.sticker_cooldown_sec:
                try:
                    image_file, sticker_from = self._pick_sticker(
                        items, cfg, str(first.get("text") or ""), reply, emotion_tags=emo_tags)
                except Exception:
                    logger.warning("QQ 表情包挑选失败，这条只发文字", exc_info=True)
                    image_file = ""
                    sticker_from = ""
        ok, msg = self._send(first["chat_type"], first["chat_id"], reply,
                             image_file=image_file)
        self._last_sent = time.time()
        if not ok and image_file:
            logger.info("QQ 带图发送失败(%s)，降级为纯文字重试", msg)
            self._bump("stickers_fail")
            self._note_error("_last_sticker_error", msg)
            ok, msg = self._send(first["chat_type"], first["chat_id"], reply,
                                 image_file="")
            if ok:
                image_file = ""
                sticker_from = ""
        if not ok:
            self._bump("failed")
            logger.warning("QQ 发送失败：%s", msg)
            return
        if image_file:
            with self._batch_lock:
                self._sticker_ts[chat_key] = time.time()
            self._bump("stickers_sent")
            logger.info("QQ 表情包已发：%s", sticker_from or "已发")
        self._bump("responded")
        if cfg.history_turns > 0:
            with self._batch_lock:
                hist = self._history.setdefault(chat_key, [])
                hist.append(
                    (str(first.get("user_name") or ""),
                     str(first.get("text") or ""), reply)
                )
                del hist[:-cfg.history_turns]
        session_id = first.get("session_id") or f"qq_{datetime.now().strftime('%Y-%m-%d')}"
        try:
            self._memory.record_viewer_reply(
                first["person_id"], first["user_name"], reply,
                reply_to_ts=first.get("ts"),
                session_id=session_id,
                message_id=first.get("message_id") or None,
            )
        except Exception:
            logger.warning("记 QQ 回复失败", exc_info=True)

        # 实时即时记忆提炼（事件驱动）：不阻塞当前回复，异步抽取并固化该观众的事实
        if self._executor is not None and len(combined_text) >= 4:
            self._executor.submit(
                self._async_extract_facts,
                first["person_id"],
                str(first.get("user_name") or ""),
                combined_text,
            )

    def _async_extract_facts(self, person_id: str, user_name: str, text: str) -> None:
        """异步即时事实抽取：有新表达时自动提炼画像，不再死等 30 分钟后台轮询。"""
        try:
            if self._memory is None:
                return
            fn = getattr(self._memory, "extract_and_save_viewer_facts", None)
            if callable(fn):
                facts = fn(
                    person_id,
                    user_name,
                    text,
                    summarize_fn=self._summarize_with_brain,
                )
                if facts:
                    logger.info("即时记忆提炼成功 [%s]: 提炼并固化 %s 条事实: %s",
                                user_name, len(facts), [f.get("fact") for f in facts])
        except Exception:
            logger.debug("异步即时记忆提炼跳过: %s", text[:30], exc_info=True)

    # -- 记忆维护 --------------------------------------------------------
    def _maintain_loop(self) -> None:
        """QQ 侧低频记忆维护：以前这些只在「停播整理」时跑，QQ 单独用
        （从不开播）就永远轮不上。全部幂等，和直播的整理互不冲突。"""
        stop = self._stop_event
        cfg = self._cfg
        interval = cfg.maintain_interval_sec if cfg else 1800.0
        if stop.wait(60.0):  # 启动 1 分钟后先跑一轮，尽快补上 QQ 新消息
            return
        while not stop.is_set():
            try:
                self._maintain_once()
            except Exception:
                logger.warning("QQ 记忆维护异常", exc_info=True)
            if stop.wait(interval):
                return

    def _maintain_once(self) -> None:
        memory = self._memory
        if memory is None:
            return
        try:
            n = memory.flush_embeddings()
            if n:
                logger.info("QQ 记忆补向量：%s 条", n)
        except Exception:
            logger.info("QQ 记忆补向量跳过（embedding 未配置时属正常）")
        try:
            n = memory.prune_if_due()
            if n:
                logger.info("QQ 记忆 TTL 清理：%s 行", n)
        except Exception:
            logger.warning("QQ 记忆 TTL 清理异常", exc_info=True)
        try:
            res = memory.extract_viewer_facts_batch(self._summarize_with_brain)
            if res and res.get("facts"):
                logger.info("QQ 观众画像提炼：%s", res)
        except Exception:
            logger.info("QQ 观众画像提炼跳过（无大脑/无互动时属正常）", exc_info=True)
        try:
            ok, detail = memory.consolidate_if_due(
                summarize_fn=self._summarize_with_brain)
            if ok:
                logger.info("QQ 记忆 7 天整理完成：%s", detail)
        except Exception:
            logger.info("QQ 记忆 7 天整理跳过（未到间隔/无大脑时属正常）", exc_info=True)

    @staticmethod
    def _build_embed_fn(cfg: QQConfig):
        if not cfg.embedding_enabled or not cfg.embedding_model or not cfg.embedding_base_url:
            return None
        import json
        import urllib.request
        base_url = cfg.embedding_base_url.rstrip("/")
        url = base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"
        model = cfg.embedding_model
        api_key = cfg.embedding_api_key
        timeout = cfg.embedding_timeout_sec

        def _embed(text: str) -> list[float]:
            payload = json.dumps({"model": model, "input": text}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vec = [float(x) for x in data["data"][0]["embedding"]]
            norm = sum(x * x for x in vec) ** 0.5
            return [x / norm for x in vec] if norm else vec

        return _embed

    def _summarize_with_brain(self, prompt: str) -> str:
        """只借大脑做一次摘要，不传 tools，不读写主记忆。"""
        brain = brain_mod.get_brain()
        if brain is None:
            raise RuntimeError("brain_unavailable")
        msg = brain.chat_once([
            {"role": "system",
             "content": "你是岁岁念（鱼鱼），只输出要求的摘要，不解释。"},
            {"role": "user", "content": prompt},
        ])
        if not isinstance(msg, dict):
            raise RuntimeError("brain_no_reply")
        return str(msg.get("content") or "")

    def _describe_images(self, event: dict, cfg: QQConfig) -> dict:
        """图片段 → 本机取字节 → 视觉模型一句话描述，拼进文本。

        任何一步失败都留下原因并计入状态，不再静默当没图。
        动图按"表情包"写进文本：这条文本会原样进 CompanionMemory，
        表情名（QQ 给的 summary）和画面描述一起记下来，下次同一张表情
        再来才认得出是"又来了"。顺手把收到的动图存进表情包库——对方发的
        表情才是最合适的素材，光靠同步永远长不出能用的库。
        视觉没开（或模型挂了）时图和动图照收、sticker_in 照标，只是不写画面描述。
        """
        refs = list(event.get("images") or [])[:cfg.vision_max_images]
        notes: list[str] = []
        net_timeout = min(max(3.0, cfg.vision_timeout_sec), 12.0)
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            data, mime, trail = media_mod.fetch_bytes(
                ref, self._transport, net_timeout)
            if not data:
                self._bump("images_fail")
                self._note_error("_last_image_error", "；".join(trail))
                logger.warning("QQ 取图失败：%s", "；".join(trail))
                continue
            is_gif = media_mod.sniff_ext(data) == ".gif"
            if is_gif:
                # 当场就标：后面取描述/入库出岔也不该丢掉"对方甩了表情"这件事
                event["sticker_in"] = True
            if cfg.sticker_harvest:
                self._bank_gif(data, ref)
            if not cfg.vision_enabled:
                continue
            data, mime = media_mod.shrink(
                data, mime, cfg.vision_max_bytes, cfg.vision_max_edge)
            text, err = vision_mod.describe_image(
                media_mod.to_data_url(data, mime),
                cfg.vision_base_url, cfg.vision_api_key,
                cfg.vision_model, cfg.vision_timeout_sec,
            )
            if not text:
                self._bump("images_fail")
                self._note_error("_last_vision_error", err)
                continue
            self._bump("images_ok")
            name = str(ref.get("summary") or "").strip()[:20]
            kind = "表情包" if is_gif else "图"
            head = f"“{name}”这个{kind}" if name else f"个{kind}"
            notes.append(f"（对方发了{head}：{text}）")
        if not notes:
            return event
        joined = "".join(notes)
        if event.get("text") == "[图片]":
            event["text"] = joined
        else:
            event["text"] = str(event.get("text") or "") + joined
        return event

    def _bank_gif(self, data: bytes, ref: dict) -> None:
        """对方发来的动图收进表情包库（只收 gif，太大的截图不算表情包）。"""
        if media_mod.sniff_ext(data) != ".gif" or len(data) > _MAX_HARVEST_BYTES:
            return
        name = str(ref.get("summary") or "").strip()
        try:
            if stickers_mod.add(data, ".gif", [name] if name else [], "recv") is not None:
                self._bump("stickers_banked")
        except Exception:
            logger.info("动图入库失败", exc_info=True)

    def _pick_sticker(self, items, cfg: QQConfig,
                      said: str, reply: str,
                      emotion_tags: list[str] | None = None) -> tuple[str, str]:
        """这一轮该甩哪张表情：返回 (OneBot file 值, 来源标记)，只挑不发。

        顺序是硬要求：先本地库，库里一张动图都没有才允许线上搜。
        没抽中概率就整轮不发——那是"不该发"，不是"找不到"，不能拿线上搜补一张。
        对方这轮甩了动图（sticker_in）时 chance=1.0，必回一张，互甩才像正常往来。
        """
        chance = sticker_chance(items, cfg.sticker_chance)
        if chance < 1.0 and random.random() >= chance:
            return "", ""
        tag_str = " ".join(emotion_tags or [])
        search_kw = f"{said} {reply} {tag_str}".strip()
        picked = stickers_mod.pick(search_kw, 1.0)
        if picked is None:
            if not cfg.sticker_search or stickers_mod.has_gif():
                return "", ""
            search_query = tag_str or said or reply
            found = memes_mod.search(search_query)
            if not found:
                return "", ""
            data, ext, key = found
            path = stickers_mod.add(data, ext, [key], "search")
            if path is None:
                return "", ""
            self._bump("sticker_search")
            return self._sticker_file_value(path), f"search:{key}"
        return self._sticker_file_value(picked[0]), picked[1]

    def _sticker_file_value(self, path) -> str:
        """本地表情文件 → OneBot 的 file 值。

        只发动图：表情包不是 gif 就别发（超管明确要求），格式不对直接返回空。
        值给 NapCat 的本地绝对路径而不是 base64——base64 分支会把它存成一个
        随机文件名，扩展名丢了，QQ 就按静态图处理（gif 不动）；本地路径分支
        保留 .gif。file:// 前缀不拼，中文路径靠 file:// 转义是 NapCat 的
        老雷区（issue #718），直接给 fs 能认的原生路径最稳。
        """
        from pathlib import Path

        p = Path(str(path))
        try:
            data = p.read_bytes()
        except OSError as exc:
            self._note_error("_last_sticker_error", f"表情读不出：{exc}")
            logger.warning("表情包读不出 %s: %s", path, exc)
            return ""
        if media_mod.sniff_ext(data) != ".gif":
            self._note_error("_last_sticker_error",
                             f"表情不是 gif：{p.name}")
            return ""
        if len(data) > _MAX_STICKER_BYTES:
            self._note_error("_last_sticker_error", f"表情太大({len(data)}B)")
            return ""
        return str(p.resolve())

    def sync_stickers(self, limit: int = 60) -> tuple[bool, str]:
        """把 QQ 账号收藏的表情同步进表情包库。"""
        transport = self._transport
        if transport is None:
            return False, "QQ 接入还没启动，先说“上QQ”再同步表情。"
        cfg = self._cfg
        if cfg is not None and not cfg.sticker_enabled:
            return False, "qq.sticker_enabled 是关的，先打开再同步。"
        res = memes_mod.sync_favorite(transport, limit)
        detail = (f"表情库同步：拉到 {res.get('fetched', 0)} 个，"
                  f"新存 {res.get('added', 0)} 张，"
                  f"已有 {res.get('reused', 0)} 张，"
                  f"跳过 {res.get('skipped', 0)} 张，"
                  f"库里现在 {stickers_mod.count()} 张")
        if res.get("error"):
            detail += f"；最近错误：{res['error']}"
            return False if not res.get("fetched") else True, detail
        return True, detail

    def _send(self, chat_type: str, chat_id: str, text: str,
              image_file: str = "") -> tuple[bool, str]:
        transport = self._transport
        if transport is None:
            return False, "未启动"
        action, params = channel.send_action(
            chat_type, chat_id, text, image_file=image_file)
        cfg = self._cfg
        timeout = max(20.0, cfg.request_timeout_sec if cfg else 30.0)
        return transport.call_action(action, params, timeout)

    def _send_segments(self, chat_type: str, chat_id: str,
                       segments: list[dict]) -> tuple[bool, str]:
        transport = self._transport
        if transport is None:
            return False, "未启动"
        action, params = channel.send_segments_action(chat_type, chat_id, segments)
        cfg = self._cfg
        timeout = max(20.0, cfg.request_timeout_sec if cfg else 30.0)
        return transport.call_action(action, params, timeout)

