"""Kekesi v2.0 控制层 — 执行、验证、结果协议。"""



from .cancellation import CancellationToken, merge_cancel_checks

from .event_bus import EventBus

from .execution_result import (

    ExecutionResult,

    can_report_success,

    from_r1_response,

    from_tuple,

    result_to_summary,

)

from .exec_locks import (

    DOMAIN_DEFAULT,

    DOMAIN_FILE,

    DOMAIN_MEDIA,

    DOMAIN_NETWORK,

    DOMAIN_NONE,

    DOMAIN_SYSTEM,

    DOMAIN_UI,

    domain_lock,

    execution_domain,

)

from .executor import Executor, execute_pipeline

from .metrics import RuntimeMetrics, bind_runtime_metrics, get_runtime_metrics

from .planner import Planner

from .recovery import Recovery

from .runtime import KekesiRuntime

from .supervisor import ResponsePolicy, Supervisor as RuntimeSupervisor

from .task_store import TaskStore

from .verifier import Verifier, needs_verification



__all__ = [

    "CancellationToken",

    "DOMAIN_DEFAULT",

    "DOMAIN_FILE",

    "DOMAIN_MEDIA",

    "DOMAIN_NETWORK",

    "DOMAIN_NONE",

    "DOMAIN_SYSTEM",

    "DOMAIN_UI",

    "EventBus",

    "ExecutionResult",

    "Executor",

    "KekesiRuntime",

    "Planner",

    "Recovery",

    "RuntimeMetrics",

    "ResponsePolicy",

    "RuntimeSupervisor",

    "TaskStore",

    "Verifier",

    "can_report_success",

    "domain_lock",

    "execute_pipeline",

    "execution_domain",

    "from_r1_response",

    "from_tuple",

    "get_runtime_metrics",

    "merge_cancel_checks",

    "needs_verification",

    "result_to_summary",

]

