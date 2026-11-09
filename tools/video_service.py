"""科科斯视频空间宿主 + 看剧访客（WebView2）。

独立进程：本地 HTTP API + 唯一 pywebview 窗口（与听歌体内空间分离）。
访客默认腾讯视频 v.qq.com；登录态 data/video_space/storage。
仅本机 127.0.0.1 可访问。

入口：python -m kekesi.video_space.host
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger("kekesi.video_space")

_DEFAULT_HOST = "127.0.0.1"
_HOME = "https://v.qq.com/"

_SOURCE_HOME = {
    "tencent": "https://v.qq.com/",
    "bilibili": "https://www.bilibili.com/",
    "iqiyi": "https://www.iqiyi.com/",
}

_VIDEO_LABELS = {
    "tencent": "腾讯视频",
    "bilibili": "哔哩哔哩",
    "iqiyi": "爱奇艺",
}


def _normalize_video_source(raw: str | None) -> str:
    t = (raw or "").strip().lower().replace(" ", "")
    if t in ("bilibili", "b站", "哔哩", "哔哩哔哩", "bili"):
        return "bilibili"
    if t in ("tencent", "腾讯", "腾讯视频", "tx", "v.qq"):
        return "tencent"
    if t in ("iqiyi", "爱奇艺", "奇艺", "iqy"):
        return "iqiyi"
    return "tencent"


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "video_space"


def _state_path() -> Path:
    return _data_dir() / "service.json"


def _storage_path() -> Path:
    p = _data_dir() / "storage"
    p.mkdir(parents=True, exist_ok=True)
    return p


# 与听歌 9222 错开，避免同时开两边冲突
_REMOTE_DEBUG_PORT = 9223
_SPACE_NAME = "科科斯视频空间"


def _write_state(port: int, pid: int) -> None:
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": port,
        "pid": pid,
        "host": _DEFAULT_HOST,
        "remoteDebugPort": _REMOTE_DEBUG_PORT,
        "space": _SPACE_NAME,
        "guest": STATE.guest,
    }
    _state_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _clear_state() -> None:
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass


class _PlayerState:
    def __init__(self) -> None:
        self.window: Any = None
        self.lock = threading.RLock()
        self.last_query = ""
        self.ready = threading.Event()
        self.headless = True
        self.visible = False
        self.guest = ""
        self.guest_url = ""
        self.hwnd = 0
        self.video: dict[str, Any] = {}


STATE = _PlayerState()

_OFFSCREEN_XY = (-32000, -32000)
_VISIBLE_XY = (140, 90)
_WINDOW_SIZE = (1100, 720)


def _eval_js(script: str, timeout: float = 12.0) -> Any:
    window = STATE.window
    if window is None:
        raise RuntimeError("视频空间未就绪")
    result_box: dict[str, Any] = {}
    done = threading.Event()

    def _run() -> None:
        try:
            result_box["value"] = window.evaluate_js(script)
        except Exception as e:
            result_box["error"] = str(e)
        finally:
            done.set()

    threading.Thread(target=_run, name="video-space-eval", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("页面脚本执行超时")
    if "error" in result_box:
        raise RuntimeError(result_box["error"])
    return result_box.get("value")


def _load_url(url: str, wait: float = 2.0, *, guest: str | None = None) -> None:
    window = STATE.window
    if window is None:
        raise RuntimeError("视频空间未就绪")
    if guest is not None:
        STATE.guest = guest
    STATE.guest_url = url
    window.load_url(url)
    time.sleep(max(0.8, wait))


def _enum_space_hwnd() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # type: ignore[no-untyped-def]
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        if "科科斯视频空间" in title or ("科科斯" in title and "视频" in title):
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value) if found.value else 0


def _native_hwnd(*, touch_forms: bool = False) -> int:
    if STATE.hwnd:
        return int(STATE.hwnd)
    if not touch_forms:
        hwnd = _enum_space_hwnd()
        if hwnd:
            STATE.hwnd = hwnd
        return hwnd
    w = STATE.window
    if w is None:
        return 0
    native = getattr(w, "native", None)
    if native is None:
        return 0
    try:
        handle = getattr(native, "Handle", None)
        if handle is not None:
            hwnd = int(handle.ToInt64())
            STATE.hwnd = hwnd
            return hwnd
    except Exception:
        pass
    return 0


def _set_show_in_taskbar_ui(show: bool) -> None:
    w = STATE.window
    if w is None:
        return
    native = getattr(w, "native", None)
    if native is None:
        return
    try:
        native.ShowInTaskbar = bool(show)
    except Exception:
        pass


def _win32_move_resize(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    if not hwnd:
        return
    import ctypes

    user32 = ctypes.windll.user32
    try:
        user32.SetWindowPos(hwnd, 0, int(x), int(y), int(width), int(height), 0x0004 | 0x0010)
    except Exception as e:
        logger.debug("win32 move failed: %s", e)


def _win32_set_stealth(hwnd: int, stealth: bool) -> None:
    if not hwnd:
        return
    import ctypes

    user32 = ctypes.windll.user32
    gwl_exstyle = -20
    ws_ex_appwindow = 0x00040000
    ws_ex_toolwindow = 0x00000080
    try:
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
        else:
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW
        ex = int(get_long(hwnd, gwl_exstyle))
        if stealth:
            ex = (ex | ws_ex_toolwindow) & ~ws_ex_appwindow
        else:
            ex = (ex | ws_ex_appwindow) & ~ws_ex_toolwindow
        set_long(hwnd, gwl_exstyle, ex)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0010)
    except Exception as e:
        logger.debug("win32 stealth failed: %s", e)


def _show_window() -> None:
    hwnd = _native_hwnd(touch_forms=False)
    _win32_set_stealth(hwnd, False)
    _win32_move_resize(hwnd, _VISIBLE_XY[0], _VISIBLE_XY[1], _WINDOW_SIZE[0], _WINDOW_SIZE[1])
    if hwnd:
        import ctypes

        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    STATE.visible = True


def _hide_window() -> None:
    hwnd = _native_hwnd(touch_forms=False)
    _win32_move_resize(
        hwnd, _OFFSCREEN_XY[0], _OFFSCREEN_XY[1], _WINDOW_SIZE[0], _WINDOW_SIZE[1]
    )
    _win32_set_stealth(hwnd, True)
    STATE.visible = False


# F12：取全屏按钮中心坐标（真正点击用 CDP Input，JS click 无法触发播放器全屏）
_JS_FULLSCREEN_BTN_BOX = r"""
(() => {
  const el = document.querySelector('.txp_btn.txp_btn_fullscreen, .txp_btn_fullscreen');
  if (!el) return { ok: false, reason: 'no .txp_btn_fullscreen' };
  const r = el.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) {
    return { ok: false, reason: 'btn_hidden', w: r.width, h: r.height };
  }
  return {
    ok: true,
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    w: r.width,
    h: r.height,
    status: el.getAttribute('data-status') || '',
    aria: el.getAttribute('aria-label') || '',
  };
})()
"""

_JS_FULLSCREEN_STATUS = r"""
(() => {
  const el = document.querySelector('.txp_btn.txp_btn_fullscreen, .txp_btn_fullscreen');
  return {
    dataStatus: el ? (el.getAttribute('data-status') || '') : '',
    aria: el ? (el.getAttribute('aria-label') || '') : '',
    fs: !!(document.fullscreenElement || document.webkitFullscreenElement),
  };
})()
"""


def _cdp_click_xy(x: float, y: float, *, timeout: float = 8.0) -> None:
    """CDP 真实鼠标点击（统一走 control_finder）。"""
    from kekesi.tools.control_finder import VIDEO_DEBUG_PORT, VIDEO_URL_HINT, cdp_click_xy

    cdp_click_xy(
        float(x),
        float(y),
        debug_port=VIDEO_DEBUG_PORT,
        url_hint=VIDEO_URL_HINT,
        timeout=timeout,
    )


def _enter_fullscreen_after_play() -> dict[str, Any]:
    """播放成功后：多指纹定位全屏按钮并用 CDP 点击（不最大化窗口）。"""
    from kekesi.tools.control_finder import (
        VIDEO_DEBUG_PORT,
        click_control,
        find_control,
    )

    _show_window()
    last: dict[str, Any] = {"ok": False}
    for i in range(10):
        time.sleep(0.45 if i else 0.8)
        box = find_control(
            "tencent.fullscreen",
            debug_port=VIDEO_DEBUG_PORT,
            timeout=5.0,
        )
        if not box.get("ok"):
            # 兼容旧 JS 探针
            try:
                legacy = _page_eval(_JS_FULLSCREEN_BTN_BOX, timeout=5.0) or {}
            except Exception as e:
                last = {"ok": False, "reason": str(e), "fingerprint": box}
                continue
            if isinstance(legacy, dict) and legacy.get("ok"):
                box = legacy
            else:
                last = box if isinstance(box, dict) else {"ok": False}
                continue
        hit = click_control(
            "tencent.fullscreen",
            debug_port=VIDEO_DEBUG_PORT,
            timeout=5.0,
        )
        if not hit.get("clicked"):
            try:
                _cdp_click_xy(float(box["x"]), float(box["y"]))
                hit = {**box, "clicked": True, "via": "cdp_input:legacy_box"}
            except Exception as e:
                last = {"ok": False, "reason": f"cdp_click: {e}", "box": box}
                continue
        time.sleep(0.6)
        try:
            st = _page_eval(_JS_FULLSCREEN_STATUS, timeout=5.0) or {}
        except Exception:
            st = {}
        last = {
            "ok": True,
            "via": hit.get("via") or "fingerprint:tencent.fullscreen",
            "box": box,
            "playClick": hit,
            "status": st,
        }
        return last
    return last if isinstance(last, dict) else {"ok": False, "reason": "fullscreen_failed"}


# Cookie 优先判断登录（F12 实探：已登录仍可能短暂露出 #mod_head_login）
_JS_TENCENT_LOGIN_STATUS = r"""
(() => {
  try {
    const cookie = String(document.cookie || '');
    const pick = (k) => {
      const m = cookie.match(new RegExp('(?:^|;\\s*)' + k + '=([^;]*)'));
      return m ? decodeURIComponent(m[1] || '') : '';
    };
    const vuid = pick('vqq_vuserid');
    const session = pick('vqq_vusession');
    const main = pick('main_login');
    const cookieHint = /^\d+$/.test(vuid) && (!!session || /^(qq|wx|phone)$/i.test(main));
    const loginBtn = document.querySelector('#mod_head_login, .quick_login');
    const loginVisible = !!(loginBtn && ((loginBtn.innerText || '').replace(/\s+/g, ' ').trim() === '登录'));
    const hasQr = !!document.querySelector('#txv-login-wnd, #login_win .login-dialog-wrapper');
    return {
      loggedIn: !!cookieHint,
      loginVisible: !!loginVisible && !cookieHint,
      cookieHint: !!cookieHint,
      hasQr: !!hasQr,
      vuid: vuid ? vuid.slice(0, 12) : '',
      mainLogin: main,
      href: String(location.href || ''),
      title: String(document.title || ''),
    };
  } catch (e) {
    return { loggedIn: false, loginVisible: true, cookieHint: false, error: String(e) };
  }
})()
"""

# F12：#mod_head_login → 下拉「立即登录」→ #txv-login-wnd 扫码窗（可能先出「同意并继续」）
_JS_TENCENT_CLICK_LOGIN = r"""
(() => {
  const steps = [];
  const clickText = (want) => {
    const nodes = Array.from(document.querySelectorAll('button, a, span, div'));
    for (const el of nodes) {
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t !== want) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      try { el.click(); steps.push(want); return true; } catch (e) {}
    }
    return false;
  };
  if (document.querySelector('#txv-login-wnd')) {
    clickText('同意并继续');
    return { ok: true, via: 'already_qr', steps };
  }
  const btn = document.querySelector('#mod_head_login') || document.querySelector('.quick_login');
  if (btn) {
    try { btn.click(); steps.push('#mod_head_login'); } catch (e) {}
  } else {
    return { ok: false, reason: '未找到 #mod_head_login', steps };
  }
  // 同步再点面板按钮（面板若已渲染）
  clickText('立即登录');
  clickText('同意并继续');
  clickText('确认');
  const hasQr = !!document.querySelector('#txv-login-wnd, #login_win .web-login-mask');
  return { ok: true, via: 'mod_head_login', steps, hasQr };
})()
"""

_JS_TENCENT_CLICK_LOGIN_FOLLOWUP = r"""
(() => {
  const steps = [];
  const clickText = (want) => {
    const nodes = Array.from(document.querySelectorAll('button, a, span, div'));
    for (const el of nodes) {
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t !== want) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      try { el.click(); steps.push(want); return true; } catch (e) {}
    }
    return false;
  };
  clickText('立即登录');
  clickText('同意并继续');
  clickText('确认');
  return {
    ok: true,
    steps,
    hasQr: !!document.querySelector('#txv-login-wnd'),
    panel: !!document.querySelector('#new-nav-login-panel, .nav-login-panel-sdk'),
  };
})()
"""

_JS_BILI_LOGIN_STATUS = r"""
(() => {
  const cookie = document.cookie || '';
  const cookieHint = /DedeUserID=\d+/i.test(cookie) || /SESSDATA=/i.test(cookie);
  return {
    loggedIn: !!cookieHint,
    loginVisible: !cookieHint,
    cookieHint: !!cookieHint,
    href: location.href,
    title: document.title || '',
  };
})()
"""

_JS_GENERIC_CLICK_LOGIN = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll('a, button, span, div'));
  for (const el of nodes) {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!(t === '登录' || t === '登陆' || t === '立即登录' || t === '扫码登录')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    try { el.click(); return { ok: true, via: 'text:' + t }; } catch (e) {}
  }
  return { ok: false, reason: '未找到登录入口' };
})()
"""


def _cdp_eval(expression: str, timeout: float = 8.0) -> Any:
    """优先走统一 CDP（腾讯首页 pywebview evaluate_js 易超时）。"""
    from kekesi.tools.control_finder import VIDEO_DEBUG_PORT, VIDEO_URL_HINT, cdp_eval

    return cdp_eval(
        expression,
        debug_port=VIDEO_DEBUG_PORT,
        url_hint=VIDEO_URL_HINT,
        timeout=timeout,
        await_promise=False,
    )


def _page_eval(expression: str, timeout: float = 8.0) -> Any:
    """CDP 优先，失败再回退 pywebview evaluate_js。"""
    from kekesi.tools.control_finder import VIDEO_DEBUG_PORT, VIDEO_URL_HINT, page_eval

    return page_eval(
        expression,
        debug_port=VIDEO_DEBUG_PORT,
        url_hint=VIDEO_URL_HINT,
        fallback_eval=lambda js, t: _eval_js(js, timeout=t),
        timeout=timeout,
        await_promise=False,
    )


def _js_login_status_for(source: str) -> str:
    if source == "tencent":
        return _JS_TENCENT_LOGIN_STATUS
    if source == "bilibili":
        return _JS_BILI_LOGIN_STATUS
    return _JS_TENCENT_LOGIN_STATUS


def _js_click_login_for(source: str) -> str:
    if source == "tencent":
        return _JS_TENCENT_CLICK_LOGIN
    return _JS_GENERIC_CLICK_LOGIN


def _ensure_video_login(source: str) -> dict[str, Any]:
    """检查登录；未登录则弹窗并点开扫码（F12 对齐 #mod_head_login / #txv-login-wnd）。"""
    src = _normalize_video_source(source)
    label = _VIDEO_LABELS.get(src, src)
    home = _SOURCE_HOME.get(src, _HOME)
    _show_window()
    cur = (STATE.guest_url or "")
    need_reload = True
    if src == "tencent" and ("v.qq.com" in cur or "video.qq.com" in cur):
        need_reload = False
    elif src == "bilibili" and "bilibili.com" in cur:
        need_reload = False
    elif src == "iqiyi" and "iqiyi.com" in cur:
        need_reload = False
    if need_reload or STATE.guest != src:
        _load_url(home, wait=3.0, guest=src)
    else:
        time.sleep(0.8)

    st: dict[str, Any] = {}
    try:
        st = _page_eval(_js_login_status_for(src), timeout=10.0) or {}
    except Exception as e:
        # 腾讯首页脚本易超时：失败时不要当成未登录，否则只开窗不播
        logger.warning("login status failed (继续尝试点播): %s", e)
        st = {
            "loggedIn": True,
            "loginVisible": False,
            "probeFailed": True,
            "error": str(e),
        }

    if st.get("loggedIn"):
        return {
            "ok": True,
            "loggedIn": True,
            "needLogin": False,
            "source": src,
            "login": st,
            "message": f"已登录 {label}",
        }

    clicked: dict[str, Any] = {}
    try:
        clicked = _page_eval(_js_click_login_for(src), timeout=8.0) or {}
    except Exception as e:
        clicked = {"ok": False, "reason": str(e)}
    time.sleep(1.0)
    if src == "tencent":
        try:
            follow = _page_eval(_JS_TENCENT_CLICK_LOGIN_FOLLOWUP, timeout=8.0) or {}
            clicked = {"first": clicked, "follow": follow}
        except Exception as e:
            clicked = {"first": clicked, "follow_error": str(e)}
        time.sleep(0.8)

    try:
        st2 = _page_eval(_js_login_status_for(src), timeout=10.0) or {}
        if st2.get("loggedIn"):
            return {
                "ok": True,
                "loggedIn": True,
                "needLogin": False,
                "source": src,
                "login": st2,
                "message": f"已登录 {label}",
            }
        st = st2 or st
    except Exception as e:
        # 二次探测也失败：放行点播，避免卡在空白窗
        logger.warning("login re-check failed (放行点播): %s", e)
        return {
            "ok": True,
            "loggedIn": True,
            "needLogin": False,
            "source": src,
            "login": {"probeFailed": True, "error": str(e)},
            "message": f"登录状态未确认，继续尝试用{label}点播",
        }

    return {
        "ok": True,
        "loggedIn": False,
        "needLogin": True,
        "source": src,
        "login": st,
        "clicked": clicked,
        "message": (
            f"尚未登录{label}，已弹出扫码框，请用手机扫码；"
            f"完成后可以说「用{label}看剧名第N集」"
        ),
    }


_JS_TENCENT_MBSEARCH = r"""
(query) => {
  const q = String(query || '').trim();
  const url = 'https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch?vversion_platform=2';
  const body = {
    version: '26022601',
    clientType: 1,
    filterValue: '',
    retry: 0,
    query: q,
    pagenum: 0,
    isPrefetch: false,
    pagesize: 30,
    queryFrom: 0,
    searchDatakey: '',
    transInfo: '',
    isneedQc: true,
    preQid: '',
    adClientInfo: '',
    extraInfo: {
      isNewMarkLabel: '1',
      multi_terminal_pc: '1',
      themeType: '1',
      sugRelatedIds: '{}',
      appVersion: '',
      frontVersion: '26060108',
    },
  };
  try {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, false);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.send(JSON.stringify(body));
    const data = JSON.parse(xhr.responseText || '{}');
    const boxes = (((data || {}).data) || {}).areaBoxList || [];
    const hits = [];
    for (const box of boxes) {
      for (const it of (box.itemList || [])) {
        const vi = it.videoInfo || {};
        const doc = it.doc || {};
        const title = String(vi.title || '').replace(/<[^>]+>/g, '').trim();
        const typeName = String(vi.typeName || '');
        const cid = String(doc.id || doc.md || '');
        if (!title || !cid) continue;
        const sites = vi.episodeSites || [];
        let total = 0;
        let ep0 = null;
        if (sites.length && sites[0].episodeInfoList && sites[0].episodeInfoList.length) {
          total = Number(sites[0].totalEpisode || sites[0].episodeInfoList.length || 0);
          ep0 = sites[0].episodeInfoList[0];
        }
        hits.push({
          title,
          typeName,
          cid,
          totalEpisode: total,
          ep0Url: ep0 && ep0.url ? ep0.url : '',
          ep0Id: ep0 && ep0.id ? ep0.id : '',
          episodes: (sites[0] && sites[0].episodeInfoList || []).slice(0, 80).map((e) => ({
            id: e.id,
            title: String(e.title || ''),
            url: e.url || '',
          })),
        });
      }
    }
    return { ok: true, status: xhr.status, ret: data.ret, n: hits.length, hits: hits.slice(0, 12) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""

_JS_CLICK_EPISODE = r"""
(episode, preferMain) => {
  let n = Number(episode);
  if (!n) return { ok: false, reason: 'no_ep' };
  const wantMain = preferMain !== false; // 默认跳过预告

  const isTrailer = (el) => {
    if (!el) return false;
    const full = (el.innerText || el.textContent || '');
    if (/预告/.test(full)) return true;
    return Array.from(el.querySelectorAll('.corner-wrap, .corner-wrap .text')).some(
      (c) => /预告/.test(c.innerText || c.textContent || '')
    );
  };

  // 收集 1-30 / 271-282 这类分段（只要 .tab-node）
  const ranges = [];
  for (const el of document.querySelectorAll('.tab-node')) {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^\d{1,4}-\d{1,4}$/.test(t)) continue;
    const parts = t.split('-').map((x) => Number(x));
    if (parts.length !== 2) continue;
    ranges.push({ el, t, lo: parts[0], hi: parts[1] });
  }
  const uniq = [];
  const seen = new Set();
  for (const it of ranges) {
    if (seen.has(it.t)) continue;
    seen.add(it.t);
    uniq.push(it);
  }

  let rangeClicked = null;
  let clamped = false;
  let hitRange = uniq.find((it) => n >= it.lo && n <= it.hi) || null;
  if (!hitRange && uniq.length) {
    uniq.sort((a, b) => b.hi - a.hi);
    hitRange = uniq[0];
    if (n > hitRange.hi || n < hitRange.lo) {
      n = hitRange.hi;
      clamped = true;
    }
  }
  if (hitRange) {
    try { hitRange.el.click(); rangeClicked = hitRange.t; } catch (e) {}
  }

  const items = Array.from(document.querySelectorAll('.episode-item'));
  const pickByNum = (num) => {
    const hits = [];
    for (const el of items) {
      const textEl = el.querySelector('.episode-item-text');
      const bare = ((textEl && textEl.innerText) || '').replace(/\s+/g, ' ').trim();
      if (bare !== String(num)) continue;
      const trailer = isTrailer(el);
      if (wantMain && trailer) continue;
      hits.push({
        el,
        text: bare,
        trailer,
        title: el.getAttribute('title') || '',
        cls: String(el.className || '').slice(0, 80),
      });
    }
    return hits;
  };

  let hits = pickByNum(n);
  // 目标集是预告且只要正片：在当前分段里找比 n 小的最大正片
  if (!hits.length && wantMain) {
    let best = null;
    for (const el of items) {
      const textEl = el.querySelector('.episode-item-text');
      const bare = ((textEl && textEl.innerText) || '').replace(/\s+/g, ' ').trim();
      if (!/^\d{1,4}$/.test(bare)) continue;
      const num = Number(bare);
      if (num > n) continue;
      if (isTrailer(el)) continue;
      if (!best || num > best.num) best = { el, num, text: bare, title: el.getAttribute('title') || '' };
    }
    if (best) {
      n = best.num;
      hits = [{ el: best.el, text: best.text, trailer: false, title: best.title, cls: '' }];
      clamped = true;
    }
  }

  if (!hits.length) {
    return {
      ok: false,
      reason: 'ep_not_found',
      n,
      rangeClicked,
      clamped,
      ranges: uniq.map((x) => x.t),
      preferMain: wantMain,
    };
  }
  const hit = hits[0];
  let clicked = null;
  try { hit.el.click(); clicked = hit.text; } catch (e) {}
  return {
    ok: !!clicked,
    clicked,
    n,
    requested: Number(episode),
    rangeClicked,
    clamped,
    trailer: !!hit.trailer,
    epTitle: hit.title || '',
    href: location.href,
  };
}
"""

# 最新正片：从最后分段往前找，跳过角标「预告」
_JS_RESOLVE_LATEST_MAIN = r"""
(() => {
  const isTrailer = (el) => {
    const full = (el.innerText || el.textContent || '');
    if (/预告/.test(full)) return true;
    return Array.from(el.querySelectorAll('.corner-wrap, .corner-wrap .text')).some(
      (c) => /预告/.test(c.innerText || c.textContent || '')
    );
  };
  const ranges = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('.tab-node')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/^\d{1,4}-\d{1,4}$/.test(t) || seen.has(t)) continue;
    seen.add(t);
    const parts = t.split('-').map((x) => Number(x));
    ranges.push({ el, t, lo: parts[0], hi: parts[1] });
  }
  ranges.sort((a, b) => b.hi - a.hi);
  return {
    ok: true,
    ranges: ranges.map((r) => ({ t: r.t, lo: r.lo, hi: r.hi })),
  };
})()
"""

_JS_SCAN_MAIN_IN_TAB = r"""
(() => {
  const isTrailer = (el) => {
    const full = (el.innerText || el.textContent || '');
    if (/预告/.test(full)) return true;
    return Array.from(el.querySelectorAll('.corner-wrap, .corner-wrap .text')).some(
      (c) => /预告/.test(c.innerText || c.textContent || '')
    );
  };
  let best = null;
  let trailerMax = null;
  for (const el of document.querySelectorAll('.episode-item')) {
    const textEl = el.querySelector('.episode-item-text');
    const bare = ((textEl && textEl.innerText) || '').replace(/\s+/g, ' ').trim();
    if (!/^\d{1,4}$/.test(bare)) continue;
    const num = Number(bare);
    const trailer = isTrailer(el);
    const title = el.getAttribute('title') || '';
    if (trailer) {
      if (!trailerMax || num > trailerMax.n) trailerMax = { n: num, title };
      continue;
    }
    if (!best || num > best.n) best = { n: num, title, id: el.id || '' };
  }
  return {
    ok: !!best,
    main: best,
    trailerMax,
    selectedRange: ((document.querySelector('.tab-node.tab-node-selected') || {}).innerText || '').trim(),
  };
})()
"""


def _mbsearch_tencent_host(query: str) -> dict[str, Any]:
    """在宿主进程直调腾讯 MbSearch（不依赖 WebView evaluate_js）。

    语音冷启动时页面脚本易超时；直连热窗口时 page_eval 往往还能跑通——
    这正是「语音和直连不一样」的根因。宿主 HTTP 搜索两边行为一致。
    """
    from urllib.request import Request, urlopen

    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty", "hits": []}
    url = (
        "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search"
        ".MultiTerminalSearch/MbSearch?vversion_platform=2"
    )
    body = {
        "version": "26022601",
        "clientType": 1,
        "filterValue": "",
        "retry": 0,
        "query": q,
        "pagenum": 0,
        "isPrefetch": False,
        "pagesize": 30,
        "queryFrom": 0,
        "searchDatakey": "",
        "transInfo": "",
        "isneedQc": True,
        "preQid": "",
        "adClientInfo": "",
        "extraInfo": {
            "isNewMarkLabel": "1",
            "multi_terminal_pc": "1",
            "themeType": "1",
            "sugRelatedIds": "{}",
            "appVersion": "",
            "frontVersion": "26060108",
        },
    }
    req = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://v.qq.com",
            "Referer": "https://v.qq.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=12.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
    except Exception as e:
        return {"ok": False, "error": str(e), "hits": []}
    try:
        data = json.loads(raw or "{}")
    except Exception as e:
        return {"ok": False, "error": f"json:{e}", "hits": []}
    boxes = (((data or {}).get("data") or {}).get("areaBoxList") or [])
    hits: list[dict[str, Any]] = []
    for box in boxes:
        for it in box.get("itemList") or []:
            vi = it.get("videoInfo") or {}
            doc = it.get("doc") or {}
            title = re.sub(r"<[^>]+>", "", str(vi.get("title") or "")).strip()
            type_name = str(vi.get("typeName") or "")
            cid = str(doc.get("id") or doc.get("md") or "")
            if not title or not cid:
                continue
            sites = vi.get("episodeSites") or []
            total = 0
            ep0 = None
            eps: list[dict[str, Any]] = []
            if sites and (sites[0].get("episodeInfoList") or []):
                elist = sites[0].get("episodeInfoList") or []
                total = int(sites[0].get("totalEpisode") or len(elist) or 0)
                ep0 = elist[0] if elist else None
                for e in elist[:80]:
                    eps.append(
                        {
                            "id": e.get("id"),
                            "title": str(e.get("title") or ""),
                            "url": e.get("url") or "",
                        }
                    )
            hits.append(
                {
                    "title": title,
                    "typeName": type_name,
                    "cid": cid,
                    "totalEpisode": total,
                    "ep0Url": (ep0 or {}).get("url") or "",
                    "ep0Id": (ep0 or {}).get("id") or "",
                    "episodes": eps,
                }
            )
    return {
        "ok": True,
        "status": status,
        "ret": data.get("ret"),
        "n": len(hits),
        "hits": hits[:12],
        "via": "host_http",
    }


def _pick_tencent_hit(query: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not hits:
        return None
    q = (query or "").strip()
    want_movie = bool(re.search(r"电影|影片|大片", q))
    want_series = bool(re.search(r"剧|动漫|番|连续剧|电视剧", q))
    # 去掉类型词再比标题
    q_core = re.sub(r"(?:电影|影片|大片|电视剧|连续剧|动漫|综艺|纪录片)$", "", q).strip() or q

    def _overlap(title: str) -> float:
        if not title:
            return 0.0
        if q_core == title or q == title:
            return 1.0
        if q_core in title or title in q_core:
            return 0.9
        qs, ts = set(q_core), set(title)
        if not qs:
            return 0.0
        return len(qs & ts) / len(qs)

    def score(h: dict[str, Any]) -> tuple:
        title = str(h.get("title") or "")
        typ = str(h.get("typeName") or "")
        ov = _overlap(title)
        exact = 3 if title in (q, q_core) else (2 if (q_core in title or title in q_core) else 0)
        if ov >= 0.75 and exact == 0:
            exact = 1
        type_bonus = 0
        if want_movie:
            type_bonus = 2 if typ == "电影" else (-1 if typ in ("动漫", "电视剧", "少儿") else 0)
        elif want_series:
            type_bonus = 2 if typ in ("动漫", "电视剧", "少儿") else (0 if typ == "电影" else 1)
        else:
            # 未点名类型：标题贴合优先，电影/剧同等
            type_bonus = 1 if typ in ("电影", "动漫", "电视剧", "少儿") else 0
        pen = 0
        if any(x in title for x in ("合集", "剧场版", "特别篇", "动态漫", "高光")):
            pen -= 2
        return (exact + type_bonus + pen, ov, exact, int(h.get("totalEpisode") or 0))

    best = max(hits, key=score)
    title = str(best.get("title") or "")
    # 标题几乎无关时不要硬开（避免「诺斯费拉图」→「诡新娘」）
    if _overlap(title) < 0.55 and q_core not in title and title not in q_core:
        return None
    return best


def _resolve_latest_episode(hit: dict[str, Any]) -> int | None:
    """用搜索结果的 totalEpisode / 分集列表解析最新集号。"""
    total = int(hit.get("totalEpisode") or 0)
    if total > 0:
        return total
    nums: list[int] = []
    for ep in hit.get("episodes") or []:
        t = str(ep.get("title") or "").strip()
        if t.isdigit():
            nums.append(int(t))
    return max(nums) if nums else None


def _api_play_video_tencent(
    query: str,
    episode: int | None,
    *,
    latest: bool = False,
) -> dict[str, Any]:
    """登录检查 → 宿主 HTTP MbSearch → 封面页 → 点集数。"""
    q = (query or "").strip()
    with STATE.lock:
        STATE.last_query = q
        STATE.guest = "tencent"
        _show_window()
        _load_url("https://v.qq.com/", wait=2.0, guest="tencent")

        # 与 QQ 音乐一致：未登录先弹扫码，不继续点播
        gate = _ensure_video_login("tencent")
        if gate.get("needLogin") and not gate.get("loggedIn"):
            return {
                "ok": False,
                "needLogin": True,
                "source": "tencent",
                "message": str(gate.get("message") or "请先扫码登录腾讯视频"),
                "login": gate.get("login"),
                "clicked": gate.get("clicked"),
                "pending": {"query": q, "episode": episode, "latest": latest},
            }

        # 搜剧走宿主 HTTP，不依赖 WebView 里跑同步 XHR（语音冷启动易脚本超时）。
        found = _mbsearch_tencent_host(q)
        hits = list(found.get("hits") or []) if isinstance(found, dict) else []
        hit = _pick_tencent_hit(q, hits)
        if hit:
            logger.info(
                "MbSearch host_http ok q=%r hit=%r n=%s",
                q[:40],
                (hit.get("title") or "")[:40],
                found.get("n"),
            )
        else:
            logger.warning(
                "MbSearch host_http miss q=%r err=%s n=%s; fallback page_eval once",
                q[:40],
                found.get("error") if isinstance(found, dict) else None,
                len(hits),
            )
            # 探测失败时页面可能未稳；仍给一次短 settle，仅作兜底
            login_info = gate.get("login") if isinstance(gate.get("login"), dict) else {}
            if login_info.get("probeFailed"):
                time.sleep(1.5)
            try:
                found_page = (
                    _page_eval(
                        f"({_JS_TENCENT_MBSEARCH})({json.dumps(q)})",
                        timeout=12.0,
                    )
                    or {}
                )
            except Exception as e:
                found_page = {"ok": False, "error": str(e)}
                logger.warning("MbSearch page_eval fallback failed: %s", e)
            hits_page = (
                list(found_page.get("hits") or [])
                if isinstance(found_page, dict)
                else []
            )
            hit = _pick_tencent_hit(q, hits_page)
            if hit:
                found = {**found_page, "via": "page_eval_fallback"}
            else:
                found = {
                    "host": found,
                    "page": found_page,
                    "via": "both_miss",
                }

        if not hit:
            search_url = f"https://v.qq.com/x/search/?q={quote(q)}"
            _load_url(search_url, wait=3.5, guest="tencent")
            STATE.video = {"title": q, "episode": episode, "url": search_url}
            return {
                "ok": True,
                "source": "tencent",
                "needPick": True,
                "message": (
                    f"已在腾讯视频打开「{q}」搜索页（请在弹出窗口点选）。"
                    + ("目标最新一集。" if latest else (f"目标第{episode}集。" if episode else ""))
                ),
                "url": search_url,
                "search": found,
                "video": STATE.video,
            }

        # 最新一集：先记搜索 totalEpisode 作参考，真正集数以页面「非预告」最大集为准
        search_total = _resolve_latest_episode(hit) if latest else None
        if latest and episode is None:
            episode = search_total  # 稍后可能被正片集号覆盖

        cid = str(hit.get("cid") or "")
        show_title = str(hit.get("title") or q)
        play_url = ""
        if episode and not latest:
            for ep in hit.get("episodes") or []:
                if str(ep.get("title") or "") == str(int(episode)):
                    play_url = str(ep.get("url") or "")
                    break

        if not play_url:
            cover_url = f"https://v.qq.com/x/cover/{cid}.html"
            _load_url(cover_url, wait=2.5, guest="tencent")
            for _ in range(12):
                try:
                    ready = _page_eval(
                        "!!(document.querySelector('.tab-node') || document.querySelector('.episode-item-text'))",
                        timeout=4.0,
                    )
                    if ready:
                        break
                except Exception:
                    pass
                time.sleep(0.45)

            clicked = None
            if latest:
                # 从最后分段往前扫，跳过角标「预告」，取最大正片集
                try:
                    meta = _page_eval(_JS_RESOLVE_LATEST_MAIN, timeout=6.0) or {}
                except Exception:
                    meta = {}
                range_list = list(meta.get("ranges") or []) if isinstance(meta, dict) else []
                main_ep: int | None = None
                for rg in range_list:
                    t = str(rg.get("t") or "")
                    if not t:
                        continue
                    try:
                        _page_eval(
                            f"""(() => {{
  for (const el of document.querySelectorAll('.tab-node')) {{
    const x = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (x === {json.dumps(t)}) {{ el.click(); return x; }}
  }}
  return null;
}})()""",
                            timeout=5.0,
                        )
                    except Exception:
                        pass
                    time.sleep(0.8)
                    try:
                        scan = _page_eval(_JS_SCAN_MAIN_IN_TAB, timeout=6.0) or {}
                    except Exception:
                        scan = {}
                    main = (scan.get("main") if isinstance(scan, dict) else None) or {}
                    if isinstance(main, dict) and main.get("n"):
                        main_ep = int(main["n"])
                        break
                if main_ep is not None:
                    episode = main_ep
                # 用跳过预告的点击逻辑点正片
                if episode:
                    for _attempt in range(4):
                        try:
                            clicked = _page_eval(
                                f"({_JS_CLICK_EPISODE})({int(episode)}, true)",
                                timeout=8.0,
                            )
                        except Exception as e:
                            clicked = {"ok": False, "reason": str(e)}
                        if isinstance(clicked, dict) and clicked.get("ok"):
                            if clicked.get("n"):
                                episode = int(clicked["n"])
                            time.sleep(1.0)
                            break
                        time.sleep(1.0)
            elif episode:
                # 明确集数：原样点（含预告）；若只要正片可再改
                try:
                    _page_eval(f"({_JS_CLICK_EPISODE})({int(episode)}, false)", timeout=8.0)
                except Exception:
                    pass
                time.sleep(1.2)
                for _attempt in range(4):
                    try:
                        clicked = _page_eval(
                            f"({_JS_CLICK_EPISODE})({int(episode)}, false)",
                            timeout=8.0,
                        )
                    except Exception as e:
                        clicked = {"ok": False, "reason": str(e)}
                    if isinstance(clicked, dict) and clicked.get("ok"):
                        if clicked.get("n"):
                            episode = int(clicked["n"])
                        time.sleep(1.0)
                        break
                    time.sleep(1.0)

            try:
                now_loc = _page_eval(
                    "({href: location.href, title: document.title || ''})",
                    timeout=5.0,
                ) or {}
            except Exception:
                now_loc = {}
            play_url = str(now_loc.get("href") or STATE.guest_url or cover_url)
            page_title = str(now_loc.get("title") or "")
            STATE.guest_url = play_url

            ep_label = (
                f"最新一集（第{episode}集）"
                if latest and episode
                else (f"第{episode}集" if episode else "")
            )
            on_target = False
            if episode:
                if f"_{int(episode)}_" in page_title or page_title.startswith(
                    f"{show_title}_{int(episode)}"
                ):
                    on_target = True
                if f"/{cid}/" in play_url and str(int(episode)) in page_title:
                    on_target = True
            if episode and (
                on_target or (isinstance(clicked, dict) and clicked.get("ok"))
            ):
                msg = f"正在播放《{show_title}》{ep_label}（腾讯视频）"
            elif episode:
                msg = (
                    f"已打开《{show_title}》详情页（腾讯视频），"
                    f"请在窗口确认{ep_label or f'第{episode}集'}"
                )
            else:
                msg = f"已打开《{show_title}》（腾讯视频）"
            STATE.video = {
                "title": show_title,
                "episode": episode,
                "url": play_url,
            }
            fs = {}
            if "正在播放" in msg or (isinstance(clicked, dict) and clicked.get("ok")) or not episode:
                fs = _enter_fullscreen_after_play()
                if fs.get("ok") and "全屏" not in msg:
                    msg = msg.rstrip("。") + "，已全屏"
            return {
                "ok": True,
                "source": "tencent",
                "message": msg,
                "title": show_title,
                "episode": episode,
                "latest": latest,
                "totalEpisode": hit.get("totalEpisode"),
                "cid": cid,
                "url": play_url,
                "pageTitle": page_title,
                "clicked": clicked,
                "fullscreen": fs,
                "video": STATE.video,
            }

        _load_url(play_url, wait=3.5, guest="tencent")
        ep_label = (
            f"最新一集（第{episode}集）"
            if latest and episode
            else (f"第{episode}集" if episode else "")
        )
        STATE.video = {
            "title": show_title,
            "episode": episode,
            "url": play_url,
        }
        fs = _enter_fullscreen_after_play()
        msg = f"正在播放《{show_title}》{ep_label}（腾讯视频）"
        if fs.get("ok"):
            msg += "，已全屏"
        return {
            "ok": True,
            "source": "tencent",
            "message": msg,
            "title": show_title,
            "episode": episode,
            "latest": latest,
            "totalEpisode": hit.get("totalEpisode"),
            "cid": cid,
            "url": play_url,
            "fullscreen": fs,
            "video": STATE.video,
        }


def _api_play_video_stub(
    query: str,
    episode: int | None,
    source: str,
    *,
    latest: bool = False,
) -> dict[str, Any]:
    """非腾讯源：先打开官网搜索页，后续再精细化。"""
    q = (query or "").strip()
    label = _VIDEO_LABELS.get(source, source)
    if source == "bilibili":
        url = f"https://search.bilibili.com/all?keyword={quote(q)}"
    elif source == "iqiyi":
        url = f"https://so.iqiyi.com/so/q_{quote(q)}"
    else:
        url = _SOURCE_HOME.get(source, _HOME)
    with STATE.lock:
        STATE.last_query = q
        STATE.guest = source
        _show_window()
        _load_url(url, wait=3.0, guest=source)
        STATE.video = {"title": q, "episode": episode, "url": url}
        note = (
            "目标最新一集。"
            if latest
            else (f"目标第{episode}集。" if episode else "")
        )
        return {
            "ok": True,
            "source": source,
            "needPick": True,
            "message": f"已在{label}打开「{q}」搜索页（请在弹出窗口点选）。{note}".strip(),
            "url": url,
            "video": STATE.video,
        }


def _api_play_video(
    query: str,
    *,
    episode: int | None = None,
    source: str = "tencent",
    show: bool = True,
    latest: bool = False,
) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "message": "请提供剧名"}
    src = _normalize_video_source(source)
    _ = show
    if src == "tencent":
        return _api_play_video_tencent(q, episode, latest=latest)
    return _api_play_video_stub(q, episode, src, latest=latest)


def _api_video_login(source: str = "tencent") -> dict[str, Any]:
    src = _normalize_video_source(source)
    home = _SOURCE_HOME.get(src, _HOME)
    with STATE.lock:
        STATE.guest = src
        _show_window()
        _load_url(home, wait=2.5, guest=src)
        gate = _ensure_video_login(src)
        if gate.get("loggedIn"):
            if STATE.headless:
                _hide_window()
            return {
                "ok": True,
                "loggedIn": True,
                "source": src,
                "message": str(gate.get("message") or f"已登录{_VIDEO_LABELS.get(src, src)}，可直接看剧"),
                "login": gate.get("login"),
            }
        return {
            "ok": True,
            "loggedIn": False,
            "needLogin": True,
            "source": src,
            "message": str(gate.get("message") or "请扫码登录"),
            "login": gate.get("login"),
            "clicked": gate.get("clicked"),
        }


def _api_status() -> dict[str, Any]:
    with STATE.lock:
        login: dict[str, Any] = {}
        src = _normalize_video_source(STATE.guest or "tencent")
        try:
            if STATE.ready.is_set() and STATE.window is not None:
                login = _page_eval(_js_login_status_for(src), timeout=6.0) or {}
        except Exception as e:
            login = {"error": str(e)}
        return {
            "ok": True,
            "ready": STATE.ready.is_set(),
            "space": _SPACE_NAME,
            "guest": STATE.guest,
            "guestUrl": STATE.guest_url,
            "lastQuery": STATE.last_query,
            "visible": STATE.visible,
            "loggedIn": bool(login.get("loggedIn")),
            "login": login,
            "video": dict(STATE.video) if STATE.video else {},
            "pid": os.getpid(),
        }


def _api_space_status() -> dict[str, Any]:
    return _api_status()


def _api_space_load(url: str, guest: str = "", show: bool = True) -> dict[str, Any]:
    u = (url or "").strip()
    if not u.startswith("http"):
        return {"ok": False, "message": "url 无效"}
    g = guest or STATE.guest or "web"
    with STATE.lock:
        if show:
            _show_window()
        else:
            _hide_window()
        _load_url(u, wait=2.5, guest=g)
        return {"ok": True, "space": _SPACE_NAME, "message": "已加载", "url": u}


def _api_space_eval(script: str, timeout: float = 12.0) -> dict[str, Any]:
    s = (script or "").strip()
    if not s:
        return {"ok": False, "message": "script 为空"}
    with STATE.lock:
        try:
            result = _eval_js(s, timeout=float(timeout) if timeout else 12.0)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "result": result, "message": "ok"}


def _api_show(minimize: bool = False) -> dict[str, Any]:
    with STATE.lock:
        if minimize:
            _hide_window()
            return {"ok": True, "message": "已收回到视频空间（离屏）"}
        _show_window()
        return {"ok": True, "message": "已显示科科斯视频空间"}


def _api_control(action: str) -> dict[str, Any]:
    act = (action or "").strip().lower()
    with STATE.lock:
        try:
            if act in ("pause", "stop"):
                _eval_js(
                    """
(() => {
  const v = document.querySelector('video');
  if (v) { try { v.pause(); return 'video.pause'; } catch (e) {} }
  return null;
})()
""",
                    timeout=5.0,
                )
                return {"ok": True, "message": "已暂停"}
            if act in ("play", "resume"):
                _eval_js(
                    """
(() => {
  const v = document.querySelector('video');
  if (v) { try { v.play(); return 'video.play'; } catch (e) {} }
  return null;
})()
""",
                    timeout=5.0,
                )
                return {"ok": True, "message": "已继续播放"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
        return {"ok": False, "message": f"不支持的控制: {act}"}


def _api_close() -> dict[str, Any]:
    def _die() -> None:
        time.sleep(0.3)
        try:
            if STATE.window is not None:
                STATE.window.destroy()
        except Exception:
            pass
        _clear_state()
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在关闭科科斯视频空间"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "ready": STATE.ready.is_set(),
                    "pid": os.getpid(),
                    "space": _SPACE_NAME,
                    "guest": STATE.guest,
                },
            )
            return
        if path in ("/space/status", "/status"):
            if not STATE.ready.wait(timeout=30):
                self._send(503, {"ok": False, "message": "视频空间尚未就绪"})
                return
            self._send(200, _api_space_status() if path == "/space/status" else _api_status())
            return
        self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not STATE.ready.wait(timeout=45):
            self._send(503, {"ok": False, "message": "视频空间尚未就绪"})
            return
        data = self._read_json()
        try:
            if path == "/space/load":
                self._send(
                    200,
                    _api_space_load(
                        str(data.get("url") or ""),
                        guest=str(data.get("guest") or ""),
                        show=bool(data.get("show", True)),
                    ),
                )
                return
            if path == "/space/eval":
                self._send(
                    200,
                    _api_space_eval(
                        str(data.get("script") or ""),
                        timeout=float(data.get("timeout") or 12.0),
                    ),
                )
                return
            if path == "/space/show":
                self._send(200, _api_show(minimize=False))
                return
            if path == "/space/hide":
                self._send(200, _api_show(minimize=True))
                return
            if path == "/play_video":
                ep = data.get("episode")
                episode = int(ep) if ep is not None and str(ep).strip().isdigit() else None
                if episode is None and isinstance(ep, (int, float)):
                    episode = int(ep)
                latest = bool(data.get("latest"))
                # LLM 可能传 episode="latest"
                if isinstance(ep, str) and ep.strip().lower() in ("latest", "newest", "最新"):
                    latest = True
                    episode = None
                self._send(
                    200,
                    _api_play_video(
                        str(data.get("query") or ""),
                        episode=episode,
                        source=str(data.get("source") or data.get("guest") or "tencent"),
                        show=bool(data.get("show", True)),
                        latest=latest,
                    ),
                )
                return
            if path == "/video/login":
                self._send(
                    200,
                    _api_video_login(str(data.get("source") or data.get("guest") or "tencent")),
                )
                return
            if path == "/control":
                self._send(200, _api_control(str(data.get("action") or "")))
                return
            if path == "/show":
                self._send(200, _api_show(minimize=bool(data.get("minimize"))))
                return
            if path == "/close":
                self._send(200, _api_close())
                return
        except Exception as e:
            logger.exception("API error")
            self._send(
                500,
                {"ok": False, "message": str(e), "trace": traceback.format_exc()[-800:]},
            )
            return
        self._send(404, {"ok": False, "message": "not found"})


def _serve_http() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((_DEFAULT_HOST, 0), _Handler)
    port = int(server.server_address[1])
    _write_state(port, os.getpid())
    t = threading.Thread(target=server.serve_forever, name="video-space-http", daemon=True)
    t.start()
    logger.info("视频空间 API on http://%s:%s", _DEFAULT_HOST, port)
    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        import webview
    except ImportError:
        print("缺少 pywebview，请先 pip install pywebview", file=sys.stderr)
        return 1

    storage = str(_storage_path())

    try:
        extra = (os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
        flag = "--remote-allow-origins=*"
        if flag not in extra:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                f"{extra} {flag}".strip()
            )
    except Exception:
        pass

    try:
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        webview.settings["REMOTE_DEBUGGING_PORT"] = _REMOTE_DEBUG_PORT
    except Exception:
        pass

    try:
        import webview.platforms.edgechromium as _edge

        _orig_edge_init = _edge.EdgeChrome.__init__

        def _edge_init_no_taskbar(self, form, window, cache_dir):  # type: ignore[no-untyped-def]
            try:
                form.ShowInTaskbar = False
            except Exception:
                pass
            result = _orig_edge_init(self, form, window, cache_dir)
            try:
                props = self.webview.CreationProperties
                args = str(getattr(props, "AdditionalBrowserArguments", None) or "")
                if "remote-allow-origins" not in args:
                    props.AdditionalBrowserArguments = (
                        args + " --remote-allow-origins=*"
                    ).strip()
                    self.webview.CreationProperties = props
            except Exception:
                pass
            return result

        _edge.EdgeChrome.__init__ = _edge_init_no_taskbar  # type: ignore[method-assign]
    except Exception:
        logger.debug("EdgeChrome ShowInTaskbar patch skipped", exc_info=True)

    logger.info(
        "%s；远程调试: http://127.0.0.1:%s",
        _SPACE_NAME,
        _REMOTE_DEBUG_PORT,
    )

    server = _serve_http()
    STATE.guest = "tencent"
    STATE.guest_url = _HOME

    def _on_ready() -> None:
        STATE.ready.set()
        try:
            _hide_window()
        except Exception:
            pass

    def _on_before_show() -> None:
        try:
            _set_show_in_taskbar_ui(False)
        except Exception:
            pass
        try:
            _native_hwnd(touch_forms=True)
        except Exception:
            pass
        try:
            _hide_window()
        except Exception:
            pass

        def _late_stealth() -> None:
            time.sleep(0.4)
            try:
                _native_hwnd(touch_forms=False)
                _hide_window()
            except Exception:
                pass

        threading.Thread(target=_late_stealth, daemon=True).start()

    create_kwargs: dict[str, Any] = {
        "width": _WINDOW_SIZE[0],
        "height": _WINDOW_SIZE[1],
        "x": _OFFSCREEN_XY[0],
        "y": _OFFSCREEN_XY[1],
        "hidden": False,
        "minimized": False,
        "confirm_close": False,
        "text_select": True,
    }
    try:
        window = webview.create_window(_SPACE_NAME, _HOME, focus=False, **create_kwargs)
    except TypeError:
        window = webview.create_window(_SPACE_NAME, _HOME, **create_kwargs)
    STATE.window = window

    if window is not None:
        try:
            window.events.before_show += _on_before_show
        except Exception:
            try:
                window.before_show += _on_before_show  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            window.events.loaded += lambda: STATE.ready.set()
        except Exception:
            pass

    threading.Timer(2.0, _on_ready).start()

    start_kwargs: dict[str, Any] = {
        "private_mode": False,
        "storage_path": storage,
        "debug": False,
    }
    try:
        webview.start(gui="edgechromium", **start_kwargs)
    except Exception:
        logger.exception("edgechromium 启动失败，回退默认 GUI")
        try:
            webview.start(**start_kwargs)
        except Exception:
            logger.exception("webview 启动失败")
            _clear_state()
            try:
                server.shutdown()
            except Exception:
                pass
            return 1
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        _clear_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
