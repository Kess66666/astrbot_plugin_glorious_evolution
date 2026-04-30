"""
光荣进化系统 - 数据模型
MIA 风格的记忆条目定义
"""

import json
from dataclasses import dataclass, field
import secrets
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryType(str, Enum):
    """记忆类型 — MIA 蓝图"""
    PROCEDURAL = "procedural"
    DECLARATIVE = "declarative"
    EPISODIC = "episodic"


class Judgement(str, Enum):
    """评判状态 — MIA 胜率体系"""
    PENDING = "pending"
    CORRECT = "correct"
    INCORRECT = "incorrect"


@dataclass
class MemoryEntry:
    """
    MIA 风格的记忆条目。
    """
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
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            rand = secrets.token_hex(3)
            self.id = f"MEM-{ts}-{rand}"

    def update_win_rate(self, success: bool, decay_factor: float = 0.0) -> None:
        """更新胜率（默认使用 MIA 简单比值，与 MemoryManager 保持一致）。"""
        self.usage_count += 1
        if success:
            self.success_count += 1
        safe_usage = max(1, self.usage_count)
        if decay_factor > 0:
            self.win_rate = (
                decay_factor * self.win_rate
                + (1 - decay_factor) * (self.success_count / safe_usage)
            )
        else:
            self.win_rate = self.success_count / safe_usage
        self.updated_at = datetime.now().isoformat()

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
