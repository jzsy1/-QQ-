"""屏幕读取 — 截图落盘存档，messages 只注入当前需要的一张。"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_SCREENSHOT_MARKER = "[screenshot_id="
_SCREENSHOT_ID_RE = re.compile(r"\[screenshot_id=([^\]]+)\]")
_KEEP_DAYS = 30
_THUMB_MAX = 1568
_JPEG_QUALITY = 82
# 注入图体积上限：超限阶梯降质/降分辨率重存。4K 桌面画面细节多，
# q82 也可能 >100KB，vision 请求会把 base64 整体塞给 LLM——
# 模型导航失败 fallback 读屏时连续大图注入曾把本地 LLM 打到 400
_INJECT_MAX_BYTES = 80 * 1024


def screenshot_dir() -> Path:
    """kekesi/data/screenshots/"""
    d = Path(__file__).resolve().parent.parent / "data" / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_shot_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"


def _path_for_id(shot_id: str) -> Path:
    return screenshot_dir() / f"{shot_id}.jpg"


@dataclass
class ScreenReadResult:
    ok: bool
    text: str
    inject_messages: list[dict] = field(default_factory=list)
    screenshot_id: str = ""


def _b64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _save_compressed(img, path: Path) -> int:
    """落盘截图并保证体积 ≤ _INJECT_MAX_BYTES：先降质量，再降分辨率。

    存档与注入共用一份文件；质量下限 50、宽度下限 1024，读屏分析仍可辨认。
    返回最终字节数。
    """
    im = img.convert("RGB")
    im.save(path, "JPEG", quality=_JPEG_QUALITY)
    quality = _JPEG_QUALITY
    max_side = _THUMB_MAX
    while path.stat().st_size > _INJECT_MAX_BYTES and (quality > 50 or max_side > 1024):
        if quality > 50:
            quality -= 8
        else:
            max_side = int(max_side * 0.8)
            im.thumbnail((max_side, max_side))
        im.save(path, "JPEG", quality=quality)
    size = path.stat().st_size
    if size > _INJECT_MAX_BYTES:
        logger.warning("截图压缩后仍 %d KB（超 %d KB 上限）", size // 1024, _INJECT_MAX_BYTES // 1024)
    return size


def _inject_for(shot_id: str, path: Path, *, caption: str) -> list[dict]:
    data = _b64_file(path)
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"{_SCREENSHOT_MARKER}{shot_id}] {caption}"
                        f"（已落盘 {path.name}；历史截图用 recall_screenshot 按 id 取回，"
                        f"messages 里只保留当前这一张。）"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{data}"},
                },
            ],
        }
    ]


def read_screen() -> ScreenReadResult:
    """截取主屏幕：落盘归档，并注入当前截图供视觉模型分析。"""
    try:
        from PIL import ImageGrab
    except ImportError:
        return ScreenReadResult(False, "缺少 Pillow，无法读屏（pip install Pillow）")

    shot_id = _new_shot_id()
    path = _path_for_id(shot_id)
    try:
        img = ImageGrab.grab(all_screens=True)
        img.thumbnail((_THUMB_MAX, _THUMB_MAX))
        size = _save_compressed(img, path)
        logger.info("read_screen 落盘 %s（%d KB）", path.name, size // 1024)
    except Exception as e:
        return ScreenReadResult(False, f"读屏失败: {e}")

    inject = _inject_for(
        shot_id,
        path,
        caption="这是用户当前的屏幕截图，请根据截图回答用户刚才的问题。",
    )
    return ScreenReadResult(
        ok=True,
        text=(
            f"已读取当前屏幕并保存为 {shot_id}。"
            "请结合紧随其后的截图回答；若之后需要对比更早的画面，"
            f"调用 recall_screenshot(screenshot_id=\"{shot_id}\")。"
        ),
        inject_messages=inject,
        screenshot_id=shot_id,
    )


def recall_screenshot(screenshot_id: str) -> ScreenReadResult:
    """从落盘目录按 id/文件名取回历史截图，注入 messages（由 loop 踢掉旧图）。"""
    raw = (screenshot_id or "").strip()
    if not raw:
        return ScreenReadResult(False, "请提供 screenshot_id（read_screen 返回的 id）")
    shot_id = raw.replace(".jpg", "").replace(".jpeg", "")
    path = _path_for_id(shot_id)
    if not path.is_file():
        # 允许只给时间戳前缀模糊匹配最近一张
        hits = sorted(screenshot_dir().glob(f"{shot_id}*.jpg"), reverse=True)
        if not hits:
            recent = list_recent_screenshots(5)
            hint = f"最近有：{', '.join(recent)}" if recent else "目录为空"
            return ScreenReadResult(False, f"未找到截图「{shot_id}」。{hint}")
        path = hits[0]
        shot_id = path.stem

    inject = _inject_for(
        shot_id,
        path,
        caption="这是按 id 召回的历史屏幕截图，请结合该图回答。",
    )
    return ScreenReadResult(
        ok=True,
        text=f"已从存档召回截图 {shot_id}，请结合紧随其后的截图分析。",
        inject_messages=inject,
        screenshot_id=shot_id,
    )


def list_recent_screenshots(limit: int = 10) -> list[str]:
    limit = max(1, min(int(limit), 50))
    files = sorted(screenshot_dir().glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files[:limit]]


def cleanup_old_screenshots(keep_days: int = _KEEP_DAYS) -> dict[str, int]:
    """删除超过 keep_days 的截图。启动时调用即可（约等于每月清一轮）。"""
    days = max(1, int(keep_days))
    cutoff = time.time() - days * 86400
    deleted = 0
    kept = 0
    for p in screenshot_dir().glob("*.jpg"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                deleted += 1
            else:
                kept += 1
        except OSError:
            logger.debug("删除截图失败: %s", p, exc_info=True)
    return {"deleted": deleted, "kept": kept, "keep_days": days}


def message_has_screenshot(msg: dict) -> bool:
    """判断 messages 条目是否含截图（用于 loop 去堆叠）。"""
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return _SCREENSHOT_MARKER in content
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                return True
            text = str(part.get("text") or "")
            if _SCREENSHOT_MARKER in text:
                return True
    return False


def strip_screenshot_messages(messages: list[dict]) -> int:
    """就地移除 messages 中所有截图注入，返回移除条数。"""
    before = len(messages)
    messages[:] = [m for m in messages if not message_has_screenshot(m)]
    return before - len(messages)
