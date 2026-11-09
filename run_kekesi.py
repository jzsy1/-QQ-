#!/usr/bin/env python3
"""
Kekesi v2.0 启动包装器
解决相对导入问题
"""

import sys
from pathlib import Path
import os

# 设置 PYTHONPATH 包含当前目录
current_dir = Path(__file__).resolve().parent
os.environ["PYTHONPATH"] = str(current_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")

# 运行主程序
import io
with io.open(current_dir / "main.py", "r", encoding="utf-8") as f:
    code = f.read()
exec(compile(code, current_dir / "main.py", "exec"))