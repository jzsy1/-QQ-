"""Agent 编排 — Agent Loop 单路径。"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from .agent_loop import AgentLoop
from .brain import Brain
from .memory.qs_store import QsStore
from .runtime_context import build_extra_context
from .session_store import SessionStore
from .memory import MemoryService
from .tools.desktop_apps import refresh_desktop_index
from .tools.executor import ToolContext, repair_app_registry
from .mcp import McpBridge
from .sensory import SensorySystem
from .trace import span as trace_span
from .r1_executor import reset_tool_call_budget, set_tool_call_budget

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(
        self,
        brain: Brain,
        memory: MemoryService,
        apps_from_rules: dict | None = None,
        web_search_engine: str = "bing",
        mcp_bridge: McpBridge | None = None,
        skill_bridge=None,
        web_research_cfg: dict | None = None,
        tools_cfg: dict | None = None,
        learning=None,
        routing_cfg: dict | None = None,
        session_store: SessionStore | None = None,
        qs_store: QsStore | None = None,
        proactor=None,
        sensory_cfg: dict | None = None,
        static_replay_cfg: dict | None = None,
    ):
        self.brain = brain
        self.memory = memory
        self.learning = learning
        self.mcp_bridge = mcp_bridge
        self.tools_cfg = tools_cfg or {}
        self._strong_model = brain.is_strong
        threading.Thread(
            target=self._health_monitor_loop,
            daemon=True,
        ).start()
        threading.Thread(
            target=self._warmup_app_index,
            args=(memory, apps_from_rules or {}),
            name="agent-warmup",
            daemon=True,
        ).start()
        self.tool_ctx = ToolContext(
            memory=memory,
            apps_from_rules=apps_from_rules or {},
            web_search_engine=web_search_engine,
            web_research_cfg=web_research_cfg,
            tools_cfg=self.tools_cfg,
        )
        self.tool_ctx.learning = learning
        if skill_bridge is not None:
            self.tool_ctx.skill_bridge = skill_bridge
        self._session_store = session_store or SessionStore()
        self._qs_store = qs_store or QsStore(memory.db)
        self._proactor = proactor
        self._agent_loop = AgentLoop(
            brain=self.brain,
            memory=memory,
            tool_ctx=self.tool_ctx,
            mcp_bridge=mcp_bridge,
            mcp_call=self._mcp_call if mcp_bridge else None,
            skill_bridge=skill_bridge,
            session_store=self._session_store,
            qs_store=self._qs_store,
            extra_context_fn=self._extra_context,
            proactor=proactor,
            static_replay_cfg=static_replay_cfg,
        )
        # === 感官系统（纯旁路 sidecar，fail-soft，不影响主流程）===
        try:
            self.sensory = SensorySystem(
                sensory_cfg,
                session_store=self._session_store,
                qs_store=self._qs_store,
            )
            self.sensory.start()
        except Exception:  # noqa: BLE001
            logger.warning("[Sensory] 启动失败，降级为无感官模式", exc_info=True)
            self.sensory = None

    @property
    def qs(self) -> QsStore:
        return self._qs_store

    @property
    def session_store(self) -> SessionStore:
        return self._session_store

    def _call_mcp_guarded(
        self, tool_name: str, args: dict, timeout: float = 90.0
    ) -> tuple[bool, str]:
        if not self.mcp_bridge:
            return False, f"MCP 工具不可用: {tool_name}"
        try:
            return self.mcp_bridge.call(tool_name, args or {})
        except Exception as e:
            logger.exception("MCP 调用异常 %s", tool_name)
            return False, f"调用 MCP 工具出错：{e}"

    def _mcp_call(self, tool_name: str, args: dict, timeout: float = 90.0) -> tuple[bool, str]:
        if self.mcp_bridge and self.mcp_bridge.has(tool_name):
            return self._call_mcp_guarded(tool_name, args, timeout=timeout)
        return False, f"MCP 工具不可用: {tool_name}"

    @staticmethod
    def _warmup_app_index(memory, apps_from_rules: dict):
        try:
            refresh_desktop_index()
            repair_app_registry(memory, apps_from_rules)
        except Exception as e:  # noqa: BLE001
            logger.warning("应用索引预热失败: %s", e)

    def _health_monitor_loop(self):
        import time

        from .tools import winops

        last_hint = ""
        while True:
            time.sleep(60)
            try:
                ok, raw = winops.check_system_health()
                if not ok:
                    continue
                data = json.loads(raw)
                issues = data.get("issues", [])
                warnings = data.get("warnings", [])
                if issues or warnings:
                    msg = []
                    if issues:
                        msg.append("检测到系统问题：" + "、".join(issues))
                    if warnings:
                        msg.append("系统警告：" + "、".join(warnings))
                    hint = " ".join(msg)
                    # 内容没变化就不重复刷日志，避免 C 盘偏紧这类持续状态每分钟刷屏
                    if hint != last_hint:
                        last_hint = hint
                        logger.info("[健康监控] %s", hint)
            except Exception as e:
                logger.warning("[健康监控] 检查失败: %s", e, exc_info=True)

    def _extra_context(self, user_text: str = "") -> str:
        mcp_names = list(self.mcp_bridge.names) if self.mcp_bridge else None
        body = build_extra_context(
            user_text,
            apps_from_rules=self.tool_ctx.apps_from_rules,
            mcp_names=mcp_names,
            strong_model=self._strong_model,
        )
        # 感官叙事按意图追加（不需要时返回空串，不影响原上下文）
        if self.sensory is not None:
            # 第二操作者：共享桌面态势每轮常驻（不依赖意图关键词）
            try:
                snap = self.sensory.format_working_snapshot()
                if snap:
                    body = f"{body}\n\n{snap}" if body else snap
            except Exception as e:  # noqa: BLE001
                logger.debug("[Sensory] 工作台快照失败: %s", e)
            try:
                narrative = self.sensory.format_for_llm(user_text)
                if narrative:
                    body = f"{body}\n\n{narrative}" if body else narrative
            except Exception as e:  # noqa: BLE001
                logger.debug("[Sensory] 格式化叙事失败: %s", e)
        return body

    def run(
        self,
        text: str,
        agent_id: str = "keks",
        session_id: str = "default",
        on_chunk: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        on_tool_event: Callable[[str, bool, str], None] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        memory_text: str | None = None,
        execution_policy_context: str | None = None,
        goal_controller: object | None = None,  # GOAL_CTRL_V1_1
        max_tool_calls: int | None = None,
        tool_call_budget_state: dict | None = None,
    ) -> tuple[str | None, str, list[dict]]:
        sense = getattr(self, "sensory", None)

        # 感官 hook：用户轮开始
        if sense is not None:
            try:
                sense.on_user_turn_start(text or "", session_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[Sensory] 用户回合启动回调失败: %s", e)

        # 包装 on_tool_event / on_tool_start：既回调原调用方，又喂给感官系统
        wrapped_tool_event = on_tool_event
        wrapped_tool_start = on_tool_start
        if sense is not None:
            orig_event = on_tool_event
            orig_start = on_tool_start

            def _tool_event_bridge(func: str, ok: bool, summary: str) -> None:
                try:
                    sense.on_tool_event(func, ok, summary)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Sensory] 工具事件回调失败: %s", e)
                if orig_event is not None:
                    try:
                        orig_event(func, ok, summary)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[Agent] 原始工具事件回调失败: %s", e)

            def _tool_start_bridge(func: str) -> None:
                try:
                    sense.on_tool_start(func)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Sensory] 工具启动回调失败: %s", e)
                if orig_start is not None:
                    try:
                        orig_start(func)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[Agent] 原始工具启动回调失败: %s", e)

            wrapped_tool_event = _tool_event_bridge
            wrapped_tool_start = _tool_start_bridge

        _budget_token = set_tool_call_budget(max_tool_calls, tool_call_budget_state)
        try:
            with trace_span(
                "agent_loop.run",
                agent_id=agent_id,
                session_id=session_id,
            ) as s:
                reply, layer, tool_results = self._agent_loop.run(
                    text,
                    agent_id=agent_id,
                    session_id=session_id,
                    on_chunk=on_chunk,
                    on_start=on_start,
                    on_tool_event=wrapped_tool_event,
                    on_tool_start=wrapped_tool_start,
                    cancel_check=cancel_check,
                    memory_text=memory_text,
                    execution_policy_context=execution_policy_context,
                    goal_controller=goal_controller,  # GOAL_CTRL_V1_1
                )
                s.set_attribute("layer", layer or "")
                s.set_attribute("tool_count", len(tool_results or []))
                s.set_attribute("reply_len", len(reply or ""))
        finally:
            reset_tool_call_budget(_budget_token)
            # 感官 hook：用户轮结束（无论成功失败都通知）
            if sense is not None:
                try:
                    sense.on_user_turn_end(
                        reply if "reply" in locals() else None,
                        tool_results if "tool_results" in locals() else [],
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Sensory] 用户回合结束回调失败: %s", e)

        return reply, layer, tool_results
