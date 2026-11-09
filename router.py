"""路由入口 — Agent Loop 单路径；物理执行经 R1；记忆/学习模块独立挂载。"""

from __future__ import annotations

# 添加修复代码，支持独立运行
import sys
from pathlib import Path

# 如果直接运行，添加路径
if __name__ == "__main__":
    current_dir = Path(__file__).parent
    if str(current_dir) not in sys.path:
        sys.path.insert(0, str(current_dir))
    if str(current_dir.parent) not in sys.path:
        sys.path.insert(0, str(current_dir.parent))

import json
import logging
import sqlite3
from pathlib import Path

from .agent import AgentRunner
from .brain import Brain
from .llm_config import is_local_llm_url
from .memory import Database, MemoryService, LearningService, QsStore, Evolver
from .mcp import McpBridge
from .proactor import Proactor
from .session_store import SessionStore
from .tools.web_research import configure_web_research, WebResearchConfig
from .trace import (
    TraceConfig,
    TraceStore,
    get_trace_logger,
    span as trace_span,
    start_trace,
)

logger = get_trace_logger(__name__)


def _load_yaml_section(base: Path, section: str) -> dict:
    yaml_path = base / "config.yaml"
    if not yaml_path.exists():
        return {}
    try:
        import yaml

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        block = data.get(section, {})
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


class AgentRouter:
    def __init__(self, config_dir: Path | None = None):
        base = config_dir or Path(__file__).parent
        self.base_dir = base
        mem_cfg = _load_yaml_section(base, "memory")
        from .memory.db import resolve_memory_db_path

        db_path = resolve_memory_db_path(base)
        self.db = Database(db_path)
        from .memory.execution_history import ExecutionHistoryStore
        from .security.audit import AuditLog

        self.audit_log = AuditLog(base / "data" / "audit.db")
        self.execution_history = ExecutionHistoryStore(self.db)
        # === Global Trace 系统（fail-soft，初始化失败不影响主流程）===
        try:
            self.trace_cfg = TraceConfig.from_dict(
                _load_yaml_section(base, "trace")
            )
            self.trace_store = TraceStore(self.db) if self.trace_cfg.enabled else None
        except Exception:
            logger.debug("TraceStore 初始化失败，trace 系统降级为内存模式", exc_info=True)
            self.trace_cfg = TraceConfig()
            self.trace_store = None
        self.session_store = SessionStore(
            max_messages=int((mem_cfg.get("session") or {}).get("max_messages", 16)),
            max_sessions=int((mem_cfg.get("session") or {}).get("max_sessions", 32)),
        )
        from .memory.vector_chunks import VectorChunkStore

        self.vector = VectorChunkStore(
            db_path,
            vector_cfg=mem_cfg.get("vectors"),
            embedding_cfg=_load_yaml_section(base, "embedding"),
        )
        self.memory = MemoryService(
            self.db,
            learning=mem_cfg.get("learning"),
            context=mem_cfg.get("context"),
            vector=self.vector,
        )
        self.vector.start_background()
        self._run_startup_cleanup()
        try:
            resumed = self.memory.resume_forget_jobs()
            if resumed:
                logger.info("遗忘作业恢复完成：%d 条核验通过", resumed)
        except Exception as e:
            logger.warning("遗忘作业恢复失败（下次启动重试）: %s", e)
        self.brain = Brain(config_dir=base)
        self.rules = self._load_rules()
        self.session_id = "default"
        self._last_layer = ""
        self._last_tool_results: list[dict] = []
        self.mcp_bridge = self._init_mcp()
        web_research_cfg = _load_yaml_section(base, "web_research")
        if web_research_cfg:
            rules_engine = self.rules.get("web_search_engine")
            if rules_engine and "search_engine" not in web_research_cfg:
                web_research_cfg = {
                    **web_research_cfg,
                    "search_engine": rules_engine,
                }
            configure_web_research(WebResearchConfig.from_dict(web_research_cfg))
        tools_cfg = _load_yaml_section(base, "tools")
        routing_cfg = _load_yaml_section(base, "routing")
        sensory_cfg = _load_yaml_section(base, "sensory")

        self.qs_store = QsStore(self.db)
        try:
            from .memory.perception import register_qs_reader

            register_qs_reader(lambda limit=10: self.qs_store.recent(limit=limit))
        except Exception:
            logger.debug("register_qs_reader failed", exc_info=True)
        proactive_cfg = _load_yaml_section(base, "proactive")
        quiet = proactive_cfg.get("quiet_hours") or [23, 8]
        self.proactor = Proactor(
            self.db,
            interval_sec=int(proactive_cfg.get("interval_sec", 120)),
            max_daily=int(proactive_cfg.get("max_daily", 3)),
            min_confidence=float(proactive_cfg.get("min_confidence", 0.7)),
            speak_enabled=bool(proactive_cfg.get("speak", True)),
            quiet_hours=(int(quiet[0]), int(quiet[1])),
        )
        self.evolver = Evolver(
            self.db,
            interval_sec=int(proactive_cfg.get("evolve_interval_sec", 6 * 3600)),
            skills_root=self.base_dir / "skills",
            reload_skills_fn=self.reload_skills,
        )
        if proactive_cfg.get("evolve", True):
            self.evolver.start()

        # KEKESI_ROUTER_GOAL_STORE_V1: 目标持久化 v1——GoalStore 与 goal_sink。
        # 在 evolver 构造后立即设置，不放在 _init_self_study 内，以免自习关闭时丢失。
        from .core.goal_store import GoalStore

        self.goal_store = GoalStore(self.db)
        self.evolver.goal_sink = self.goal_store.save_topics

        # === 自主学习初始化 ===（必须在 AgentRunner 之前）
        self._init_learning(mem_cfg)
        self._init_self_memory(mem_cfg)

        from .skills.registry import SkillRegistry
        from .skills.bridge import SkillBridge

        self.skill_registry = SkillRegistry(self.base_dir / "skills")
        self.skill_registry.load_all()
        self.skill_registry.health_check_all()
        self.skill_bridge = SkillBridge(self.skill_registry, auto_reload=False)
        # registry 已在上面刷新过，这里不再让构造器重复 discover/load/health_check
        self.skill_bridge.reload(refresh_registry=False)
        logger.info(
            "SkillBridge: %d 个动态工具",
            len(self.skill_bridge.tool_names()),
        )

        self.agent = AgentRunner(
            brain=self.brain,
            memory=self.memory,
            apps_from_rules=self.rules.get("apps", {}),
            web_search_engine=self.rules.get("web_search_engine", "bing"),
            mcp_bridge=self.mcp_bridge,
            skill_bridge=self.skill_bridge,
            web_research_cfg=web_research_cfg if web_research_cfg else None,
            tools_cfg=tools_cfg if tools_cfg else None,
            learning=self.learning,
            routing_cfg=routing_cfg if routing_cfg else None,
            session_store=self.session_store,
            qs_store=self.qs_store,
            proactor=self.proactor,
            sensory_cfg=sensory_cfg if sensory_cfg else None,
            static_replay_cfg=mem_cfg.get("static_replay"),
        )
        self.agent.tool_ctx.execution_history = self.execution_history
        self._init_task_engine(proactive_cfg)
        self._init_event_engine(proactive_cfg)
        self._init_sentinel(proactive_cfg)
        self._init_self_study()
        self._init_skill_acquisition()
        from .core.runtime import KekesiRuntime
        from .supervisor import Supervisor

        self.runtime = KekesiRuntime(
            self.agent,
            self.db,
            config_dir=base,
            mcp_bridge=self.mcp_bridge,
            learning=self.learning,
        )
        self.proactor.bind_goal_driver(self.runtime.goal_driver)
        self.supervisor = Supervisor(self.agent, router=self)

    def _init_skill_acquisition(self):
        """初始化自我扩展（缺能力时上网找现成技能/插件并自动安装）。"""
        self.skill_acquirer = None
        # 让工具侧随时能触发技能热重载（同 _self_study / _sentinel 注入模式）
        self.agent.tool_ctx._reload_skills = self.reload_skills
        try:
            cfg = _load_yaml_section(self.base_dir, "skill_acquisition")
        except Exception:
            cfg = {}
        if not cfg.get("enabled", True):
            logger.info("skill_acquisition 已在 config.yaml 中禁用")
            return
        from .skills.acquisition import SkillAcquirer
        from .tools.web_research import search_web

        self.skill_acquirer = SkillAcquirer(
            cfg=cfg,
            llm_chat=self.brain.chat_once if self.brain else None,
            search_fn=search_web,
            registry=self.skill_registry,
            bridge=self.skill_bridge,
            reload_fn=self.reload_skills,
            mcp_reload_fn=self._reload_mcp,
            skills_root=self.base_dir / "skills",
            mcp_config_path=self.base_dir / "mcp.json",
        )
        self.agent.tool_ctx._skill_acquirer = self.skill_acquirer
        logger.info("自我扩展已就绪（来源：%s）", ", ".join(self.skill_acquirer.cfg.get("sources") or []))

    def _reload_mcp(self) -> dict:
        """重新读取 mcp.json 并重启 MCP 接入（供自我扩展装上 MCP 插件后热接入）。"""
        old = getattr(self, "mcp_bridge", None)
        if old is not None:
            try:
                old.shutdown()
            except Exception:
                logger.debug("旧 MCP 桥关闭失败", exc_info=True)
        self.mcp_bridge = self._init_mcp()
        return self._attach_mcp_bridge(self.mcp_bridge)

    def _attach_mcp_bridge(self, bridge) -> dict:
        """把新桥刷新到所有持有旧引用的组件，返回逐项结果供健康报告。"""
        results: dict[str, bool] = {}
        agent = getattr(self, "agent", None)

        def _refresh(name: str, target, attr: str = "mcp_bridge") -> None:
            if target is None or not hasattr(target, attr):
                results[name] = False
                return
            try:
                setattr(target, attr, bridge)
                results[name] = True
            except Exception:
                results[name] = False
                logger.debug("刷新 %s.%s 失败", name, attr, exc_info=True)

        _refresh("agent", agent)
        # AgentLoop 在构造时拿到桥的引用，不刷新会一直指向已 shutdown 的旧桥
        _refresh("agent_loop", getattr(agent, "_agent_loop", None))
        runtime = getattr(self, "runtime", None)
        _refresh("runtime", runtime)
        # 真正执行 MCP 的工具路径在 runtime.executor 内部
        _refresh("runtime_executor", getattr(runtime, "executor", None))
        return results

    def reload_skills(self) -> dict:
        """重新加载技能包并刷新 SkillBridge。"""
        loaded = self.skill_registry.load_all()
        checks = self.skill_registry.health_check_all()
        healthy = sum(1 for ok in checks.values() if ok)
        count = self.skill_bridge.reload(refresh_registry=False)
        self.agent.tool_ctx.skill_bridge = self.skill_bridge
        return {
            "loaded": loaded,
            "healthy": healthy,
            "tools": count,
            "skills": self.skill_registry.health(),
        }

    def shutdown(self) -> None:
        """优雅关闭后台组件。"""
        if getattr(self, "agent", None) and getattr(self.agent, "sensory", None):
            try:
                self.agent.sensory.stop()
            except Exception:
                logger.debug("sensory stop failed", exc_info=True)
        if getattr(self, "event_engine", None):
            self.event_engine.stop()
        if getattr(self, "task_engine", None):
            try:
                self.task_engine.stop()
            except Exception:
                logger.debug("task_engine stop failed", exc_info=True)
        if getattr(self, "mcp_bridge", None):
            self.mcp_bridge.shutdown()
        if getattr(self, "evolver", None):
            try:
                self.evolver.stop()
            except Exception:
                logger.debug("evolver stop failed", exc_info=True)
        if getattr(self, "_self_memory_stop", None):
            try:
                self._self_memory_stop.set()
            except Exception:
                logger.debug("self_memory stop failed", exc_info=True)
        if getattr(self, "self_study", None):
            try:
                self.self_study.stop()
            except Exception:
                logger.debug("self_study stop failed", exc_info=True)

    def _build_execute_fn(self, *, source: str = "system"):
        """统一 R1 → Critic 执行路径，供 Sentinel / TaskEngine 共用。"""
        from .execution_critic import execute_with_critic
        from .r1_executor import execute_call
        from .safety.guard import check_tool_call
        from .security.policy import RiskLevel, risk_for_tool

        def _execute(
            func: str,
            args: dict,
            preauth: str,
            *,
            task_id: int | None = None,
            rule_id: int | None = None,
        ) -> tuple[bool, str]:
            # 这条路径（Sentinel / TaskEngine / EventEngine）都是用户不在场时
            # 触发的后台执行，真正的"授权"发生在创建规则/任务的那一刻。
            # 用显式 pre_authorized 标志放行，而不是靠 preauth 文本里拼一句
            # "确认执行" 去骗过面向交互式对话设计的确认词匹配——denylist /
            # 路径保护 / 空闲检测等硬性拦截依然会正常执行，不受影响。
            reason_bits = [f"source={source}"]
            if task_id is not None:
                reason_bits.append(f"task#{task_id}")
            if rule_id is not None:
                reason_bits.append(f"rule#{rule_id}")
            allowed, deny = check_tool_call(
                func,
                args or {},
                user_text=preauth,
                pre_authorized=True,
                pre_authorized_reason=", ".join(reason_bits),
            )
            if not allowed:
                return False, deny
            resp, _extra, ok, summary = execute_with_critic(
                func,
                args or {},
                self.agent.tool_ctx,
                execute_call=execute_call,
                mcp_bridge=getattr(self.agent, "mcp_bridge", None),
                source=source,
            )
            risk = risk_for_tool(func)
            if risk >= RiskLevel.MEDIUM and getattr(self, "audit_log", None):
                verified = resp.data.get("verified")
                if verified is None:
                    verified = ok
                self.audit_log.record(
                    func,
                    task_id=str(task_id or rule_id or source),
                    action=func,
                    params=args or {},
                    permission_level=int(risk),
                    success=ok,
                    verified=bool(verified),
                    error="" if ok else (summary or "")[:200],
                )
            if getattr(self, "qs_store", None):
                try:
                    self.qs_store.append_tool_result(
                        func,
                        args or {},
                        ok=ok,
                        text=summary,
                        r1_status=resp.status,
                        session_id="system",
                    )
                except Exception:
                    logger.debug("QS 记录后台执行失败", exc_info=True)
            if getattr(self, "observer", None):
                if rule_id is not None:
                    self.observer.on_sentinel_action(
                        rule_id, func, ok=ok, message=summary
                    )
                elif task_id is not None:
                    self.observer.on_task_executed(
                        task_id, func, ok=ok, message=summary, verified=ok
                    )
            return ok, summary

        return _execute

    def _init_sentinel(self, proactive_cfg: dict):
        from .sentinel import Sentinel

        base_execute = self._build_execute_fn(source="sentinel")

        def _execute(func: str, args: dict, preauth: str) -> tuple[bool, str]:
            return base_execute(func, args, preauth)

        self.sentinel = Sentinel(
            self.db,
            execute_fn=_execute,
            interval_sec=int(proactive_cfg.get("sentinel_interval_sec", 30)),
            observer=getattr(self, "observer", None),
        )
        self.agent.tool_ctx._sentinel = self.sentinel
        try:
            from .tools.hardware_monitor import start_thermal_monitoring

            start_thermal_monitoring()
        except Exception:
            logger.debug("thermal monitoring start failed", exc_info=True)

    def _init_task_engine(self, proactive_cfg: dict):
        from .notifications import notify_reminder
        from .observer import SystemObserver
        from .task_engine import TaskEngine

        te_cfg = _load_yaml_section(self.base_dir, "task_engine")
        poll_sec = int(
            te_cfg.get("poll_interval_sec")
            or proactive_cfg.get("task_poll_sec")
            or 60
        )
        max_retries = int(te_cfg.get("max_retries", 0))

        self.observer = SystemObserver(self.qs_store)
        self.task_engine = TaskEngine(
            self.db,
            poll_interval_sec=poll_sec,
            max_retries=max_retries,
        )
        self.task_engine.set_observer(self.observer)
        self.task_engine.set_reminder_fn(
            lambda task: notify_reminder(
                f"提醒：{task.title}"
                + (f"，{task.description}" if task.description else "")
            )
        )

        base_execute = self._build_execute_fn(source="task_engine")

        def _task_execute(task, func: str, args: dict) -> tuple[bool, str]:
            # 不再需要拼"确认执行"这几个字——_build_execute_fn 已经用
            # pre_authorized=True 显式放行后台任务；这里的文本只是描述性
            # 上下文，仍会走 denylist / 桌面客户端拦截等其他检查。
            preauth = f"定时任务#{task.id}（{task.title}）"
            return base_execute(
                func,
                args,
                preauth,
                task_id=task.id,
            )

        self.task_engine.set_execute_fn(_task_execute)
        self.task_engine.start()
        self.agent.tool_ctx._task_scheduler = self.task_engine.scheduler
        self.agent.tool_ctx._task_engine = self.task_engine
        # 兼容旧属性名
        self.task_scheduler = self.task_engine.scheduler

    def _init_event_engine(self, proactive_cfg: dict) -> None:
        """轻量事件调度 — 与 TaskEngine / Sentinel 并存。"""
        ee_cfg = _load_yaml_section(self.base_dir, "events")
        if not ee_cfg.get("enabled", True):
            self.event_engine = None
            return
        from .events.engine import EventEngine

        poll = float(
            ee_cfg.get("poll_interval_sec")
            or proactive_cfg.get("event_poll_sec")
            or 30
        )
        self.event_engine = EventEngine(poll_interval_sec=poll)
        base_execute = self._build_execute_fn(source="event")

        def _on_scheduled(rule) -> None:
            action = dict(rule.action or {})
            func = str(action.get("func") or action.get("tool") or "").strip()
            if not func:
                return
            args = dict(action.get("args") or {})
            preauth = str(action.get("preauth") or f"event:{rule.id}")
            base_execute(func, args, preauth)

        self.event_engine.register_handler("scheduled", _on_scheduled)
        for item in ee_cfg.get("rules") or []:
            if not isinstance(item, dict):
                continue
            self.event_engine.add_rule(
                str(item.get("type") or "scheduled"),
                dict(item.get("condition") or {}),
                dict(item.get("action") or {}),
                enabled=bool(item.get("enabled", True)),
            )
        self.event_engine.start()
        logger.info("EventEngine 已启动（规则 %d 条）", len(self.event_engine._rules))

    def _init_self_study(self):
        """初始化定时自习服务（每日自动上网搜资料→总结→记知识库）。"""
        self.self_study = None
        try:
            cfg = _load_yaml_section(self.base_dir, "self_study")
        except Exception:
            cfg = {}
        if not cfg.get("enabled", True):  # KEKESI_PCE_P3_20261101_V2
            return
        from .memory.self_study import SelfStudyService

        self.self_study = SelfStudyService(
            db=self.db,
            llm_chat=self.brain.chat_once if self.brain else None,
            cfg=cfg,
            memory=self.memory,
        )
        self.self_study.start()
        # 挂到工具上下文，供 self_study 工具手动触发（同 _sentinel 模式）
        self.agent.tool_ctx._self_study = self.self_study
        if getattr(self, "evolver", None) is not None:
            self.evolver.study_submit_fn = self.self_study.run_now_async

    def _init_learning(self, mem_cfg: dict):
        """初始化自主学习服务"""
        learning_cfg = mem_cfg.get("learning", {}) or {}
        self.learning = None

        learning_enabled = learning_cfg.get(
            "enabled", learning_cfg.get("auto_learning", True)
        )
        if learning_enabled:
            self.learning = LearningService(
                db=self.db,
                llm_chat=self.brain.chat_once if self.brain else None,
                cfg=learning_cfg,
                memory=self.memory,
            )
            logger.info("自主学习引擎已启动（SQLite FTS）")
            self._run_learning_cleanup(learning_cfg)
            # 后台补齐历史对话摘要，让重启后仍能记得之前聊过什么
            self.learning.summarize_in_background()

    def _init_self_memory(self, mem_cfg: dict) -> None:
        """自我记忆层：L2 后台清洗线程在此启动（远程 embedding 只允许出现在这里），
        L0/L1 的每轮读取走本地 SQLite。"""
        import threading

        from .memory import self_memory

        section = mem_cfg.get("self_memory") or {}
        self_memory.configure(section)
        self._self_memory_stop: threading.Event | None = None
        if not section.get("enabled", True):
            logger.info("自我记忆层已关闭（memory.self_memory.enabled=false）")
            return
        cfg = self_memory.resolved_cfg()
        loaders = [
            (
                "douyin_browse",
                self_memory.make_plugin_loader(
                    self.base_dir,
                    pkg_name="_kekesi_skill_douyin_browse",
                    sub_path="skills/media/douyin_browse",
                ),
            )
        ]
        stop = threading.Event()
        self._self_memory_stop = stop
        self_memory.start_refresh_thread(
            self.db.connect,
            loaders,
            interval_sec=float(cfg["interval_sec"]),
            embedder=self.vector.embed_texts if self.vector else None,
            sim_threshold=float(cfg["sim_threshold"]),
            max_rows=int(cfg["max_rows"]),
            stop_event=stop,
        )
        logger.info(
            "自我记忆层已启动：%d 分钟清洗一次，L0 每轮最多 %d 条 / %d 字，向量后端=%s",
            int(cfg["interval_sec"] / 60),
            int(cfg["l0_limit"]),
            int(cfg["l0_max_chars"]),
            "已接" if self.vector else "未接（退回关键词去重）",
        )

    def _run_learning_cleanup(self, learning_cfg: dict):
        """启动时清理学习数据（非关键路径，另一进程占用时不阻断启动）"""
        if not self.learning:
            return
        try:
            self.learning.cleanup()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                # 另一进程正持有数据库锁（如 voice 入口长事务）→ 跳过本次清理
                logger.debug("学习数据清理跳过（数据库被占用）: %s", e)
            else:
                logger.warning("学习数据清理失败: %s", e)
        except Exception as e:
            logger.warning("学习数据清理失败: %s", e)

    def _run_startup_cleanup(self):
        mem_cfg = _load_yaml_section(self.base_dir, "memory")
        cleanup_cfg = mem_cfg.get("cleanup") or {}
        if not cleanup_cfg.get("on_startup", True):
            return
        try:
            stats = self.memory.cleanup(
                digest_keep=int(cleanup_cfg.get("digest_keep", 50)),
                task_keep=int(cleanup_cfg.get("task_keep", 200)),
                prune_stale_prefs=bool(cleanup_cfg.get("prune_stale_prefs", True)),
                qs_keep=int(cleanup_cfg.get("qs_keep", 2000)),
                digest_per_session=int(cleanup_cfg.get("digest_per_session", 40)),
                execution_history_keep=int(
                    cleanup_cfg.get("execution_history_keep", 2000)
                ),
                working_retention_days=int(
                    ((mem_cfg.get("zones") or {}).get("working") or {}).get(
                        "retention_days", 7
                    )
                ),
                salient_keep_days=int(cleanup_cfg.get("salient_keep_days", 365)),
                salient_keep_max=int(cleanup_cfg.get("salient_keep_max", 500)),
            )
            if getattr(self, "session_store", None):
                pruned = self.session_store.prune_idle()
                if pruned:
                    stats = {**stats, "sessions_pruned": pruned}
            if any(stats.values()):
                logger.info("记忆库启动清理: %s", stats)
        except Exception as e:
            logger.warning("记忆库启动清理失败: %s", e)
        try:
            from .tools.screen_ops import cleanup_old_screenshots

            shot_stats = cleanup_old_screenshots(
                keep_days=int(cleanup_cfg.get("screenshot_keep_days", 30))
            )
            if shot_stats.get("deleted"):
                logger.info("截图存档清理: %s", shot_stats)
        except Exception as e:
            logger.warning("截图存档清理失败: %s", e)

    def _load_rules(self) -> dict:
        rules_path = self.base_dir / "data" / "router_rules.json"
        if rules_path.exists():
            try:
                with open(rules_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取 router_rules.json 失败，使用空规则: %s", e)
        return {}

    def _init_mcp(self) -> McpBridge | None:
        mcp_cfg = _load_yaml_section(self.base_dir, "mcp")
        if not mcp_cfg.get("enabled", True):
            logger.info("MCP 已在 config.yaml 中禁用")
            return None

        config_file = mcp_cfg.get("config_file", "mcp.json")
        config_path = self.base_dir / config_file
        if not config_path.exists():
            logger.info("未找到 MCP 配置 %s，跳过", config_path)
            return None

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取 MCP 配置失败: %s", e)
            return None

        if not raw:
            return None

        bridge = McpBridge()
        timeout = float(mcp_cfg.get("startup_timeout_sec", 60))
        call_timeout = float(mcp_cfg.get("call_timeout_sec", 90))

        def _log(msg: str) -> None:
            logger.info(msg)
            print(f"[MCP] {msg}")

        print("[MCP] 正在接入 MCP 工具服务器…")
        bridge.start(raw, log=_log, timeout=timeout, config_dir=self.base_dir, call_timeout=call_timeout)
        if bridge.names:
            print(f"[MCP] 已接入: {', '.join(bridge.names)}")
        else:
            print("[MCP] 无可用 MCP 服务器（检查 mcp.json 与 Node.js/npx）")
        return bridge

    def precheck(self) -> dict:
        llm_ok = self.brain.is_available()
        mcp_info = {
            "enabled": self.mcp_bridge is not None,
            "servers": self.mcp_bridge.names if self.mcp_bridge else [],
            "tool_count": len(self.mcp_bridge.tool_schemas())
            if self.mcp_bridge
            else 0,
        }
        learning_info = (
            self.learning.get_stats() if self.learning else {}
        )
        privilege = "unknown"
        autostart_on = False
        try:
            from .tools.privilege import privilege_level
            from .tools.system_integration import autostart_status

            privilege = privilege_level()
            _ok, _st = autostart_status()
            autostart_on = "已启用" in (_st or "")
        except Exception:
            pass
        return {
            "llm": llm_ok,
            "provider": self.brain.provider,
            "database": self.db.path.exists(),
            "model": self.brain.model,
            "base_url": self.brain.base_url,
            "capability": self.brain.config.capability,
            "vision": self.brain.config.vision,
            "mcp": mcp_info,
            "learning": learning_info,
            "privilege": privilege,
            "autostart": autostart_on,
        }

    def handle(
        self,
        text: str,
        agent_id: str = "keks",
        on_chunk=None,
        on_start=None,
        on_tool_event=None,
        on_tool_start=None,
        cancel_check=None,
    ) -> tuple[str | None, str]:
        text = text.strip()
        if not text:
            self._last_tool_results = []
            return None, "empty"

        # === 开启 Global Trace：贯穿 router.handle → supervisor → runtime 全链路 ===
        cfg = getattr(self, "trace_cfg", None) or TraceConfig()
        store = getattr(self, "trace_store", None) if cfg.enabled else None
        with start_trace(
            text,
            agent_id=agent_id,
            session_id=self.session_id,
            store=store,
            user_text_max=cfg.user_text_max_chars,
            reply_max=cfg.reply_max_chars,
        ) as trace:
            with trace_span("router.handle", agent_id=agent_id):
                return self._handle_inner(
                    text,
                    agent_id=agent_id,
                    on_chunk=on_chunk,
                    on_start=on_start,
                    on_tool_event=on_tool_event,
                    on_tool_start=on_tool_start,
                    cancel_check=cancel_check,
                    trace=trace,
                )

    def _handle_inner(
        self,
        text: str,
        agent_id: str,
        on_chunk,
        on_start,
        on_tool_event,
        on_tool_start,
        cancel_check,
        trace,
    ) -> tuple[str | None, str]:
        """handle 的实际业务逻辑（已由 handle 开启 trace 上下文）。"""
        # 用户开口 → 取消 Sentinel 等待中的默认动作（回应交给正常对话流）
        if getattr(self, "sentinel", None):
            self.sentinel.notify_user_activity()

        try:
            handle_res = self.supervisor.handle(
                text,
                agent_id=agent_id,
                session_id=self.session_id,
                on_chunk=on_chunk,
                on_start=on_start,
                on_tool_event=on_tool_event,
                on_tool_start=on_tool_start,
                cancel_check=cancel_check,
                memory_text=text,
            )
            reply, layer, tool_results = handle_res[:3]
            task = handle_res[3] if len(handle_res) > 3 else None
        except Exception:
            logger.error("handle 异常", exc_info=True)
            trace.finish(
                "error",
                error="supervisor.handle exception",
                layer="error",
            )
            raise

        self._last_layer = layer
        self._last_tool_results = list(tool_results or [])

        if reply:
            self._finish(text, reply, layer, tool_results, agent_id=agent_id, task=task)
            if layer == "loop_degraded":
                trace.finish(
                    "error",
                    reply=reply,
                    error="llm unavailable fallback",
                    layer=layer,
                )
            else:
                trace.finish("success", reply=reply, layer=layer)
            return reply, layer

        provider = self.brain.config.provider_label
        if self.brain.provider == "openai":
            if is_local_llm_url(self.brain.config.base_url):
                fallback = (
                    f"抱歉，本地推理服务（{self.brain.config.base_url}）暂时不可用。"
                    "请在 LM Studio 中加载模型「"
                    f"{self.brain.model}」并确认 Server 已启动后再试。"
                )
            else:
                fallback = (
                    f"抱歉，{provider} API 暂时不可用，"
                    "请检查 config.yaml 中的 base_url、model 和 api_key。"
                )
        else:
            fallback = (
                f"抱歉，{provider} 暂时无法处理，"
                "请确认已启动 ollama serve 并已拉取模型。"
            )
        self._finish(text, fallback, "error", [], agent_id=agent_id)
        trace.finish("error", reply=fallback, error="llm unavailable", layer="error")
        return fallback, "error"

    _SKIP_DIGEST_LAYERS = frozenset(
        {
            "video_fastpath",
            "music_fastpath",
            "search_fastpath",
            "windows_fastpath",
            "static_compile",
            "static_fastpath",
            "loop_degraded",
        }
    )

    def _finish(
        self,
        user_text: str,
        reply: str,
        layer: str,
        tool_results: list[dict] | None = None,
        agent_id: str = "keks",
        task: Any | None = None,
    ):
        # 工具快路径（含意图分类后直达）：不写 digest / 不触发学习，避免测试与点播污染记忆
        if layer in self._SKIP_DIGEST_LAYERS:
            return
        self.memory.save_digest(user_text, reply, layer, self.session_id)
        # 若已有 Supervisor 任务产出，由其持久化反馈链路带证据执行学习，避免 Router 再次重复触发学习
        if task is not None:
            return
        if self.learning:
            self.learning.on_conversation_end(
                user_text=user_text,
                reply_text=reply,
                session_id=self.session_id,
                agent_id=agent_id,
                tool_results=tool_results,
            )
