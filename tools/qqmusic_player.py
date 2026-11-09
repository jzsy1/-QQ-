"""QQ / 网易云 / 酷狗网页访客 — 在科科斯体内空间中运行。

兼容旧接口；实际实现见 web_music_player。
"""

from __future__ import annotations

from kekesi.inner_space.client import (
    _health,
    _read_state,
    _request,
    _state_path,
    close_space,
)
from kekesi.tools.web_music_player import (
    control_web_music,
    ensure_service,
    music_login,
    music_show,
    music_status,
    play_music as _play_music,
)


def ensure_service_compat(*, start: bool = True) -> tuple[bool, str, int | None]:
    return ensure_service(start=start)


def qqmusic_login() -> tuple[bool, str]:
    return music_login("qq")


def play_music(
    query: str,
    *,
    show: bool = False,
    source: str | None = None,
    playlist: str | None = None,
) -> tuple[bool, str]:
    return _play_music(query, source=source, show=show, playlist=playlist)


def control_qqmusic(action: str) -> tuple[bool, str]:
    return control_web_music(action)


def qqmusic_status() -> tuple[bool, str]:
    return music_status()


def qqmusic_show(*, minimize: bool = False) -> tuple[bool, str]:
    return music_show(minimize=minimize)


def close_qqmusic() -> tuple[bool, str]:
    return close_space()


# 兼容：ensure_service 名称
ensure_service = ensure_service_compat  # type: ignore[assignment]

__all__ = [
    "ensure_service",
    "qqmusic_login",
    "play_music",
    "control_qqmusic",
    "qqmusic_status",
    "qqmusic_show",
    "close_qqmusic",
    "_read_state",
    "_request",
    "_health",
    "_state_path",
]
