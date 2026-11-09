"""科科斯体内空间 — 在进程内承载 WebView 等访客，默认不占桌面。

访客示例：QQ 音乐网页播放器（guest=qqmusic）。
宿主进程入口：python -m kekesi.inner_space.host
客户端：kekesi.inner_space.client
"""

from .client import (
    ensure_space,
    eval_js,
    hide_space,
    load_url,
    show_space,
    space_status,
)

__all__ = [
    "ensure_space",
    "load_url",
    "eval_js",
    "show_space",
    "hide_space",
    "space_status",
]
