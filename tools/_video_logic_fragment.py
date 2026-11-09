def _normalize_video_source(raw: str | None) -> str:
    t = (raw or "").strip().lower().replace(" ", "")
    if t in ("bilibili", "b站", "哔哩", "哔哩哔哩", "bili"):
        return "bilibili"
    if t in ("tencent", "腾讯", "腾讯视频", "tx", "v.qq"):
        return "tencent"
    if t in ("iqiyi", "爱奇艺", "奇艺", "iqy"):
        return "iqiyi"
    return "bilibili"


def _strip_html(s: str) -> str:
    import re as _re

    return _re.sub(r"<[^>]+>", "", s or "").strip()


def _http_json_cookie(url: str, *, referer: str) -> dict[str, Any]:
    """带简易 CookieJar 的 JSON GET（B 站搜索需要 buvid）。"""
    import http.cookiejar
    from urllib.request import HTTPCookieProcessor, build_opener

    try:
        cj = http.cookiejar.CookieJar()
        opener_obj = build_opener(HTTPCookieProcessor(cj))
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        opener_obj.open(
            Request("https://www.bilibili.com/", headers={"User-Agent": ua}),
            timeout=10.0,
        ).read(400)
        req = Request(
            url,
            headers={
                "User-Agent": ua,
                "Referer": referer,
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with opener_obj.open(req, timeout=12.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("http_json_cookie fail: %s", e)
        return {}


def _search_bilibili_media(query: str) -> list[dict[str, Any]]:
    q = quote((query or "").strip())
    out: list[dict[str, Any]] = []
    for st in ("media_bangumi", "media_ft"):
        url = (
            "https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type={st}&keyword={q}&page=1"
        )
        data = _http_json_cookie(url, referer="https://search.bilibili.com")
        result = ((data.get("data") or {}).get("result")) or []
        for it in result:
            if not isinstance(it, dict):
                continue
            sid = it.get("season_id")
            if not sid:
                continue
            title = _strip_html(str(it.get("title") or ""))
            out.append(
                {
                    "title": title,
                    "season_id": int(sid),
                    "media_id": it.get("media_id"),
                    "season_type": it.get("season_type_name") or st,
                    "styles": str(it.get("styles") or ""),
                }
            )
    return out


def _bilibili_season_episodes(season_id: int) -> list[dict[str, Any]]:
    url = f"https://api.bilibili.com/pgc/view/web/season?season_id={int(season_id)}"
    data = _http_json_cookie(url, referer="https://www.bilibili.com")
    episodes = (((data.get("result") or {}).get("episodes")) or [])
    out: list[dict[str, Any]] = []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        epid = ep.get("id") or ep.get("ep_id")
        if not epid:
            continue
        out.append(
            {
                "id": int(epid),
                "title": str(ep.get("title") or ""),
                "long_title": str(ep.get("long_title") or ""),
                "url": str(
                    ep.get("share_url")
                    or ep.get("link")
                    or f"https://www.bilibili.com/bangumi/play/ep{epid}"
                ),
            }
        )
    return out


def _pick_bilibili_show(query: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    q = (query or "").strip().lower()

    def score(it: dict[str, Any]) -> tuple:
        title = (it.get("title") or "").lower()
        exact = 2 if title == q else (1 if q in title else 0)
        # 电影单集降权（用户常要追番）
        styles = (it.get("styles") or "") + str(it.get("season_type") or "")
        penalty = -1 if "电影" in styles else 0
        # 过短错配（完美世界→完美的世界）惩罚标题多字
        length_pen = -abs(len(title) - len(q)) * 0.01
        return (exact + penalty + length_pen, exact)

    return max(items, key=score)


def _pick_episode(episodes: list[dict[str, Any]], episode: int | None) -> dict[str, Any] | None:
    if not episodes:
        return None
    if episode is None:
        return episodes[0]
    n = int(episode)
    # 1) title 精确为集数
    for ep in episodes:
        t = (ep.get("title") or "").strip()
        if t == str(n) or t == f"第{n}话" or t == f"第{n}集":
            return ep
    # 2) 按下标
    if 1 <= n <= len(episodes):
        return episodes[n - 1]
    return None


def _title_matches(expected: str, actual: str) -> bool:
    e = (expected or "").strip().lower()
    a = (actual or "").strip().lower()
    if not e or not a:
        return False
    return e == a or e in a or a in e


def _api_play_video_bilibili(query: str, episode: int | None) -> dict[str, Any]:
    q = (query or "").strip()
    items = _search_bilibili_media(q)
    hit = _pick_bilibili_show(q, items)
    if not hit or not _title_matches(q, str(hit.get("title") or "")):
        # 标题对不上也允许最相关，但提示
        if not hit:
            return {
                "ok": False,
                "source": "bilibili",
                "message": (
                    f"在哔哩哔哩没搜到「{q}」番剧/影视。"
                    "可换源：说「用腾讯视频看…」或「用爱奇艺看…」"
                ),
            }
    season_id = int(hit["season_id"])
    show_title = str(hit.get("title") or q)
    episodes = _bilibili_season_episodes(season_id)
    ep = _pick_episode(episodes, episode)
    if not ep:
        return {
            "ok": False,
            "source": "bilibili",
            "message": (
                f"已找到《{show_title}》，但没有第{episode}集"
                if episode
                else f"已找到《{show_title}》，但没有可播放集数"
            ),
            "season_id": season_id,
            "episode_count": len(episodes),
        }
    play_url = str(ep["url"])
    with STATE.lock:
        STATE.last_query = q
        STATE.guest = "bilibili"
        _show_window()
        _load_url(play_url, wait=3.5, guest="bilibili")
        # 尝试点播放（部分页需手势）
        try:
            _eval_js(
                """
(() => {
  const sels = [
    'button.bpx-player-ctrl-play',
    '.bpx-player-ctrl-play',
    '.squirtle-video-start',
    'div.bilibili-player-video-btn-start',
    'button[aria-label*=\"播放\"]',
  ];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) { try { el.click(); return s; } catch (e) {} }
  }
  const v = document.querySelector('video');
  if (v) { try { v.play(); return 'video.play'; } catch (e) {} }
  return null;
})()
""",
                timeout=6.0,
            )
        except Exception:
            pass
        ep_label = f"第{episode}集" if episode else (ep.get("title") or "")
        return {
            "ok": True,
            "source": "bilibili",
            "message": f"正在打开《{show_title}》{ep_label}（哔哩哔哩）",
            "title": show_title,
            "episode": episode,
            "url": play_url,
            "ep_id": ep.get("id"),
            "video": {
                "title": show_title,
                "episode": episode,
                "url": play_url,
            },
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
          episodes: (sites[0] && sites[0].episodeInfoList || []).slice(0, 40).map((e) => ({
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


_JS_FIND_VIDEO_RESULTS = r"""
(query) => {
  const q = String(query || '').trim();
  const out = [];
  const push = (href, text) => {
    if (!href || !text) return;
    const t = text.replace(/\s+/g, ' ').trim().slice(0, 80);
    if (t.length < 2) return;
    out.push({ href, text: t });
  };
  document.querySelectorAll('a[href]').forEach((a) => {
    const href = a.href || '';
    if (!/\/x\/cover\/|\/v_|\.iqiyi\.com\/v_|bangumi\/play|bangumi\/media/.test(href)) return;
    const t = (a.innerText || a.textContent || a.getAttribute('title') || '').trim();
    push(href, t);
  });
  // 去重
  const seen = new Set();
  const uniq = [];
  for (const it of out) {
    if (seen.has(it.href)) continue;
    seen.add(it.href);
    uniq.push(it);
  }
  return { ok: true, href: location.href, items: uniq.slice(0, 30), q };
}
"""


_JS_CLICK_EPISODE = r"""
(episode) => {
  const n = Number(episode);
  if (!n) return { ok: false, reason: 'no_ep' };

  // 腾讯视频选集常按「1-30 / 31-60」分页，先点对应分段
  let rangeClicked = null;
  const rangeNodes = Array.from(document.querySelectorAll('.tab-node, .tab-header, a, button, span, div, li'));
  for (const el of rangeNodes) {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^\d{1,4}-\d{1,4}$/.test(t)) continue;
    const parts = t.split('-').map((x) => Number(x));
    if (parts.length !== 2 || !(n >= parts[0] && n <= parts[1])) continue;
    try { el.click(); rangeClicked = t; } catch (e) {}
    break;
  }

  const prefer = Array.from(document.querySelectorAll('.episode-item-text, .episode-item'));
  const pool = prefer.length ? prefer : Array.from(document.querySelectorAll('a, button, span, li, div'));
  const hits = [];
  for (const el of pool) {
    const raw = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    const bare = raw.replace(/VIP|预告|免费/g, '').trim();
    if (!(bare === String(n) || raw === ('第' + n + '集') || raw === ('第' + n + '话'))) continue;
    // 腾讯虚拟列表里节点宽高可能为 0，仍可 click
    hits.push({
      text: raw,
      bare,
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 80),
    });
  }
  if (!hits.length) {
    return { ok: false, reason: 'ep_not_found', n, rangeClicked };
  }
  hits.sort((a, b) => {
    const ap = /episode-item-text/.test(a.cls) ? 0 : 1;
    const bp = /episode-item-text/.test(b.cls) ? 0 : 1;
    return ap - bp || a.bare.length - b.bare.length;
  });
  const hit = hits[0];
  let clicked = null;
  for (const el of pool) {
    const raw = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const bare = raw.replace(/VIP|预告|免费/g, '').trim();
    if (bare === String(n) || raw === hit.text) {
      try { el.click(); clicked = raw; break; } catch (e) {}
    }
  }
  return { ok: !!clicked, clicked, hit, rangeClicked, n, href: location.href };
}
"""


def _pick_tencent_hit(query: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not hits:
        return None
    q = (query or "").strip()

    def score(h: dict[str, Any]) -> tuple:
        title = str(h.get("title") or "")
        typ = str(h.get("typeName") or "")
        exact = 3 if title == q else (2 if q in title else 0)
        type_bonus = 2 if typ in ("动漫", "电视剧", "少儿") else (0 if typ == "电影" else 1)
        # 合集/剧场版略降权
        pen = 0
        if any(x in title for x in ("合集", "剧场版", "特别篇", "动态漫", "高光")):
            pen -= 2
        return (exact + type_bonus + pen, exact, int(h.get("totalEpisode") or 0))

    return max(hits, key=score)


def _api_play_video_tencent(query: str, episode: int | None) -> dict[str, Any]:
    """F12 同源 MbSearch → 封面页 → 点集数。"""
    q = (query or "").strip()
    with STATE.lock:
        STATE.last_query = q
        STATE.guest = "tencent"
        _show_window()
        # 先到腾讯域，带登录 Cookie 再调 MbSearch
        _load_url("https://v.qq.com/", wait=2.0, guest="tencent")
        try:
            found = _eval_js(f"({_JS_TENCENT_MBSEARCH})({json.dumps(q)})", timeout=15.0) or {}
        except Exception as e:
            found = {"ok": False, "error": str(e)}
        hits = list(found.get("hits") or []) if isinstance(found, dict) else []
        hit = _pick_tencent_hit(q, hits)
        if not hit:
            search_url = f"https://v.qq.com/x/search/?q={quote(q)}"
            _load_url(search_url, wait=3.5, guest="tencent")
            return {
                "ok": True,
                "source": "tencent",
                "needPick": True,
                "message": (
                    f"已在腾讯视频打开「{q}」搜索页（请在弹出窗口点选）。"
                    + (f"目标第{episode}集。" if episode else "")
                ),
                "url": search_url,
                "search": found,
                "video": {"title": q, "episode": episode, "url": search_url},
            }

        cid = str(hit.get("cid") or "")
        show_title = str(hit.get("title") or q)
        play_url = ""
        # 若搜索结果里已有目标集 URL，直接开
        if episode:
            for ep in hit.get("episodes") or []:
                if str(ep.get("title") or "") == str(int(episode)):
                    play_url = str(ep.get("url") or "")
                    break
        if not play_url:
            # 打开封面，再点集数
            cover_url = f"https://v.qq.com/x/cover/{cid}.html"
            _load_url(cover_url, wait=2.5, guest="tencent")
            # 等选集 UI
            for _ in range(12):
                try:
                    ready = _eval_js(
                        "!!(document.querySelector('.tab-node') || document.querySelector('.episode-item-text'))",
                        timeout=4.0,
                    )
                    if ready:
                        break
                except Exception:
                    pass
                time.sleep(0.45)

            clicked = None
            if episode:
                # 先点分段，再点集数（分两步，虚拟列表需要间隔）
                try:
                    _eval_js(f"({_JS_CLICK_EPISODE})({int(episode)})", timeout=8.0)
                except Exception:
                    pass
                time.sleep(1.2)
                for _attempt in range(3):
                    try:
                        clicked = _eval_js(
                            f"({_JS_CLICK_EPISODE})({int(episode)})",
                            timeout=8.0,
                        )
                    except Exception as e:
                        clicked = {"ok": False, "reason": str(e)}
                    if isinstance(clicked, dict) and clicked.get("ok"):
                        time.sleep(1.0)
                        break
                    time.sleep(1.0)

            # SPA 跳转不会自动改 STATE.guest_url，读真实地址
            try:
                now_loc = _eval_js(
                    "({href: location.href, title: document.title || ''})",
                    timeout=5.0,
                ) or {}
            except Exception:
                now_loc = {}
            play_url = str(now_loc.get("href") or STATE.guest_url or cover_url)
            page_title = str(now_loc.get("title") or "")
            STATE.guest_url = play_url

            ep_label = f"第{episode}集" if episode else ""
            on_target = False
            if episode:
                if f"_{int(episode)}_" in page_title or page_title.startswith(
                    f"{show_title}_{int(episode)}"
                ):
                    on_target = True
                if f"/{cid}/" in play_url and play_url.rstrip("/").endswith(".html"):
                    # cover/cid/vid.html 且标题含集数
                    if str(int(episode)) in page_title:
                        on_target = True
            if episode and (
                on_target or (isinstance(clicked, dict) and clicked.get("ok"))
            ):
                msg = f"正在播放《{show_title}》{ep_label}（腾讯视频）"
                ok_flag = True
            elif episode:
                msg = f"已打开《{show_title}》详情页（腾讯视频），请在窗口确认第{episode}集"
                ok_flag = True
            else:
                msg = f"已打开《{show_title}》（腾讯视频）"
                ok_flag = True
            return {
                "ok": ok_flag,
                "source": "tencent",
                "message": msg,
                "title": show_title,
                "episode": episode,
                "cid": cid,
                "url": play_url,
                "pageTitle": page_title,
                "clicked": clicked,
                "video": {
                    "title": show_title,
                    "episode": episode,
                    "url": play_url,
                },
            }

        _load_url(play_url, wait=3.5, guest="tencent")
        ep_label = f"第{episode}集" if episode else ""
        return {
            "ok": True,
            "source": "tencent",
            "message": f"正在播放《{show_title}》{ep_label}（腾讯视频）",
            "title": show_title,
            "episode": episode,
            "cid": cid,
            "url": play_url,
            "video": {
                "title": show_title,
                "episode": episode,
                "url": play_url,
            },
        }


def _api_play_video_via_search_page(
    query: str,
    episode: int | None,
    *,
    source: str,
) -> dict[str, Any]:
    """爱奇艺等：打开官网搜索页 → 点进封面 → 点集数 → 弹窗。"""
    q = (query or "").strip()
    label = _VIDEO_LABELS.get(source, source)
    if source == "tencent":
        return _api_play_video_tencent(q, episode)
    search_url = f"https://so.iqiyi.com/so/q_{quote(q)}"

    with STATE.lock:
        STATE.last_query = q
        STATE.guest = source
        _show_window()
        _load_url(search_url, wait=4.0, guest=source)

        # 等结果链接出现
        items: list[dict[str, Any]] = []
        for _ in range(10):
            try:
                found = _eval_js(f"({_JS_FIND_VIDEO_RESULTS})({json.dumps(q)})", timeout=8.0) or {}
                items = list(found.get("items") or [])
            except Exception as e:
                logger.debug("find video results: %s", e)
                items = []
            if items:
                break
            time.sleep(0.6)

        # 选标题最像的
        best = None
        best_score = -1
        for it in items:
            text = str(it.get("text") or "")
            href = str(it.get("href") or "")
            if q in text:
                sc = 10 + (5 if text.startswith(q) else 0)
            elif any(ch in text for ch in q):
                sc = sum(1 for ch in q if ch in text)
            else:
                sc = 0
            if sc > best_score:
                best_score = sc
                best = it

        if not best or best_score <= 0:
            return {
                "ok": True,
                "source": source,
                "needPick": True,
                "message": (
                    f"已在{label}打开「{q}」搜索页（请在弹出窗口点选）。"
                    + (f"目标第{episode}集。" if episode else "")
                ),
                "url": search_url,
                "video": {"title": q, "episode": episode, "url": search_url},
            }

        cover_url = str(best["href"])
        _load_url(cover_url, wait=3.5, guest=source)
        clicked = None
        if episode:
            try:
                clicked = _eval_js(
                    f"({_JS_CLICK_EPISODE})({int(episode)})",
                    timeout=8.0,
                )
            except Exception as e:
                clicked = {"ok": False, "reason": str(e)}
            if not (isinstance(clicked, dict) and clicked.get("ok")):
                time.sleep(1.2)
                try:
                    clicked = _eval_js(
                        f"({_JS_CLICK_EPISODE})({int(episode)})",
                        timeout=8.0,
                    )
                except Exception:
                    pass

        ep_label = f"第{episode}集" if episode else ""
        if episode and isinstance(clicked, dict) and clicked.get("ok"):
            msg = f"正在播放《{q}》{ep_label}（{label}）"
        elif episode:
            msg = f"已打开《{q}》详情页（{label}），请在窗口点第{episode}集。"
        else:
            msg = f"已打开《{q}》（{label}）"
        return {
            "ok": True,
            "source": source,
            "message": msg,
            "title": q,
            "episode": episode,
            "url": STATE.guest_url or cover_url,
            "clicked": clicked,
            "video": {
                "title": q,
                "episode": episode,
                "url": STATE.guest_url or cover_url,
            },
        }


def _api_play_video(
    query: str,
    *,
    episode: int | None = None,
    source: str = "bilibili",
    show: bool = True,
) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "message": "请提供剧名"}
    src = _normalize_video_source(source)
    # 视频默认弹窗
    _ = show
    if src == "bilibili":
        return _api_play_video_bilibili(q, episode)
    if src == "tencent":
        return _api_play_video_tencent(q, episode)
    if src == "iqiyi":
        return _api_play_video_via_search_page(q, episode, source=src)
    return {"ok": False, "message": f"暂不支持源站: {src}"}


def _api_video_login(source: str = "bilibili") -> dict[str, Any]:
    src = _normalize_video_source(source)
    home = _SOURCE_HOME.get(src, "https://www.bilibili.com/")
    label = _VIDEO_LABELS.get(src, src)
    with STATE.lock:
        STATE.guest = src
        _show_window()
        _load_url(home, wait=2.5, guest=src)
        return {
            "ok": True,
            "source": src,
            "message": f"已在科科斯体内空间打开 {label}，请扫码/登录后说「看剧名第N集」",
        }

