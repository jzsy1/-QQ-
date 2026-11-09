# -*- coding: utf-8 -*-
"""QQ 天气查询服务。

基于 Open-Meteo 高精度气象与 wttr.in 双通道，免 Key、高可用、高速度。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# 天气匹配正则
_WEATHER_PATTERNS = [
    re.compile(r"^(?:查|查看|查询)?\s*天气\s*(.*)$", re.IGNORECASE),
    re.compile(r"^(?:查|查看|查询)?\s*(.+?)(?:的)?天气(?:预报|情况|怎么样|如何)?\s*[?？!！~]*$", re.IGNORECASE),
    re.compile(r"^(.+?)(?:今天|明天|后天)?(?:会)?(?:下雨|下雪|降温|降水)吗\s*[?？!！~]*$", re.IGNORECASE),
    re.compile(r"^(.+?)(?:今天|明天|后天)?(?:冷不冷|热不热|多少度|气温多少)\s*[?？!！~]*$", re.IGNORECASE),
]

# WMO 标准天气代码对照表
_WMO_CODES: dict[int, str] = {
    0: "晴朗 ☀️",
    1: "大致晴朗 🌤️",
    2: "多云 ⛅",
    3: "阴天 ☁️",
    45: "有雾 🌫️",
    48: "雾凇 🌫️",
    51: "微毛毛雨 🌦️",
    53: "毛毛雨 🌧️",
    55: "密毛毛雨 🌧️",
    61: "小雨 🌧️",
    63: "中雨 🌧️",
    65: "大雨 ⛈️",
    71: "小雪 🌨️",
    73: "中雪 🌨️",
    75: "大雪 ❄️",
    80: "阵雨 🌦️",
    81: "中度阵雨 🌧️",
    82: "强阵雨 ⛈️",
    95: "雷暴 🌩️",
    96: "雷暴伴有冰雹 ⛈️",
}


def is_weather_intent(text: str) -> tuple[bool, str]:
    """判断是否为天气查询意图。

    返回 (is_hit, city_query)。
    如果用户只发了“查天气”等未带城市的指令，返回 (True, "")。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False, ""

    for pat in _WEATHER_PATTERNS:
        m = pat.match(cleaned)
        if m:
            city = m.group(1).strip()
            # 清理常见无关前后缀
            city = re.sub(r"^(?:一下|下|查|查一下|帮我查|帮我查下|告诉我)\s*", "", city).strip()
            city = re.sub(r"^(?:今天|明天|后天|现在|目前)\s*", "", city).strip()
            city = re.sub(r"(?:今天|明天|后天|现在|目前)$", "", city).strip()
            city = re.sub(r"[!！?？。，,~～呀啊吧啦呢]+$", "", city).strip()
            return True, city

    return False, ""


def _query_open_meteo(city: str) -> dict[str, Any] | None:
    """通道 1：Open-Meteo 全球高精度气象。"""
    ctx = ssl._create_unverified_context()
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + urllib.parse.quote(city)
        + "&count=1&language=zh&format=json"
    )
    req = urllib.request.Request(geo_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            geo_data = json.loads(r.read().decode("utf-8", "ignore"))
        results = geo_data.get("results")
        if not results:
            return None
        item = results[0]
        lat, lon = item["latitude"], item["longitude"]
        name = item.get("name") or city

        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current_weather=true&daily=temperature_2m_max,temperature_2m_min&timezone=Asia/Shanghai"
        )
        req_w = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req_w, context=ctx, timeout=5) as r_w:
            w_data = json.loads(r_w.read().decode("utf-8", "ignore"))

        curr = w_data.get("current_weather", {})
        daily = w_data.get("daily", {})
        wcode = int(curr.get("weathercode", 0))
        return {
            "city": name,
            "temp": curr.get("temperature", "--"),
            "weather": _WMO_CODES.get(wcode, "晴朗 🌤️"),
            "wind": f"{curr.get('windspeed', '--')} km/h",
            "max_temp": daily.get("temperature_2m_max", ["--"])[0],
            "min_temp": daily.get("temperature_2m_min", ["--"])[0],
        }
    except Exception as exc:
        logger.warning("Open-Meteo 天气查询异常: %s", exc)
        return None


def _query_wttr(city: str) -> dict[str, Any] | None:
    """通道 2（备用）：wttr.in 气象查询。"""
    ctx = ssl._create_unverified_context()
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=zh"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        curr = data.get("current_condition", [{}])[0]
        today = data.get("weather", [{}])[0]
        desc_zh = (
            curr.get("lang_zh", [{}])[0].get("value")
            or curr.get("weatherDesc", [{}])[0].get("value")
            or "晴朗"
        )
        return {
            "city": city,
            "temp": curr.get("temp_C", "--"),
            "weather": f"{desc_zh} 🌤️",
            "wind": f"{curr.get('windspeedKmph', '--')} km/h",
            "max_temp": today.get("maxtempC", "--"),
            "min_temp": today.get("mintempC", "--"),
        }
    except Exception as exc:
        logger.warning("wttr.in 天气查询异常: %s", exc)
        return None


def get_weather_report(city: str) -> str:
    """获取指定城市的完整天气预报播报文本。"""
    if not city:
        return "你想查询哪个城市的天气呢？例如发送：北京天气 / 查上海天气 🌤️"

    data = _query_open_meteo(city) or _query_wttr(city)
    if not data:
        return f"抱歉呀，暂时没有找到城市【{city}】的气象信息，请检查城市名称是否准确哦~"

    city_name = data["city"]
    temp = data["temp"]
    weather = data["weather"]
    wind = data["wind"]
    max_t = data["max_temp"]
    min_t = data["min_temp"]

    # 贴心生活提醒
    if any(k in weather for k in ("雨", "雷", "暴")):
        tip = "出门记得带伞，路面湿滑出行注意安全哦~ ☔"
    elif any(k in weather for k in ("雪", "冰")):
        tip = "外面下雪/有冰冻啦，天寒地冻务必添衣保暖防滑哦~ ❄️"
    elif isinstance(temp, (int, float)) and temp < 10:
        tip = "气温偏低，早晚温差较大，出门多加一件厚外套防着凉哦~ 🧣"
    elif isinstance(temp, (int, float)) and temp > 30:
        tip = "气温较高炎热，出门注意做好防晒并多补充水分防中暑哦~ 🥤"
    else:
        tip = "气温舒适宜人，愿你今天也拥有好心情哦~ ✨"

    return (
        f"🌤️【{city_name}天气预报】\n"
        f"当前天气：{weather}\n"
        f"实时气温：{temp}°C（今日最高 {max_t}°C / 最低 {min_t}°C）\n"
        f"风力风速：{wind}\n"
        f"💡 贴心建议：{tip}"
    )
