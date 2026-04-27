"""
光荣进化系统 - 存储层
SQLite + FTS5 全文搜索 + 向量存储
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement


class Storage:
    """
    SQLite 存储层，支持 FTS5 全文搜索。
    线程安全：每个操作通过 threading.Lock 串行化。
    """

    def __init__(self, data_dir: str) -> None:
        self.db_path = os.path.join(data_dir, "evolution.db")
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        memory_type TEXT NOT NULL DEFAULT 'procedural',
                        category TEXT NOT NULL DEFAULT 'general',
                        question TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL DEFAULT '',
                        trajectory TEXT NOT NULL DEFAULT '',
                        rules TEXT NOT NULL DEFAULT '',
                        judgement TEXT NOT NULL DEFAULT 'pending',
                        usage_count INTEGER NOT NULL DEFAULT 0,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        win_rate REAL NOT NULL DEFAULT 0.0,
                        embedding TEXT,
                        tags TEXT NOT NULL DEFAULT '[]',
                        related_ids TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT ''
                    )
                """)
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(
                        question, content, category,
                        content='memories',
                        content_rowid='rowid'
                    )
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                    BEGIN
                        INSERT INTO memories_fts(rowid, question, content, category)
                        VALUES (new.rowid, new.question, new.content, new.category);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                    BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, question, content, category)
                        VALUES ('delete', old.rowid, old.question, old.content, old.category);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
                    BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, question, content, category)
                        VALUES ('delete', old.rowid, old.question, old.content, old.category);
                        INSERT INTO memories_fts(rowid, question, content, category)
                        VALUES (new.rowid, new.question, new.content, new.category);
                    END
                """)
                conn.commit()
            finally:
                conn.close()

    async def add_entry(self, entry: MemoryEntry) -> str:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                d = entry.to_db_dict()
                conn.execute(
                    """INSERT INTO memories
                       (id, memory_type, category, question, content,
                        trajectory, rules, judgement, usage_count, success_count,
                        win_rate, embedding, tags, related_ids, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (d["id"], d["memory_type"], d["category"], d["question"],
                     d["content"], d["trajectory"], d["rules"], d["judgement"],
                     d["usage_count"], d["success_count"], d["win_rate"],
                     d["embedding"], d["tags"], d["related_ids"],
                     d["created_at"], d["updated_at"]),
                )
                conn.commit()
                return entry.id
            finally:
                conn.close()

    async def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM memories WHERE id = ?", (entry_id,))
                row = cursor.fetchone()
                return MemoryEntry.from_db_row(dict(row)) if row else None
            finally:
                conn.close()

    async def update_entry(self, entry_id: str, **kwargs: Any) -> bool:
        if not kwargs:
            return False
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                values = list(kwargs.values()) + [entry_id]
                cursor = conn.execute(f"UPDATE memories SET {sets} WHERE id = ?", values)
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def delete_entry(self, entry_id: str) -> bool:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def search_entries(self, query: str, top_k: int = 5,
                             memory_type: Optional[str] = None,
                             min_win_rate: float = 0.0) -> List[MemoryEntry]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                where_clauses = []
                params: list[Any] = []
                if memory_type:
                    where_clauses.append("memory_type = ?")
                    params.append(memory_type)
                if min_win_rate > 0:
                    where_clauses.append("win_rate >= ?")
                    params.append(min_win_rate)
                where_sql = "AND " + " AND ".join(where_clauses) if where_clauses else ""
                cursor = conn.execute(
                    f"""SELECT m.* FROM memories m
                        JOIN memories_fts fts ON m.rowid = fts.rowid
                        WHERE memories_fts MATCH ? {where_sql}
                        ORDER BY rank LIMIT ?""",
                    params + [query, top_k],
                )
                return [MemoryEntry.from_db_row(dict(r)) for r in cursor.fetchall()]
            except Exception as e:
                logger.error(f"[Glorious Evolution] FTS 搜索失败: {e}")
                return []
            finally:
                conn.close()

    async def get_all_entries(self, limit: int = 1000) -> List[MemoryEntry]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,))
                return [MemoryEntry.from_db_row(dict(r)) for r in cursor.fetchall()]
            finally:
                conn.close()

    async def get_statistics(self) -> Dict[str, Any]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                total = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
                avg_wr = conn.execute("SELECT AVG(win_rate) as avg FROM memories").fetchone()["avg"] or 0
                by_type = {}
                for row in conn.execute("SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"):
                    by_type[row["memory_type"]] = row["cnt"]
                by_judgement = {}
                for row in conn.execute("SELECT judgement, COUNT(*) as cnt FROM memories GROUP BY judgement"):
                    by_judgement[row["judgement"]] = row["cnt"]
                top_wr = []
                for row in conn.execute("SELECT id, win_rate, usage_count FROM memories ORDER BY win_rate DESC LIMIT 5"):
                    top_wr.append({"id": row["id"], "win_rate": row["win_rate"], "usage_count": row["usage_count"]})
                return {"total_memories": total, "avg_win_rate": avg_wr, "by_type": by_type,
                        "by_judgement": by_judgement, "top_win_rate": top_wr}
            finally:
                conn.close()
