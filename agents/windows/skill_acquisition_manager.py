"""SkillAcquisitionManager — 自我扩展：上网找现成技能/插件并自动安装。

薄壳：真正流水线在 kekesi/skills/acquisition.py 的 SkillAcquirer，
由 AgentRouter 在启动时构造并挂到 tool_ctx._skill_acquirer。
"""

from __future__ import annotations

import logging
from typing import Any

from ...tools.execution_result import ExecutionResult, from_tuple

logger = logging.getLogger(__name__)


class SkillAcquisitionManager:
    """转发到注入的 SkillAcquirer。"""

    def __init__(self, tool_ctx: Any = None) -> None:
        self._ctx = tool_ctx

    def _acquirer(self) -> Any:
        return getattr(self._ctx, "_skill_acquirer", None)

    def acquire(self, need: str) -> ExecutionResult:
        need = str(need or "").strip()
        if not need:
            return ExecutionResult(
                success=False,
                message="你想让我装个什么能力的技能？说具体点我马上去找。",
                verified=False,
                action="acquire_skill",
            )
        acquirer = self._acquirer()
        if acquirer is None:
            return ExecutionResult(
                success=False,
                message="自学能力还没接上（config.yaml 的 skill_acquisition 没启用）。",
                verified=False,
                action="acquire_skill",
            )
        try:
            ok, msg = acquirer.acquire(need)
        except Exception as exc:  # noqa: BLE001
            logger.exception("acquire_skill 执行失败")
            return ExecutionResult(
                success=False,
                message=f"自学没成功：{exc}",
                verified=False,
                action="acquire_skill",
                error=str(exc),
            )
        return from_tuple(bool(ok), str(msg), verified=bool(ok), action="acquire_skill", target=need[:40])

    def uninstall(self, skill_ref: str) -> ExecutionResult:
        ref = str(skill_ref or "").strip()
        if not ref:
            return ExecutionResult(
                success=False,
                message="你想卸载哪个插件/技能？说名字（不确定可先让我列出已装的）。",
                verified=False,
                action="uninstall_skill",
            )
        acquirer = self._acquirer()
        if acquirer is None:
            return ExecutionResult(
                success=False,
                message="自学能力还没接上（config.yaml 的 skill_acquisition 没启用）。",
                verified=False,
                action="uninstall_skill",
            )
        try:
            ok, msg = acquirer.uninstall(ref)
        except Exception as exc:  # noqa: BLE001
            logger.exception("uninstall_skill 执行失败")
            return ExecutionResult(
                success=False,
                message=f"卸载没成功：{exc}",
                verified=False,
                action="uninstall_skill",
                error=str(exc),
            )
        return from_tuple(bool(ok), str(msg), verified=bool(ok), action="uninstall_skill", target=ref[:40])
