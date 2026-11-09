# -*- coding: utf-8 -*-
"""QQ 大脑 Web 管理后台控制面板启动入口。

用法:
    python admin_server.py [端口，默认 18888]
"""

from __future__ import annotations

import sys
from admin_panel.server import DEFAULT_PORT, run_admin_server

if __name__ == "__main__":
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_admin_server(port)
