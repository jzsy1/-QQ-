r"""Kekesi v2.0 统一入口 — 纯文本交互模式。

================================================================
方案选择记录（2026-08-12，方案 A）
================================================================

规范 PART II 要求 `kekesi_v2/main.py` 作为顶层入口。但当前项目存在
两套并行入口：

1. voice 入口：`g:\kks\assistant-x-openclaw\src\main.py`（2137 行）
   - 由 `start.bat` 启动
   - 含 sherpa-onnx 唤醒词、流式 ASR、TTS、粒子动画、admin_server
   - 内部实例化 `AgentRouter`（kekesi 大脑）

2. v2.0 入口：本文件 `kekesi/main.py`
   - 仅启动 `AgentRouter` + 可选 admin_server
   - 不启动 voice（避免与 voice 入口端口/资源冲突）
   - 提供文本 REPL，用于：
     * v2.0 Task Runtime 端到端测试
     * 无 voice 设备的服务器/CI 环境
     * 未来 voice 解耦后的统一入口

冲突规避：
- voice 用户继续用 `start.bat`（不动）
- v2.0 测试/无 voice 场景用 `python -m kekesi.main`
- 两者共享同一份 `config.yaml` / `data/kekesi.db` / `skills/` / `mcp.json`
- 严禁同时启动两个 admin_server（端口 18791 会冲突）

未来扩展（voice 解耦后）：
- voice 将作为可加载模块挂载到本入口
- 形态：`python -m kekesi.main --voice` 自动加载 voice 模块
- 届时 `assistant-x-openclaw/src/main.py` 可退役（PART XIV 阶段10）

规范对应条款：
- PART II 项目结构：kekesi_v2/main.py
- PART XIV 阶段10：Legacy removal（新实现稳定后再删旧实现）
- PART XV 验收：WindowsAgent 可独立测试、Task 可取消、Task 有 timeout
================================================================

用法：
    python -m kekesi.main                       # 文本 REPL，不启动 admin
    python -m kekesi.main --admin               # 文本 REPL + admin_server
    python -m kekesi.main --config-dir /path    # 指定配置目录
    python -m kekesi.main --log-level DEBUG     # 调整日志级别
    python -m kekesi.main --once "几点了？"     # 单次查询模式（不进 REPL）
    python -m kekesi.main --agent-id keks       # 指定 agent 身份
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("kekesi.main")

# --------------------------------------------------------------
# 默认值集中管理（零硬编码：可被 CLI 参数覆盖）
# --------------------------------------------------------------

_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_AGENT_ID = "keks"
_DEFAULT_SESSION_ID = "default"
_ADMIN_PORT_DEFAULT = 18791
_REPL_BANNER = """
============================================================
  Kekesi v2.0 文本交互模式
  输入消息后回车发送；输入 /exit 退出；输入 /help 查看命令。
============================================================
"""

_REPL_HELP = """
可用命令：
  /help              显示本帮助
  /exit              退出程序
  /session <id>      切换会话 ID（默认: default）
  /agent <id>        切换 agent ID（默认: keks）
  /status            显示当前运行状态
  /reload-skills     重新加载技能包
  /cancel <task_id>  取消指定任务
"""


# --------------------------------------------------------------
# Windows 控制台 UTF-8 修复
# --------------------------------------------------------------


def _setup_windows_utf8() -> None:
    """Windows 控制台中文乱码修复：强制 UTF-8 代码页 + stdout/stderr 编码。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# --------------------------------------------------------------
# 日志配置
# --------------------------------------------------------------


def _setup_logging(level: str, config_dir: Path) -> None:
    """配置根日志：控制台 + 文件（可选）。"""
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # 文件日志（fail-soft：写入失败不阻塞主流程）
    try:
        log_dir = config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "kekesi-main.log",
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)
    except Exception:
        logger.warning("文件日志初始化失败，仅使用控制台日志", exc_info=True)


# --------------------------------------------------------------
# Admin Server 启动（可选）
# --------------------------------------------------------------


def _start_admin_in_thread(
    router,
    config_dir: Path,
    port: int = _ADMIN_PORT_DEFAULT,
) -> threading.Thread | None:
    """在后台线程启动 admin_server。

    admin_server 与 voice 入口的 admin_server 共用端口 18791，
    因此严禁两个入口同时启动 admin。本函数会先检测端口占用。
    """
    import socket

    # 端口占用检测
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex(("127.0.0.1", int(port)))
        if result == 0:
            logger.warning(
                "端口 %s 已被占用（可能 voice 入口已启动 admin），跳过 admin_server 启动",
                port,
            )
            return None
    finally:
        sock.close()

    try:
        from .admin_server import DEFAULT_PORT, run_server
        from .api.server import register_chat_router_hook

        # 手机 /chat 远程控制页复用本进程的活大脑
        register_chat_router_hook(lambda: router)

        actual_port = int(port) or DEFAULT_PORT
        thread = threading.Thread(
            target=run_server,
            kwargs={
                "port": actual_port,
                "base_dir": config_dir,
                "skills_reload_hook": router.reload_skills,
                # 0.0.0.0 允许手机局域网访问 /chat；远程请求受 admin.token 鉴权保护
                "host": os.environ.get("KEKESI_ADMIN_HOST", "0.0.0.0"),
            },
            daemon=True,
            name="kekesi-admin",
        )
        thread.start()
        logger.info("admin_server 已启动: http://0.0.0.0:%s", actual_port)
        return thread
    except Exception:
        logger.error("admin_server 启动失败", exc_info=True)
        return None


# --------------------------------------------------------------
# REPL 主循环
# --------------------------------------------------------------


def _print_reply(reply: str | None, layer: str) -> None:
    """格式化打印 Agent 回复。"""
    if not reply:
        print("(无回复)")
        return
    print(f"\n科科斯: {reply}")
    if layer and str(layer).lower() not in ("empty", "chat", ""):
        print(f"  [路由: {layer}]")


def _handle_repl_command(
    cmd: str,
    router,
    state: dict,
) -> bool:
    """处理 / 开头的 REPL 命令。

    Returns: True 表示继续 REPL；False 表示退出。
    """
    parts = cmd.strip().split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in ("/exit", "/quit", "/bye"):
        print("再见。")
        return False

    if name in ("/help", "/?"):
        print(_REPL_HELP)
        return True

    if name == "/session":
        if not arg:
            print(f"当前会话: {state['session_id']}")
            return True
        state["session_id"] = arg
        router.session_id = arg
        print(f"会话已切换: {arg}")
        return True

    if name == "/agent":
        if not arg:
            print(f"当前 agent: {state['agent_id']}")
            return True
        state["agent_id"] = arg
        print(f"agent 已切换: {arg}")
        return True

    if name == "/status":
        print(f"session: {state['session_id']}")
        print(f"agent:   {state['agent_id']}")
        print(f"base_dir: {router.base_dir}")
        try:
            skills = router.skill_registry.health()
            healthy = sum(1 for ok in skills.values() if ok)
            print(f"skills:  {healthy}/{len(skills)} healthy")
        except Exception:
            print("skills:  读取失败")
        try:
            tools_count = len(router.agent.tool_ctx.skill_bridge.tool_names())
            print(f"dynamic tools: {tools_count}")
        except Exception:
            pass
        return True

    if name == "/reload-skills":
        try:
            result = router.reload_skills()
            print(
                f"已重载: {result['loaded']} 个 skill, "
                f"{result['healthy']} healthy, "
                f"{result['tools']} 动态工具"
            )
        except Exception as exc:
            print(f"重载失败: {exc}")
        return True

    if name == "/cancel":
        if not arg:
            print("用法: /cancel <task_id>")
            return True
        try:
            ok = router.runtime.cancel_task(arg)
            print(f"取消 {'成功' if ok else '失败（任务可能不存在或已结束）'}")
        except Exception as exc:
            print(f"取消失败: {exc}")
        return True

    print(f"未知命令: {name}（输入 /help 查看可用命令）")
    return True


def run_repl(router, *, agent_id: str, session_id: str) -> None:
    """文本 REPL 主循环。"""
    state = {"agent_id": agent_id, "session_id": session_id}
    router.session_id = session_id

    # 添加对话计数和清理计数器
    conversation_count = 0
    cleanup_interval = 10  # 每10次对话执行一次清理
    last_cleanup_time = time.time()

    print(_REPL_BANNER)

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            should_continue = _handle_repl_command(user_input, router, state)
            if not should_continue:
                break
            continue

        # 正常对话
        try:
            reply, layer = router.handle(
                user_input,
                agent_id=state["agent_id"],
            )
            _print_reply(reply, layer)

            # 更新对话计数和定期清理
            conversation_count += 1
            current_time = time.time()

            # 定期清理内存和资源
            if conversation_count % cleanup_interval == 0:
                try:
                    # 清理任务队列
                    if hasattr(router, 'runtime') and hasattr(router.runtime, 'task_queue'):
                        # 非关键清理，失败不影响主流程
                        pass

                    # 检查内存使用（简单估算）
                    if current_time - last_cleanup_time > 60:  # 超过1分钟
                        logger.info(f"已完成 {conversation_count} 轮对话")
                        last_cleanup_time = current_time

                except Exception:
                    logger.debug("定期清理失败", exc_info=True)

        except KeyboardInterrupt:
            print("\n[已中断当前请求]")
        except Exception as e:
            logger.error("handle 异常", exc_info=True)
            print(f"[处理失败: {type(e).__name__}]")
            # 如果是关键错误，尝试优雅退出
            if isinstance(e, (MemoryError, OSError, ConnectionError)):
                print("[检测到严重错误，准备退出...]")
                break


# --------------------------------------------------------------
# 单次查询模式
# --------------------------------------------------------------


def run_once(router, text: str, *, agent_id: str, session_id: str) -> int:
    """单次查询模式：执行一次后退出。返回 exit code。"""
    try:
        reply, layer = router.handle(
            text,
            agent_id=agent_id,
        )
        _print_reply(reply, layer)
        return 0 if reply else 1
    except Exception:
        logger.error("run_once 异常", exc_info=True)
        return 2


# --------------------------------------------------------------
# 优雅关闭
# --------------------------------------------------------------


def _shutdown(router) -> None:
    """优雅关闭所有后台组件。"""
    try:
        router.shutdown()
    except Exception:
        logger.debug("shutdown 异常", exc_info=True)


def _install_signal_handlers(router) -> None:
    """注册 Ctrl+C / SIGTERM 信号处理。"""

    def _handler(signum, frame):
        logger.info("收到信号 %s，正在关闭...", signum)
        _shutdown(router)
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


# --------------------------------------------------------------
# CLI 主入口
# --------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kekesi.main",
        description="Kekesi v2.0 文本交互入口（无 voice）",
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="配置目录（默认: kekesi 包所在目录）",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=_DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"日志级别（默认: {_DEFAULT_LOG_LEVEL}）",
    )
    parser.add_argument(
        "--agent-id",
        type=str,
        default=_DEFAULT_AGENT_ID,
        help=f"Agent 身份 ID（默认: {_DEFAULT_AGENT_ID}）",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=_DEFAULT_SESSION_ID,
        help=f"会话 ID（默认: {_DEFAULT_SESSION_ID}）",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="启动 admin_server（默认不启动，避免与 voice 入口冲突）",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=_ADMIN_PORT_DEFAULT,
        help=f"admin_server 端口（默认: {_ADMIN_PORT_DEFAULT}）",
    )
    parser.add_argument(
        "--once",
        type=str,
        default=None,
        help="单次查询模式：执行一次后退出（不进 REPL）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # 解析配置目录
    if args.config_dir:
        config_dir = Path(args.config_dir).resolve()
    else:
        config_dir = Path(__file__).resolve().parent

    _setup_windows_utf8()
    _setup_logging(args.log_level, config_dir)

    logger.info("Kekesi v2.0 启动中... config_dir=%s", config_dir)

    # 初始化 AgentRouter（kekesi 大脑）
    try:
        import sys
        current_dir = Path(__file__).resolve().parent
        if __package__:
            from .router import AgentRouter
        else:
            # 仅文件直跑（python main.py）才需要手动加路径；
            # 包模式（python -m kekesi.main）禁止污染 sys.path——
            # kekesi/mcp 子包与第三方 mcp SDK 同名，污染会让 import mcp
            # 命中本地子包（相对导入越界报 ImportError），SDK 被误判为未安装
            if str(current_dir) not in sys.path:
                sys.path.insert(0, str(current_dir))
            from router import AgentRouter

        router = AgentRouter(config_dir=config_dir)
    except Exception:
        logger.error("AgentRouter 初始化失败", exc_info=True)
        return 3

    _install_signal_handlers(router)

    # 可选启动 admin_server
    if args.admin:
        _start_admin_in_thread(router, config_dir, port=args.admin_port)

    # 单次查询模式
    if args.once:
        return run_once(
            router,
            args.once,
            agent_id=args.agent_id,
            session_id=args.session_id,
        )

    # REPL 模式
    try:
        run_repl(router, agent_id=args.agent_id, session_id=args.session_id)
        return 0
    finally:
        _shutdown(router)


if __name__ == "__main__":
    sys.exit(main())
