"""系统信息类工具 — 天气、截图、倒计时等"""

from __future__ import annotations

import datetime
import os
import threading
import urllib.parse
import urllib.request

from .winops import desktop_dir


def get_time_full() -> tuple[bool, str]:
    now = datetime.datetime.now()
    week = "一二三四五六日"[now.weekday()]
    text = now.strftime(f"现在是 %Y年%m月%d日 星期{week} %H点%M分")
    return True, text


def get_weather(city: str) -> tuple[bool, str]:
    city = (city or "").strip()
    if not city:
        return False, "请告诉我要查哪个城市"
    try:
        c = urllib.parse.quote(city)
        fmt = urllib.parse.quote("%l：%C，%t，体感%f，湿度%h")
        url = f"https://wttr.in/{c}?format={fmt}&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8").strip()
        return True, text
    except Exception as e:
        return False, f"查天气失败: {e}"


def take_screenshot() -> tuple[bool, str]:
    try:
        from PIL import ImageGrab

        name = datetime.datetime.now().strftime("截图-%Y%m%d-%H%M%S.png")
        path = os.path.join(desktop_dir(), name)
        ImageGrab.grab(all_screens=True).save(path)
        return True, f"截图已保存到桌面: {name}"
    except ImportError:
        return False, "缺少 Pillow 库，无法截图（pip install Pillow）"
    except Exception as e:
        return False, f"截图失败: {e}"


def set_timer(seconds: int, message: str = "时间到") -> tuple[bool, str]:
    seconds = max(1, int(seconds))
    message = (message or "时间到").strip()

    def _fire() -> None:
        # 先响叮咚引起注意
        try:
            import winsound

            for _ in range(3):
                winsound.MessageBeep(winsound.MB_OK)
                threading.Event().wait(0.4)
        except Exception:
            pass
        # 再语音播报提醒内容（走 notify_reminder → _play_reminder_tts）
        try:
            from ..notifications import notify_reminder

            notify_reminder(message)
        except Exception:
            pass
        print(f"\n[倒计时] {message}", flush=True)

    threading.Timer(seconds, _fire).start()
    mins = seconds // 60
    desc = f"{mins}分钟" if mins else f"{seconds}秒"
    return True, f"好的，{desc}后提醒你：{message}"
