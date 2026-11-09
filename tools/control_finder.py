"""统一 WebView 控件层：CDP 求值/点击 + 多指纹定位。

指纹库：kekesi/data/control_fingerprints.json（可热更新）。
听歌空间默认调试口 9222，看剧空间 9223。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_FP_PATH = Path(__file__).resolve().parent.parent / "data" / "control_fingerprints.json"
_FP_CACHE: dict[str, Any] | None = None
_FP_MTIME: float | None = None

# 空间预设：端口 + URL 偏好
MUSIC_DEBUG_PORT = 9222
VIDEO_DEBUG_PORT = 9223
MUSIC_URL_HINT = ["y.qq.com", "music.163.com", "kugou.com"]
VIDEO_URL_HINT = ["v.qq.com", "bilibili.com", "iqiyi.com"]

EvalFn = Callable[[str, float], Any]
ClickFn = Callable[[float, float], None]


def _load_library(*, force: bool = False) -> dict[str, Any]:
    global _FP_CACHE, _FP_MTIME
    try:
        mtime = _FP_PATH.stat().st_mtime
    except OSError:
        return {"version": 0, "controls": {}}
    if not force and _FP_CACHE is not None and _FP_MTIME == mtime:
        return _FP_CACHE
    try:
        data = json.loads(_FP_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("load fingerprints failed: %s", e)
        data = {"version": 0, "controls": {}}
    if not isinstance(data, dict):
        data = {"version": 0, "controls": {}}
    _FP_CACHE = data
    _FP_MTIME = mtime
    return data


def reload_fingerprints() -> dict[str, Any]:
    return _load_library(force=True)


def get_control_def(control_id: str) -> dict[str, Any] | None:
    lib = _load_library()
    controls = lib.get("controls") or {}
    item = controls.get(control_id)
    return item if isinstance(item, dict) else None


def list_controls() -> list[str]:
    lib = _load_library()
    controls = lib.get("controls") or {}
    return sorted(str(k) for k in controls.keys())


def fingerprints_path() -> Path:
    return _FP_PATH


# ---------------------------------------------------------------------------
# CDP 底层（唯一实现）
# ---------------------------------------------------------------------------


def _hint_matches_url(url: str, hint: str) -> bool:
    """域名类 hint 只比 hostname，避免 iframe 查询串误命中（如 cocos 带 v.qq.com）。"""
    from urllib.parse import urlparse

    h = (hint or "").strip().lower()
    u = (url or "").strip()
    if not h or not u:
        return False
    try:
        parsed = urlparse(u)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return h in u.lower()
    # 纯域名 / 站点提示：只匹配 host
    if "." in h and "/" not in h and " " not in h:
        return h == host or host.endswith("." + h) or host.endswith(h)
    # 路径片段（如 songDetail）
    if h in path:
        return True
    # 其余：完整 URL 子串（仍避免仅 query 命中域名类）
    return h in f"{host}{path}"


def cdp_page_ws_url(debug_port: int, *, url_hint: list[str] | None = None) -> str | None:
    try:
        raw = urlopen(f"http://127.0.0.1:{int(debug_port)}/json", timeout=2.0).read()
        pages = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug("cdp list failed port=%s: %s", debug_port, e)
        return None
    if not isinstance(pages, list):
        return None
    hints = [h for h in (url_hint or []) if h]
    # 优先级：page+hint > page > 其它+hint > 其它（避免连上腾讯 cocos iframe）
    rank: list[tuple[int, str]] = []
    for p in pages:
        if not isinstance(p, dict):
            continue
        ws = str(p.get("webSocketDebuggerUrl") or "").strip()
        if not ws:
            continue
        url = str(p.get("url") or "")
        typ = str(p.get("type") or "").lower()
        matched = bool(hints) and any(_hint_matches_url(url, h) for h in hints)
        is_page = typ == "page"
        if hints:
            if is_page and matched:
                score = 0
            elif is_page:
                score = 2
            elif matched:
                score = 3
            else:
                score = 4
        else:
            score = 0 if is_page else 1
        rank.append((score, ws))
    if not rank:
        return None
    rank.sort(key=lambda x: x[0])
    return rank[0][1]


def cdp_session(debug_port: int, *, url_hint: list[str] | None = None, timeout: float = 12.0):
    ws_url = cdp_page_ws_url(debug_port, url_hint=url_hint)
    if not ws_url:
        raise RuntimeError(f"CDP 不可用 (port={debug_port})")
    try:
        import websocket
    except ImportError as e:
        raise RuntimeError("缺少 websocket-client") from e
    ws = websocket.create_connection(ws_url, timeout=timeout)
    msg_id = {"n": 0}

    def call(method: str, params: dict | None = None) -> dict[str, Any]:
        msg_id["n"] += 1
        mid = msg_id["n"]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get("id") != mid:
                continue
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data
        raise TimeoutError(method)

    return ws, call


def cdp_eval(
    expression: str,
    *,
    debug_port: int,
    url_hint: list[str] | None = None,
    timeout: float = 10.0,
    await_promise: bool = True,
) -> Any:
    ws, call = cdp_session(debug_port, url_hint=url_hint, timeout=timeout)
    try:
        data = call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
            },
        )
        result = (data.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error" or (data.get("result") or {}).get(
            "exceptionDetails"
        ):
            raise RuntimeError(
                str((data.get("result") or {}).get("exceptionDetails") or result)
            )
        return result.get("value")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def cdp_click_xy(
    x: float,
    y: float,
    *,
    debug_port: int,
    url_hint: list[str] | None = None,
    timeout: float = 8.0,
) -> None:
    ws, call = cdp_session(debug_port, url_hint=url_hint, timeout=timeout)
    try:
        call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": float(x), "y": float(y), "button": "none"},
        )
        time.sleep(0.05)
        call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "clickCount": 1,
            },
        )
        time.sleep(0.05)
        call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "clickCount": 1,
            },
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


def cdp_navigate(
    url: str,
    *,
    debug_port: int,
    url_hint: list[str] | None = None,
    new_tab: bool = True,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """CDP 打开/跳转 URL（浏览器搜索用，勿误连 9222/9223 听歌看剧口）。"""
    target = (url or "").strip()
    if not target:
        return {"ok": False, "reason": "empty_url"}
    # 优先新标签，避免冲掉用户当前页
    if new_tab:
        try:
            raw = urlopen(f"http://127.0.0.1:{int(debug_port)}/json/new?{target}", timeout=3.0).read()
            info = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(info, dict) and (info.get("webSocketDebuggerUrl") or info.get("id")):
                return {"ok": True, "via": "cdp_json_new", "url": target, "id": info.get("id")}
        except Exception as e:
            logger.debug("cdp json/new failed: %s", e)
        try:
            ws, call = cdp_session(debug_port, url_hint=url_hint, timeout=timeout)
            try:
                call("Target.createTarget", {"url": target})
                return {"ok": True, "via": "cdp_create_target", "url": target}
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("cdp createTarget failed: %s", e)
    try:
        ws, call = cdp_session(debug_port, url_hint=url_hint, timeout=timeout)
        try:
            call("Page.navigate", {"url": target})
            return {"ok": True, "via": "cdp_navigate", "url": target}
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "reason": str(e), "url": target}


def page_eval(
    expression: str,
    *,
    debug_port: int,
    url_hint: list[str] | None = None,
    fallback_eval: EvalFn | None = None,
    timeout: float = 8.0,
    await_promise: bool = True,
) -> Any:
    """CDP 求值，失败时可选回退 pywebview evaluate_js。"""
    try:
        return cdp_eval(
            expression,
            debug_port=debug_port,
            url_hint=url_hint,
            timeout=timeout,
            await_promise=await_promise,
        )
    except Exception as e:
        if fallback_eval is None:
            raise
        logger.debug("cdp eval fallback: %s", e)
        return fallback_eval(expression, timeout)


# ---------------------------------------------------------------------------
# 多指纹定位 / 点击
# ---------------------------------------------------------------------------


def _build_finder_js(control: dict[str, Any]) -> str:
    """生成页面内打分脚本：返回最佳候选 {ok,x,y,score,matched,...}。"""
    payload = {
        "fingerprints": control.get("fingerprints") or [],
        "exclude_closest": control.get("exclude_closest") or [],
        "min_score": float(control.get("min_score") or 8),
    }
    blob = json.dumps(payload, ensure_ascii=False)
    return f"""
(() => {{
  const cfg = {blob};
  const fps = Array.isArray(cfg.fingerprints) ? cfg.fingerprints : [];
  const excludes = Array.isArray(cfg.exclude_closest) ? cfg.exclude_closest : [];
  const minScore = Number(cfg.min_score || 8);
  const vw = Math.max(window.innerWidth || 1, 1);
  const vh = Math.max(window.innerHeight || 1, 1);

  const isPresent = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 6 && r.height > 6
      && st.visibility !== 'hidden' && st.display !== 'none'
      && st.pointerEvents !== 'none';
  }};
  const inViewport = (el) => {{
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  }};

  const excluded = (el) => {{
    if (!el || !el.closest) return false;
    for (const sel of excludes) {{
      try {{ if (sel && el.closest(sel)) return true; }} catch (e) {{}}
    }}
    return false;
  }};

  const textOf = (el) => ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
  const ariaOf = (el) => ((el.getAttribute('aria-label') || el.getAttribute('title') || '') + '').trim();
  const clsOf = (el) => String(el.className || el.getAttribute('class') || '');

  const candidates = new Map();

  const bump = (el, weight, tag) => {{
    if (!el || !isPresent(el) || excluded(el)) return;
    const cur = candidates.get(el) || {{ el, score: 0, matched: [] }};
    cur.score += Number(weight) || 0;
    cur.matched.push(tag);
    candidates.set(el, cur);
  }};

  for (const fp of fps) {{
    if (!fp || !fp.type) continue;
    const type = String(fp.type);
    const weight = Number(fp.weight) || 1;
    const value = String(fp.value || '');
    const match = String(fp.match || 'exact_or_prefix');

    if (type === 'text' && value) {{
      const nodes = Array.from(document.querySelectorAll('a,button,span,div,i,em,[role=button]'));
      for (const el of nodes) {{
        if (!isPresent(el) || excluded(el)) continue;
        const t = textOf(el);
        if (!t) continue;
        if (t.length > value.length + 4) continue;
        let hit = false;
        let w = weight;
        if (match === 'exact') hit = (t === value);
        else if (match === 'contains') hit = t.includes(value);
        else {{
          hit = (t === value || t.startsWith(value));
          if (hit && t === value) w += 4;
        }}
        if (hit) bump(el, w, 'text:' + value);
      }}
    }} else if (type === 'aria' && value) {{
      const nodes = Array.from(document.querySelectorAll('[aria-label],[title],a,button'));
      for (const el of nodes) {{
        if (!isPresent(el) || excluded(el)) continue;
        const a = ariaOf(el);
        if (!a) continue;
        if (a === value || a.includes(value)) bump(el, weight, 'aria:' + value);
      }}
    }} else if (type === 'css' && value) {{
      let nodes = [];
      try {{ nodes = Array.from(document.querySelectorAll(value)); }} catch (e) {{ continue; }}
      for (const el of nodes) bump(el, weight, 'css:' + value);
    }} else if (type === 'class' && value) {{
      let nodes = [];
      try {{ nodes = Array.from(document.querySelectorAll('.' + value.replace(/^\\./, ''))); }} catch (e) {{ continue; }}
      for (const el of nodes) bump(el, weight, 'class:' + value);
    }} else if (type === 'class_contains' && value) {{
      const nodes = Array.from(document.querySelectorAll('a,button,span,div,i,em,[role=button]'));
      for (const el of nodes) {{
        if (!isPresent(el) || excluded(el)) continue;
        const c = clsOf(el);
        if (c && c.includes(value)) bump(el, weight, 'class_contains:' + value);
      }}
    }} else if (type === 'xpath' && value) {{
      try {{
        const snap = document.evaluate(value, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        for (let i = 0; i < snap.snapshotLength; i++) {{
          bump(snap.snapshotItem(i), weight, 'xpath');
        }}
      }} catch (e) {{}}
    }} else if (type === 'position') {{
      const tx = Number(fp.x);
      const ty = Number(fp.y);
      const tol = Number(fp.tolerance != null ? fp.tolerance : 0.15);
      if (!(tx >= 0 && tx <= 1 && ty >= 0 && ty <= 1)) continue;
      for (const [el, cur] of candidates) {{
        const r = el.getBoundingClientRect();
        const nx = (r.left + r.width / 2) / vw;
        const ny = (r.top + r.height / 2) / vh;
        if (Math.abs(nx - tx) <= tol && Math.abs(ny - ty) <= tol) {{
          cur.score += weight;
          cur.matched.push('position');
        }}
      }}
    }}
  }}

  let best = null;
  for (const cur of candidates.values()) {{
    if (inViewport(cur.el)) cur.score += 2;
    const r = cur.el.getBoundingClientRect();
    const area = Math.max(1, r.width * r.height);
    const tlen = textOf(cur.el).length || 99;
    cur._area = area;
    cur._tlen = tlen;
    if (!best) {{ best = cur; continue; }}
    if (cur.score > best.score) {{ best = cur; continue; }}
    if (cur.score === best.score) {{
      if (tlen < best._tlen || (tlen === best._tlen && area < best._area)) best = cur;
    }}
  }}
  if (!best || best.score < minScore) {{
    return {{
      ok: false,
      reason: 'no_match',
      minScore,
      bestScore: best ? best.score : 0,
      href: location.href,
      candidateCount: candidates.size,
      sample: ((document.body && document.body.innerText) || '').slice(0, 200),
    }};
  }}
  const el = best.el;
  try {{ el.scrollIntoView({{ block: 'center', inline: 'nearest' }}); }} catch (e) {{}}
  const r = el.getBoundingClientRect();
  return {{
    ok: true,
    score: best.score,
    matched: best.matched.slice(0, 12),
    text: textOf(el).slice(0, 40),
    aria: ariaOf(el).slice(0, 40),
    cls: clsOf(el).slice(0, 120),
    outer: (el.outerHTML || '').slice(0, 280),
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    w: r.width,
    h: r.height,
    inViewport: inViewport(el),
    href: location.href,
  }};
}})()
"""


def find_control(
    control_id: str,
    *,
    debug_port: int | None = None,
    eval_fn: EvalFn | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """按多指纹打分定位控件，不点击。"""
    control = get_control_def(control_id)
    if not control:
        return {"ok": False, "reason": f"unknown_control:{control_id}"}
    js = _build_finder_js(control)
    url_hint = [str(x) for x in (control.get("url_hint") or []) if x]
    box: Any = None
    err: str | None = None
    if debug_port is not None:
        try:
            box = cdp_eval(js, debug_port=debug_port, url_hint=url_hint, timeout=timeout)
        except Exception as e:
            err = f"cdp:{e}"
            logger.debug("find_control cdp failed %s: %s", control_id, e)
    if not isinstance(box, dict) and eval_fn is not None:
        try:
            box = eval_fn(js, timeout)
        except Exception as e:
            err = (err + ";" if err else "") + f"eval:{e}"
    if not isinstance(box, dict):
        return {"ok": False, "reason": err or "eval_failed", "controlId": control_id}
    box = dict(box)
    box["controlId"] = control_id
    if box.get("ok"):
        box["foundVia"] = "fingerprint"
    return box


def click_control(
    control_id: str,
    *,
    debug_port: int | None = None,
    eval_fn: EvalFn | None = None,
    click_fn: ClickFn | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """定位并用 CDP（或自定义 click_fn）点击。"""
    target = find_control(
        control_id,
        debug_port=debug_port,
        eval_fn=eval_fn,
        timeout=timeout,
    )
    if not target.get("ok"):
        return target
    try:
        x = float(target["x"])
        y = float(target["y"])
    except Exception:
        return {"ok": False, "reason": "bad_box", "target": target}

    control = get_control_def(control_id) or {}
    url_hint = [str(u) for u in (control.get("url_hint") or []) if u]

    if click_fn is not None:
        try:
            click_fn(x, y)
            target["clicked"] = True
            target["via"] = f"custom:{control_id}"
            return target
        except Exception as e:
            target["customClickError"] = str(e)

    if debug_port is not None:
        try:
            cdp_click_xy(
                x, y, debug_port=debug_port, url_hint=url_hint, timeout=min(8.0, timeout)
            )
            target["clicked"] = True
            target["via"] = f"cdp_input:{control_id}"
            return target
        except Exception as e:
            target["cdpClickError"] = str(e)
            logger.warning("cdp click %s failed: %s", control_id, e)

    return {"ok": False, "reason": "click_failed", "target": target}


def wait_and_click(
    control_id: str,
    *,
    debug_port: int | None = None,
    eval_fn: EvalFn | None = None,
    click_fn: ClickFn | None = None,
    tries: int = 12,
    timeout: float = 8.0,
) -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False}
    for i in range(max(1, tries)):
        last = click_control(
            control_id,
            debug_port=debug_port,
            eval_fn=eval_fn,
            click_fn=click_fn,
            timeout=timeout,
        )
        if last.get("ok") and last.get("clicked"):
            return last
        if last.get("ok") and not last.get("clicked"):
            time.sleep(0.35)
            continue
        time.sleep(0.35 if i else 0.7)
    return last if isinstance(last, dict) else {"ok": False, "reason": "timeout"}
