"""
光荣进化系统 - 数据模型
MIA 风格的记忆条目定义 + Agent Loop 状态机

v2.0: MemoryType 枚举完整 — CONSOLIDATED_RULE + INSIGHT
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryType(str, Enum):
    """记忆类型 — MIA 蓝图"""
    PROCEDURAL = "procedural"
    DECLARATIVE = "declarative"
    EPISODIC = "episodic"
    CONSOLIDATED_RULE = "consolidated_rule"
    INSIGHT = "insight"


class Judgement(str, Enum):
    """评判状态 — MIA 胜率体系"""
    PENDING = "pending"
    CORRECT = "correct"
    INCORRECT = "incorrect"


@dataclass
class MemoryEntry:
    """MIA 风格的记忆条目。"""
    id: str = ""
    memory_type: MemoryType = MemoryType.PROCEDURAL
    category: str = "general"
    question: str = ""
    content: str = ""
    trajectory: str = ""
    rules: str = ""
    judgement: Judgement = Judgement.PENDING
    usage_count: int = 0
    success_count: int = 0
    win_rate: float = 0.0
    embedding: Optional[List[float]] = None
    tags: List[str] = field(default_factory=list)
    related_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"MEM-{id(self) % 1000:03d}"

    def to_db_dict(self) -> Dict[str, Any]:
        embedding_str = None
        if self.embedding is not None:
            try:
                import numpy as np
                arr = np.array(self.embedding, dtype=np.float32)
                embedding_str = json.dumps(arr.tolist())
            except ImportError:
                embedding_str = json.dumps(self.embedding)
        return {
            "id": self.id,
            "memory_type": self.memory_type.value,
            "category": self.category,
            "question": self.question,
            "content": self.content,
            "trajectory": self.trajectory,
            "rules": self.rules,
            "judgement": self.judgement.value,
            "usage_count": self.usage_count,
            "success_count": self.success_count,
            "win_rate": self.win_rate,
            "embedding": embedding_str,
            "tags": json.dumps(self.tags),
            "related_ids": json.dumps(self.related_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "MemoryEntry":
        embedding = None
        if row.get("embedding"):
            try:
                embedding = json.loads(row["embedding"])
            except (json.JSONDecodeError, TypeError):
                embedding = None
        tags = []
        if row.get("tags"):
            try:
                tags = json.loads(row["tags"])
            except (json.JSONDecodeError, TypeError):
                tags = []
        related_ids = []
        if row.get("related_ids"):
            try:
                related_ids = json.loads(row["related_ids"])
            except (json.JSONDecodeError, TypeError):
                related_ids = []
        return cls(
            id=row["id"],
            memory_type=MemoryType(row.get("memory_type", "procedural")),
            category=row.get("category", "general"),
            question=row.get("question", ""),
            content=row.get("content", ""),
            trajectory=row.get("trajectory", ""),
            rules=row.get("rules", ""),
            judgement=Judgement(row.get("judgement", "pending")),
            usage_count=row.get("usage_count", 0),
            success_count=row.get("success_count", 0),
            win_rate=row.get("win_rate", 0.0),
            embedding=embedding,
            tags=tags,
            related_ids=related_ids,
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


class Phase(str, Enum):
    BUILD_PLAN = "BUILD_PLAN"
    EXECUTING = "EXECUTING"
    JUDGING = "JUDGING"
    REPLANNING = "REPLANNING"
    EXECUTING_REPLAN = "EXECUTING_REPLAN"
    DONE = "DONE"
    FAILED = "FAILED"


class Action(str, Enum):
    BUILD_PLAN = "BUILD_PLAN"
    EXECUTE_PLAN = "EXECUTE_PLAN"
    JUDGE_RESULT = "JUDGE_RESULT"
    BUILD_REPLAN = "BUILD_REPLAN"
    EXECUTE_REPLAN = "EXECUTE_REPLAN"
    FINISH = "FINISH"


PHASE_TRANSITIONS = {
    Phase.BUILD_PLAN: Phase.EXECUTING,
    Phase.EXECUTING: Phase.JUDGING,
    Phase.REPLANNING: Phase.EXECUTING_REPLAN,
    Phase.EXECUTING_REPLAN: Phase.JUDGING,
}

PHASE_TO_ACTION = {
    Phase.BUILD_PLAN: Action.BUILD_PLAN,
    Phase.EXECUTING: Action.EXECUTE_PLAN,
    Phase.JUDGING: Action.JUDGE_RESULT,
    Phase.REPLANNING: Action.BUILD_REPLAN,
    Phase.EXECUTING_REPLAN: Action.EXECUTE_REPLAN,
    Phase.DONE: Action.FINISH,
    Phase.FAILED: Action.FINISH,
}


@dataclass
class AgentLoopState:
    goal: str = ""
    max_iterations: int = 3
    iteration: int = 0
    phase: Phase = Phase.BUILD_PLAN
    action: Action = Action.BUILD_PLAN
    plan: str = ""
    execution_trace: str = ""
    result: str = ""
    error: str = ""
    done: bool = False
    used_memory_ids: List[str] = field(default_factory=list)
    used_neg_memory_ids: List[str] = field(default_factory=list)
    used_memory_snippets: Dict[str, str] = field(default_factory=dict)

    def to_display(self) -> str:
        return (
            f"Goal: {self.goal}\n"
            f"Iteration: {self.iteration}/{self.max_iterations}\n"
            f"Phase: {self.phase.value}\n"
            f"Action: {self.action.value}\n"
            f"Done: {self.done}\n"
            f"Used memories: {len(self.used_memory_ids)} pos + {len(self.used_neg_memory_ids)} neg"
        )
