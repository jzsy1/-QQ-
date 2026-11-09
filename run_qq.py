# -*- coding: utf-8 -*-
"""QQ 大脑独立运行启动脚本（支持一键同时拉起 NapCat 扫码客户端与大脑）。

用法:
    1. 单独启动大脑服务（等待外部 NapCat 连接）:
       python run_qq.py

    2. 一键联合启动（直接在当前控制台拉起 NapCat 扫码，扫码登录后自动通信）:
       python run_qq.py --with-napcat
       或者带账号快速登录:
       python run_qq.py --with-napcat -q <QQ号>
"""

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

# 配置路径
PROJECT_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 配置控制台日志格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("QQ-Brain-Runner")

from im.qq.service import get_service, qq_status, start_qq, stop_qq
from im.qq.config import load_config


def main():
    if any(arg in sys.argv for arg in ("--help", "-h")):
        print("QQ 大脑独立运行服务启动工具")
        print("用法:")
        print("  python run_qq.py                      只启动 QQ 大脑核心与 Web 后台")
        print("  python run_qq.py --with-napcat        一键启动 QQ 大脑 + NapCat 扫码客户端")
        print("  python run_qq.py --with-napcat -q QQ  一键启动并指定快速登录 QQ 账号")
        return

    print("=" * 60)
    print("QQ 大脑独立运行服务启动中...")
    print(f"操作系统: {sys.platform}, Python: {sys.version.split()[0]}")
    print("=" * 60)

    cfg = load_config()
    print(f"配置状态:")
    print(f"  - 启用状态: {cfg.enabled}")
    print(f"  - NapCat 地址: {cfg.ws_url or '未配置'}")
    print(f"  - 人设/大脑 ID: {cfg.agent_id}")
    print(f"  - 记忆命名空间: {cfg.memory_namespace}")
    print(f"  - 超管列表: {list(cfg.super_users)}")
    print(f"  - 主人 QQ: {list(cfg.owner_qqs)}")
    print(f"  - 表情包支持: {cfg.sticker_enabled}")
    print(f"  - 视觉理解支持: {cfg.vision_enabled}")
    print("-" * 60)

    if not cfg.ws_url:
        print("[提示] config.yaml 中 qq.ws_url 尚未配置有效地址。")
        print("请在 config.yaml 的 qq 段配置正确的 NapCat OneBot WebSocket 地址，例如:")
        print("  qq:")
        print("    ws_url: ws://127.0.0.1:3001")
        print("-" * 60)

    napcat_proc: subprocess.Popen | None = None
    with_napcat = any(arg in sys.argv for arg in ("--with-napcat", "--napcat", "-n"))
    extra_args = [a for a in sys.argv[1:] if a not in ("--with-napcat", "--napcat", "-n")]

    # 注册 Ctrl+C / SIGTERM 信号处理
    def handle_sigint(signum, frame):
        nonlocal napcat_proc
        print("\n[退出] 收到退出信号，正在停止 QQ 接入服务与相关进程...")
        if napcat_proc is not None and napcat_proc.poll() is None:
            try:
                napcat_proc.terminate()
                napcat_proc.wait(timeout=3)
            except Exception:
                try:
                    napcat_proc.kill()
                except Exception:
                    pass
            napcat_proc = None
        stop_qq()
        print("[退出] QQ 接入服务已平稳停止。")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_sigint)

    # 启动 QQ 大脑核心接入服务
    ok, msg = start_qq()
    print(f"启动结果: {'成功' if ok else '失败'}")
    print(f"消息反馈: {msg}")
    print("=" * 60)

    # 启动 Web 管理调控后台
    try:
        from admin_panel.server import start_admin_server_background
        web_port = 18888
        start_admin_server_background(port=web_port)
        print(f"[Web后台] 控制面板已就绪: http://127.0.0.1:{web_port}")
        print(f"         可在线调控模型参数、人设系统词、查看即时状态与绑定身份")
        print("=" * 60)
    except Exception as exc:
        logger.warning("Web 后台启动异常（不影响 QQ 运行）: %s", exc)

    if not ok:
        print("大脑启动未成功，请检查上方日志或 config.yaml 配置后重试。")
        return

    # 若指定了一并拉起 NapCat 协议端
    if with_napcat:
        # Windows 路径检测
        napcat_win_dir = PROJECT_ROOT / "NapCat.Shell.Windows.Node"
        node_exe = napcat_win_dir / "node.exe"
        index_js = napcat_win_dir / "index.js"

        # Linux 路径检测
        linux_qq = Path("/root/Napcat/opt/QQ/qq")

        cmd: list[str] = []
        cwd: str | None = None

        if sys.platform == "win32" and node_exe.exists() and index_js.exists():
            cmd = [str(node_exe), str(index_js), *extra_args]
            cwd = str(napcat_win_dir)
        elif linux_qq.exists():
            cmd = ["xvfb-run", "-a", str(linux_qq), "--no-sandbox", *extra_args]

        if cmd:
            print("=" * 60)
            print("正在一键拉起 NapCat OneBot 协议端 (双向联动启动)...")
            print("首次运行或未登录时，终端将显示二维码，请使用手机 QQ 扫码授权；")
            print("扫码授权完成后，NapCat 将直接与 QQ 大脑无缝通信！")
            print("=" * 60)
            try:
                napcat_proc = subprocess.Popen(cmd, cwd=cwd)
            except Exception as exc:
                logger.error("拉起 NapCat 失败: %s", exc)
        else:
            print("[提示] 未在默认位置检测到 NapCat，请手动运行协议端。")

    print("服务运行中 (按 Ctrl+C 退出)...")
    print("你可以通过 QQ 发送消息测试对话、点歌与查天气功能。")
    print("=" * 60)

    last_status = time.time()
    try:
        while True:
            # 如果拉起了 NapCat 并且 NapCat 意外退出了，则同步提醒
            if napcat_proc is not None and napcat_proc.poll() is not None:
                logger.warning("NapCat 协议端进程已结束 (code: %s)", napcat_proc.returncode)
                break
            time.sleep(1.0)
            # 每隔 60 秒打印一次心跳状态
            if time.time() - last_status >= 60.0:
                last_status = time.time()
                _, stat = qq_status()
                logger.info("心跳状态: %s", stat)
    except KeyboardInterrupt:
        pass
    finally:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()
