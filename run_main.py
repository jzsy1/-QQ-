#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
直接运行 main.py 的包装器，解决相对导入问题
"""

import sys
import os
from pathlib import Path

# 添加当前目录到路径
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir))

# 设置环境变量避免编码问题
os.environ['PYTHONIOENCODING'] = 'utf-8'

# 直接导入和运行主程序
import main
if __name__ == "__main__":
    sys.exit(main.main())