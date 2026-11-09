"""Memory 子系统。

[架构状态说明 (生产冻结)]
本模块已通过对抗性独立审计与七项核心断点整改，处于生产架构冻结状态。
所有接管本代码库的 AI 助手与开发者必须严格遵守以下红线：
1. 四库物理边界：仅允许使用 kekesi.db, cognition.db, episodes_cases.db, perception_events.db，严禁新建第五库。
2. 统一检索闭环：检索入口统一走 MemoryRetrieval.recall_context_hits，严禁破坏 5 级关键词分级与 RRF 融合。
3. 双层经验分级：严禁下调 CaseStore.PROMOTION_MIN_DATES=3 门槛，暂存经验必须低置信度只读注入。
4. 自主学习准入：候选知识必须在 candidate 隔离区经冒烟测试并由用户显式审批，严禁自动晋升。
5. 工作流受控调度：高危工具必须经过 user_confirmed 门禁与 Verifier 校验。
6. 全局认知同步：世界模型更新要求 event_count >= 2 多证据门禁，矛盾事实保留 CONFLICTED 标记。
7. 审计诊断只读：memory_audit 质量分析为纯只读，严禁动态回写或篡改底层检索权重。

详见规范文档：
- 根目录: AGENTS.md
- 记忆模块: kekesi/memory/README.md
"""

from .db import Database, resolve_memory_db_path
from .evolver import Evolver
from .execution_history import ExecutionHistoryStore
from .learning import LearningService
from .policies import (
    ALL_LAYERS,
    DEFAULT_POLICIES,
    LAYER_EXPERIENCE,
    LAYER_PREFERENCE,
    LAYER_PROFILE,
    LAYER_SESSION,
    LAYER_TASK_HISTORY,
    LAYER_WORKING,
    LayerPolicy,
    apply_layer_policy,
    compute_importance,
    get_policy,
    policies_from_config,
)
from .qs_store import QsStore
from .retrieval import MemoryRetrieval
from .service import MemoryService
from .vector_chunks import VectorChunkStore, split_text

__all__ = [
    # db
    "Database",
    "resolve_memory_db_path",
    "ExecutionHistoryStore",
    # service
    "MemoryService",
    "VectorChunkStore",
    "split_text",
    "LearningService",
    "QsStore",
    "Evolver",
    # retrieval
    "MemoryRetrieval",
    # policies
    "LayerPolicy",
    "DEFAULT_POLICIES",
    "ALL_LAYERS",
    "LAYER_WORKING",
    "LAYER_SESSION",
    "LAYER_PROFILE",
    "LAYER_PREFERENCE",
    "LAYER_TASK_HISTORY",
    "LAYER_EXPERIENCE",
    "get_policy",
    "policies_from_config",
    "apply_layer_policy",
    "compute_importance",
]
