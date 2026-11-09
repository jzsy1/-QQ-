"""WindowsVerifier — 验证 Windows 真实状态与 R1 后检。"""

from __future__ import annotations

import logging
from typing import Any

from ...core.path_verify import path_still_exists
from ...models.evidence import Evidence
from ...r1_executor import (
    R1Response,
    STATUS_PHYSICAL_BLOCKED,
    STATUS_SUCCESS,
    execute_call,
)
from ...tools.executor import ToolContext
from ...tools.process_ops import is_app_running

logger = logging.getLogger(__name__)

VERIFY_FUNCS = frozenset({
    "open_app",
    "close_app",
    "close_window",
    "kill_process",
    "delete_file",
    "move_to_trash",
    "focus_window",
    "write_registry",
    "run_shell",
    "uninstall_software",
})


def verification_enabled(func: str) -> bool:
    return (func or "").strip() in VERIFY_FUNCS


class WindowsVerifier:
    """Windows 状态验证与 R1 物理后检。"""

    def verify_process(self, name: str, *, launch_target: str = "") -> Evidence:
        running = is_app_running(name, launch_target)
        return Evidence.from_execution(
            verified=not running,
            checks=[{"type": "process", "name": name, "running": running}],
            observed_state={"process_running": running},
            reason="" if not running else "process still running",
        )

    def verify_application(self, name: str, *, launch_target: str = "") -> Evidence:
        running = is_app_running(name, launch_target)
        return Evidence.from_execution(
            verified=running,
            checks=[{"type": "application", "name": name, "running": running}],
            observed_state={"application_running": running},
        )

    def verify_file_deleted(self, path: str) -> Evidence:
        exists = path_still_exists(path)
        return Evidence.from_execution(
            verified=not exists,
            checks=[{"type": "file", "path": path, "exists": exists}],
            observed_state={"path_exists": exists},
            reason="" if not exists else "path still exists",
        )

    def verify_pid(self, pid: int) -> Evidence:
        from ...tools.process_ops import is_process_alive

        alive = is_process_alive(str(pid)) if pid else False
        return Evidence.from_execution(
            verified=not alive,
            checks=[{"type": "pid", "pid": pid, "alive": alive}],
            observed_state={"pid_alive": alive},
        )

    def check_no_side_effect(self, func: str, args: dict) -> str:
        # UNKNOWN_GATE_V2_4_3: read-only check only, never calls execute_call,
        # close_app, run_shell, or any write operation.
        # "no_effect": current state positively shows the intended effect did NOT happen.
        # "effect": current state positively shows it DID happen.
        # "unavailable": no reliable read-only check, missing args, or check failed.
        func = (func or "").strip()
        try:
            if func in ("delete_file", "move_to_trash"):
                path = str(args.get("path") or "").strip()
                if not path:
                    return "unavailable"
                return "no_effect" if path_still_exists(path) else "effect"
            if func == "close_app":
                app = str(args.get("app_name") or "").strip()
                if not app:
                    return "unavailable"
                return "no_effect" if is_app_running(app, memory=None) else "effect"
            if func == "close_window":
                from ...tools.window_ops import window_title_visible

                title = str(args.get("title") or "").strip()
                if not title:
                    return "unavailable"
                return "no_effect" if window_title_visible(title) else "effect"
            if func == "kill_process":
                pid = args.get("pid")
                if pid:
                    from ...tools.process_ops import is_process_alive

                    alive = is_process_alive(str(pid))
                    return "no_effect" if alive else "effect"
                target = str(args.get("target") or args.get("process") or "").strip()
                if not target:
                    return "unavailable"
                return "no_effect" if is_app_running(target, memory=None) else "effect"
            if func == "open_app":
                app = str(args.get("app_name") or "").strip()
                if not app:
                    return "unavailable"
                return "no_effect" if not is_app_running(app, memory=None) else "effect"
            if func == "focus_window":
                from ...tools.window_ops import window_title_visible

                title = str(args.get("title") or args.get("window_title") or "").strip()
                if not title:
                    return "unavailable"
                return "no_effect" if not window_title_visible(title) else "effect"
            if func == "uninstall_software":
                from ...tools.package_ops import is_package_installed

                package = str(
                    args.get("package") or args.get("name") or args.get("app_name") or ""
                ).strip()
                if not package:
                    return "unavailable"
                return "no_effect" if is_package_installed(package) else "effect"
            # write_registry: read-back only checks ok, not the target value, so it
            # cannot confirm no effect; run_shell: arbitrary command cannot be judged;
            # move_path/copy_path/rename_path: no read-only check exists.
            return "unavailable"
        except Exception:
            logger.debug("check_no_side_effect failed", exc_info=True)
            return "unavailable"


    def run_post_check(
        self,
        func: str,
        args: dict,
        r1: R1Response,
        ctx: ToolContext,
        *,
        mcp_bridge: Any | None = None,
    ) -> tuple[R1Response, list[dict]]:
        """R1 返回后验证物理结果（方案 Verification Engine）。"""
        extra: list[dict] = []
        if (not verification_enabled(func)  # CLOSED_LOOP_P0_V2_20261030
                or r1.status == globals().get("STATUS_EXECUTING", "executing")
                or (r1.data or {}).get("reason") == "cancelled"):
            return r1, extra

        if func == "open_app":
            return self._check_open_app(args, r1, ctx, mcp_bridge, extra)
        if func == "close_app":
            return self._check_close_app(args, r1, ctx, mcp_bridge, extra)
        if func == "close_window":
            return self._check_close_window(args, r1, extra)
        if func == "kill_process":
            return self._check_kill_process(args, r1, extra)
        if func in ("delete_file", "move_to_trash"):
            return self._check_file_deleted(args, r1, extra)
        if func == "focus_window":
            return self._check_focus_window(args, r1, extra)
        if func == "write_registry":
            return self._check_write_registry(args, r1, extra)
        if func == "run_shell":
            return self._check_run_shell(args, r1, extra)
        if func == "uninstall_software":
            return self._check_uninstall(args, r1, extra)
        return r1, extra

    def _blocked(
        self,
        func: str,
        args: dict,
        text: str,
        *,
        reason: str = "verify_failed",
    ) -> R1Response:
        return R1Response(
            status=STATUS_PHYSICAL_BLOCKED,
            func=func,
            args=args,
            data={"ok": False, "text": text, "verified": False, "reason": reason},
        )

    @staticmethod
    def _mark_verified(r1: R1Response) -> R1Response:
        data = dict(r1.data)
        data["verified"] = True
        return R1Response(
            status=STATUS_SUCCESS,
            func=r1.func,
            args=r1.args,
            data=data,
        )

    def _check_open_app(
        self,
        args: dict,
        r1: R1Response,
        ctx: ToolContext,
        mcp_bridge: Any | None,
        extra: list[dict],
    ) -> tuple[R1Response, list[dict]]:
        app = str(args.get("app_name") or "").strip()
        if not app:
            return r1, extra
        launch_cmd = ctx.app_manager.resolve_command(app) or ""
        if is_app_running(app, launch_cmd, memory=ctx.memory):
            return self._mark_verified(r1), extra

        logger.info("自检：%s 未在运行，尝试 run_shell 补开", app)
        cmd = launch_cmd or ctx.app_manager.resolve_command(app)
        if cmd:
            alt = execute_call(
                {"func": "run_shell", "args": {"command": cmd}},
                ctx,
                mcp_bridge=mcp_bridge,
            )
            extra.append({
                "name": "run_shell→selfcheck",
                "ok": alt.status == STATUS_SUCCESS,
                "args": {"command": cmd},
                "r1_status": alt.status,
                "via": "selfcheck",
            })
            if alt.status == STATUS_SUCCESS and is_app_running(
                app, cmd, memory=ctx.memory
            ):
                return self._mark_verified(alt), extra

        return self._blocked(
            r1.func,
            args,
            f"未能确认「{app}」已启动，请检查应用是否安装或名称是否正确。",
        ), extra

    def _check_close_app(
        self,
        args: dict,
        r1: R1Response,
        ctx: ToolContext,
        mcp_bridge: Any | None,
        extra: list[dict],
    ) -> tuple[R1Response, list[dict]]:
        app = str(args.get("app_name") or "").strip()
        if not app:
            return r1, extra
        launch_cmd = ctx.app_manager.resolve_command(app) or ""
        if not is_app_running(app, launch_cmd, memory=ctx.memory):
            return self._mark_verified(r1), extra

        logger.info("自检：%s 仍在运行，尝试 force close_app", app)
        alt = execute_call(
            {"func": "close_app", "args": {"app_name": app, "force": True}},
            ctx,
            mcp_bridge=mcp_bridge,
        )
        extra.append({
            "name": "close_app→selfcheck",
            "ok": alt.status == STATUS_SUCCESS,
            "args": {"app_name": app, "force": True},
            "r1_status": alt.status,
            "via": "selfcheck",
        })
        if alt.status == STATUS_SUCCESS and not is_app_running(
            app, launch_cmd, memory=ctx.memory
        ):
            return self._mark_verified(alt), extra

        return self._blocked(
            r1.func,
            args,
            f"未能确认「{app}」已关闭，可能仍有后台进程在运行。",
        ), extra

    def _check_close_window(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        from ...tools.window_ops import window_title_visible

        title = str(args.get("title") or "").strip()
        if not title:
            return self._mark_verified(r1), extra
        if not window_title_visible(title):
            return self._mark_verified(r1), extra
        return self._blocked(
            r1.func, args, f"未能确认窗口「{title}」已关闭。"
        ), extra

    def _check_kill_process(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        target = str(args.get("target") or args.get("process") or "").strip()
        if not target:
            return self._mark_verified(r1), extra
        if not is_app_running(target, memory=None):
            return self._mark_verified(r1), extra
        logger.info("自检：kill_process 后 %s 仍在运行", target)
        return self._blocked(
            r1.func, args, f"未能确认进程「{target}」已终止。"
        ), extra

    def _check_file_deleted(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        path = str(args.get("path") or "").strip()
        if not path:
            return self._mark_verified(r1), extra
        if not path_still_exists(path):
            return self._mark_verified(r1), extra
        logger.info("自检：删除后路径仍存在 %s", path[:80])
        return self._blocked(
            r1.func,
            args,
            f"未能确认「{path}」已删除或移入回收站。",
        ), extra

    def _check_focus_window(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        from ...tools.window_ops import window_title_visible

        title = str(args.get("title") or args.get("window_title") or "").strip()
        if not title:
            return self._mark_verified(r1), extra
        if window_title_visible(title):
            return self._mark_verified(r1), extra
        return self._blocked(
            r1.func, args, f"未能确认窗口「{title}」已置前。"
        ), extra

    def _check_write_registry(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        from ...tools.system_admin import read_registry

        path = str(args.get("path") or "").strip()
        reg_name = str(args.get("name") or "").strip()
        if not path or not reg_name:
            return self._mark_verified(r1), extra
        ok, _ = read_registry(path, reg_name)
        if ok:
            return self._mark_verified(r1), extra
        return self._blocked(r1.func, args, "注册表写入后读回验证失败。"), extra

    def _check_run_shell(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        text = str(r1.data.get("text") or "")
        if r1.data.get("verified") is True:
            return self._mark_verified(r1), extra
        if "退出码 0" in text or "命令已执行（无输出）" in text:
            return self._mark_verified(r1), extra
        if "执行失败（退出码" in text:
            return self._blocked(
                r1.func, args, text[:200] or "shell 退出码非零"
            ), extra
        if r1.status == STATUS_SUCCESS:
            return self._mark_verified(r1), extra
        return self._blocked(
            r1.func, args, text[:200] or "shell 执行未确认"
        ), extra

    def _check_uninstall(
        self, args: dict, r1: R1Response, extra: list[dict]
    ) -> tuple[R1Response, list[dict]]:
        from ...tools.package_ops import is_package_installed

        package = str(
            args.get("package") or args.get("name") or args.get("app_name") or ""
        ).strip()
        if not package:
            return self._mark_verified(r1), extra
        if not is_package_installed(package):
            return self._mark_verified(r1), extra
        return self._blocked(
            r1.func,
            args,
            f"未能确认「{package}」已完全卸载。",
        ), extra
