"""可取消子进程注册表 — 任务取消时终止关联子进程。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_processes: dict[str, list[subprocess.Popen[Any]]] = {}
_local = threading.local()


def set_current_task(task_id: str | None) -> None:
  _local.task_id = (task_id or "").strip() or None


def get_current_task() -> str | None:
  return getattr(_local, "task_id", None)


def register(process: subprocess.Popen[Any], *, task_id: str | None = None) -> None:
  # PROCESS_REGISTRY_V1_4_1: no silent _global fallback. Unowned processes
  # go to _unowned, which cancel_task() and cancel_all() do NOT kill by default.
  tid = task_id or get_current_task()
  if not tid:
    tid = "_unowned"
    logger.warning("ProcessRegistry 注册未归属任务的子进程 pid=%s，无法按任务取消", getattr(process, "pid", "?"))
  with _lock:
    _processes.setdefault(tid, []).append(process)


def unregister(process: subprocess.Popen[Any], *, task_id: str | None = None) -> None:
  tid = task_id or get_current_task()
  with _lock:
    bucket = _processes.get(tid) if tid else None
    if bucket and process in bucket:
      bucket.remove(process)
      if not bucket:
        _processes.pop(tid, None)
      return
    # PROCESS_REGISTRY_V1_4_1: register/unregister may run in different threads
    # (e.g. guard thread), so search all buckets before giving up.
    for key, b in list(_processes.items()):
      if process in b:
        b.remove(process)
        if not b:
          _processes.pop(key, None)
        return


def cancel_task(task_id: str) -> int:
  """终止任务关联子进程，返回树已清的注册进程数。

  PROCESS_TREE_V1_4_4: 此前返回“根被终止的进程数”，本件改为
  “根已死且子孙无残留的注册进程数”，因为只有树已清才算真正无孤儿。
  详细的 PID 列表见 cancel_task_detailed()。
  """
  detailed = cancel_task_detailed(task_id)
  return int(detailed.get("clean_roots", 0))


def cancel_task_detailed(task_id: str) -> dict:
  """终止任务关联子进程，返回按 PID 细分的树杀结果。

  返回 keys：task_id / roots / clean_roots / killed_pids / exited_pids /
  remaining_pids / failed_pids / tree_pids。
  killed = 杀前存活、杀后已死；exited = 杀前已死；remaining = 杀后仍存活；
  failed 与 remaining 相同（杀失败即残留），便于调用方直接定位。
  """
  tid = (task_id or "").strip()
  # AUTHOR_TODOS_4_3_4_4_V1: early-return dict includes all keys.
  empty: dict = {
    "task_id": tid,
    "roots": [],
    "clean_roots": 0,
    "killed_pids": [],
    "exited_pids": [],
    "remaining_pids": [],
    "failed_pids": [],
    "tree_pids": [],
    "snapshot_failed": False,
    "unverified_roots": [],
  }
  if not tid:
    return empty
  with _lock:
    procs = _processes.pop(tid, [])
  killed: list[int] = []
  exited: list[int] = []
  remaining: list[int] = []
  tree: list[int] = []
  roots: list[int] = []
  clean = 0
  unverified: list[int] = []
  for proc in procs:
    res = _terminate_process_tree(proc)
    pid = res.get("pid")
    if isinstance(pid, int):
      roots.append(pid)
    tree.extend(res.get("tree_pids") or [])
    killed.extend(res.get("killed_pids") or [])
    exited.extend(res.get("exited_pids") or [])
    remaining.extend(res.get("remaining_pids") or [])
    if res.get("snapshot_failed") and isinstance(pid, int):
      unverified.append(pid)
    if (
      not res.get("remaining_pids")
      and not res.get("snapshot_failed")
      and not res.get("refused")
      and res.get("root_status") in ("killed", "exited")
    ):
      clean += 1
  out = {
    "task_id": tid,
    "roots": roots,
    "clean_roots": clean,
    "killed_pids": killed,
    "exited_pids": exited,
    "remaining_pids": remaining,
    "failed_pids": list(remaining),
    "tree_pids": tree,
    "snapshot_failed": bool(unverified),
    "unverified_roots": unverified,
  }
  if killed or remaining:
    logger.info(
      "ProcessRegistry 取消任务 %s，树已清 %s/%s，杀死 %s，残留 %s",
      tid, clean, len(roots), killed, remaining,
    )
  return out


def cancel_all(*, include_unowned: bool = False) -> int:
  # PROCESS_REGISTRY_V1_4_1: _unowned is NOT killed by default, to avoid
  # accidental global kill. Use cancel_unowned() explicitly if intended.
  with _lock:
    keys = [k for k in _processes.keys() if include_unowned or k != "_unowned"]
  return sum(cancel_task(k) for k in keys)


def cancel_all_detailed(*, include_unowned: bool = False) -> dict:
  """按任务聚合 cancel_task_detailed()，include_unowned 语义与 cancel_all() 一致。"""
  with _lock:
    keys = [k for k in _processes.keys() if include_unowned or k != "_unowned"]
  per_task: dict = {}
  killed: list[int] = []
  exited: list[int] = []
  remaining: list[int] = []
  tree: list[int] = []
  clean = 0
  roots: list[int] = []
  for k in keys:
    d = cancel_task_detailed(k)
    per_task[k] = d
    killed.extend(d.get("killed_pids") or [])
    exited.extend(d.get("exited_pids") or [])
    remaining.extend(d.get("remaining_pids") or [])
    tree.extend(d.get("tree_pids") or [])
    roots.extend(d.get("roots") or [])
    clean += int(d.get("clean_roots", 0))
  return {
    "per_task": per_task,
    "roots": roots,
    "clean_roots": clean,
    "killed_pids": killed,
    "exited_pids": exited,
    "remaining_pids": remaining,
    "failed_pids": list(remaining),
    "tree_pids": tree,
  }


def cancel_unowned() -> int:
  return cancel_task("_unowned")


def list_task_ids() -> list[str]:
  with _lock:
    return list(_processes.keys())


def count_processes(task_id: str) -> int:
  tid = (task_id or "").strip()
  if not tid:
    return 0
  with _lock:
    return len(_processes.get(tid, []))


def _snapshot_process_map() -> dict[int, tuple[int, str]]:
  """Windows Toolhelp32 快照：返回 {pid: (parent_pid, exe_name)}。

  PROCESS_TREE_V1_4_4: 非 Windows 或快照失败时返回空 dict，调用方退化为
  仅杀根 Popen，绝不因此崩溃。父 PID 字段在 process_ops.py 已有先例，
  本件保留它用于向下求子孙（此前 process_ops 只存了进程名，丢了父 PID）。
  """
  if sys.platform != "win32":
    return {}
  try:
    import ctypes
    from ctypes import wintypes

    class _PE32(ctypes.Structure):
      _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        # PROCESS_TREE_V2_4_4: ULONG_PTR is pointer-sized, c_size_t (8 on x64, 4 on x86),
        # not c_ulong (always 4). c_ulong gave 556 bytes vs real 568 on x64 and
        # Process32FirstW failed, snapshot returned 0 entries. See process_ops.py:107.
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
      ]

    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
      return {}
    try:
      entry = _PE32()
      entry.dwSize = ctypes.sizeof(_PE32)
      # PROCESS_TREE_V2_4_4: fail fast on wrong struct size instead of silently
      # returning an empty snapshot that would be misread as "no descendants".
      _expected_size = 568 if sys.maxsize > 2 ** 32 else 556
      if ctypes.sizeof(_PE32) != _expected_size:
        logger.error(
          "ProcessRegistry 快照结构体大小异常：%s vs 预期 %s，拒绝快照",
          ctypes.sizeof(_PE32), _expected_size,
        )
        return {}
      ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
      if not ok:
        logger.debug("ProcessRegistry Process32FirstW 失败，快照为空")
      out: dict[int, tuple[int, str]] = {}
      while ok:
        out[int(entry.th32ProcessID)] = (
          int(entry.th32ParentProcessID),
          str(entry.szExeFile or ""),
        )
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
      return out
    finally:
      kernel32.CloseHandle(snap)
  except Exception:
    logger.debug("ProcessRegistry 快照失败，退化为仅杀根", exc_info=True)
    return {}


def _self_ancestor_pids(proc_map: dict[int, tuple[int, str]]) -> set[int]:
  """自身及祖先 PID 集合（沿父 PID 上溯最多 24 层，止于 PID<=4 或成环）。"""
  out: set[int] = set()
  cur = os.getpid()
  for _ in range(24):
    if cur <= 4 or cur in out:
      break
    out.add(cur)
    parent = proc_map.get(cur, (0, ""))[0] if proc_map else 0
    if not parent or parent <= 4:
      break
    cur = parent
  return out


def _snapshot_tree(root_pid: int, proc_map: dict[int, tuple[int, str]]) -> list[int]:
  """从根 PID 向下求子孙（含根），排除自身祖先与 PID<=4。"""
  if not proc_map:
    return [root_pid] if root_pid else []
  children: dict[int, list[int]] = {}
  for pid, (parent, _name) in proc_map.items():
    children.setdefault(parent, []).append(pid)
  excluded = _self_ancestor_pids(proc_map)
  out: list[int] = []
  stack = [root_pid]
  seen: set[int] = set()
  while stack:
    cur = stack.pop()
    if cur in seen or cur <= 4 or cur in excluded:
      continue
    seen.add(cur)
    out.append(cur)
    stack.extend(children.get(cur, []))
  return out


def _is_pid_alive(pid: int, exe_name: str = "") -> bool:
  """判断 PID 是否仍存活（同 PID 但 exe 名变了视为原进程已死，即 PID 复用）。"""
  if not pid or pid <= 4:
    return False
  proc_map = _snapshot_process_map()
  if not proc_map:
    # PROCESS_TREE_V2_4_4: 快照失败时绝不能返回 False（那会把活进程误判为已死，
    # 导致谎称树已清）。非 Windows 时调用方不靠本函数复核子孙，故回 False；
    # Windows 下快照失败一律保守地回 True（假设仍活着），让调用方去 taskkill
    # 并计入 remaining，而不是谎称已死。
    return False if sys.platform != "win32" else True
  if pid not in proc_map:
    return False
  if exe_name and proc_map[pid][1] and proc_map[pid][1].lower() != exe_name.lower():
    return False
  return True


def _taskkill_tree(pid: int) -> bool:
  """Windows taskkill /PID <pid> /T /F，返回码 0 或 128（已不存在）视为成功。"""
  if sys.platform != "win32" or not pid or pid <= 4:
    return False
  try:
    r = subprocess.run(
      ["taskkill", "/PID", str(pid), "/T", "/F"],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      timeout=10,
      text=True,
    )
    if r.returncode in (0, 128):
      return True
    logger.debug("taskkill /T 失败 pid=%s code=%s out=%s", pid, r.returncode, (r.stdout or "")[:200])
    return False
  except Exception:
    logger.debug("taskkill 调用失败 pid=%s", pid, exc_info=True)
    return False


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> dict:
  """杀整棵树并复核，返回按 PID 细分的 dict（见 cancel_task_detailed）。"""
  pid = getattr(proc, "pid", None)
  if not isinstance(pid, int) or not pid:
    return {
      "pid": pid,
      "root_status": "failed",
      "tree_pids": [],
      "killed_pids": [],
      "exited_pids": [],
      "remaining_pids": [],
      "failed_pids": [],
      "reason": "no pid",
      "snapshot_failed": False,
      "tree_verified": False,
    }
  root_exited_before = proc.poll() is not None
  proc_map = _snapshot_process_map()
  # PROCESS_TREE_V2_4_4: Windows 下快照为空即是快照失败，不是“没有子孙”。
  # 此前代码把空快照当成 tree=[pid]，导致 taskkill 从未被调用、remaining 恒为空、
  # 谎称 clean_roots=1。快照失败时必须标记 unverified，且不得计入 clean。
  snapshot_failed = sys.platform == "win32" and not proc_map
  exe_map = {p: name for p, (_par, name) in proc_map.items()} if proc_map else {}
  tree = _snapshot_tree(pid, proc_map) if proc_map else [pid]
  # 根本身若在排除集内（自身祖先 / PID<=4），直接拒绝，不杀。
  excluded = _self_ancestor_pids(proc_map) if proc_map else set()
  if pid in excluded or pid <= 4:
    return {
      "pid": pid,
      "root_status": "refused",
      "refused": True,
      "tree_pids": tree,
      "killed_pids": [],
      "exited_pids": [pid] if root_exited_before else [],
      "remaining_pids": list(tree) if tree else [pid],
      "failed_pids": list(tree) if tree else [pid],
      "reason": "refused: root is self ancestor or system pid",
      "snapshot_failed": snapshot_failed,
      "tree_verified": not snapshot_failed,
    }
  # 先按原逻辑杀根（terminate 等 2 秒，不行再 kill 等 2 秒）。
  root_killed_now = False
  if not root_exited_before:
    try:
      proc.terminate()
      proc.wait(timeout=2)
      root_killed_now = proc.poll() is not None
    except Exception:
      try:
        proc.kill()
        proc.wait(timeout=2)
        root_killed_now = proc.poll() is not None
      except Exception:
        logger.debug("无法终止子进程 pid=%s", pid, exc_info=True)
        root_killed_now = False
  # 再对快照中仍存活的子孙用 taskkill /T 补杀（根已死时子孙已成孤儿，
  # 仍可按快照 PID 逐个杀；根未死时 taskkill /T 会连根带树一起杀）。
  if sys.platform == "win32":
    if proc_map:
      for child_pid in list(tree):
        if child_pid == pid:
          continue
        if _is_pid_alive(child_pid, exe_map.get(child_pid, "")):
          _taskkill_tree(child_pid)
      if not root_exited_before and proc.poll() is None:
        _taskkill_tree(pid)
    elif snapshot_failed:
      # 快照失败时仅在根未退出时尽力对根调一次 taskkill /T（避免对已死根 PID 盲杀），
      # 杀完仍标记 unverified，不得谎称已清。
      if not root_exited_before and proc.poll() is None:
        _taskkill_tree(pid)
  # 杀后复核：以新快照为准，exe 名变了视为 PID 复用、原进程已死。
  killed: list[int] = []
  remaining: list[int] = []
  exited: list[int] = []
  if root_exited_before:
    exited.append(pid)
  if sys.platform == "win32" and proc_map:
    for tree_pid in tree:
      if tree_pid == pid:
        if proc.poll() is not None and not root_exited_before:
          killed.append(tree_pid)
        elif root_exited_before:
          pass  # 已计入 exited，不重复计入 killed
        elif proc.poll() is None:
          remaining.append(tree_pid)
        else:
          killed.append(tree_pid)
      else:
        if _is_pid_alive(tree_pid, exe_map.get(tree_pid, "")):
          remaining.append(tree_pid)
        else:
          killed.append(tree_pid)
  else:
    # 非 Windows 或快照失败：只能按 Popen.poll() 复核根，子孙无法复核，
    # 此时不谎称树已清——若树中除根外还有其他 PID，计入 remaining。
    if proc.poll() is not None and not root_exited_before:
      killed.append(pid)
    elif proc.poll() is None:
      remaining.append(pid)
    for tree_pid in tree:
      if tree_pid != pid:
        remaining.append(tree_pid)
  root_status = "exited" if root_exited_before else ("killed" if pid in killed else "failed")
  return {
    "pid": pid,
    "root_status": root_status,
    "tree_pids": tree,
    "killed_pids": killed,
    "exited_pids": exited,
    "remaining_pids": remaining,
    "failed_pids": list(remaining),
    "reason": (
      "snapshot failed, tree unverified"
      if snapshot_failed
      else ("" if not remaining else "remaining after tree kill")
    ),
    "snapshot_failed": snapshot_failed,
    "tree_verified": not snapshot_failed,
  }


def _terminate_process(proc: subprocess.Popen[Any]) -> bool:
  """保持原签名：仅当根已死且子孙无残留（树已清）时返回 True。

  PROCESS_TREE_V1_4_4: 此前已退出返回 False，本件改为已退出且无残留时
  返回 True，因为此时已无孤儿；已退出但子孙仍存活时仍返回 False。
  """
  res = _terminate_process_tree(proc)
  return (
    not res.get("snapshot_failed")
    and not res.get("remaining_pids")
    and res.get("root_status") in ("killed", "exited")
  )
