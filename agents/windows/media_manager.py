"""MediaManager — 音乐与视频播放。"""

from __future__ import annotations

from ...tools.execution_result import ExecutionResult, from_tuple


class MediaManager:
    """Web 音乐/视频播放器封装。"""

    def play_music(
        self,
        query: str = "",
        *,
        source: str | None = None,
        show: bool = False,
        playlist: str | None = None,
    ) -> ExecutionResult:
        from ...tools.web_music_player import play_music as play_music_impl

        q = (query or "").strip()
        pl = (playlist or "").strip() or None
        if not q and pl not in ("liked", "like", "favorite", "favourites", "favorites"):
            return ExecutionResult(
                success=False,
                message="请告诉我要听什么歌，或指定 playlist=liked 播收藏列表",
                verified=False,
                action="play_music",
            )
        ok, msg = play_music_impl(q, source=source, show=show, playlist=pl)
        tag = pl or q
        return from_tuple(ok, msg, verified=ok, action="play_music", target=tag[:40])

    def music_login(self, source: str | None = None) -> ExecutionResult:
        from ...tools.web_music_player import music_login

        ok, msg = music_login(source)
        return from_tuple(ok, msg, verified=ok, action="qqmusic_login")

    def music_status(self) -> ExecutionResult:
        from ...tools.web_music_player import music_status

        ok, msg = music_status()
        return from_tuple(ok, msg, verified=ok, action="qqmusic_status")

    def control_music(self, action: str, *, system_fallback=None) -> ExecutionResult:
        from ...tools.web_music_player import control_web_music

        raw = (action or "playpause").strip()
        ok, msg = control_web_music(raw)
        if not ok and system_fallback is not None:
            fallback = system_fallback(raw)
            if fallback.success:
                return fallback
            if "未在运行" not in msg:
                msg = f"{msg}；系统媒体键：{fallback.message}"
        return from_tuple(ok, msg, verified=ok, action="control_music", target=raw)

    def play_video(
        self,
        query: str,
        *,
        source: str | None = None,
        episode: int | None = None,
        latest: bool = False,
    ) -> ExecutionResult:
        from ...tools.web_video_player import play_video as play_video_impl

        q = (query or "").strip()
        if not q:
            return ExecutionResult(
                success=False,
                message="请告诉我要看什么剧",
                verified=False,
                action="play_video",
            )
        ok, msg = play_video_impl(q, episode=episode, source=source, latest=latest)
        return from_tuple(ok, msg, verified=ok, action="play_video", target=q[:40])

    def video_login(self, source: str | None = None) -> ExecutionResult:
        from ...tools.web_video_player import video_login

        ok, msg = video_login(source)
        return from_tuple(ok, msg, verified=ok, action="video_login")

    def video_status(self) -> ExecutionResult:
        from ...tools.web_video_player import video_status

        ok, msg = video_status()
        return from_tuple(ok, msg, verified=ok, action="video_status")

    def close_video(self) -> ExecutionResult:
        from ...tools.web_video_player import close_video

        ok, msg = close_video()
        return from_tuple(ok, msg, verified=ok, action="close_video")
