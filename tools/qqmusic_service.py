"""科科斯体内空间宿主 + QQ 音乐访客（WebView2）。

独立进程：本地 HTTP API + 唯一 pywebview 窗口（默认离屏，属于科科斯身体而非桌面客户端）。
访客 qqmusic 在体内空间打开 y.qq.com；登录态优先 data/inner_space/storage。
仅本机 127.0.0.1 可访问。

播放策略：定位页面按钮后用系统真实鼠标点击（绕过 WebView 自动播放手势限制）。
入口推荐：python -m kekesi.inner_space.host
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
from urllib.error import HTTPError, URLError
from urllib.request import (
    Request,
    build_opener,
    getproxies,
    urlopen,
    ProxyHandler,
)

logger = logging.getLogger("kekesi.inner_space")

print(f"[qqmusic_service] loaded from {__file__}", flush=True)

_HOME = "https://y.qq.com/"
_SEARCH_URL = "https://y.qq.com/n/ryqq_v2/search?w={query}&t=song"
_SONG_URL = "https://y.qq.com/n/ryqq_v2/songDetail/{mid}"
_QQ_PROFILE_URLS = (
    "https://y.qq.com/n/ryqq_v2/profile",
    "https://y.qq.com/n/ryqq/profile",
    "https://y.qq.com/portal/profile.html",
)
# 「我喜欢」歌曲列表页（F12 实探：含 a.mod_btn_green「播放全部」）
_QQ_LIKED_SONG_URLS = (
    "https://y.qq.com/n/ryqq_v2/profile/like/song",
    "https://y.qq.com/n/ryqq/profile/like/song",
)
_NETEASE_LIKED_URL = "https://music.163.com/#/my/m/music/like"
_DEFAULT_HOST = "127.0.0.1"

# 多平台网页访客（体内空间）
_NETEASE_HOME = "https://music.163.com/"
_NETEASE_SEARCH = "https://music.163.com/#/search/m/?s={query}&type=1"
_NETEASE_SONG = "https://music.163.com/#/song?id={sid}"
_KUGOU_HOME = "https://www.kugou.com/"
_KUGOU_SEARCH = (
    "https://www.kugou.com/yy/html/search.html#searchType=song&searchKeyWord={query}"
)

_SOURCE_HOME = {
    "qq": _HOME,
    "qqmusic": _HOME,
    "netease": _NETEASE_HOME,
    "kugou": _KUGOU_HOME,
}


def _normalize_source(raw: str | None) -> str:
    t = (raw or "").strip().lower().replace(" ", "")
    if t in ("netease", "网易云", "网易云音乐", "cloudmusic"):
        return "netease"
    if t in ("kugou", "酷狗", "酷狗音乐"):
        return "kugou"
    return "qq"


_JS_FIND_NETEASE_SONGS = r"""
(() => {
  const ids = [];
  const names = [];
  const seen = new Set();
  const add = (id, name) => {
    if (!id || seen.has(String(id))) return;
    seen.add(String(id));
    ids.push(String(id));
    names.push(String(name || '').trim().slice(0, 80));
  };
  document.querySelectorAll('a[href*="song?id="], a[href*="/song?id="]').forEach((a) => {
    const href = a.getAttribute('href') || a.href || '';
    const m = href.match(/song\?id=(\d+)/);
    if (m) add(m[1], (a.textContent || '').trim());
  });
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  const re = /"id"\s*:\s*(\d{5,12}).{0,80}"name"\s*:\s*"([^"]{1,80})"/g;
  let mm;
  while ((mm = re.exec(html)) && ids.length < 12) {
    add(mm[1], mm[2]);
  }
  const re2 = /song\?id=(\d+)/g;
  while ((mm = re2.exec(html)) && ids.length < 12) {
    add(mm[1], '');
  }
  return { ids, names, count: ids.length, href: location.href, title: document.title || '' };
})()
"""

_JS_FIND_KUGOU_SONGS = r"""
(() => {
  const ids = [];
  const names = [];
  const hrefs = [];
  const seen = new Set();
  const add = (id, name, href) => {
    const key = id || href;
    if (!key || seen.has(String(key))) return;
    seen.add(String(key));
    ids.push(String(id || ''));
    names.push(String(name || '').trim().slice(0, 80));
    hrefs.push(String(href || ''));
  };
  document.querySelectorAll('a[href*="mixsong"], a[href*="/song/"], a[href*="hash="]').forEach((a) => {
    const href = a.getAttribute('href') || a.href || '';
    if (!/song|mixsong|hash=/i.test(href)) return;
    if (/search|album|singer|playlist/i.test(href) && !/mixsong|\/song\//i.test(href)) return;
    let id = '';
    let m = href.match(/mixsong\/(\w+)/i) || href.match(/[?&]hash=([a-fA-F0-9]+)/);
    if (m) id = m[1];
    add(id, (a.textContent || '').trim(), href);
  });
  // 搜索结果行上的播放按钮旁歌名
  document.querySelectorAll('[class*="song"], li, tr').forEach((el) => {
    const a = el.querySelector('a[href*="mixsong"], a[href*="hash="]');
    if (!a) return;
    const href = a.getAttribute('href') || a.href || '';
    let id = '';
    let m = href.match(/mixsong\/(\w+)/i) || href.match(/[?&]hash=([a-fA-F0-9]+)/);
    if (m) id = m[1];
    add(id, (a.textContent || '').trim(), href);
  });
  return { ids, names, hrefs, count: ids.length, href: location.href, title: document.title || '' };
})()
"""

_JS_LOGIN_STATUS_GENERIC = r"""
(() => {
  const text = (document.body && document.body.innerText) || '';
  const head = text.slice(0, 2500);
  const loginBtn = document.querySelector('a.js_login, .js_login, a.top_login__link, a.login, .top_login__link');
  const loginVisible = !!loginBtn && /登录|登陆/.test(
    ((loginBtn.textContent || '') + (loginBtn.getAttribute('title') || ''))
  );
  const avatar = document.querySelector(
    'img.user_info__avatar, .header__avatar img, .js_logined img, .user_avatar, .m_head img, .top_head img'
  );
  const cookieAuth = /MUSIC_U=/.test(document.cookie)
    || /KuGoo=/.test(document.cookie);
  // kg_mid 是酷狗设备指纹 Cookie，未登录也下发，不能当登录依据
  // 「我的音乐/个人中心」多为常驻导航，不作登录依据；以登录按钮/头像/鉴权 cookie 为准
  const loggedIn = !loginVisible && (cookieAuth || !!avatar || /退出登录/.test(head));
  return {
    loggedIn: !!loggedIn,
    loginVisible: !!loginVisible,
    href: location.href,
    title: document.title || '',
  };
})()
"""

_JS_LOGIN_STATUS = r"""
(() => {
  const text = (document.body && document.body.innerText) || '';
  const loginBtn = document.querySelector('a.js_login, a.top_login__link.js_login, a.top_login__link');
  const loginVisible = !!loginBtn && /登录|登陆/.test(
    ((loginBtn.textContent || '') + (loginBtn.getAttribute('title') || ''))
  );
  const avatar = document.querySelector(
    'img.user_info__avatar, .header__avatar img, .mod_header img[src*="qzapp"], .js_logined img, .user_avatar, img.js_userInfo_img, img.top_login__cover'
  );
  const cookieHint = /(?:^|;)\s*uin=o?\d+/i.test(document.cookie)
    || /(?:^|;)\s*qm_keyst=/i.test(document.cookie)
    || /(?:^|;)\s*MUSIC_U=/.test(document.cookie);
  // 「我的音乐/个人中心」是常驻导航，不能作为登录依据；以头像/登录按钮/鉴权 cookie 为准
  // 登录后 top_login__link 元素不消失，只是把"登录"文字换成头像，
  // 因此头像一旦出现即判定已登录，避免严格渲染时误报未登录
  const loggedIn = !!avatar || (!loginVisible && (cookieHint || /退出登录/.test(text.slice(0, 2000))));
  return {
    loggedIn: !!loggedIn,
    loginVisible: !!loginVisible,
    avatar: !!avatar,
    cookieHint: !!cookieHint,
    href: location.href,
    title: document.title || '',
  };
})()
"""

_JS_CLICK_LOGIN = r"""
(() => {
  const candidates = [
    'a.top_login__link.js_login', 'a.js_login', 'a.top_login__link',
    'a.login', '.m-tophead a.login',
  ];
  for (const s of candidates) {
    const el = document.querySelector(s);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const t = ((el.textContent || '') + (el.getAttribute('title') || '')).trim();
    if ((/登录|登陆/.test(t) || s.includes('js_login')) && r.width > 0) {
      el.click();
      return { ok: true, via: s };
    }
  }
  // 兜底：头部区域内文字恰为「登录」的元素（网易云/酷狗顶栏），点上最近的可点祖先
  const roots = Array.from(
    document.querySelectorAll('header, .m-tophead, .top, #top, .header')
  );
  if (roots.length === 0 && document.body) roots.push(document.body);
  for (const root of roots) {
    const els = Array.from(root.querySelectorAll('a, button, span, div'));
    for (const el of els) {
      const t = (el.textContent || '').trim();
      if (t !== '登录' && t !== '登 录' && t !== '登陆') continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const target = (el.closest && el.closest('a, button')) || el;
      target.click();
      return { ok: true, via: 'text:登录' };
    }
  }
  return { ok: false, reason: '未找到登录入口' };
})()
"""

_JS_FIND_SONGS = r"""
(() => {
  const mids = [];
  const names = [];
  const seen = new Set();
  const add = (mid, name) => {
    if (!mid || seen.has(mid)) return;
    if (String(mid).length < 8 || String(mid).length > 24) return;
    seen.add(mid);
    mids.push(String(mid));
    names.push(String(name || '').trim().slice(0, 80));
  };
  document.querySelectorAll('a[href*="songDetail"], a[href*="songmid"]').forEach((a) => {
    const href = a.getAttribute('href') || a.href || '';
    let m = href.match(/songDetail\/([a-zA-Z0-9]+)/);
    if (!m) m = href.match(/songmid=([a-zA-Z0-9]+)/i);
    if (m) add(m[1], (a.textContent || '').trim());
  });
  document.querySelectorAll('[data-mid], [data-songmid], [mid]').forEach((el) => {
    add(el.getAttribute('data-mid') || el.getAttribute('data-songmid') || el.getAttribute('mid'),
        (el.textContent || '').trim());
  });
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  const re = /"songmid"\s*:\s*"([a-zA-Z0-9]+)"/g;
  let mm;
  while ((mm = re.exec(html)) && mids.length < 15) {
    add(mm[1], '');
  }
  return { mids, names, count: mids.length, href: location.href, title: document.title || '' };
})()
"""

_JS_FIND_PLAY_TARGET = r"""
(() => {
  const chromeH = Math.max(0, (window.outerHeight - window.innerHeight) || 0);
  const toTarget = (el, why, name) => {
    const r = el.getBoundingClientRect();
    return {
      ok: true,
      selector: why,
      name: (name || (el.innerText || el.textContent || '')).trim().slice(0, 80),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      screenX: Math.round(window.screenX + r.left + r.width / 2),
      screenY: Math.round(window.screenY + r.top + r.height / 2 + chromeH),
      href: location.href,
    };
  };
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none'
      && st.pointerEvents !== 'none' && r.bottom > 0 && r.top < window.innerHeight + 80;
  };
  const inFooter = (el) => !!(el.closest && el.closest('.player__ft, .player_bar, #player, .mod_player'));

  // 切歌时优先点歌曲页/列表播放，避开底部播放条（底栏常停留在上一首）
  const pageSelectors = [
    '.data__actions a.mod_btn_green',
    '.data__actions .mod_btn',
    '.data__actions a',
    '.song_play__btn',
    '.data__cover .btn_play',
    'a.mod_btn_green',
    '.js_play',
    'a.list_menu__play',
    'a.list_menu__item.list_menu__play',
    '.songlist__item .btn_play',
    '.playlist__list .btn_play',
    '[class*="list_menu__play"]',
  ];
  for (const s of pageSelectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    for (const el of nodes) {
      if (!isVisible(el) || inFooter(el)) continue;
      const t = ((el.innerText || el.textContent || '') + (el.getAttribute('title') || '')).trim();
      if (t && !/播放|play|试听/i.test(t) && !s.includes('play') && !s.includes('btn')) continue;
      return toTarget(el, 'page:' + s, t || '播放');
    }
  }

  // 按可见文本「播放」查找，排除底栏
  const all = Array.from(document.querySelectorAll('button, a, span, div, i, em'));
  const textHits = [];
  for (const el of all) {
    if (!isVisible(el) || inFooter(el)) continue;
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    if (t === '播放' || t === '立即播放' || t === '播放全部' || t === '开始播放') {
      textHits.push(el);
    }
  }
  textHits.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  if (textHits.length) {
    return toTarget(textHits[0], 'text:播放', textHits[0].innerText || '播放');
  }

  // 最后才用底部播放键（仅当页面上没有更合适的）
  const footerSelectors = [
    'div.player__ft a.btn_big_play',
    'a.btn_big_play',
    '.btn_big_play',
  ];
  for (const s of footerSelectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    for (const el of nodes) {
      if (!isVisible(el)) continue;
      return toTarget(el, 'footer:' + s, '');
    }
  }

  return {
    ok: false,
    reason: '未找到可点击的播放目标',
    href: location.href,
    title: document.title || '',
    sample: (document.body && document.body.innerText || '').slice(0, 300),
  };
})()
"""

_JS_DUMP = r"""
(() => {
  const items = [];
  const push = (el, why) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    items.push({
      why,
      tag: el.tagName,
      cls: String(el.className || '').slice(0, 160),
      text: (el.innerText || el.textContent || '').trim().slice(0, 40),
      aria: (el.getAttribute('aria-label') || '').slice(0, 40),
      html: (el.outerHTML || '').slice(0, 300),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    });
  };
  document.querySelectorAll('button, a, [role=button]').forEach((el) => push(el, 'ctrl'));
  document.querySelectorAll('[class*=play], [class*=Play]').forEach((el) => push(el, 'cls'));
  const body = (document.body && document.body.innerText || '').slice(0, 800);
  return { href: location.href, title: document.title, n: items.length, items: items.slice(0, 50), body };
})()
"""

_JS_INSPECT_PLAY = r"""
(() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const parent = el.parentElement;
    return {
      sel,
      tag: el.tagName,
      className: String(el.className || ''),
      text: (el.innerText || '').trim().slice(0, 40),
      outerHTML: (el.outerHTML || '').slice(0, 500),
      parentHTML: parent ? (parent.outerHTML || '').slice(0, 600) : '',
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
      visible: r.width > 2 && r.height > 2,
    };
  };
  const ft = document.querySelector('div.player__ft');
  return {
    href: location.href,
    title: document.title || '',
    playerFtHTML: ft ? (ft.outerHTML || '').slice(0, 1200) : null,
    btn: pick('div.player__ft a.btn_big_play') || pick('a.btn_big_play') || pick('.btn_big_play'),
    prev: pick('div.player__ft a.btn_big_prev') || pick('a.btn_big_prev'),
    next: pick('div.player__ft a.btn_big_next') || pick('a.btn_big_next'),
    audioCount: document.querySelectorAll('audio, video').length,
    cookieHasUin: /uin=/.test(document.cookie),
  };
})()
"""

_JS_AUDIO_STATE = r"""
(() => {
  const medias = Array.from(document.querySelectorAll('audio, video'));
  const info = medias.map((m) => ({
    tag: m.tagName,
    paused: m.paused,
    currentTime: m.currentTime || 0,
    duration: m.duration || 0,
    src: (m.currentSrc || m.src || '').slice(0, 120),
  }));
  const playBtn = document.querySelector(
    'div.player__ft a.btn_big_play, a.btn_big_play, .btn_big_play--pause, .player_bar .btn_big_play, #player .btn_big_play, .btn_big_play'
  );
  const btnClass = playBtn ? (playBtn.className || '') : '';
  // 播放中常见：class 变为 btn_big_play--pause（图标变成可暂停）
  const playingByBtn = /btn_big_play--pause|--playing/i.test(btnClass);
  const playingByMedia = info.some((m) => !m.paused && (m.src || m.currentTime > 0));
  const playing = playingByBtn || playingByMedia;

  let name = '';
  let artist = '';
  const nameEl = document.querySelector(
    '.player_music__info .song_name a, .player_music__info .song_name, .song_info__name a, .song_info__name'
  );
  const artistEl = document.querySelector(
    '.player_music__info .singer_name a, .player_music__info .singer_name, .song_info__singer a, .playlist__author'
  );
  name = ((nameEl && nameEl.textContent) || '').trim();
  artist = ((artistEl && artistEl.textContent) || '').trim();
  name = name.replace(/^歌曲名[：:]/, '').trim();
  artist = artist.replace(/^歌手[：:]/, '').trim();
  if ((!name || name.length > 40) && document.title) {
    const m = document.title.match(/^(.+?)\s*-\s*(.+?)\s*-/);
    if (m) { name = name || m[1].trim(); artist = artist || m[2].trim(); }
  }
  return {
    playing,
    mediaCount: medias.length,
    medias: info.slice(0, 3),
    name: name.slice(0, 80),
    artist: artist.slice(0, 80),
    btnClass,
    title: (document.title || '').slice(0, 120),
    href: location.href,
  };
})()
"""

_JS_CONTROL = r"""
(action) => {
  const clickFirst = (selectors) => {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      return {
        ok: true,
        selector: s,
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
        screenX: Math.round(window.screenX + r.left + r.width / 2),
        screenY: Math.round(window.screenY + r.top + r.height / 2 + ((window.outerHeight - window.innerHeight) || 0)),
      };
    }
    return null;
  };
  const map = {
    play: [
      'div.player__ft a.btn_big_play', 'a.btn_big_play', '.btn_big_play',
      '.player_bar .btn_big_play', '#player .btn_big_play',
    ],
    pause: [
      'div.player__ft a.btn_big_play--pause', 'a.btn_big_play--pause',
      '.btn_big_play--pause', '.player_bar .btn_big_play',
    ],
    playpause: [
      'div.player__ft a.btn_big_play', 'a.btn_big_play', '.btn_big_play',
    ],
    next: ['div.player__ft a.btn_big_next', 'a.btn_big_next', '.btn_big_next'],
    previous: ['div.player__ft a.btn_big_prev', 'a.btn_big_prev', '.btn_big_prev'],
  };
  const hit = clickFirst(map[action] || map.playpause);
  if (hit) return hit;
  return { ok: false, reason: '未找到播放控制按钮' };
}
"""

_JS_DEBUG = r"""
(() => {
  const sel = [
    '.btn_play', '.btn_big_play', 'a[href*="songDetail"]', '.songlist__item',
    'audio', 'video', 'a.js_login', '.js_logined'
  ];
  const counts = {};
  for (const s of sel) counts[s] = document.querySelectorAll(s).length;
  return {
    href: location.href,
    title: document.title || '',
    counts,
    cookieHasUin: /uin=/.test(document.cookie),
  };
})()
"""


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "inner_space"


def _legacy_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "qqmusic"


def _state_path() -> Path:
    return _data_dir() / "service.json"


def _storage_path() -> Path:
    """优先体内空间 storage；若新目录为空且旧 qqmusic 有登录态则沿用旧路径。"""
    p = _data_dir() / "storage"
    legacy = _legacy_data_dir() / "storage"
    try:
        new_has = p.is_dir() and any(p.iterdir())
    except OSError:
        new_has = False
    try:
        old_has = legacy.is_dir() and any(legacy.iterdir())
    except OSError:
        old_has = False
    if not new_has and old_has:
        return legacy
    p.mkdir(parents=True, exist_ok=True)
    return p


def purge_qq_play_history_disk() -> list[str]:
    """关掉宿主后调用：只清 Session Storage（会话级播放队列）。

    不要删 Local Storage / IndexedDB / Cache —— 会把 QQ 网页播放器搞坏，
    出现「未知、未开播」；登录 Cookie 也在别处，但 SPA 状态依赖 LS/IDB。
    """
    import shutil

    removed: list[str] = []
    roots = [_storage_path(), _legacy_data_dir() / "storage"]
    # 仅会话级；历史粘滞主要靠下次点歌走 songDetail 强制切歌
    kill_names = ("Session Storage",)
    for root in roots:
        if not root.is_dir():
            continue
        for default in root.rglob("Default"):
            if not default.is_dir():
                continue
            if "EBWebView" not in str(default) and "Default" != default.name:
                continue
            for name in kill_names:
                p = default / name
                if p.exists():
                    try:
                        shutil.rmtree(p, ignore_errors=True)
                        removed.append(str(p))
                    except OSError:
                        pass
    return removed


_REMOTE_DEBUG_PORT = 9222
_SPACE_NAME = "科科斯体内空间"


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
    text = json.dumps(payload, ensure_ascii=False)
    _state_path().write_text(text, encoding="utf-8")
    # 兼容仍读旧路径的客户端
    try:
        ld = _legacy_data_dir()
        ld.mkdir(parents=True, exist_ok=True)
        (ld / "service.json").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _clear_state() -> None:
    for path in (_state_path(), _legacy_data_dir() / "service.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class _PlayerState:
    def __init__(self) -> None:
        self.window: Any = None
        self.lock = threading.RLock()
        self.last_query = ""
        self.ready = threading.Event()
        # 默认内置后台：不占桌面；登录扫码时再亮出窗口
        self.headless = True
        self.visible = False
        # 当前体内空间访客：qqmusic / 空 / 其它
        self.guest = ""
        self.guest_url = ""
        # 缓存 HWND，供 HTTP 线程用 Win32 操作（禁止在非 UI 线程碰 WinForms）
        self.hwnd = 0


STATE = _PlayerState()

# 离屏坐标：hidden 可能导致 WebView2 停播，故用移出屏幕代替
_OFFSCREEN_XY = (-32000, -32000)
_VISIBLE_XY = (120, 80)
_WINDOW_SIZE = (960, 640)


def _eval_js_cdp(script: str, timeout: float) -> Any:
    """通过 WebView2 远程调试口 (CDP) 执行 JS。

    宿主进程里 pywebview 的 evaluate_js 可能整体挂起（2026-10-16 实测：
    所有调用 TimeoutError，而 CDP 直连 9222 始终可用），故 CDP 优先。
    返回 _CDP_FAILED 表示调试口不可用，调用方应回落 pywebview 桥。
    """
    import json as _json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:9222/json", timeout=2.0
        ) as resp:
            targets = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return _CDP_FAILED
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return _CDP_FAILED
    try:
        import websocket  # websocket-client
    except ImportError:
        return _CDP_FAILED
    try:
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=timeout)
    except Exception:
        return _CDP_FAILED
    try:
        ws.send(
            _json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": script, "returnByValue": True},
                }
            )
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            remain = max(0.5, deadline - time.time())
            ws.settimeout(remain)
            try:
                msg = _json.loads(ws.recv())
            except (websocket.WebSocketTimeoutException, OSError):
                raise TimeoutError("CDP 页面脚本执行超时")
            if msg.get("id") != 1:
                continue
            res = msg.get("result", {}).get("result", {})
            if msg["result"].get("exceptionDetails"):
                raise RuntimeError(
                    str(msg["result"]["exceptionDetails"].get("exception", {}).get("description") or "页面脚本执行异常")
                )
            return res.get("value")
        raise TimeoutError("CDP 页面脚本执行超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass


_CDP_FAILED = object()


def _eval_js(script: str, timeout: float = 12.0) -> Any:
    try:
        v = _eval_js_cdp(script, timeout)
        if v is not _CDP_FAILED:
            return v
    except Exception:
        pass  # CDP 失败回落 pywebview 桥
    window = STATE.window
    if window is None:
        raise RuntimeError("体内空间未就绪")
    result_box: dict[str, Any] = {}
    done = threading.Event()

    def _run() -> None:
        try:
            result_box["value"] = window.evaluate_js(script)
        except Exception as e:
            result_box["error"] = str(e)
        finally:
            done.set()

    threading.Thread(target=_run, name="inner-space-eval", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("页面脚本执行超时")
    if "error" in result_box:
        raise RuntimeError(result_box["error"])
    return result_box.get("value")


def _load_url(url: str, wait: float = 2.0, *, guest: str | None = None) -> None:
    window = STATE.window
    if window is None:
        raise RuntimeError("体内空间未就绪")
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
        if "科科斯体内空间" in title or ("科科斯" in title and "体内" in title):
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value) if found.value else 0


def _native_hwnd(*, touch_forms: bool = False) -> int:
    """取体内空间 HWND。HTTP 线程务必 touch_forms=False，只用缓存/枚举。"""
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
    """仅可在 UI 线程调用（before_show / EdgeChrome 初始化）。"""
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
    swp_nozorder = 0x0004
    swp_noactivate = 0x0010
    try:
        user32.SetWindowPos(hwnd, 0, int(x), int(y), int(width), int(height), swp_nozorder | swp_noactivate)
    except Exception as e:
        logger.debug("win32 move failed: %s", e)


def _win32_set_stealth(hwnd: int, stealth: bool) -> None:
    """不进任务栏、不进 Alt+Tab；窗口仍保持「可见」以免 WebView2 停播。"""
    if not hwnd:
        return
    import ctypes

    user32 = ctypes.windll.user32
    gwl_exstyle = -20
    ws_ex_appwindow = 0x00040000
    ws_ex_toolwindow = 0x00000080
    swp_nosize = 0x0001
    swp_nomove = 0x0002
    swp_nozorder = 0x0004
    swp_framechanged = 0x0020
    swp_noactivate = 0x0010
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
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            swp_nosize | swp_nomove | swp_nozorder | swp_framechanged | swp_noactivate,
        )
    except Exception as e:
        logger.debug("win32 stealth failed: %s", e)


def _show_window() -> None:
    """显示到桌面（仅登录/用户明确要求时）。全程 Win32，避免非 UI 线程碰 WinForms。"""
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
    """收回体内：离屏 + 不占任务栏/Alt+Tab。全程 Win32，避免死锁。"""
    hwnd = _native_hwnd(touch_forms=False)
    _win32_move_resize(
        hwnd, _OFFSCREEN_XY[0], _OFFSCREEN_XY[1], _WINDOW_SIZE[0], _WINDOW_SIZE[1]
    )
    _win32_set_stealth(hwnd, True)
    STATE.visible = False


def _minimize_window() -> None:
    """兼容旧接口：后台模式下等价于藏窗。"""
    _hide_window()


def _ensure_dpi_aware() -> None:
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _find_player_hwnd() -> int:
    """查找体内空间主窗口句柄。"""
    import ctypes
    from ctypes import wintypes

    native = _native_hwnd()
    if native:
        return native

    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # type: ignore[no-untyped-def]
        # 隐身态仍可能 IsWindowVisible=True（故意不 SW_HIDE，以免停播）
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        if "科科斯体内空间" in title or (
            "科科斯" in title and ("QQ" in title or "音乐" in title or "体内" in title)
        ):
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value) if found.value else 0


def _find_webview_child(parent: int) -> int:
    """尽量找到 WebView2/Chromium 子窗口，客户区坐标更准。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    best = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # type: ignore[no-untyped-def]
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value or ""
        if "Chrome_RenderWidgetHostHWND" in cls or "Chrome_WidgetWin" in cls or "Intermediate D3D" in cls:
            if user32.IsWindowVisible(hwnd):
                best.value = hwnd
                return False
        return True

    if parent:
        user32.EnumChildWindows(parent, _enum, 0)
    return int(best.value) if best.value else 0


def _client_to_screen(hwnd: int, client_x: int, client_y: int) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pt = wintypes.POINT(int(client_x), int(client_y))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _post_click_client(hwnd: int, client_x: int, client_y: int) -> None:
    """向目标窗口客户区发送鼠标消息（不抢系统光标，对 WebView 更稳）。"""
    import ctypes

    user32 = ctypes.windll.user32
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001
    lparam = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)


def _real_click_screen(screen_x: int, screen_y: int) -> None:
    """SendInput 屏幕坐标点击（兜底）。"""
    import ctypes
    from ctypes import wintypes

    _ensure_dpi_aware()
    user32 = ctypes.windll.user32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    screen_w = user32.GetSystemMetrics(0) or 1
    screen_h = user32.GetSystemMetrics(1) or 1
    abs_x = int(screen_x * 65535 / screen_w)
    abs_y = int(screen_y * 65535 / screen_h)
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def _send(flags: int, x: int = 0, y: int = 0) -> None:
        inp = INPUT(type=0, mi=MOUSEINPUT(x, y, 0, flags, 0, None))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    user32.SetCursorPos(int(screen_x), int(screen_y))
    time.sleep(0.05)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y)
    time.sleep(0.03)
    _send(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y)
    time.sleep(0.05)
    _send(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y)


def _js_click_selector(selector: str) -> bool:
    sel = json.dumps(selector)
    try:
        return bool(
            _eval_js(
                f"""
(() => {{
  const el = document.querySelector({sel});
  if (!el) return false;
  el.click();
  return true;
}})()
""",
                timeout=5.0,
            )
        )
    except Exception:
        return False


def _click_target(target: dict[str, Any]) -> bool:
    """离屏后台优先 JS / 客户区 PostMessage；不把窗口移回屏内。"""
    sel = str(target.get("selector") or "")
    if sel and not sel.startswith("text:") and not sel.startswith("aria:"):
        if _js_click_selector(sel):
            return True
    if _js_click_selector("div.player__ft a.btn_big_play") or _js_click_selector(
        "a.btn_big_play"
    ):
        return True

    # 离屏也可用：向 WebView 子窗口发客户区点击（不要 SendInput 屏幕坐标）
    parent = _find_player_hwnd()
    child = _find_webview_child(parent)
    hwnd = child or parent
    cx = target.get("x")
    cy = target.get("y")
    try:
        if hwnd and isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
            _post_click_client(hwnd, int(cx), int(cy))
            return True
    except Exception as e:
        logger.warning("离屏客户区点击失败: %s", e)
    return False


def _wait_playing(timeout: float = 6.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            last = _eval_js(_JS_AUDIO_STATE, timeout=8.0) or {}
        except Exception as e:
            last = {"playing": False, "error": str(e)}
        if last.get("playing"):
            return last
        # 播放按钮变 pause 态也算
        btn = str(last.get("btnClass") or "")
        if "pause" in btn.lower() or "playing" in btn.lower():
            last["playing"] = True
            return last
        time.sleep(0.6)
    return last


def _api_login(source: str = "qq") -> dict[str, Any]:
    src = _normalize_source(source)
    home = _SOURCE_HOME.get(src, _HOME)
    labels = {"qq": "QQ音乐", "netease": "网易云音乐", "kugou": "酷狗音乐"}
    label = labels.get(src, src)
    with STATE.lock:
        STATE.guest = src
        _show_window()
        _load_url(home, wait=2.5, guest=src)
        try:
            js = _JS_LOGIN_STATUS if src == "qq" else _JS_LOGIN_STATUS_GENERIC
            st: dict[str, Any] = {}
            # 页面自带的 JS 会用 cookie/token 异步校验登录态并替换页头 UI，
            # 刚加载完时可能仍是"登录"按钮的中间态：未登录时短轮询复核，
            # 避免已登录被误判成需要扫码
            for _ in range(9):
                st = _eval_js(js) or {}
                if st.get("loggedIn"):
                    break
                time.sleep(0.5)
        except Exception as e:
            return {"ok": False, "message": f"体内空间打开 {label} 失败: {e}"}
        if st.get("loggedIn"):
            if STATE.headless:
                _hide_window()
            return {
                "ok": True,
                "loggedIn": True,
                "source": src,
                "message": f"已在体内空间登录 {label}，可直接点歌",
            }
        try:
            _eval_js(_JS_CLICK_LOGIN)
        except Exception:
            pass
        return {
            "ok": True,
            "loggedIn": False,
            "source": src,
            "message": f"已在科科斯体内空间打开 {label} 登录页，请扫码；完成后可以说「我想听xxx」",
        }


def _try_click_play_on_page() -> dict[str, Any]:
    before = {}
    try:
        before = _eval_js(_JS_AUDIO_STATE, timeout=6.0) or {}
    except Exception:
        pass

    target = _eval_js(_JS_FIND_PLAY_TARGET, timeout=10.0) or {}
    if not target.get("ok"):
        return {"ok": False, "reason": target.get("reason") or "无播放目标", "before": before}

    clicked = _click_target(target)
    if not clicked:
        # 退回 JS click（可能被自动播放策略拦截，仅作兜底）
        try:
            sel = json.dumps(target.get("selector"))
            _eval_js(
                f"""
(() => {{
  const el = document.querySelector({sel}) || document.querySelector('div.player__ft a.btn_big_play, a.btn_big_play');
  if (el) {{ el.click(); return true; }}
  return false;
}})()
"""
            )
            clicked = True
            via = "js_click:" + str(target.get("selector"))
        except Exception as e:
            return {"ok": False, "reason": str(e), "before": before}
    else:
        via = "real_click:" + str(target.get("selector"))

    time.sleep(0.8)
    after = {}
    try:
        after = _eval_js(_JS_AUDIO_STATE, timeout=6.0) or {}
    except Exception:
        pass

    return {
        "ok": True,
        "via": via,
        "name": target.get("name") or after.get("name") or "",
        "beforePlaying": bool(before.get("playing")),
        "afterPlaying": bool(after.get("playing")),
        "selector": target.get("selector"),
    }


_JS_SEARCH_NETEASE = r"""
(async (query) => {
  const q = encodeURIComponent(String(query || '').trim());
  const url = `/api/search/get/web?s=${q}&type=1&offset=0&total=true&limit=8`;
  try {
    const resp = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
    const data = await resp.json();
    const songs = (((data || {}).result) || {}).songs || [];
    return {
      ok: true,
      songs: songs.map((s) => ({
        id: String(s.id || ''),
        name: String(s.name || ''),
        artist: ((s.artists || []).map((a) => a && a.name).filter(Boolean)).join(','),
        url: s.id ? (`https://music.163.com/song?id=` + s.id) : '',
      })).filter((x) => x.id),
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})
"""

_JS_SEARCH_KUGOU = r"""
(async (query) => {
  const q = encodeURIComponent(String(query || '').trim());
  const url = `https://songsearch.kugou.com/song_search_v2?keyword=${q}&page=1&pagesize=8&userid=0&clientver=&platform=WebFilter`;
  try {
    const resp = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
    const data = await resp.json();
    const lists = (((data || {}).data) || {}).lists || [];
    return {
      ok: true,
      songs: lists.map((s) => {
        const fileHash = String(s.FileHash || '');
        const albumId = String(s.AlbumID || '0');
        const mixId = String(s.MixSongID || s.Audioid || '');
        const page = fileHash
          ? (`https://www.kugou.com/song/#hash=${fileHash}&album_id=${albumId}`)
          : (mixId ? (`https://www.kugou.com/mixsong/${mixId}.html`) : '');
        return {
          id: mixId || fileHash,
          name: String(s.SongName || s.OriSongName || ''),
          artist: String(s.SingerName || ''),
          url: page,
          hash: fileHash,
        };
      }).filter((x) => x.url),
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})
"""


def _eval_async_js(wrapper_js: str, arg: str, timeout: float = 15.0) -> Any:
    """执行 (async (arg)=>...)(arg) 并等待 Promise。"""
    payload = json.dumps(arg, ensure_ascii=False)
    script = f"""
(() => {{
  const __fn = {wrapper_js};
  const __p = Promise.resolve(__fn({payload}));
  return __p;
}})()
"""
    return _eval_js(script, timeout=timeout)


def _http_json(url: str, *, referer: str) -> dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
    }

    def _req() -> Request:
        # 每次尝试必须新建 Request：经代理的请求会被 set_proxy 改写目标地址，
        # 复用旧 Request 的「直连重试」实际还是会打到代理上
        return Request(url, headers=headers, method="GET")

    # 每次现取系统代理新建 opener：裸 urlopen 会把进程首次联网时的代理
    # 永久固定，代理内核一死（clash 崩溃/关掉）搜索就全部 10061 拒连
    proxies = getproxies()
    try:
        with build_opener(ProxyHandler(proxies)).open(_req(), timeout=12.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError:
        raise
    except URLError:
        if not proxies:
            raise
        # 代理拒连/超时：这里全是国内接口（c.y.qq.com/music.163.com 等），
        # 直连兜底重试一次
        with build_opener(ProxyHandler({})).open(_req(), timeout=12.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _search_netease_http(query: str) -> list[dict[str, str]]:
    """宿主进程直连搜索（无登录态兜底）。"""
    q = quote((query or "").strip())
    url = (
        f"https://music.163.com/api/search/get/web?s={q}&type=1"
        f"&offset=0&total=true&limit=8"
    )
    data = _http_json(url, referer="https://music.163.com/")
    songs = ((data.get("result") or {}).get("songs")) or []
    out: list[dict[str, str]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if not sid:
            continue
        artists = s.get("artists") or []
        artist = ",".join(
            str(a.get("name") or "") for a in artists if isinstance(a, dict)
        )
        out.append(
            {
                "id": sid,
                "name": str(s.get("name") or ""),
                "artist": artist,
                "url": f"https://music.163.com/song?id={sid}",
            }
        )
    return out


def _search_kugou_http(query: str) -> list[dict[str, str]]:
    q = quote((query or "").strip())
    url = (
        "https://songsearch.kugou.com/song_search_v2"
        f"?keyword={q}&page=1&pagesize=8&userid=0&clientver=&platform=WebFilter"
    )
    data = _http_json(url, referer="https://www.kugou.com/")
    lists = ((data.get("data") or {}).get("lists")) or []
    out: list[dict[str, str]] = []
    for s in lists:
        if not isinstance(s, dict):
            continue
        file_hash = str(s.get("FileHash") or "").strip()
        album_id = str(s.get("AlbumID") or "0").strip() or "0"
        mix_id = str(s.get("MixSongID") or s.get("Audioid") or "").strip()
        if not file_hash and not mix_id:
            continue
        page = (
            f"https://www.kugou.com/song/#hash={file_hash}&album_id={album_id}"
            if file_hash
            else f"https://www.kugou.com/mixsong/{mix_id}.html"
        )
        out.append(
            {
                "id": mix_id or file_hash,
                "name": str(s.get("SongName") or s.get("OriSongName") or ""),
                "artist": str(s.get("SingerName") or ""),
                "url": page,
                "hash": file_hash,
            }
        )
    return out


def _pick_best_song(query: str, songs: list[dict[str, str]]) -> dict[str, str] | None:
    if not songs:
        return None
    q = (query or "").strip()
    ql = q.lower()

    def _score(s: dict[str, str]) -> tuple:
        name = (s.get("name") or "").strip()
        artist = (s.get("artist") or "").strip()
        base = name.split("(")[0].split("（")[0].strip().lower()
        exact = 2 if base == ql or name.lower() == ql else (1 if ql in name.lower() else 0)
        # 查询里带的歌手名命中加权
        artist_hit = 1 if artist and artist in q else 0
        # 封面/伴奏降权
        penalty = 0
        low = f"{name} {artist}".lower()
        for bad in ("伴奏", "消音", "翻唱", "cover", "深情版", "抖音", "live版"):
            if bad in low:
                penalty -= 1
        return (exact + artist_hit + penalty, exact, artist_hit)

    return max(songs, key=_score)


def _api_play_via_search_api(
    query: str,
    *,
    source: str,
    show: bool,
) -> dict[str, Any]:
    """网易云/酷狗：F12 同源搜索 API → 体内打开歌曲页 → 点播放。

    搜歌走网页公开接口；播放与会员鉴权依赖 WebView 里扫码登录留下的 cookie。
    """
    q = (query or "").strip()
    labels = {"netease": "网易云音乐", "kugou": "酷狗音乐"}
    label = labels.get(source, source)

    try:
        songs = (
            _search_netease_http(q)
            if source == "netease"
            else _search_kugou_http(q)
        )
    except Exception as e:
        return {"ok": False, "source": source, "message": f"{label}搜歌接口失败: {e}"}

    hit = _pick_best_song(q, songs)
    if not hit:
        return {
            "ok": False,
            "source": source,
            "message": f"在{label}没搜到「{q}」，可换歌名或先扫码登录",
            "songs": {"count": 0, "ids": []},
        }

    song_url = str(hit.get("url") or "")
    song_name = hit.get("name") or q
    artist = hit.get("artist") or ""

    with STATE.lock:
        STATE.last_query = q
        STATE.guest = source
        if show or not STATE.headless:
            _show_window()
        else:
            _hide_window()

        # 每次播放前检测登录态；未登录自动亮窗弹扫码，扫完继续播
        login_guard = _ensure_play_login(source)
        if not login_guard.get("loggedIn"):
            return {
                "ok": False,
                "needLogin": True,
                "source": source,
                "message": login_guard.get("message")
                or f"请先登录{label}后再点歌",
            }

        # 登录保障后页面已在源站（首页/登录页），无需再预载 home
        _load_url(song_url, wait=4.0, guest=source)
        click_result = _try_click_play_on_page()
        audio = _wait_playing(5.0)
        if not audio.get("playing"):
            click_result = _try_click_play_on_page()
            audio = _wait_playing(4.0)

        name = (audio.get("name") or song_name).strip()
        art = (audio.get("artist") or artist).strip()
        label_song = f"{name}" + (f" - {art}" if art else "")
        if audio.get("playing") or click_result.get("ok"):
            if show:
                _show_window()
            elif STATE.headless:
                _hide_window()
            return {
                "ok": True,
                "source": source,
                "message": f"正在播放《{label_song}》",
                "name": name,
                "artist": art,
                "query": q,
                "via": click_result.get("via") or "search_api",
                "songs": {
                    "count": len(songs),
                    "ids": [str(s.get("id") or "") for s in songs[:3]],
                },
            }
        if show:
            _show_window()
        return {
            "ok": False,
            "source": source,
            "needLogin": True,
            "message": (
                f"「{q}」在{label}已打开《{label_song}》但未能自动开播。"
                f"可说「登录{label}」扫码后重试"
            ),
            "songs": {
                "count": len(songs),
                "ids": [str(s.get("id") or "") for s in songs[:3]],
            },
            "audio": audio,
            "url": song_url,
        }


def _api_play_web_generic(
    query: str,
    *,
    source: str,
    show: bool,
    search_url: str,
    find_js: str,
    song_url_builder,
) -> dict[str, Any]:
    """兼容旧路径：页面 DOM 搜歌（优先改用 _api_play_via_search_api）。"""
    return _api_play_via_search_api(query, source=source, show=show)


def _search_qq_http(query: str) -> list[dict[str, str]]:
    """F12 同源：c.y.qq.com/soso/fcgi-bin/client_search_cp"""
    q = quote((query or "").strip())
    url = (
        "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        f"?w={q}&format=json&p=1&n=8&cr=1&g_tk=5381&loginUin=0&hostUin=0"
        "&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0"
    )
    data = _http_json(url, referer="https://y.qq.com/")
    # 偶发 jsonp
    if not data:
        return []
    lists = (((data.get("data") or {}).get("song") or {}).get("list")) or []
    out: list[dict[str, str]] = []
    for s in lists:
        if not isinstance(s, dict):
            continue
        mid = str(s.get("songmid") or "").strip()
        if not mid:
            continue
        singers = s.get("singer") or []
        artist = ",".join(
            str(a.get("name") or "") for a in singers if isinstance(a, dict)
        )
        out.append(
            {
                "id": mid,
                "name": str(s.get("songname") or s.get("songname_hilight") or ""),
                "artist": artist,
                "url": f"https://y.qq.com/n/ryqq/player?songmid={mid}",
            }
        )
    return out


def _search_qq_smartbox(query: str) -> list[dict[str, str]]:
    """兜底：smartbox 联想接口（client_search_cp 服务端下线后仍存活）。

    返回结构与 _search_qq_http 一致；联想榜条目有限，精确歌名/热歌命中率最高。
    """
    q = quote((query or "").strip())
    url = (
        "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg"
        f"?is_xml=0&format=json&key={q}&platform=yqq"
    )
    data = _http_json(url, referer="https://y.qq.com/")
    songs = (((data.get("data") or {}).get("song") or {}).get("itemlist")) or []
    out: list[dict[str, str]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        mid = str(s.get("mid") or "").strip()
        if not mid:
            continue
        out.append(
            {
                "id": mid,
                "name": str(s.get("name") or ""),
                "artist": str(s.get("singer") or ""),
                "url": f"https://y.qq.com/n/ryqq/player?songmid={mid}",
            }
        )
    return out


def _search_qq_any(query: str) -> list[dict[str, str]]:
    """主搜索（client_search_cp）异常或空结果时自动兜底 smartbox。"""
    try:
        hits = _search_qq_http(query)
        if hits:
            return hits
    except Exception:
        pass
    return _search_qq_smartbox(query)


def _song_name_matches(expected: str, actual: str) -> bool:
    e = (expected or "").strip().lower()
    a = (actual or "").strip().lower()
    if not e or not a:
        return False
    e0 = e.split("(")[0].split("（")[0].strip()
    a0 = a.split("(")[0].split("（")[0].strip()
    return e0 == a0 or e0 in a or a0 in e


_JS_CLEAR_QQ_PLAY_HISTORY = r"""
(() => {
  const removed = [];
  // 只清「播放队列/最近播放」相关键；绝不删 IndexedDB（会弄坏 SPA）
  const playRe = /^(?:.*)?(?:playqueue|play_queue|playlist|playList|recentplay|recent_play|playHistory|play_history|songlist|currentSong|currentsong|playingSong|lastPlay|last_play|player_state|playerState)(?:.*)?$/i;
  const scrub = (store, label) => {
    try {
      const keys = [];
      for (let i = 0; i < store.length; i++) keys.push(store.key(i));
      for (const k of keys) {
        if (!k || !playRe.test(k)) continue;
        try { store.removeItem(k); removed.push(label + ':' + k); } catch (e) {}
      }
    } catch (e) {}
  };
  scrub(localStorage, 'ls');
  scrub(sessionStorage, 'ss');
  try {
    document.querySelectorAll('audio,video').forEach((m) => {
      try { m.pause(); m.removeAttribute('src'); m.load(); } catch (e) {}
    });
  } catch (e) {}
  return { ok: true, removed: removed.slice(0, 40), href: location.href };
})()
"""


def _qq_click_song_detail_play() -> dict[str, Any]:
    """点歌曲详情页绿色「播放」——会把该曲写入播放队列并开播。

    注意：不要点底栏 btn_big_play；底栏播的是队列里「当前曲」，
    若新歌还没入队，就会听起来像在播上一首（与「历史列表往下刷」无关）。
    """
    try:
        hit = _eval_js(
            """
(() => {
  const els = Array.from(document.querySelectorAll('a.mod_btn_green, .mod_btn_green, .data__actions a'));
  for (const el of els) {
    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!(t === '播放' || t.startsWith('播放'))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10 || r.top < 40 || r.top > 900) continue;
    if (el.closest && el.closest('.player__ft, .player_bar, #player, .mod_player')) continue;
    try { el.click(); } catch (e) {}
    return {
      ok: true,
      via: 'js:detail_mod_btn_green',
      name: t.slice(0, 40),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      selector: 'a.mod_btn_green',
    };
  }
  return { ok: false, reason: 'no_detail_play' };
})()
""",
            timeout=6.0,
        ) or {}
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    if hit.get("ok"):
        return hit
    # 兜底：通用找页内播放钮（仍排除底栏）
    return _try_click_play_on_page()


_JS_QQ_FETCH_LIKED = r"""
(async (_arg) => {
  const getCookie = (n) => {
    const m = String(document.cookie || '').match(new RegExp('(?:^|;\\s*)' + n + '=([^;]*)'));
    return m ? decodeURIComponent(m[1] || '') : '';
  };
  let uin = (getCookie('uin') || getCookie('wxuin') || '').replace(/^o/i, '').replace(/^0+/, '');
  if (!uin || uin === '0') {
    return { ok: false, reason: 'not_logged_in', cookieHint: !!(getCookie('uin') || getCookie('wxuin')) };
  }
  const headers = {
    Accept: 'application/json,text/javascript,*/*',
    Referer: 'https://y.qq.com/',
  };
  const tryJson = async (url) => {
    const r = await fetch(url, { credentials: 'include', headers });
    const t = await r.text();
    try { return JSON.parse(t.replace(/^[^(]*\(|\)[^)]*$/g, '')); } catch (e) {
      try { return JSON.parse(t); } catch (e2) { return null; }
    }
  };
  let disstid = '';
  let listTitle = '';
  try {
    const home = await tryJson(
      'https://c.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg'
      + '?hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0'
      + '&platform=yqq&needNewCode=0&cid=205360838&userid=' + encodeURIComponent(uin)
      + '&reqfrom=1&reqtype=0'
    );
    const mine = (((home || {}).data || {}).mymusic) || [];
    for (const it of mine) {
      if (!it) continue;
      const title = String(it.title || it.name || '');
      const id = String(it.id || it.dissid || it.tid || '');
      if (!id) continue;
      if (/喜欢|我喜欢|红心|收藏/.test(title) || Number(it.type) === 1) {
        disstid = id;
        listTitle = title || '我喜欢';
        break;
      }
    }
    if (!disstid && mine.length) {
      disstid = String(mine[0].id || mine[0].dissid || '');
      listTitle = String(mine[0].title || mine[0].name || '我喜欢');
    }
  } catch (e) {
    return { ok: false, reason: 'profile_fetch:' + String(e), uin };
  }
  if (!disstid) {
    return { ok: false, reason: 'no_liked_diss', uin };
  }
  try {
    const detail = await tryJson(
      'https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg'
      + '?type=1&json=1&utf8=1&onlysong=1&nosign=1&new_format=1'
      + '&song_begin=0&song_num=20&ctx=1&disstid=' + encodeURIComponent(disstid)
      + '&g_tk=5381&loginUin=' + encodeURIComponent(uin)
      + '&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0'
      + '&platform=yqq.json&needNewCode=0'
    );
    const cdlist = ((detail || {}).cdlist) || [];
    const songs = (cdlist[0] && (cdlist[0].songlist || cdlist[0].songList)) || [];
    const out = [];
    for (const s of songs) {
      if (!s) continue;
      const mid = String(s.songmid || s.mid || '');
      if (!mid) continue;
      const singers = s.singer || [];
      const artist = singers.map((a) => (a && a.name) || '').filter(Boolean).join(',');
      out.push({
        id: mid,
        name: String(s.songname || s.name || ''),
        artist,
        url: 'https://y.qq.com/n/ryqq_v2/songDetail/' + mid,
      });
      if (out.length >= 12) break;
    }
    if (!out.length) {
      return { ok: false, reason: 'liked_empty', disstid, listTitle, uin };
    }
    return { ok: true, disstid, listTitle, uin, songs: out };
  } catch (e) {
    return { ok: false, reason: 'diss_fetch:' + String(e), disstid, uin };
  }
})
"""


_JS_CLICK_TEXT_TARGET = r"""
(texts) => {
  const want = (Array.isArray(texts) ? texts : [texts]).map((t) => String(t || '').trim()).filter(Boolean);
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none'
      && st.pointerEvents !== 'none' && r.bottom > 0 && r.top < window.innerHeight + 120;
  };
  const nodes = Array.from(document.querySelectorAll('a, button, span, div, li, em, i'));
  for (const w of want) {
    for (const el of nodes) {
      if (!isVisible(el)) continue;
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t) continue;
      if (t === w || t.startsWith(w) || (w.length >= 2 && t.includes(w) && t.length <= w.length + 8)) {
        const r = el.getBoundingClientRect();
        const chromeH = (window.outerHeight - window.innerHeight) || 0;
        return {
          ok: true,
          text: t.slice(0, 40),
          want: w,
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          screenX: Math.round(window.screenX + r.left + r.width / 2),
          screenY: Math.round(window.screenY + r.top + r.height / 2 + chromeH),
        };
      }
    }
  }
  return {
    ok: false,
    reason: 'text_not_found',
    want,
    sample: ((document.body && document.body.innerText) || '').slice(0, 240),
  };
}
"""


def _click_by_texts(texts: list[str], *, timeout: float = 8.0) -> dict[str, Any]:
    try:
        target = _eval_js(
            f"({_JS_CLICK_TEXT_TARGET})({json.dumps(texts, ensure_ascii=False)})",
            timeout=timeout,
        ) or {}
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    if not target.get("ok"):
        return target if isinstance(target, dict) else {"ok": False}
    if _click_target(target):
        target["via"] = "real_click:text"
        return target
    return {"ok": False, "reason": "click_failed", "target": target}


def _ensure_logged_in(source: str) -> dict[str, Any]:
    src = _normalize_source(source)
    labels = {"qq": "QQ音乐", "netease": "网易云音乐", "kugou": "酷狗音乐"}
    label = labels.get(src, src)
    home = _SOURCE_HOME.get(src, _HOME)
    try:
        cur = ""
        try:
            cur = str(_eval_js("location.href", timeout=3.0) or "")
        except Exception:
            cur = ""
        if src == "qq" and "y.qq.com" not in cur:
            _load_url(home, wait=2.0, guest=src)
        elif src == "netease" and "music.163.com" not in cur:
            _load_url(home, wait=2.0, guest=src)
        elif src == "kugou" and "kugou.com" not in cur:
            _load_url(home, wait=2.0, guest=src)
        js = _JS_LOGIN_STATUS if src == "qq" else _JS_LOGIN_STATUS_GENERIC
        st: dict[str, Any] = {}
        # 页面自带 JS 会异步用 token 校验登录态并替换页头 UI，刚打开时
        # 可能还是"登录"按钮的中间态：未登录时短轮询复核，避免误判
        for _ in range(9):
            st = _eval_js(js, timeout=6.0) or {}
            if st.get("loggedIn"):
                break
            time.sleep(0.5)
    except Exception as e:
        return {"ok": False, "loggedIn": False, "message": f"检查{label}登录态失败: {e}"}
    if st.get("loggedIn"):
        return {"ok": True, "loggedIn": True, "source": src, "label": label}
    return {
        "ok": False,
        "loggedIn": False,
        "needLogin": True,
        "source": src,
        "label": label,
        "message": f"请先登录{label}（可以说「登录{label}」扫码），再播「我喜欢」",
    }


_LOGIN_WAIT_SECONDS = 60.0


def _ensure_play_login(source: str = "qq") -> dict[str, Any]:
    """播放前登录保障：已登录直接通过；未登录亮窗扫码，轮询等待完成后自动收起窗口。

    返回 dict：loggedIn=True 表示可直接播放；否则 needLogin=True（等待超时）。
    """
    src = _normalize_source(source)
    labels = {"qq": "QQ音乐", "netease": "网易云音乐", "kugou": "酷狗音乐"}
    label = labels.get(src, src)

    st = _ensure_logged_in(src)
    if st.get("loggedIn"):
        return {"ok": True, "loggedIn": True, "source": src, "label": label}

    # 未登录：亮窗 + 打开首页并点登录入口，开始轮询扫码结果
    status_js = _JS_LOGIN_STATUS if src == "qq" else _JS_LOGIN_STATUS_GENERIC
    _show_window()
    try:
        home = _SOURCE_HOME.get(src, _HOME)
        _load_url(home, wait=2.5, guest=src)
        _eval_js(status_js, timeout=6.0)
        try:
            _eval_js(_JS_CLICK_LOGIN, timeout=5.0)
        except Exception:
            pass
    except Exception as e:
        logger.warning("打开登录页失败: %s", e)

    deadline = time.time() + _LOGIN_WAIT_SECONDS
    while time.time() < deadline:
        try:
            cur = _eval_js(status_js, timeout=6.0) or {}
        except Exception:
            cur = {}
        if cur.get("loggedIn"):
            if STATE.headless:
                _hide_window()
            return {
                "ok": True,
                "loggedIn": True,
                "justLoggedIn": True,
                "source": src,
                "label": label,
            }
        time.sleep(2.0)

    if STATE.headless:
        _hide_window()
    return {
        "ok": False,
        "loggedIn": False,
        "needLogin": True,
        "source": src,
        "label": label,
        "message": f"等待扫码登录{label}超时（{int(_LOGIN_WAIT_SECONDS)}秒），"
        "已收起窗口，请重新说「我想听xxx」再试",
    }


def _find_play_all_btn(*, timeout: float = 8.0, source: str = "qq") -> dict[str, Any]:
    """多指纹定位「播放全部」（指纹库可热更新）。"""
    from kekesi.tools.control_finder import MUSIC_DEBUG_PORT, find_control

    control_id = "netease.liked_play_all" if source == "netease" else "qq.liked_play_all"
    return find_control(
        control_id,
        debug_port=MUSIC_DEBUG_PORT,
        eval_fn=lambda js, t: _eval_js(js, timeout=t),
        timeout=timeout,
    )


def _click_play_all_button(*, source: str = "qq") -> dict[str, Any]:
    """多指纹定位 + CDP 点击「播放全部」（禁止误点底栏）。"""
    from kekesi.tools.control_finder import MUSIC_DEBUG_PORT, click_control

    control_id = "netease.liked_play_all" if source == "netease" else "qq.liked_play_all"

    def _client_click(x: float, y: float) -> None:
        parent = _find_player_hwnd()
        child = _find_webview_child(parent)
        hwnd = child or parent
        if not hwnd:
            raise RuntimeError("no hwnd")
        _post_click_client(hwnd, int(x), int(y))

    target = click_control(
        control_id,
        debug_port=MUSIC_DEBUG_PORT,
        eval_fn=lambda js, t: _eval_js(js, timeout=t),
        timeout=8.0,
    )
    if target.get("ok") and target.get("clicked"):
        return target

    # CDP 失败时用客户区坐标兜底（仍是指纹给出的坐标）
    if target.get("ok") and "x" in target and "y" in target:
        try:
            _client_click(float(target["x"]), float(target["y"]))
            target["clicked"] = True
            target["via"] = f"client_post:{control_id}"
            return target
        except Exception as e:
            target["postError"] = str(e)

    # 最后：精确 JS 点绿色按钮文案（仍不走底栏）
    try:
        ok = _eval_js(
            """
(() => {
  const greens = Array.from(document.querySelectorAll(
    '.mod_songlist_toolbar a.mod_btn_green, a.mod_btn_green, .mod_btn_green, a, button'
  ));
  for (const el of greens) {
    const t = ((el.innerText || '')).replace(/\\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    if ((t === '播放全部' || t.startsWith('播放全部') || t === '全部播放')
        && r.width > 8 && r.height > 8) {
      if (el.closest && el.closest('.player__ft, .player_bar, #player, .mod_player')) continue;
      el.click();
      return true;
    }
  }
  return false;
})()
""",
            timeout=5.0,
        )
        if ok:
            out = dict(target) if isinstance(target, dict) else {}
            out.update({"ok": True, "clicked": True, "via": "js_click:play_all_text"})
            return out
    except Exception as e:
        if isinstance(target, dict):
            target["jsError"] = str(e)

    return target if isinstance(target, dict) else {"ok": False, "reason": "click_failed"}


def _wait_play_all_ready(*, tries: int = 12, source: str = "qq") -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False}
    for i in range(tries):
        last = _find_play_all_btn(timeout=5.0, source=source)
        if last.get("ok"):
            return last
        time.sleep(0.35 if i else 0.8)
    return last if isinstance(last, dict) else {"ok": False}


def _api_play_liked(*, source: str = "qq", show: bool = False) -> dict[str, Any]:
    """登录后进入「我喜欢」，点「播放全部」开播。"""
    src = _normalize_source(source)
    labels = {"qq": "QQ音乐", "netease": "网易云音乐", "kugou": "酷狗音乐"}
    label = labels.get(src, src)

    with STATE.lock:
        STATE.last_query = "我喜欢"
        STATE.guest = src
        if show or not STATE.headless:
            _show_window()
        else:
            _hide_window()

        login = _ensure_play_login(src)
        if not login.get("loggedIn"):
            return {
                "ok": False,
                "needLogin": True,
                "source": src,
                "message": login.get("message")
                or f"请先登录{label}后再播「我喜欢」",
            }

        if src == "qq":
            last: dict[str, Any] = {"ok": False}
            # 1) 直达「我喜欢」歌曲页 → 等「播放全部」→ 真实点击
            for url in _QQ_LIKED_SONG_URLS:
                try:
                    _load_url(url, wait=2.8, guest="qq")
                except Exception as e:
                    last = {"ok": False, "reason": f"load:{e}", "url": url}
                    continue
                ready = _wait_play_all_ready(tries=14, source="qq")
                if not ready.get("ok"):
                    # 可能落在 profile，再点一次「我喜欢」
                    hit_like = _click_by_texts(["我喜欢"])
                    if hit_like.get("ok"):
                        time.sleep(1.4)
                        ready = _wait_play_all_ready(tries=10, source="qq")
                    if not ready.get("ok"):
                        last = {
                            "ok": False,
                            "reason": "no_play_all",
                            "url": url,
                            "ready": ready,
                            "likeClick": hit_like,
                        }
                        continue
                play_hit = _click_play_all_button(source="qq")
                audio = _wait_playing(6.0)
                if not audio.get("playing"):
                    play_hit = _click_play_all_button(source="qq")
                    audio = _wait_playing(4.0)
                if audio.get("playing") or play_hit.get("ok"):
                    name = (audio.get("name") or "").strip()
                    art = (audio.get("artist") or "").strip()
                    extra = (
                        f"：《{name}" + (f" - {art}" if art else "") + "》" if name else ""
                    )
                    if show:
                        _show_window()
                    elif STATE.headless:
                        _hide_window()
                    return {
                        "ok": True,
                        "source": "qq",
                        "playlist": "liked",
                        "message": f"正在播放「我喜欢」{extra}",
                        "name": name,
                        "artist": art,
                        "via": "liked_play_all",
                        "playClick": play_hit,
                        "url": url,
                    }
                last = {
                    "ok": False,
                    "reason": "play_all_clicked_but_silent",
                    "url": url,
                    "playClick": play_hit,
                    "audio": audio,
                }

            # 2) 回退：个人主页 → 点「我喜欢」→「播放全部」
            for url in _QQ_PROFILE_URLS:
                try:
                    _load_url(url, wait=2.8, guest="qq")
                except Exception as e:
                    last = {"ok": False, "reason": str(e)}
                    continue
                time.sleep(0.8)
                hit_like = _click_by_texts(["我喜欢"])
                if hit_like.get("ok"):
                    time.sleep(1.6)
                ready = _wait_play_all_ready(tries=12, source="qq")
                play_hit = (
                    _click_play_all_button(source="qq") if ready.get("ok") else {"ok": False}
                )
                if not play_hit.get("ok"):
                    play_hit = _click_by_texts(["播放全部", "全部播放"])
                audio = _wait_playing(5.0)
                if audio.get("playing") or play_hit.get("ok"):
                    name = (audio.get("name") or "").strip()
                    art = (audio.get("artist") or "").strip()
                    extra = (
                        f"：《{name}" + (f" - {art}" if art else "") + "》" if name else ""
                    )
                    if show:
                        _show_window()
                    elif STATE.headless:
                        _hide_window()
                    return {
                        "ok": True,
                        "source": "qq",
                        "playlist": "liked",
                        "message": f"正在播放「我喜欢」{extra}",
                        "name": name,
                        "artist": art,
                        "via": "liked_dom_profile",
                        "likeClick": hit_like,
                        "playClick": play_hit,
                    }
                last = {
                    "ok": False,
                    "likeClick": hit_like,
                    "playClick": play_hit,
                    "audio": audio,
                }

            if show:
                _show_window()
            return {
                "ok": False,
                "source": "qq",
                "playlist": "liked",
                "message": (
                    "已进入「我喜欢」但未点到「播放全部」。"
                    "可说「显示音乐」后手动点绿色播放全部"
                ),
                "detail": last,
            }

        if src == "netease":
            try:
                _load_url(_NETEASE_LIKED_URL, wait=3.5, guest="netease")
            except Exception as e:
                return {
                    "ok": False,
                    "source": "netease",
                    "message": f"打开网易云「我喜欢」失败: {e}",
                }
            time.sleep(1.2)
            ready = _wait_play_all_ready(tries=10, source="netease")
            play_hit = (
                _click_play_all_button(source="netease") if ready.get("ok") else {"ok": False}
            )
            if not play_hit.get("ok"):
                play_hit = _click_by_texts(["播放全部", "全部播放", "播放"])
            if not play_hit.get("ok"):
                play_hit = _try_click_play_on_page()
            audio = _wait_playing(5.0)
            if not audio.get("playing"):
                play_hit = _click_play_all_button(source="netease")
                audio = _wait_playing(4.0)
            if audio.get("playing") or play_hit.get("ok"):
                name = (audio.get("name") or "").strip()
                art = (audio.get("artist") or "").strip()
                extra = f"：《{name}" + (f" - {art}" if art else "") + "》" if name else ""
                if show:
                    _show_window()
                elif STATE.headless:
                    _hide_window()
                return {
                    "ok": True,
                    "source": "netease",
                    "playlist": "liked",
                    "message": f"正在播放「我喜欢」{extra}",
                    "name": name,
                    "artist": art,
                    "via": "liked_play_all",
                    "playClick": play_hit,
                }
            if show:
                _show_window()
            return {
                "ok": False,
                "source": "netease",
                "playlist": "liked",
                "message": "已打开网易云「我喜欢」但未能自动开播，可说「显示音乐」后手动点播放全部",
                "playClick": play_hit,
                "audio": audio,
            }

        return {
            "ok": False,
            "source": src,
            "playlist": "liked",
            "message": f"{label}暂未支持「我喜欢」列表播放，请改用 QQ 音乐或网易云，或直接点歌名",
        }


def _api_play(
    query: str,
    show: bool = False,
    source: str = "qq",
    playlist: str | None = None,
) -> dict[str, Any]:
    pl = (playlist or "").strip().lower()
    if pl in ("liked", "like", "favorite", "favourites", "favorites"):
        return _api_play_liked(source=source, show=show)

    q = (query or "").strip()
    # 仅当 query 明确是「列表/歌单/喜欢的歌」等强信号时才当歌单；裸「我喜欢」按歌名搜
    if re.fullmatch(
        r"(?:播放|听|放)?(?:一下)?(?:"
        r"我喜欢(?:的)?(?:歌单|列表)+|"
        r"喜欢(?:歌单|列表)+|"
        r"我喜欢的(?:歌|音乐|歌曲|曲子)|我的喜欢|"
        r"红心(?:歌单|列表|歌|音乐)?|"
        r"收藏的(?:歌|音乐|列表|歌曲)|我的收藏"
        r")",
        q,
    ):
        return _api_play_liked(source=source, show=show)

    if not q:
        return {"ok": False, "message": "请提供歌名"}
    src = _normalize_source(source)
    if src in ("netease", "kugou"):
        return _api_play_via_search_api(q, source=src, show=show)

    # QQ：先登录保障再搜歌——未登录自动弹扫码窗，登录通过后才搜索
    with STATE.lock:
        STATE.guest = "qq"
    login_guard = _ensure_play_login("qq")
    if not login_guard.get("loggedIn"):
        return {
            "ok": False,
            "needLogin": True,
            "source": "qq",
            "message": login_guard.get("message")
            or "请先登录 QQ 音乐后再点歌",
        }

    # QQ：搜 songmid → 歌曲详情页点「播放」入队开播（不要只开空的 player?songmid=）
    try:
        hits = _search_qq_any(q)
    except Exception as e:
        return {"ok": False, "source": "qq", "message": f"QQ音乐搜歌失败: {e}"}
    hit = _pick_best_song(q, hits)
    if not hit:
        return {
            "ok": False,
            "source": "qq",
            "message": f"没搜到「{q}」，请换个歌名或先登录 QQ 音乐",
        }
    mid = hit["id"]
    song_name = hit.get("name") or q
    artist_hint = hit.get("artist") or ""

    with STATE.lock:
        STATE.last_query = q
        if show or not STATE.headless:
            _show_window()
        else:
            _hide_window()

        need_login_hint = False

        # 仅暂停；切歌靠详情页「播放」换队列，不要点底栏续播旧队列
        try:
            _eval_js(f"({_JS_CONTROL})({json.dumps('pause')})", timeout=4.0)
        except Exception:
            pass

        click_result: dict[str, Any] = {"ok": False}
        audio: dict[str, Any] = {}

        def _read_audio() -> dict[str, Any]:
            try:
                return _eval_js(_JS_AUDIO_STATE, timeout=5.0) or {}
            except Exception:
                return {}

        def _matched(a: dict[str, Any]) -> bool:
            n = (a.get("name") or "").strip()
            t = (a.get("title") or "").strip()
            return bool(
                (_song_name_matches(song_name, n) or _song_name_matches(q, n))
                or (_song_name_matches(song_name, t) or _song_name_matches(q, t))
            )

        detail = _SONG_URL.format(mid=mid)
        for attempt in range(2):
            _load_url(detail, wait=3.0 if attempt == 0 else 2.5, guest="qq")
            # 等详情页绿色播放钮出现
            for _ in range(8):
                try:
                    ready = _eval_js(
                        """
(() => {
  const els = Array.from(document.querySelectorAll('a.mod_btn_green, .mod_btn_green'));
  return els.some((el) => {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    return (t === '播放' || t.startsWith('播放')) && r.width > 20 && r.top > 40;
  });
})()
""",
                        timeout=4.0,
                    )
                    if ready:
                        break
                except Exception:
                    pass
                time.sleep(0.35)

            click_result = _qq_click_song_detail_play()
            if not click_result.get("ok"):
                click_result = _try_click_play_on_page()

            for _ in range(10):
                audio = _read_audio()
                if audio.get("playing") and _matched(audio):
                    click_result = {
                        "ok": True,
                        "via": click_result.get("via") or "song_detail",
                    }
                    break
                # 已在播但歌名不对：再点详情页播放强切，绝不点底栏
                if audio.get("playing") and not _matched(audio):
                    if "songDetail" not in (audio.get("href") or STATE.guest_url or ""):
                        _load_url(detail, wait=2.2, guest="qq")
                    click_result = _qq_click_song_detail_play()
                time.sleep(0.5)
            if audio.get("playing") and _matched(audio):
                break

        audio = _read_audio() or audio
        name = (audio.get("name") or "").strip()
        artist = (audio.get("artist") or "").strip()
        label = f"{name}" + (f" - {artist}" if artist else "")

        if audio.get("playing") and _matched(audio):
            if show:
                _show_window()
            elif STATE.headless:
                _hide_window()
            if not name:
                name, artist = song_name, artist_hint
                label = f"{name}" + (f" - {artist}" if artist else "")
            return {
                "ok": True,
                "message": f"正在播放《{label}》",
                "name": name or song_name,
                "artist": artist or artist_hint,
                "query": q,
                "mid": mid,
                "via": click_result.get("via") or "song_detail",
            }

        if need_login_hint or show:
            _show_window()
        shown = name or "未知"
        return {
            "ok": False,
            "needLogin": need_login_hint,
            "message": (
                f"「{q}」未能切到目标曲（当前《{shown}》"
                f"{'，仍在播' if audio.get('playing') else '，未开播'}）。"
                + ("可先说「登录 QQ 音乐」扫码后重试" if need_login_hint else "请再说一次要听的歌")
            ),
            "mid": mid,
            "audio": audio,
            "expected": song_name,
        }


def _api_control(action: str) -> dict[str, Any]:
    action = (action or "").strip().lower()
    if action == "stop":
        action = "pause"
    if action not in ("play", "pause", "playpause", "next", "previous"):
        return {"ok": False, "message": f"不支持的操作: {action}"}
    with STATE.lock:
        # 后台控制：不弹窗
        try:
            target = _eval_js(f"({_JS_CONTROL})({json.dumps(action)})", timeout=8.0) or {}
        except Exception as e:
            return {"ok": False, "message": f"控制失败: {e}"}
        if target.get("ok") and _click_target(target):
            names = {
                "play": "已继续播放",
                "pause": "已暂停",
                "playpause": "已切换播放/暂停",
                "next": "已切到下一首",
                "previous": "已切到上一首",
            }
            if STATE.headless:
                _hide_window()
            return {"ok": True, "message": names.get(action, "已操作")}
        return {"ok": False, "message": target.get("reason") or "控制失败"}


def _api_status() -> dict[str, Any]:
    with STATE.lock:
        try:
            login = _eval_js(_JS_LOGIN_STATUS, timeout=8.0) or {}
            now = _eval_js(_JS_AUDIO_STATE, timeout=8.0) or {}
        except Exception as e:
            return {
                "ok": True,
                "ready": True,
                "space": _SPACE_NAME,
                "guest": STATE.guest,
                "message": f"状态读取异常: {e}",
            }
        return {
            "ok": True,
            "ready": True,
            "space": _SPACE_NAME,
            "guest": STATE.guest or "qqmusic",
            "guestUrl": STATE.guest_url,
            "loggedIn": bool(login.get("loggedIn")),
            "lastQuery": STATE.last_query,
            "now": now,
            "visible": bool(STATE.visible),
            "message": "体内空间就绪",
        }


def _api_space_status() -> dict[str, Any]:
    with STATE.lock:
        return {
            "ok": True,
            "ready": STATE.ready.is_set(),
            "space": _SPACE_NAME,
            "guest": STATE.guest,
            "guestUrl": STATE.guest_url,
            "headless": STATE.headless,
            "visible": STATE.visible,
            "message": "体内空间运行中" if STATE.ready.is_set() else "体内空间启动中",
        }


def _api_space_load(url: str, guest: str = "", show: bool = False) -> dict[str, Any]:
    u = (url or "").strip()
    if not u.startswith("http"):
        return {"ok": False, "message": "url 无效"}
    g = (guest or "").strip() or "web"
    with STATE.lock:
        if show or not STATE.headless:
            _show_window()
        else:
            _hide_window()
        _load_url(u, wait=2.5, guest=g)
        return {
            "ok": True,
            "space": _SPACE_NAME,
            "guest": STATE.guest,
            "message": f"已在体内空间加载访客「{STATE.guest}」",
        }


def _api_space_eval(script: str, timeout: float = 12.0) -> dict[str, Any]:
    s = script or ""
    if not s.strip():
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
            return {"ok": True, "message": "已收回到科科斯体内（离屏）"}
        _show_window()
        return {"ok": True, "message": "已显示科科斯体内空间"}


def _api_debug() -> dict[str, Any]:
    with STATE.lock:
        try:
            return {
                "ok": True,
                "debug": _eval_js(_JS_DEBUG, timeout=8.0),
                "dump": _eval_js(_JS_DUMP, timeout=10.0),
                "inspectPlay": _eval_js(_JS_INSPECT_PLAY, timeout=10.0),
                "remoteDebug": f"http://127.0.0.1:{_REMOTE_DEBUG_PORT}",
                "headless": STATE.headless,
                "visible": STATE.visible,
            }
        except Exception as e:
            return {"ok": False, "message": str(e)}


def _api_goto(url: str) -> dict[str, Any]:
    u = (url or "").strip()
    if not u.startswith("http"):
        return {"ok": False, "message": "url 无效"}
    with STATE.lock:
        _load_url(u, wait=3.0, guest=STATE.guest or "web")
        try:
            inspect = _eval_js(_JS_INSPECT_PLAY, timeout=10.0)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        if STATE.headless:
            _hide_window()
        return {"ok": True, "message": "已在体内空间打开", "inspectPlay": inspect}


def _api_close() -> dict[str, Any]:
    def _die() -> None:
        time.sleep(0.3)
        try:
            if STATE.window is not None:
                STATE.window.destroy()
        except Exception:
            pass
        _clear_state()
        # 进程退出前清播放历史缓存，避免下次仍续播上一首
        try:
            purge_qq_play_history_disk()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在关闭科科斯体内空间"}


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
                self._send(503, {"ok": False, "message": "体内空间尚未就绪"})
                return
            if path == "/space/status":
                self._send(200, _api_space_status())
            else:
                self._send(200, _api_status())
            return
        if path == "/debug":
            if not STATE.ready.wait(timeout=30):
                self._send(503, {"ok": False, "message": "体内空间尚未就绪"})
                return
            self._send(200, _api_debug())
            return
        self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not STATE.ready.wait(timeout=45):
            self._send(503, {"ok": False, "message": "体内空间尚未就绪"})
            return
        data = self._read_json()
        try:
            if path == "/space/load":
                self._send(
                    200,
                    _api_space_load(
                        str(data.get("url") or ""),
                        guest=str(data.get("guest") or ""),
                        show=bool(data.get("show")),
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
            if path == "/login":
                self._send(
                    200,
                    _api_login(str(data.get("source") or data.get("guest") or "qq")),
                )
                return
            if path == "/play":
                # 默认后台播放，不占桌面
                show = bool(data.get("show")) if "show" in data else False
                self._send(
                    200,
                    _api_play(
                        str(data.get("query") or ""),
                        show=show,
                        source=str(data.get("source") or data.get("guest") or "qq"),
                        playlist=str(data.get("playlist") or "") or None,
                    ),
                )
                return
            if path == "/control":
                self._send(200, _api_control(str(data.get("action") or "")))
                return
            if path == "/show":
                self._send(200, _api_show(minimize=bool(data.get("minimize"))))
                return
            if path == "/goto":
                self._send(200, _api_goto(str(data.get("url") or "")))
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
    t = threading.Thread(target=server.serve_forever, name="inner-space-http", daemon=True)
    t.start()
    logger.info("体内空间 API on http://%s:%s", _DEFAULT_HOST, port)
    return server


def _probe_existing_host() -> dict[str, Any] | None:
    """启动前探测：已有健康宿主在跑则返回其 state，本进程不得再开第二个窗口。

    曾实测 8 分钟内连开 5 个宿主（service.json 只记最后一个，旧宿主成孤儿
    且各自挂着 WebView 继续出声），表现为「两个音乐同时播放」。
    """
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    port = state.get("port")
    if not isinstance(port, int) or port <= 0:
        return None
    try:
        with urlopen(
            f"http://{_DEFAULT_HOST}:{port}/health", timeout=2.0
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    if str(data.get("space") or "") != _SPACE_NAME:
        return None
    if data.get("pid") == os.getpid():
        return None
    return state


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    existing = _probe_existing_host()
    if existing is not None:
        logger.info(
            "体内空间已有健康宿主在运行 (pid=%s, port=%s)，本实例退出避免双开出声",
            existing.get("pid"),
            existing.get("port"),
        )
        return 0
    try:
        import webview
    except ImportError:
        print("缺少 pywebview，请先 pip install pywebview", file=sys.stderr)
        return 1

    storage = str(_storage_path())

    # 允许本机 CDP/F12 连 WebView2（须在创建控件前注入）
    try:
        extra = (os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
        flag = "--remote-allow-origins=*"
        if flag not in extra:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                f"{extra} {flag}".strip()
            )
    except Exception:
        pass

    # 后台内置：不自动弹 DevTools 窗口；保留远程调试端口供排查
    try:
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        webview.settings["REMOTE_DEBUGGING_PORT"] = _REMOTE_DEBUG_PORT
    except Exception:
        pass

    # 窗口创建前就关掉任务栏图标（WinForms ShowInTaskbar）
    try:
        import webview.platforms.edgechromium as _edge

        _orig_edge_init = _edge.EdgeChrome.__init__

        def _edge_init_no_taskbar(self, form, window, cache_dir):  # type: ignore[no-untyped-def]
            try:
                form.ShowInTaskbar = False
            except Exception:
                pass
            # 再保险：控件创建属性里补上 allow-origins
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
        "%s 后台模式；远程调试: http://127.0.0.1:%s",
        _SPACE_NAME,
        _REMOTE_DEBUG_PORT,
    )

    server = _serve_http()
    STATE.guest = "qqmusic"
    STATE.guest_url = _HOME

    def _on_ready() -> None:
        STATE.ready.set()
        # 启动后立刻隐身：离屏 + 无任务栏
        try:
            _hide_window()
        except Exception:
            pass

    def _on_before_show() -> None:
        # UI 线程：关掉任务栏图标并缓存 HWND
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
        "hidden": False,  # 不用 hide：WebView2 隐藏会停播；用离屏+无任务栏
        "minimized": False,
        "confirm_close": False,
        "text_select": True,
    }
    try:
        # 尽量不抢焦点（部分 pywebview 版本支持）
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
    # 兼容旧入口；推荐 python -m kekesi.inner_space.host
    raise SystemExit(main())
