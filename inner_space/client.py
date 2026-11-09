"""科科斯体内空间 — Agent 侧客户端。

按需拉起唯一 WebView 宿主进程，通过本机 HTTP 控制访客页面。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_START_TIMEOUT = 45.0
_HTTP_TIMEOUT = 60.0


def _local_opener():
    """回环控制面专用 opener，每次现建：系统代理（clash 等）挂掉时也必须能直连本机宿主。"""
    return build_opener(ProxyHandler({}))


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "inner_space"


def _legacy_state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "qqmusic" / "service.json"


def _state_path() -> Path:
    return _data_dir() / "service.json"


def _read_state() -> dict[str, Any] | None:
    for path in (_state_path(), _legacy_state_path()):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        port = data.get("port")
        if isinstance(port, int) and port > 0:
            return data
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    port: int,
    timeout: float = _HTTP_TIMEOUT,
) -> dict[str, Any]:
    url = f"http://{_HOST}:{port}{path}"
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _local_opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            return {"ok": False, "message": f"HTTP {e.code}: {raw[:200]}"}
        if isinstance(data, dict):
            data.setdefault("ok", False)
            return data
        return {"ok": False, "message": f"HTTP {e.code}"}
    except URLError as e:
        return {"ok": False, "message": f"连接体内空间失败: {e.reason}"}
    except Exception as e:
        return {"ok": False, "message": f"请求失败: {e}"}
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return {"ok": False, "message": f"无效响应: {raw[:200]}"}
    return data if isinstance(data, dict) else {"ok": False, "message": "无效响应"}


def _health(port: int) -> bool:
    r = _request("GET", "/health", port=port, timeout=2.0)
    return bool(r.get("ok"))


def _qq_host_stale(port: int) -> bool:
    """宿主活着且 ready，但 QQ 页面已掉登录且跑过歌（僵尸宿主）。

    2026-10-29 实锤：宿主熬过主程序重启后 QQ 会话过期，页面掉回 y.qq.com
    首页且 loggedIn=false；健康判定只看 HTTP+ready 会永远复用它，
    点歌/听歌全废。lastQuery 非空说明这代宿主播放过——关掉重开；
    刚拉起的新宿主 lastQuery 为空，留给上层「登录QQ音乐」引导，
    避免误杀和反复重启。
    """
    st = _request("GET", "/status", port=port, timeout=3.0)
    if not (isinstance(st, dict) and st.get("ok") and st.get("ready")):
        return False
    if str(st.get("guest") or "") != "qq":
        return False
    if st.get("loggedIn"):
        return False
    return bool(str(st.get("lastQuery") or "").strip())


def _recycle_stale_host(port: int) -> None:
    """关掉僵尸宿主并等服务退净（宿主退出时会自清 service.json）。"""
    try:
        _request("POST", "/close", payload={}, port=port, timeout=3.0)
    except Exception:
        pass
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not _health(port):
            break
        time.sleep(0.3)
    for p in (_state_path(), _legacy_state_path()):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _spawn_host() -> tuple[bool, str]:
    _data_dir().mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    extra = (env.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
    if "--remote-allow-origins" not in extra:
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"{extra} --remote-allow-origins=*".strip()
        )
    try:
        import webview  # noqa: F401
    except ImportError:
        return False, "缺少 pywebview，请先执行: pip install pywebview"

    log_path = _data_dir() / "service.log"
    try:
        log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    except OSError:
        log_f = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-m", "kekesi.inner_space.host"],
            cwd=str(root),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except Exception as e:
        if log_f is not subprocess.DEVNULL:
            try:
                log_f.close()
            except Exception:
                pass
        return False, f"启动体内空间失败: {e}"
    return True, "已启动"


# 点歌线程与手动点歌可能并发调 ensure_space：无锁时两边同时 spawn，
# 会开出多个宿主进程（各自挂 WebView 出声），表现为多首音乐同时播放
_SPAWN_LOCK = threading.Lock()


def ensure_space(*, start: bool = True) -> tuple[bool, str, int | None]:
    """确保体内空间可用。返回 (ok, message, port)。"""
    state = _read_state()
    if state:
        port = int(state["port"])
        if _health(port) and not _qq_host_stale(port):
            return True, "体内空间已就绪", port
    if not start:
        return False, "科科斯体内空间未运行", None

    with _SPAWN_LOCK:
        # 双重检查：等锁期间别的线程可能已把宿主拉起
        state = _read_state()
        if state:
            port = int(state["port"])
            pid = int(state.get("pid") or 0)
            if _health(port) and not _qq_host_stale(port):
                return True, "体内空间已就绪", port
            if _health(port):
                # 宿主活着但已僵尸（QQ 掉登录且跑过歌）：关掉重开，
                # 否则会被「只看 HTTP+ready」的判定永远复用
                _recycle_stale_host(port)
            elif pid and not _pid_alive(pid):
                for p in (_state_path(), _legacy_state_path()):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass

        ok, msg = _spawn_host()
        if not ok:
            return False, msg, None

        deadline = time.time() + _START_TIMEOUT
        while time.time() < deadline:
            state = _read_state()
            if state:
                port = int(state["port"])
                if _health(port):
                    st = _request("GET", "/space/status", port=port, timeout=5.0)
                    if st.get("ready") or st.get("ok"):
                        return True, "体内空间已启动", port
                    # 兼容旧路径尚未升级时
                    st2 = _request("GET", "/status", port=port, timeout=5.0)
                    if st2.get("ready") or st2.get("ok"):
                        return True, "体内空间已启动", port
            time.sleep(0.4)

    return False, "体内空间启动超时，请检查是否已安装 WebView2/Edge", None


def space_status() -> dict[str, Any]:
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return {"ok": True, "ready": False, "message": msg}
    r = _request("GET", "/space/status", port=port, timeout=10.0)
    if not r.get("ok"):
        r = _request("GET", "/status", port=port, timeout=10.0)
        if r.get("ok"):
            r.setdefault("guest", "qqmusic")
            r.setdefault("space", "科科斯体内空间")
    return r


def load_url(url: str, *, guest: str = "", show: bool = False) -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        return False, msg
    r = _request(
        "POST",
        "/space/load",
        {"url": url, "guest": guest, "show": bool(show)},
        port=port,
        timeout=45.0,
    )
    return bool(r.get("ok")), str(r.get("message") or msg)


def eval_js(script: str, *, timeout: float = 12.0) -> tuple[bool, Any, str]:
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return False, None, msg
    r = _request(
        "POST",
        "/space/eval",
        {"script": script, "timeout": timeout},
        port=port,
        timeout=max(timeout + 5.0, 20.0),
    )
    if not r.get("ok"):
        return False, None, str(r.get("message") or "执行失败")
    return True, r.get("result"), str(r.get("message") or "ok")


def show_space() -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=True)
    if not ok or port is None:
        return False, msg
    r = _request("POST", "/space/show", {}, port=port, timeout=15.0)
    if not r.get("ok"):
        r = _request("POST", "/show", {"minimize": False}, port=port, timeout=15.0)
    return bool(r.get("ok")), str(r.get("message") or msg)


def hide_space() -> tuple[bool, str]:
    ok, msg, port = ensure_space(start=False)
    if not ok or port is None:
        return True, "体内空间未运行"
    r = _request("POST", "/space/hide", {}, port=port, timeout=15.0)
    if not r.get("ok"):
        r = _request("POST", "/show", {"minimize": True}, port=port, timeout=15.0)
    return bool(r.get("ok")), str(r.get("message") or msg)


def close_space(*, purge_play_history: bool = True) -> tuple[bool, str]:
    state = _read_state()
    if not state:
        if purge_play_history:
            try:
                from kekesi.tools.qqmusic_service import purge_qq_play_history_disk

                purge_qq_play_history_disk()
            except Exception:
                pass
        return True, "体内空间未运行"
    port = int(state["port"])
    pid = int(state.get("pid") or 0)
    if not _health(port):
        for p in (_state_path(), _legacy_state_path()):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if purge_play_history:
            try:
                from kekesi.tools.qqmusic_service import purge_qq_play_history_disk

                purge_qq_play_history_disk()
            except Exception:
                pass
        return True, "体内空间未运行"
    r = _request("POST", "/close", {}, port=port, timeout=10.0)
    # 等宿主退出再清磁盘历史，否则文件可能被占用
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if pid and not _pid_alive(pid):
            break
        if not _health(port):
            break
        time.sleep(0.3)
    time.sleep(0.4)
    if purge_play_history:
        try:
            from kekesi.tools.qqmusic_service import purge_qq_play_history_disk

            n = purge_qq_play_history_disk()
            logger.info("已清理 QQ 播放历史缓存 %s 项", len(n))
        except Exception as e:
            logger.debug("purge play history: %s", e)
    return bool(r.get("ok")), str(r.get("message") or "已关闭")
