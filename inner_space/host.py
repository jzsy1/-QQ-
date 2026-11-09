"""科科斯体内空间宿主进程。

唯一 WebView 窗口：默认离屏不占桌面；访客（如 QQ 音乐）在此空间内运行。
启动：python -m kekesi.inner_space.host
"""

from __future__ import annotations

import sys


def main() -> int:
    # 宿主实现与 QQ 音乐访客 API 同进程（共用一个 WebView）
    from kekesi.tools.qqmusic_service import main as _run_host

    return int(_run_host())


if __name__ == "__main__":
    raise SystemExit(main())
