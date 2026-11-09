"""P5-2: success / failure trajectory case memory bank.

Two tables live in the same P5 store file (episodes_cases.db) but are
kept strictly separate: staging_cases holds fresh trajectories, cases
holds promoted permanent cases. Promotion is evidence-gated: a lesson
reaches the permanent bank only with >= 3 independent evidences, where
independent means distinct calendar dates, and only rows that carry
evidence ids count. Reflections without evidence never promote.
Permanent cases are never auto-deleted and never feed P3 skill pruning.

Marker: KEKESI_PCE_P5_20261101
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MARKER = "KEKESI_PCE_P5_20261101"
PROMOTION_MIN_DATES = 3

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CJK_SEQ = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK_STOP = frozenset("的了呢吗吧啊呀哦哈我你他她它在是有和与或把被给让去来用做干看说请问帮")
_TOOL_CN_ALIASES = {
    "open_app": "打开 启动 运行 应用 软件 程序 浏览器 记事本 计算器 微信",
    "close_app": "关闭 关掉 退出 停止 应用 软件 程序",
    "get_weather": "天气 气温 下雨 温度 预报 查天气",
    "execute_gui_macro": "宏 鼠标 键盘 点击 自动化 操作 界面 gui",
    "douyin_browse": "抖音 刷抖音 视频 浏览 直播 评论 点赞",
    "douyin": "抖音 刷抖音 视频 浏览 直播",
    "napcat_qq_bot": "qq 机器人 napcat 群聊 消息 启动",
    "napcat_control": "qq 机器人 napcat 群聊 消息 启动 控制",
    "pvz": "植物大战僵尸 植物 僵尸 托管 游戏",
    "doudizhu": "斗地主 打牌 出牌 托管 游戏",
    "web_search": "搜索 网页 查询 百度 浏览器",
    "fetch_webpage": "网页 抓取 浏览 链接",
    "run_shell": "命令行 终端 脚本 执行 命令 shell",
    "run_python": "代码 脚本 运行 python",
}


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _default_store_path():
    return _repo_root() / "kekesi" / "data" / "episodes_cases.db"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm_key(outcome, lesson):
    text = re.sub(r"\s+", " ", (lesson or "").strip().lower())
    return "%s|%s" % (outcome, text)


def _date_of(created_at):
    if not created_at:
        return ""
    text = str(created_at)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return text[:10]


def _tokens(text):
    raw = text or ""
    words = _WORD.findall(raw.lower())
    found = set(words)
    for w in list(words):
        for part in w.split("_"):
            if len(part) >= 2:
                found.add(part)
            if part in _TOOL_CN_ALIASES:
                found.update(_tokens_cn(_TOOL_CN_ALIASES[part]))
        if w in _TOOL_CN_ALIASES:
            found.update(_tokens_cn(_TOOL_CN_ALIASES[w]))
    found.update(_tokens_cn(raw))
    return found


def _tokens_cn(text):
    out = set()
    for ch in _CJK.findall(text or ""):
        if ch not in _CJK_STOP:
            out.add(ch)
    for seq in _CJK_SEQ.findall(text or ""):
        for i in range(len(seq) - 1):
            bg = seq[i : i + 2]
            if not all(c in _CJK_STOP for c in bg):
                out.add(bg)
    return out


class CaseStore:
    """staging_cases + cases inside the P5 store (never the main db)."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else _default_store_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS staging_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_key TEXT,
                    trajectory_summary TEXT,
                    outcome TEXT,
                    lesson TEXT,
                    steps TEXT,
                    evidence_ids TEXT,
                    created_at TEXT,
                    promoted INTEGER DEFAULT 0
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_key TEXT UNIQUE,
                    trajectory_summary TEXT,
                    outcome TEXT,
                    lesson TEXT,
                    steps TEXT,
                    evidence_ids TEXT,
                    evidence_count INTEGER,
                    promoted_at TEXT
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def record_case(
        self,
        trajectory,
        outcome,
        lesson,
        steps=None,
        evidence_ids=None,
        created_at=None,
    ):
        """Store one trajectory in staging; promote when evidence suffices.

        trajectory may be a summary string or a list of step names.
        Returns "staged" or "promoted".
        """
        if isinstance(trajectory, (list, tuple)):
            summary = " -> ".join(str(step) for step in trajectory)
            step_list = [str(step) for step in trajectory]
        else:
            summary = str(trajectory or "")
            step_list = list(steps or [])
        outcome = str(outcome or "unknown")
        lesson = str(lesson or "")
        key = _norm_key(outcome, lesson)
        evidence = list(evidence_ids or [])
        stamp = created_at or _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO staging_cases(lesson_key, trajectory_summary,"
                " outcome, lesson, steps, evidence_ids, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    summary,
                    outcome,
                    lesson,
                    json.dumps(step_list, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    stamp,
                ),
            )
            conn.commit()
            promoted = self._maybe_promote(conn, key)
            conn.commit()
            return "promoted" if promoted else "staged"
        finally:
            conn.close()

    def _maybe_promote(self, conn, key):
        """Promote a lesson once >= 3 distinct dated evidences exist.

        Only unpromoted staging rows that carry evidence ids count.
        Returns True when a permanent row was inserted or refreshed.
        """
        rows = conn.execute(
            "SELECT id, evidence_ids, created_at FROM staging_cases"
            " WHERE lesson_key = ? AND promoted = 0",
            (key,),
        ).fetchall()
        dated = {}
        all_evidence = []
        for row in rows:
            try:
                evidence = json.loads(row["evidence_ids"] or "[]")
            except (ValueError, TypeError):
                evidence = []
            if not evidence:
                continue
            dated.setdefault(_date_of(row["created_at"]), []).append(row["id"])
            all_evidence.extend(evidence)
        if len(dated) < PROMOTION_MIN_DATES:
            return False
        head = conn.execute(
            "SELECT trajectory_summary, outcome, lesson, steps"
            " FROM staging_cases WHERE lesson_key = ? ORDER BY id LIMIT 1",
            (key,),
        ).fetchone()
        if head is None:
            return False
        conn.execute(
            "INSERT INTO cases(lesson_key, trajectory_summary, outcome,"
            " lesson, steps, evidence_ids, evidence_count, promoted_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(lesson_key) DO UPDATE SET"
            " evidence_ids = excluded.evidence_ids,"
            " evidence_count = excluded.evidence_count,"
            " promoted_at = excluded.promoted_at",
            (
                key,
                head["trajectory_summary"],
                head["outcome"],
                head["lesson"],
                head["steps"],
                json.dumps(sorted(set(all_evidence)), ensure_ascii=False),
                len(dated),
                _now_iso(),
            ),
        )
        for row_id in [rid for ids in dated.values() for rid in ids]:
            conn.execute(
                "UPDATE staging_cases SET promoted = 1 WHERE id = ?", (row_id,)
            )
        return True

    def query_cases(self, situation, limit=5):
        """Retrieve similar permanent cases by token overlap (no LLM)."""
        wanted = _tokens(situation)
        if not wanted:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT lesson_key, trajectory_summary, outcome, lesson,"
                " steps, evidence_ids, evidence_count, promoted_at"
                " FROM cases ORDER BY promoted_at DESC"
            ).fetchall()
        finally:
            conn.close()
        scored = []
        for row in rows:
            item = dict(row)
            haystack = _tokens(
                (item["lesson"] or "") + " " + (item["trajectory_summary"] or "")
            )
            score = len(wanted & haystack)
            if score <= 0:
                continue
            try:
                item["steps"] = json.loads(item["steps"] or "[]")
            except (ValueError, TypeError):
                item["steps"] = []
            try:
                item["evidence_ids"] = json.loads(item["evidence_ids"] or "[]")
            except (ValueError, TypeError):
                item["evidence_ids"] = []
            scored.append((score, item["promoted_at"] or "", item))
        scored.sort(key=lambda entry: entry[1], reverse=True)
        scored.sort(key=lambda entry: entry[0], reverse=True)
        from .tombstones import filter_rows, main_db_path_for_store
        return filter_rows([item for _, _, item in scored[: int(limit)]], main_db_path_for_store(self.db_path), lambda r: " ".join(str(r.get(k) or "") for k in ("lesson", "trajectory_summary", "outcome", "steps")))

    def query_cases_tiered(
        self,
        situation,
        limit=3,
        *,
        include_staging=True,
        min_staging_score=3,
        max_staging=2,
    ):
        """Tiered trajectory retrieval: permanent `cases` first, followed by high-overlap `staging_cases`.

        - Never modifies `PROMOTION_MIN_DATES = 3`.
        - Permanent cases carry `tier='confirmed'`, `confidence=0.85`, `tier_label='[正式案例·已验证]'`.
        - Unpromoted staging cases carry `tier='staging'`, `confidence<=0.55`,
          `tier_label='[暂存经验·仅供参考·勿直接触发工具]'`, and require valid `evidence_ids`
          plus token overlap >= `min_staging_score`.
        """
        limit = max(1, int(limit))
        perm_hits = self.query_cases(situation, limit=limit)
        results = []
        seen_keys = set()
        for item in perm_hits:
            enriched = dict(item)
            enriched["tier"] = "confirmed"
            enriched["confidence"] = 0.85
            enriched["tier_label"] = "[正式案例·已验证]"
            results.append(enriched)
            if enriched.get("lesson_key"):
                seen_keys.add(enriched["lesson_key"])

        if not include_staging or len(results) >= limit:
            return results[:limit]

        wanted = _tokens(situation)
        if not wanted:
            return results[:limit]

        conn = self._connect()
        try:
            srows = conn.execute(
                "SELECT id, lesson_key, trajectory_summary, outcome, lesson,"
                " steps, evidence_ids, created_at"
                " FROM staging_cases WHERE promoted = 0"
                " ORDER BY id DESC LIMIT 250"
            ).fetchall()
        finally:
            conn.close()

        # Group by lesson_key to count distinct dates and keep latest row
        grouped = {}
        for row in srows:
            lkey = row["lesson_key"] or ""
            if not lkey or lkey in seen_keys:
                continue
            try:
                ev_ids = json.loads(row["evidence_ids"] or "[]")
            except (ValueError, TypeError):
                ev_ids = []
            if not ev_ids:
                continue
            entry = grouped.get(lkey)
            d_str = _date_of(row["created_at"])
            if entry is None:
                grouped[lkey] = {
                    "row": dict(row),
                    "dates": {d_str} if d_str else set(),
                    "evidence_ids": list(ev_ids),
                }
            else:
                if d_str:
                    entry["dates"].add(d_str)
                for eid in ev_ids:
                    if eid not in entry["evidence_ids"]:
                        entry["evidence_ids"].append(eid)

        staging_scored = []
        for lkey, g in grouped.items():
            item = g["row"]
            haystack = _tokens(
                (item.get("lesson") or "") + " " + (item.get("trajectory_summary") or "")
            )
            overlap = wanted & haystack
            score = len(overlap)
            # Require at least one multi-char token/bigram or >= min_staging_score total overlap
            has_multichar = any(len(tok) >= 2 for tok in overlap)
            if score < int(min_staging_score) or not has_multichar:
                continue
            try:
                item["steps"] = json.loads(item.get("steps") or "[]")
            except (ValueError, TypeError):
                item["steps"] = []
            item["evidence_ids"] = g["evidence_ids"]
            dated_cnt = max(1, len(g["dates"]))
            item["evidence_count"] = dated_cnt
            item["tier"] = "staging"
            item["confidence"] = round(min(0.55, 0.30 + 0.10 * dated_cnt), 2)
            item["tier_label"] = "[暂存经验·仅供参考·勿直接触发工具]"
            staging_scored.append((score, dated_cnt, item.get("created_at") or "", item))

        staging_scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        from .tombstones import filter_rows, main_db_path_for_store

        filtered_staging = filter_rows(
            [it for _, _, _, it in staging_scored],
            main_db_path_for_store(self.db_path),
            lambda r: " ".join(str(r.get(k) or "") for k in ("lesson", "trajectory_summary", "outcome", "steps")),
        )
        slots = min(int(max_staging), limit - len(results))
        results.extend(filtered_staging[: max(0, slots)])
        return results[:limit]
