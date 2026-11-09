"""科科斯管理后台 — 本地 Web 控制台，在线改配置无需改代码。

迁移自 kekesi/admin_server.py（v2.0 目录结构调整）。
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..brain import Brain
from ..config_store import ConfigStore
from ..llm_config import is_local_llm_url, load_brain_config
from ..memory import Database, MemoryService, resolve_memory_db_path
from ..personas import PersonaStore, get_active_role
from ..r2_persona import list_personas, get_persona_config, normalize_agent_id
from ..tools.registry import get_tool_names, register_builtin_tools
from ..tools_policy import _APP_TOOLS, _DEFER_BY_DEFAULT, _FILE_TOOLS, _SHELL_TOOLS

logger = logging.getLogger(__name__)

# 包根目录（kekesi/），用于定位 admin/ 静态资源和 config.yaml
_PKG_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORT = 18791
STATIC_DIR = _PKG_ROOT / "admin"
_ASSISTANT_SRC = _PKG_ROOT.parent / "assistant-x-openclaw" / "src"
_skills_reload_hook: Callable[[], dict[str, Any]] | None = None


def register_skills_reload_hook(fn: Callable[[], dict[str, Any]] | None) -> None:
    """注册运行中 AgentRouter.reload_skills，供 POST /api/skills/reload 热更新。"""
    global _skills_reload_hook
    _skills_reload_hook = fn


# === 语音助手配置生效方式（默认热重载，可在后台切换）===
_assistant_reload_hook: Callable[[], dict[str, Any]] | None = None
ASSISTANT_RELOAD_MODES = ("hot", "prompt", "cold")


def register_assistant_reload_hook(
    fn: Callable[[], dict[str, Any]] | None,
) -> None:
    """注册运行中 VoiceAssistant.reload_runtime（语音进程与管理后台同进程时）。"""
    global _assistant_reload_hook
    _assistant_reload_hook = fn


def _assistant_reload_mode(base_dir: Path) -> str:
    """读取 assistant_reload.mode（缺省 hot），非法值回退 hot。"""
    try:
        cfg = ConfigStore(base_dir).load_config(masked=False)
        section = cfg.get("assistant_reload") or {}
        mode = str(section.get("mode") or "hot").strip().lower()
    except Exception:
        logger.exception("读取 assistant_reload.mode 失败，回退 hot")
        return "hot"
    return mode if mode in ASSISTANT_RELOAD_MODES else "hot"


def _hot_reload_message(report: dict[str, Any]) -> str:
    keywords = str(report.get("keywords") or "")
    if keywords == "applied":
        return "已保存并热重载生效（助手、语音、唤醒词均已应用，无需重启）"
    if keywords == "pending":
        return "已保存并热重载；唤醒词将在本次音频重置后生效"
    if keywords == "failed":
        return "已保存并热重载助手/语音；唤醒词重建失败，沿用旧唤醒词"
    return "已保存并热重载生效，无需重启"


def _schedule_cold_restart() -> tuple[bool, str]:
    """后台延迟冷重启语音助手：start.bat 会先释放 18790/18791 再重新拉起。"""
    project_dir = _ASSISTANT_SRC.parent
    bat = project_dir / "scripts" / "start.bat"
    if not bat.exists():
        return False, f"未找到 {bat}"

    cmd = f'timeout /t 2 /nobreak >nul & start "" /D "{project_dir}" "{bat}"'
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(
            ["cmd", "/c", cmd], creationflags=flags, close_fds=True
        )
        return True, ""
    except Exception as e:
        logger.exception("冷重启语音助手失败")
        return False, str(e)


def _reload_voice_assistant(base_dir: Path) -> dict[str, Any]:
    """按配置的生效方式处理刚保存的助手/语音/唤醒词配置。"""
    mode = _assistant_reload_mode(base_dir)

    if mode == "prompt":
        return {
            "mode": mode,
            "applied": False,
            "message": "已保存，重启语音助手后生效",
        }

    if mode == "cold":
        ok, err = _schedule_cold_restart()
        if ok:
            return {
                "mode": mode,
                "applied": True,
                "message": "已保存，语音助手正在自动冷重启（约几秒）",
            }
        return {
            "mode": mode,
            "applied": False,
            "message": f"已保存，自动冷重启失败（{err}），请手动重启语音助手",
        }

    # hot：优先走同进程 hook，独立运行管理后台时回退调用 18790 /reload
    hook = _assistant_reload_hook
    if hook is not None:
        try:
            report = hook() or {}
        except Exception as e:
            logger.exception("语音助手热重载失败")
            return {
                "mode": mode,
                "applied": False,
                "message": f"已保存，但热重载失败：{e}，请重启语音助手",
            }
        return {
            "mode": mode,
            "applied": True,
            "report": report,
            "message": _hot_reload_message(report),
        }

    try:
        import urllib.request

        req = urllib.request.Request(
            "http://127.0.0.1:18790/reload",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            report = json.loads(resp.read().decode("utf-8") or "{}")
        return {
            "mode": mode,
            "applied": True,
            "report": report,
            "message": _hot_reload_message(report),
        }
    except Exception as e:
        return {
            "mode": mode,
            "applied": False,
            "message": f"已保存，热重载不可用（{e}），请重启语音助手",
        }


def _reload_live_brain() -> bool:
    """保存大脑配置后热重启运行中的 Brain（重建 provider），返回是否成功。"""
    router = _get_chat_router()
    brain = getattr(router, "brain", None) if router is not None else None
    if brain is None or not hasattr(brain, "reload"):
        return False
    try:
        brain.reload()
        return True
    except Exception:
        logger.exception("运行中的大脑热重启失败")
        return False


def _brain_saved_message(message: str) -> str:
    if _reload_live_brain():
        return f"{message}，已热重启生效"
    return f"{message}，下一次对话即生效（无需重启）"


# === 远程文本控制（手机浏览器 → /chat 页面 → /api/chat）===
# 语音进程内嵌 admin server 时，通过 chat_router_hook 注入「活的」AgentRouter，
# 手机发出的指令与语音共用同一个大脑（会话/记忆/工具上下文一致）；
# 独立运行 `python -m kekesi.api.server` 时懒构建自己的 AgentRouter。
_chat_router_hook: Callable[[], Any] | None = None
_chat_state: dict[str, Any] = {
    "router": None,
    "building": False,
    "error": "",
    "busy": False,
}
_chat_lock = threading.Lock()      # 同一时刻只处理一条指令（router 非并发安全）
_chat_cancel = threading.Event()   # 当前指令取消标志
_chat_build_lock = threading.Lock()


def register_chat_router_hook(fn: Callable[[], Any] | None) -> None:
    """注册返回运行中 AgentRouter 的回调（语音进程注入活大脑）。"""
    global _chat_router_hook
    _chat_router_hook = fn


_wake_hook: Callable[[str], None] | None = None


def register_wake_hook(fn: Callable[[str], None] | None) -> None:
    """注册远程文本指令到达时唤醒语音助手的回调。"""
    global _wake_hook
    _wake_hook = fn


def _trigger_wake(source: str = "chat") -> None:
    hook = _wake_hook
    if hook is None:
        return
    try:
        hook(source)
    except Exception:
        logger.exception("wake_hook 调用失败")


def _get_chat_router() -> Any | None:
    """优先取 hook 注入的活 router，否则取本进程懒构建的 router。"""
    hook = _chat_router_hook
    if hook is not None:
        try:
            router = hook()
            if router is not None:
                return router
        except Exception:
            logger.exception("chat_router_hook 调用失败，回退本进程 router")
    return _chat_state["router"]


def get_active_router() -> Any | None:
    """供同进程技能复用活 AgentRouter（只读用途，不触发懒构建）。"""
    hook = _chat_router_hook
    if hook is not None:
        try:
            return hook()
        except Exception:
            logger.exception("读取活 router 失败")
            return None
    return _chat_state["router"]


def _ensure_chat_router_async() -> None:
    """独立运行模式：后台构建 AgentRouter（MCP 启动可达 60s，不能阻塞请求）。"""
    with _chat_build_lock:
        if _chat_state["router"] is not None or _chat_state["building"]:
            return
        _chat_state["building"] = True
        _chat_state["error"] = ""

    def _work():
        try:
            from ..router import AgentRouter

            router = AgentRouter(_PKG_ROOT)
            register_skills_reload_hook(router.reload_skills)
            _chat_state["router"] = router
            logger.info("聊天后端 AgentRouter 构建完成")
        except Exception as e:
            _chat_state["error"] = str(e)
            logger.exception("聊天后端 AgentRouter 构建失败")
        finally:
            _chat_state["building"] = False

    threading.Thread(target=_work, daemon=True, name="chat-warmup").start()


def execute_chat_text(
    text: str,
    session_id: str,
    *,
    blocking: bool = False,
    collect_steps: bool = False,
) -> dict:
    """共享文本指令执行：/api/chat 与企微回调共用（同一把锁、同一个大脑）。

    blocking=False 时锁被占用立即返回 busy（HTTP 快速失败）；
    blocking=True 时排队等待（企微异步线程可接受慢）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text 不能为空"}
    # 唤醒只由远程渠道触发（企微/iLink 各自调 _trigger_wake）；本地打字不唤醒语音
    router = _get_chat_router()
    if router is None:
        if _chat_state["building"]:
            return {"ok": False, "ready": False, "error": "大脑正在预热，请几秒后重试"}
        return {"ok": False, "error": _chat_state["error"] or "聊天后端不可用，请查看服务日志"}

    if not _chat_lock.acquire(blocking=blocking):
        return {"ok": False, "busy": True, "error": "正在处理上一条指令，请等待完成或点击停止"}

    _chat_state["busy"] = True
    _chat_cancel.clear()
    chunks: list[str] = []
    steps: list[dict] = []

    def on_chunk(chunk) -> None:
        if chunk:
            chunks.append(str(chunk))

    def on_tool_event(func, ok, summary) -> None:
        if collect_steps:
            steps.append(
                {"tool": str(func), "ok": bool(ok), "summary": str(summary or "")[:120]}
            )

    try:
        try:
            router.session_id = session_id
        except Exception:
            logger.warning("设置会话失败，沿用 router 当前会话", exc_info=True)
        try:
            reply, layer = router.handle(
                text,
                agent_id="keks",
                on_chunk=on_chunk,
                on_tool_event=on_tool_event,
                cancel_check=_chat_cancel.is_set,
            )
        except Exception as e:
            logger.exception("指令处理失败: %s", text[:80])
            return {"ok": False, "error": f"处理失败: {e}"}
        reply_text = "".join(chunks).strip() or (str(reply) if reply else "")
        return {
            "ok": True,
            "reply": reply_text,
            "layer": str(layer or ""),
            "steps": steps,
            "cancelled": _chat_cancel.is_set(),
            "session_id": session_id,
        }
    finally:
        _chat_state["busy"] = False
        _chat_lock.release()


def _admin_token(base_dir: Path) -> str:
    env = (os.environ.get("KEKESI_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    try:
        import yaml

        cfg_path = base_dir / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            admin = data.get("admin") or {}
            if isinstance(admin, dict):
                return str(admin.get("token") or "").strip()
    except Exception:
        pass
    return ""


_LOCAL_IP_CACHE: set[str] | None = None


def _host_own_ips() -> set[str]:
    """本机自身的 IP（回环 + 局域网网卡）。同机走局域网 IP 访问时源地址不是 127.0.0.1，也应视为本机。"""
    global _LOCAL_IP_CACHE
    if _LOCAL_IP_CACHE is None:
        ips = {"127.0.0.1", "::1"}
        try:
            ips.update(_lan_ips())
        except Exception:
            pass
        _LOCAL_IP_CACHE = ips
    return _LOCAL_IP_CACHE


def _check_admin_auth(handler: BaseHTTPRequestHandler) -> bool:
    """本机访问（回环或本机网卡 IP）免鉴权；远程必须带 Bearer / X-Admin-Token。"""
    client = handler.client_address[0] if handler.client_address else ""
    if client in _host_own_ips():
        return True
    token = _admin_token(getattr(handler, "base_dir", _PKG_ROOT))
    if not token:
        logger.warning("Admin 非本机访问但未配置 admin.token，已拒绝 %s", client)
        return False
    auth = (handler.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == token
    return (handler.headers.get("X-Admin-Token") or "").strip() == token


def _require_auth(handler: BaseHTTPRequestHandler) -> bool:
    if _check_admin_auth(handler):
        return True
    _json_response(handler, 401, {"ok": False, "error": "需要管理员认证"})
    return False


def _is_masked_secret(value: str) -> bool:
    text = (value or "").strip()
    return bool(text) and "***" in text


def _resolve_tts_api_key(body: dict) -> str:
    """与 assistants.api_tts._resolve_api_key 保持一致。"""
    key = (body.get("api_key") or "").strip()
    if key and not _is_masked_secret(key):
        return key
    for env_name in (
        body.get("api_key_env"),
        "SILICONFLOW_API_KEY",
        "KEKESI_LLM_API_KEY",
        "OPENAI_API_KEY",
    ):
        if not env_name:
            continue
        val = os.environ.get(str(env_name).strip(), "").strip()
        if val:
            return val
    return ""


def _test_tts_with_api_tts(body: dict) -> tuple[bool, str]:
    """复用语音助手 ApiTTS，确保后台测试与运行时一致。"""
    src = str(_ASSISTANT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)

    from assistants.api_tts import ApiTTS

    config = {
        "api_base_url": (body.get("api_base_url") or "").strip(),
        "api_key": _resolve_tts_api_key(body),
        "api_key_env": (body.get("api_key_env") or "").strip(),
        "api_model": (body.get("api_model") or "").strip(),
        "api_voice": (body.get("api_voice") or "").strip(),
        "api_format": (body.get("api_format") or "wav").lower(),
        "api_stream": bool(body.get("api_stream", True)),
        "api_sample_rate": int(body.get("api_sample_rate", 24000)),
        "api_timeout": float(body.get("api_timeout", 120)),
        "speed": float(body.get("speed", 1.0)),
        "volume": float(body.get("volume", 1.2)),
    }
    if not config["api_base_url"] or not config["api_model"]:
        return False, "请填写 api_base_url 和 api_model"

    tts = ApiTTS(config)
    if not tts.is_available():
        return False, "请填写 api_key，或配置 api_key_env 指向对应的环境变量"

    result = tts.synthesize_to_array("你好，这是一个测试语音。")
    if result is None:
        return False, "合成失败，请检查 API 配置、音色与网络连接"
    audio, sample_rate = result
    if len(audio) == 0:
        return False, "合成返回空音频"
    duration = len(audio) / sample_rate
    return True, f"测试成功！{len(audio)} 采样点 @ {sample_rate}Hz（约 {duration:.1f}s）"


def _test_tts_with_vits(body: dict) -> tuple[bool, str]:
    """本地 VITS(MeloTTS) 引擎测试，与语音助手运行时一致。"""
    src = str(_ASSISTANT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)

    from tts_vits import VitsEngine

    kwargs = {}
    model_dir = (body.get("model_dir") or "").strip()
    if model_dir:
        kwargs["model_dir"] = model_dir
    if body.get("speed") is not None:
        kwargs["speed"] = float(body["speed"])
    if body.get("volume") is not None:
        kwargs["volume"] = float(body["volume"])

    engine = VitsEngine(**kwargs)
    if not engine.is_available():
        return False, "本地 VITS 模型缺失，请检查 tts_config.model_dir 指向的模型目录"

    result = engine.synthesize_to_array("你好，这是一个测试语音。")
    if result is None:
        return False, "合成失败，请检查本地模型文件是否完整"
    audio, sample_rate = result
    if len(audio) == 0:
        return False, "合成返回空音频"
    duration = len(audio) / sample_rate
    return True, f"本地 VITS 测试成功！{len(audio)} 采样点 @ {sample_rate}Hz（约 {duration:.1f}s）"


def _test_tts_with_zipvoice(body: dict) -> tuple[bool, str]:
    """本地 ZipVoice 克隆引擎测试，与语音助手运行时一致。"""
    src = str(_ASSISTANT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)

    from assistants.zipvoice_tts import ZipVoiceTTS

    tts = ZipVoiceTTS(body)
    if not tts.is_available():
        return False, "本地 ZipVoice 模型缺失，请确认 encoder/decoder、vocoder 与参考音频已就绪"

    result = tts.synthesize_to_array("你好，这是一个测试语音。")
    if result is None:
        return False, "合成失败，请检查 ZipVoice 模型与参考音频（prompt_wav）"
    audio, sample_rate = result
    if len(audio) == 0:
        return False, "合成返回空音频"
    duration = len(audio) / sample_rate
    return True, f"本地 ZipVoice 测试成功！{len(audio)} 采样点 @ {sample_rate}Hz（约 {duration:.1f}s）"


def _test_tts(body: dict) -> tuple[bool, str]:
    engine = str(body.get("engine") or "vits").strip().lower()
    if engine in ("api", "cloud"):
        return _test_tts_with_api_tts(body)
    if engine == "zipvoice":
        return _test_tts_with_zipvoice(body)
    return _test_tts_with_vits(body)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _memory_stats(base_dir: Path) -> dict:
    db_path = resolve_memory_db_path(base_dir)
    if not db_path.exists():
        return {"database": False}
    db = Database(db_path)
    with db.connect() as conn:
        def count(table: str) -> int:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            return int(row["c"]) if row else 0

        def safe_count(table: str) -> int:
            try:
                return count(table)
            except Exception:
                return 0

        return {
            "database": True,
            "conversation_digest": count("conversation_digest"),
            "task_history": count("task_history"),
            "apps_registry": count("apps_registry"),
            "user_profile": count("user_profile"),
            "knowledge_entries": count("knowledge_entries"),
            "habit_patterns": safe_count("habit_patterns"),
            "workflows": safe_count("workflows"),
            "execution_history": safe_count("execution_history"),
            "ui_cache": safe_count("ui_cache"),
        }


def _fast_llm_status(base_dir: Path) -> dict:
    """快速状态检查，避免 models.list 长时间阻塞后台。"""
    cfg = load_brain_config(base_dir)
    status = {
        "llm": False,
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.normalized_base_url(),
        "llm_check": "unknown",
    }
    if cfg.provider == "ollama":
        try:
            import requests

            r = requests.get(f"{cfg.base_url.rstrip('/')}/api/tags", timeout=3)
            status["llm"] = r.status_code == 200
            status["llm_check"] = "ollama_tags"
        except Exception:
            status["llm_check"] = "ollama_unreachable"
        return status

    # openai 兼容：本地服务（LM Studio 等）可能无 api_key，不再阻断
    try:
        import requests

        headers = {}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        r = requests.get(
            f"{cfg.normalized_base_url()}/models",
            headers=headers,
            timeout=5,
        )
        status["llm"] = r.status_code == 200
        status["llm_check"] = "models_http"
        if not status["llm"]:
            status["llm"] = True
            status["llm_check"] = "api_key_only"
    except Exception:
        status["llm"] = True
        status["llm_check"] = "api_key_only"
    return status


class AdminHandler(BaseHTTPRequestHandler):
    store: ConfigStore
    base_dir: Path

    def log_message(self, fmt: str, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_static(self, rel_path: str):
        safe = Path(rel_path).name if rel_path else "index.html"
        file_path = STATIC_DIR / safe
        if not file_path.exists():
            self.send_error(404, "Not Found")
            return
        content = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_chat(self):
        """远程文本指令：手机 /chat 页面 → 共享执行管线。"""
        body = _read_json_body(self)
        text = str(body.get("text") or "").strip()
        if not text:
            _json_response(self, 400, {"ok": False, "error": "text 不能为空"})
            return
        if len(text) > 2000:
            _json_response(self, 400, {"ok": False, "error": "指令过长（上限 2000 字）"})
            return
        session_id = str(body.get("session_id") or "").strip() or "web"

        result = execute_chat_text(
            text, session_id, blocking=False, collect_steps=True
        )
        if result.get("busy"):
            _json_response(self, 409, result)
            return
        if result.get("ready") is False:
            _json_response(self, 503, result)
            return
        if not result.get("ok"):
            _json_response(self, 500, result)
            return
        _json_response(self, 200, result)

    def _send_plain(self, status: int, text: str):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_wecom_callback(self):
        """企微自建应用回调：验签/解密在 wecom 模块，执行走共享 chat 管线。

        挂在 admin token 鉴权之外（企微回调有自己的签名校验），
        未启用 wecom 时表现为 404，路由不可见。
        """
        from . import wecom

        if not wecom.is_wecom_enabled(self.base_dir):
            self.send_error(404, "Not Found")
            return

        def dispatch(text: str, session_id: str) -> dict:
            _trigger_wake("wecom")  # 远程文本指令：唤醒语音助手
            return execute_chat_text(text, session_id, blocking=True)

        qs = parse_qs(urlparse(self.path).query)
        body = ""
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 0:
                body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            status, resp_text = wecom.handle_callback(self.base_dir, qs, body, dispatch)
        except Exception as e:
            logger.exception("wecom 回调处理异常")
            self._send_plain(500, f"error: {e}")
            return
        self._send_plain(status, resp_text)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        if path in ("/chat", "/chat.html"):
            self._send_static("chat.html")
            return
        if path == "/api/wecom/callback":
            self._handle_wecom_callback()
            return
        if path.startswith("/api/") and not _check_admin_auth(self):
            _json_response(self, 401, {"ok": False, "error": "需要管理员认证"})
            return
        if path == "/api/chat/status":
            hook = _chat_router_hook is not None
            router = _get_chat_router()
            if router is None and not hook:
                _ensure_chat_router_async()
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "ready": router is not None,
                    "building": bool(_chat_state["building"]),
                    "error": str(_chat_state["error"] or ""),
                    "source": "live" if (router is not None and hook) else "standalone",
                    "busy": bool(_chat_state["busy"]),
                },
            )
            return
        if path == "/api/status":
            llm = _fast_llm_status(self.base_dir)
            precheck = {
                **llm,
                "database": resolve_memory_db_path(self.base_dir).exists(),
                "paths": self.store.paths(),
            }
            try:
                from ..tools.system_integration import autostart_status

                _ok, _st = autostart_status()
                precheck["autostart_detail"] = _st
            except Exception as e:
                precheck["autostart_detail"] = f"查询失败: {e}"
            _json_response(self, 200, {"ok": True, "status": precheck})
            return
        if path == "/api/config":
            _json_response(self, 200, {"ok": True, "config": self.store.load_config()})
            return
        if path == "/api/brain-profiles":
            _json_response(
                self, 200, {"ok": True, **self.store.load_brain_profiles()}
            )
            return
        if path == "/api/mcp-servers":
            _json_response(
                self, 200, {"ok": True, "servers": self.store.load_mcp_servers()}
            )
            return
        if path == "/api/routing":
            _json_response(
                self, 200, {"ok": True, "rules": self.store.load_routing_rules()}
            )
            return
        if path == "/api/assistants":
            exists = self.store.assistants_path.exists()
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "exists": exists,
                    "assistants": self.store.load_assistants() if exists else {},
                },
            )
            return
        if path == "/api/tools":
            register_builtin_tools()
            names = get_tool_names()
            _json_response(self, 200, {"ok": True, "tools": names, "count": len(names)})
            return
        if path == "/api/skills":
            from ..skills.registry import SkillRegistry
            from ..skills.bridge import SkillBridge

            reg = SkillRegistry(self.base_dir / "skills")
            reg.load_all()
            reg.health_check_all()
            bridge = SkillBridge(reg)
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "skills": reg.list_skills(),
                    "health": reg.health(),
                    "tools": bridge.tool_names(),
                },
            )
            return
        if path == "/api/runtime/metrics":
            from ..core.metrics import get_runtime_metrics

            _json_response(
                self,
                200,
                {"ok": True, "metrics": get_runtime_metrics().snapshot()},
            )
            return
        if path == "/api/runtime/metrics":
            from ..core.metrics import get_runtime_metrics

            _json_response(
                self,
                200,
                {"ok": True, "metrics": get_runtime_metrics().snapshot()},
            )
            return
        if path == "/api/execution/history":
            from ..memory.execution_history import ExecutionHistoryStore

            qs = parse_qs(urlparse(self.path).query)
            limit = 20
            try:
                limit = int((qs.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            task_id = str((qs.get("task_id") or [""])[0] or "").strip()
            db_path = resolve_memory_db_path(self.base_dir)
            if not db_path.exists():
                _json_response(self, 200, {"ok": True, "history": [], "count": 0})
                return
            store = ExecutionHistoryStore(Database(db_path))
            rows = store.recent(limit=limit, task_id=task_id)
            _json_response(
                self,
                200,
                {"ok": True, "history": rows, "count": len(rows)},
            )
            return
        if path == "/api/traces" or path == "/api/traces/errors":
            # Global Trace 列表：支持 ?limit=20&status=failed
            # /api/traces/errors 等价于 ?status=failed（语义化别名）
            from ..trace import TraceStore

            qs = parse_qs(urlparse(self.path).query)
            limit = 20
            try:
                limit = int((qs.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            status = str((qs.get("status") or [""])[0] or "").strip()
            if path == "/api/traces/errors" and not status:
                status = "failed"
            db_path = resolve_memory_db_path(self.base_dir)
            if not db_path.exists():
                _json_response(self, 200, {"ok": True, "traces": [], "count": 0})
                return
            trace_store = TraceStore(Database(db_path))
            if status:
                rows = trace_store.recent_traces(limit=limit, status=status)
            else:
                rows = trace_store.recent_traces(limit=limit)
            _json_response(
                self,
                200,
                {"ok": True, "traces": rows, "count": len(rows)},
            )
            return
        if path.startswith("/api/traces/"):
            # Global Trace 详情：/api/traces/<trace_id> → trace + spans 树
            from ..trace import TraceStore

            trace_id = path.split("/api/traces/", 1)[1].strip("/")
            if not trace_id:
                _json_response(self, 400, {"ok": False, "error": "缺少 trace_id"})
                return
            db_path = resolve_memory_db_path(self.base_dir)
            if not db_path.exists():
                _json_response(
                    self, 404, {"ok": False, "error": "trace 不存在"}
                )
                return
            trace_store = TraceStore(Database(db_path))
            data = trace_store.get_trace(trace_id)
            if data is None:
                _json_response(self, 404, {"ok": False, "error": "trace 不存在"})
                return
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "trace": data["trace"],
                    "spans": data["spans"],
                },
            )
            return
        if path == "/api/memory/stats":
            _json_response(
                self, 200, {"ok": True, "stats": _memory_stats(self.base_dir)}
            )
            return
        if path == "/api/app-aliases":
            _json_response(
                self, 200, {"ok": True, "aliases": self.store.load_app_aliases()}
            )
            return
        if path == "/api/keywords":
            _json_response(
                self,
                200,
                {"ok": True, "files": self.store.list_keyword_files()},
            )
            return
        if path.startswith("/api/keywords/"):
            name = path.split("/api/keywords/", 1)[1].strip("/")
            try:
                content = self.store.load_keywords(name)
                _json_response(
                    self, 200, {"ok": True, "name": name, "content": content}
                )
            except ValueError as e:
                _json_response(self, 400, {"ok": False, "error": str(e)})
            return
        if path == "/api/tts/prompts":
            prompts_dir = _ASSISTANT_SRC.parent / "models" / "prompts"
            files: list[str] = []
            if prompts_dir.is_dir():
                root = _ASSISTANT_SRC.parent
                files = sorted(
                    p.relative_to(root).as_posix() for p in prompts_dir.glob("*.wav")
                )
            _json_response(self, 200, {"ok": True, "files": files})
            return
        if path == "/api/tools-policy":
            cfg = self.store.load_config(masked=False).get("tools", {})
            if not isinstance(cfg, dict):
                cfg = {}
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "config": cfg,
                    "groups": {
                        "shell": sorted(_SHELL_TOOLS),
                        "file": sorted(_FILE_TOOLS),
                        "app": sorted(_APP_TOOLS),
                        "deferred": sorted(_DEFER_BY_DEFAULT),
                    },
                },
            )
            return
        if path == "/api/personas":
            db_path = resolve_memory_db_path(self.base_dir)
            memory = MemoryService(Database(db_path))
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "custom": PersonaStore(self.base_dir).list_custom(),
                    "active": get_active_role(memory),
                },
            )
            return
        if path == "/api/persona":
            db_path = resolve_memory_db_path(self.base_dir)
            memory = MemoryService(Database(db_path))
            current = (memory.get_pref("persona") or "").strip()
            available = list_personas()
            configs = {p: get_persona_config(p) for p in available}
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "current": current,
                    "available": available,
                    "configs": configs,
                },
            )
            return
        self.send_error(404, "Not Found")

    def do_PUT(self):
        if not _require_auth(self):
            return
        path = urlparse(self.path).path
        try:
            body = _read_json_body(self)
            if path == "/api/mcp-servers":
                servers = body.get("servers", body)
                saved = self.store.save_mcp_servers(servers)
                _json_response(self, 200, {"ok": True, "servers": saved})
                return
            if path == "/api/routing":
                rules = body.get("rules", body)
                saved = self.store.save_routing_rules(rules)
                _json_response(self, 200, {"ok": True, "rules": saved})
                return
            if path == "/api/assistants":
                data = body.get("assistants", body)
                saved = self.store.save_assistants(data)
                applied = _reload_voice_assistant(self.base_dir)
                _json_response(
                    self, 200, {"ok": True, "assistants": saved, **applied}
                )
                return
            if path == "/api/app-aliases":
                aliases = body.get("aliases", body)
                saved = self.store.save_app_aliases(aliases)
                from ..app_aliases import reload_aliases

                reload_aliases()
                _json_response(self, 200, {"ok": True, "aliases": saved})
                return
            if path.startswith("/api/personas/"):
                pid = path.split("/api/personas/", 1)[1].strip("/")
                if not pid:
                    _json_response(self, 400, {"ok": False, "error": "缺少人设 id"})
                    return
                name = (body.get("name") or "").strip()
                description = (body.get("description") or "").strip()
                system_prompt = (body.get("system_prompt") or "").strip()
                item = PersonaStore(self.base_dir).update_custom(
                    pid, name, description, system_prompt
                )
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "persona": item,
                        "message": f"人设「{item['name']}」已更新",
                    },
                )
                return
            if path.startswith("/api/keywords/"):
                name = path.split("/api/keywords/", 1)[1].strip("/")
                content = body.get("content", "")
                self.store.save_keywords(name, str(content))
                applied = _reload_voice_assistant(self.base_dir)
                _json_response(self, 200, {"ok": True, "name": name, **applied})
                return
            self.send_error(404, "Not Found")
        except Exception as e:
            _json_response(self, 400, {"ok": False, "error": str(e)})

    def do_PATCH(self):
        if not _require_auth(self):
            return
        path = urlparse(self.path).path
        try:
            body = _read_json_body(self)
            if path.startswith("/api/brain-profiles/"):
                profile_id = path.split("/api/brain-profiles/", 1)[1].strip("/")
                if not profile_id:
                    _json_response(self, 400, {"ok": False, "error": "缺少配置 ID"})
                    return
                saved = self.store.save_brain_profile(profile_id, body)
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        **saved,
                        "message": _brain_saved_message("大脑配置已保存"),
                    },
                )
                return
            if path == "/api/config":
                updates = body.get("sections", body)
                saved = self.store.update_config_sections(updates)
                sections = set(updates.keys()) if isinstance(updates, dict) else set()
                if sections and sections <= {"brain"}:
                    message = _brain_saved_message("大脑配置已保存")
                elif sections == {"assistant_reload"}:
                    message = "生效方式已保存，下次保存助手/语音/唤醒词配置时按此方式处理"
                else:
                    message = "配置已保存，大脑配置即时生效；其余配置段需重启后生效"
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "config": saved,
                        "message": message,
                    },
                )
                return
            if path == "/api/assistants":
                saved = self.store.patch_assistants(body)
                applied = _reload_voice_assistant(self.base_dir)
                _json_response(
                    self,
                    200,
                    {"ok": True, "assistants": saved, **applied},
                )
                return
            self.send_error(404, "Not Found")
        except Exception as e:
            _json_response(self, 400, {"ok": False, "error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/wecom/callback":
            self._handle_wecom_callback()
            return
        if not _require_auth(self):
            return
        try:
            if path == "/api/chat":
                self._handle_chat()
                return
            if path == "/api/brain-profiles":
                body = _read_json_body(self)
                profile = self.store.add_brain_profile(str(body.get("name") or ""))
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "profile": profile,
                        "message": "配置已添加，原配置保留；填写参数后保存并热重启",
                    },
                )
                return
            if path == "/api/assistants":
                body = _read_json_body(self)
                assistant = self.store.create_assistant(body)
                warning = ""
                wake_word = str(body.get("wake_word") or "").strip()
                if wake_word:
                    try:
                        from .voice_enroll import build_keyword_lines

                        result = build_keyword_lines(wake_word)
                        self.store.save_keywords(assistant["id"], result["content"])
                        if result["missing"]:
                            warning = (
                                "唤醒词含模型不支持的音节："
                                + ", ".join(result["missing"])
                            )
                    except Exception as e:  # noqa: BLE001
                        warning = f"唤醒词未生成（{e}），可在后台录音重录"
                applied = _reload_voice_assistant(self.base_dir)
                message = f"助手「{assistant['name']}」已创建"
                if applied.get("message"):
                    message = f"{message}；{applied['message']}"
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "assistant": assistant,
                        "warning": warning,
                        **applied,
                        "message": message,
                    },
                )
                return
            if path == "/api/keywords/preview":
                body = _read_json_body(self)
                from .voice_enroll import build_keyword_lines, transcribe_wav

                text = str(body.get("text") or "").strip()
                audio_b64 = str(body.get("audio_base64") or "").strip()
                if audio_b64:
                    if audio_b64.startswith("data:") and "," in audio_b64:
                        audio_b64 = audio_b64.split(",", 1)[1]
                    text = transcribe_wav(base64.b64decode(audio_b64))
                result = build_keyword_lines(text)
                payload = {"ok": True, **result}
                if result["missing"]:
                    payload["warning"] = (
                        "模型词表缺少这些音节："
                        + ", ".join(result["missing"])
                        + "，建议换一个唤醒词"
                    )
                _json_response(self, 200, payload)
                return
            if path == "/api/chat/cancel":
                _chat_cancel.set()
                _json_response(
                    self,
                    200,
                    {"ok": True, "active": bool(_chat_state["busy"])},
                )
                return
            if path == "/api/llm/models":
                # 从 base_url 拉取可用模型列表，供前端下拉选择
                # body 可选：provider/base_url/api_key/auto_v1（未传则用已保存配置）
                body = _read_json_body(self) or {}
                cfg = load_brain_config(self.base_dir)
                provider = str(body.get("provider") or cfg.provider or "openai").strip()
                base_url = str(body.get("base_url") or cfg.base_url or "").strip()
                api_key = str(body.get("api_key") or cfg.api_key or "").strip()
                auto_v1 = body.get("auto_v1", cfg.auto_v1 if hasattr(cfg, "auto_v1") else True)
                if isinstance(auto_v1, str):
                    auto_v1 = auto_v1.lower() in ("true", "1", "yes", "on")
                if not base_url:
                    _json_response(self, 200, {"ok": False, "models": [], "error": "base_url 为空"})
                    return
                try:
                    import requests

                    if provider == "ollama":
                        r = requests.get(
                            f"{base_url.rstrip('/')}/api/tags", timeout=8
                        )
                        if r.status_code != 200:
                            _json_response(self, 200, {"ok": False, "models": [], "error": f"HTTP {r.status_code}"})
                            return
                        names = [
                            m.get("name", "")
                            for m in (r.json().get("models") or [])
                            if m.get("name")
                        ]
                        _json_response(self, 200, {"ok": True, "models": names})
                        return
                    # openai 兼容
                    norm_url = base_url.rstrip("/")
                    if auto_v1 and "/v1" not in norm_url:
                        norm_url = norm_url + "/v1"
                    headers = {}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    r = requests.get(f"{norm_url}/models", headers=headers, timeout=8)
                    if r.status_code != 200:
                        _json_response(self, 200, {"ok": False, "models": [], "error": f"HTTP {r.status_code}"})
                        return
                    data = r.json()
                    # OpenAI 格式：{data: [{id: "model-name"}, ...]}
                    models = [
                        m.get("id", "")
                        for m in (data.get("data") or [])
                        if m.get("id")
                    ]
                    _json_response(self, 200, {"ok": True, "models": models})
                    return
                except Exception as e:
                    _json_response(self, 200, {"ok": False, "models": [], "error": str(e)})
                    return
            if path == "/api/test/llm":
                cfg = load_brain_config(self.base_dir)
                brain = Brain(config=cfg, config_dir=self.base_dir)
                if not brain.is_available():
                    hint = (
                        "请在 LM Studio 加载模型并启动 Server"
                        if is_local_llm_url(cfg.base_url)
                        else "请检查 provider / api_key / base_url"
                    )
                    _json_response(
                        self,
                        200,
                        {"ok": False, "message": f"LLM 不可用，{hint}"},
                    )
                    return
                reply = brain.chat_stream("回复两个字：正常")
                _json_response(
                    self,
                    200,
                    {"ok": bool(reply), "message": reply or "无回复"},
                )
                return
            if path == "/api/memory/cleanup":
                body = _read_json_body(self)
                mem_cfg = self.store.load_config(masked=False).get("memory", {})
                cleanup_cfg = mem_cfg.get("cleanup", {}) if isinstance(mem_cfg, dict) else {}
                digest_keep = int(body.get("digest_keep", cleanup_cfg.get("digest_keep", 50)))
                task_keep = int(body.get("task_keep", cleanup_cfg.get("task_keep", 200)))
                prune = bool(
                    body.get(
                        "prune_stale_prefs",
                        cleanup_cfg.get("prune_stale_prefs", True),
                    )
                )
                db_path = resolve_memory_db_path(self.base_dir)
                memory = MemoryService(
                    Database(db_path),
                    learning=mem_cfg.get("learning"),
                    context=mem_cfg.get("context"),
                )
                stats = memory.cleanup(
                    digest_keep=digest_keep,
                    task_keep=task_keep,
                    prune_stale_prefs=prune,
                    execution_history_keep=int(
                        body.get(
                            "execution_history_keep",
                            cleanup_cfg.get("execution_history_keep", 2000),
                        )
                    ),
                )
                _json_response(self, 200, {"ok": True, "stats": stats})
                return
            if path == "/api/skills/reload":
                hook = getattr(self, "skills_reload_hook", None) or _skills_reload_hook
                if hook:
                    payload = hook()
                    _json_response(
                        self,
                        200,
                        {"ok": True, **payload},
                    )
                    return
                from ..skills.registry import SkillRegistry
                from ..skills.bridge import SkillBridge

                reg = SkillRegistry(self.base_dir / "skills")
                loaded = reg.load_all()
                checks = reg.health_check_all()
                healthy = sum(1 for ok in checks.values() if ok)
                bridge = SkillBridge(reg)
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "loaded": loaded,
                        "healthy": healthy,
                        "tools": len(bridge.tool_names()),
                        "health": reg.health(),
                        "standalone": True,
                    },
                )
                return
            if path == "/api/personas":
                body = _read_json_body(self)
                name = (body.get("name") or "").strip()
                description = (body.get("description") or "").strip()
                system_prompt = (body.get("system_prompt") or "").strip()
                if not name or not system_prompt:
                    _json_response(
                        self,
                        400,
                        {"ok": False, "error": "name 与 system_prompt 不能为空"},
                    )
                    return
                item = PersonaStore(self.base_dir).create_custom(
                    name, description, system_prompt
                )
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "persona": item,
                        "message": f"自定义人设「{item['name']}」已创建",
                    },
                )
                return
            if path == "/api/personas/activate":
                body = _read_json_body(self)
                persona_id = (body.get("persona_id") or "").strip()
                if not persona_id:
                    _json_response(self, 400, {"ok": False, "error": "persona_id 不能为空"})
                    return
                store = PersonaStore(self.base_dir)
                if store.get_custom(persona_id) is None:
                    _json_response(
                        self,
                        400,
                        {"ok": False, "error": f"人设不存在: {persona_id}"},
                    )
                    return
                db_path = resolve_memory_db_path(self.base_dir)
                memory = MemoryService(Database(db_path))
                memory.set_pref("persona_role", persona_id)
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "persona_id": persona_id,
                        "message": "人设已切换，新对话立即生效",
                    },
                )
                return
            if path == "/api/persona":
                body = _read_json_body(self)
                persona = (body.get("persona") or "").strip()
                valid = list_personas()
                if not persona or persona not in valid:
                    _json_response(
                        self,
                        400,
                        {"ok": False, "error": f"无效的 persona，可选值: {', '.join(valid)}"},
                    )
                    return
                db_path = resolve_memory_db_path(self.base_dir)
                memory = MemoryService(Database(db_path))
                memory.set_pref("persona", persona)
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "persona": persona,
                        "message": "人格设置已保存，新对话立即生效",
                    },
                )
                return
            if path == "/api/test/tts":
                body = _read_json_body(self)
                try:
                    # 已保存的全局 TTS 配置兜底：界面里 api_key 显示为掩码时用落盘的值
                    stored = self.store.load_assistants().get("tts_config")
                    if isinstance(stored, dict):
                        merged = dict(stored)
                        merged.update({k: v for k, v in body.items() if v not in (None, "")})
                        body = merged
                    ok, message = _test_tts(body)
                    _json_response(
                        self,
                        200 if ok else 400,
                        {"ok": ok, "message" if ok else "error": message},
                    )
                except Exception as e:
                    logger.exception("TTS 测试失败")
                    _json_response(self, 400, {"ok": False, "error": str(e)})
                return
            self.send_error(404, "Not Found")
        except Exception as e:
            _json_response(self, 400, {"ok": False, "error": str(e)})

    def do_DELETE(self):
        if not _require_auth(self):
            return
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/brain-profiles/"):
                profile_id = path.split("/api/brain-profiles/", 1)[1].strip("/")
                if not profile_id:
                    _json_response(self, 400, {"ok": False, "error": "缺少配置 ID"})
                    return
                removed = self.store.delete_brain_profile(profile_id)
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        **removed,
                        "message": _brain_saved_message("大脑配置已删除"),
                    },
                )
                return
            if path.startswith("/api/assistants/"):
                assistant_id = path.split("/api/assistants/", 1)[1].strip("/")
                if not assistant_id:
                    _json_response(self, 400, {"ok": False, "error": "缺少助手 ID"})
                    return
                removed = self.store.delete_assistant(assistant_id)
                applied = _reload_voice_assistant(self.base_dir)
                message = f"助手「{removed.get('name') or assistant_id}」已删除"
                if applied.get("message"):
                    message = f"{message}；{applied['message']}"
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        **applied,
                        "message": message,
                    },
                )
                return
            if path.startswith("/api/personas/"):
                pid = path.split("/api/personas/", 1)[1].strip("/")
                if not pid:
                    _json_response(self, 400, {"ok": False, "error": "缺少人设 id"})
                    return
                store = PersonaStore(self.base_dir)
                if not store.delete_custom(pid):
                    _json_response(
                        self,
                        404,
                        {"ok": False, "error": f"自定义人设不存在: {pid}"},
                    )
                    return
                # 删除的是当前激活人设时,一并清空激活状态
                db_path = resolve_memory_db_path(self.base_dir)
                memory = MemoryService(Database(db_path))
                if get_active_role(memory) == pid:
                    memory.set_pref("persona_role", "")
                _json_response(self, 200, {"ok": True, "message": "人设已删除"})
                return
            self.send_error(404, "Not Found")
        except Exception as e:
            _json_response(self, 400, {"ok": False, "error": str(e)})


def _lan_ips() -> list[str]:
    """枚举本机局域网 IPv4（0.0.0.0 绑定时打印给手机访问）。"""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                ips.append(ip)
        except Exception:
            pass
    return ips


def run_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    base_dir: Path | None = None,
    *,
    skills_reload_hook: Callable[[], dict[str, Any]] | None = None,
):
    base = base_dir or _PKG_ROOT
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    class Handler(AdminHandler):
        pass

    Handler.store = ConfigStore(base)
    Handler.base_dir = base
    Handler.skills_reload_hook = skills_reload_hook or _skills_reload_hook

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"[科科斯后台] 已启动: {url}")
    if host in ("0.0.0.0", "::"):
        for ip in _lan_ips():
            print(f"[科科斯后台] 局域网访问: http://{ip}:{port}  （手机控制页: /chat）")
        if not _admin_token(base):
            print(
                "[科科斯后台] 警告: 未配置 admin.token，局域网设备将被拒绝访问。"
                "请在 kekesi/config.yaml 的 admin.token 或环境变量 KEKESI_ADMIN_TOKEN 设置令牌"
            )
    print("[科科斯后台] 助手/语音/唤醒词保存后按「配置生效方式」处理（默认热重载）")

    # iLink 微信渠道（拉模式长轮询；未绑定/未启用时不影响后台启动）
    try:
        from . import ilink

        if ilink.is_ilink_enabled(base):
            ilink.start_polling(
                base, lambda text, sid: (_trigger_wake("ilink"),
                                         execute_chat_text(text, sid, blocking=True))[1]
            )
    except Exception:
        logger.exception("ilink 渠道启动失败")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[科科斯后台] 已停止")
        server.shutdown()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = DEFAULT_PORT
    host = os.environ.get("KEKESI_ADMIN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--host", "-H") and i + 1 < len(args):
            host = args[i + 1]
            i += 2
            continue
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
            i += 1
            continue
        try:
            port = int(arg)
        except ValueError:
            print(
                f"用法: python -m kekesi.api.server [端口] [--host 0.0.0.0]，"
                f"默认 {host}:{DEFAULT_PORT}"
            )
            sys.exit(1)
        i += 1
    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
