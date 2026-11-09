"""OneBot v11 正向 WebSocket 传输层：收事件、发 action、断线重连。

只用 websocket-client（直播插件已在用），不引新依赖。
重连按 generation 代数比对：stop() 拉新一代后旧线程自然退出，
不会出现旧 session 复活写回的脏状态（minecraft 插件的教训）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket

logger = logging.getLogger(__name__)


class OneBotTransport:
    def __init__(self, ws_url: str, access_token: str,
                 on_event: Callable[[dict], None],
                 reconnect_sec: float = 5.0):
        self._ws_url = ws_url
        self._token = access_token
        self._on_event = on_event
        self._reconnect_sec = max(1.0, reconnect_sec)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._ws: websocket.WebSocket | None = None
        self._connected = False
        self._connected_at = 0.0
        self._gen = 0
        self._echo = 0
        self._pending: dict[str, dict] = {}
        self._last_error = ""

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="qq-onebot", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._gen += 1
        with self._state_lock:
            ws = self._ws
            self._ws = None
            self._connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._fail_pending("已停止")

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    @property
    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    # -- 收发 ------------------------------------------------------------
    def _run(self) -> None:
        headers = []
        if self._token:
            headers.append(f"Authorization: Bearer {self._token}")
        while not self._stop_event.is_set():
            gen = self._gen
            try:
                ws = websocket.create_connection(
                    self._ws_url, timeout=5, header=headers
                )
            except Exception as exc:
                self._set_error(f"连接失败：{exc}")
                self._stop_event.wait(self._reconnect_sec)
                continue

            with self._state_lock:
                self._ws = ws
                self._connected = True
                self._connected_at = time.time()
                self._last_error = ""
            logger.info("OneBot 已连接 %s", self._ws_url)
            try:
                while gen == self._gen and not self._stop_event.is_set():
                    ws.settimeout(1.0)
                    try:
                        frame = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not frame:
                        break
                    try:
                        data = json.loads(frame)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    echo = data.get("echo")
                    if echo is not None and str(echo) in self._pending:
                        self._resolve_pending(str(echo), data)
                    elif data.get("post_type"):
                        try:
                            self._on_event(data)
                        except Exception:
                            logger.warning("处理 OneBot 事件失败", exc_info=True)
            except Exception as exc:
                if gen == self._gen and not self._stop_event.is_set():
                    self._set_error(f"连接中断：{exc}")
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                with self._state_lock:
                    if self._ws is ws:
                        self._ws = None
                        self._connected = False
                self._fail_pending("连接断开")
            if gen == self._gen and not self._stop_event.is_set():
                self._stop_event.wait(self._reconnect_sec)

    def call_action(self, action: str, params: dict,
                    timeout: float = 10.0) -> tuple[bool, str]:
        """同步调一个 OneBot action，返回 (ok, 描述)。"""
        ok, data, err = self.call_action_data(action, params, timeout)
        if not ok:
            return False, err
        if isinstance(data, dict):
            return True, str(data.get("status") or "ok")
        return True, str(data if data is not None else "ok")

    def call_action_data(self, action: str, params: dict,
                         timeout: float = 10.0) -> tuple[bool, object, str]:
        """同上，但把响应 data 原样带回来（get_image / 表情库要用）。

        返回 (ok, data, 错误描述)。
        """
        with self._state_lock:
            ws = self._ws
            if ws is None:
                return False, None, "OneBot 未连接"
        with self._state_lock:
            self._echo += 1
            echo = f"qq-{self._echo}"
        entry = {"cond": threading.Condition(), "resp": None}
        with entry["cond"]:
            self._pending[echo] = entry
        payload = json.dumps(
            {"action": action, "params": params, "echo": echo},
            ensure_ascii=False,
        )
        try:
            with self._send_lock:
                ws.send(payload)
        except Exception as exc:
            self._pending.pop(echo, None)
            return False, None, f"发送失败：{exc}"
        with entry["cond"]:
            entry["cond"].wait(timeout)
            self._pending.pop(echo, None)
            resp = entry["resp"]
        if resp is None:
            return False, None, "等待响应超时"
        if resp.get("status") == "failed":
            return False, None, str(
                resp.get("message") or resp.get("wording") or "action 失败")[:200]
        return True, resp.get("data"), ""

    # -- 内部 ------------------------------------------------------------
    def _resolve_pending(self, echo: str, resp: dict) -> None:
        entry = self._pending.get(echo)
        if entry is None:
            return
        with entry["cond"]:
            entry["resp"] = resp
            entry["cond"].notify_all()

    def _fail_pending(self, reason: str) -> None:
        entries = list(self._pending.values())
        self._pending.clear()
        for entry in entries:
            with entry["cond"]:
                if entry["resp"] is None:
                    entry["resp"] = {"status": "failed", "message": reason}
                    entry["cond"].notify_all()

    def _set_error(self, text: str) -> None:
        with self._state_lock:
            self._last_error = str(text)[:300]
        logger.info("OneBot：%s", text)
