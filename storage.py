"""
光荣进化系统 - 存储层
SQLite + FTS5 全文搜索 + 向量存储

v1.0.32: FTS5 保留关键词过滤 (NOT/AND/OR/NEAR)
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement

_ALLOWED_UPDATE_FIELDS = frozenset({
    "question", "content", "memory_type", "category", "judgement",
    "win_rate", "usage_count", "success_count", "trajectory", "rules",
    "embedding", "tags", "related_ids", "updated_at",
})


class Storage:
    """
    SQLite 存储层，支持 FTS5 全文搜索。

    线程安全：所有写操作通过 _lock 串行化。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        logger.info(f"[GE] Storage db_path={db_path}, exists={os.path.exists(db_path)}")
        self._init_db()

    def _init_db(self) -> None:
        logger.info(f"[GE] _init_db connecting to: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL DEFAULT 'declarative',
                    category TEXT NOT NULL DEFAULT 'general',
                    judgement TEXT NOT NULL DEFAULT 'pending',
                    win_rate REAL NOT NULL DEFAULT 0.5,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    trajectory TEXT NOT NULL DEFAULT '',
                    rules TEXT NOT NULL DEFAULT '',
                    embedding TEXT,
                    tags TEXT NOT NULL DEFAULT '[]',
                    related_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    question, content, content=memories, content_rowid=rowid
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, question, content)
                    VALUES (new.rowid, new.question, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, question, content)
                    VALUES ('delete', old.rowid, old.question, old.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, question, content)
                    VALUES ('delete', old.rowid, old.question, old.content);
                    INSERT INTO memories_fts(rowid, question, content)
                    VALUES (new.rowid, new.question, new.content);
                END;

                CREATE TABLE IF NOT EXISTS distilled_rules (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_memory_ids TEXT NOT NULL DEFAULT '[]',
                    avg_win_rate REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def get_max_id_counter(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM memories WHERE id LIKE 'MEM-%' ORDER BY id DESC LIMIT 100"
            ).fetchall()
            max_counter = 0
            for (entry_id,) in row:
                try:
                    parts = entry_id.split("-")
                    if len(parts) == 3:
                        counter = int(parts[2])
                        if counter > max_counter:
                            max_counter = counter
                except (ValueError, IndexError):
                    continue
            return max_counter
        finally:
            conn.close()

    async def insert_entry(self, entry: MemoryEntry) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    """INSERT INTO memories
                       (id, question, content, memory_type, category, judgement,
                        win_rate, usage_count, success_count, trajectory, rules,
                        embedding, tags, related_ids, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        entry.id, entry.question, entry.content,
                        entry.memory_type.value, entry.category, entry.judgement.value,
                        entry.win_rate, entry.usage_count, entry.success_count,
                        entry.trajectory, entry.rules,
                        entry.to_db_dict().get("embedding"),
                        json.dumps(entry.tags), json.dumps(entry.related_ids),
                        entry.created_at, entry.updated_at,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.IntegrityError:
                logger.warning(f"[GE] INSERT 冲突，记忆已存在: {entry.id}")
                conn.rollback()
                return False
            finally:
                conn.close()

    async def add_entry(self, entry: MemoryEntry) -> str:
        ok = await self.insert_entry(entry)
        if not ok:
            logger.error(f"[GE] add_entry 失败: {entry.id} 可能已存在")
        return entry.id

    async def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM memories WHERE id = ?", (entry_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                return MemoryEntry.from_db_row(dict(row))
            finally:
                conn.close()

    async def update_entry(self, entry_id: str, **fields) -> bool:
        if not fields:
            return False
        invalid_keys = set(fields.keys()) - _ALLOWED_UPDATE_FIELDS
        if invalid_keys:
            logger.error(f"[GE] update_entry 非法字段: {invalid_keys}")
            return False
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                set_clauses = []
                params = []
                for key, value in fields.items():
                    set_clauses.append(f"{key} = ?")
                    params.append(value)
                params.append(datetime.now().isoformat())
                params.append(entry_id)
                sql = f"UPDATE memories SET {', '.join(set_clauses)}, updated_at = ? WHERE id = ?"
                cursor = conn.execute(sql, params)
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def delete_entry(self, entry_id: str) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def update_judgement(self, entry_id: str, judgement: Judgement) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    "UPDATE memories SET judgement = ?, updated_at = ? WHERE id = ?",
                    (judgement.value, datetime.now().isoformat(), entry_id),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """净化 FTS5 查询字符串。
        v1.0.32: 过滤 FTS5 保留关键字 (NOT/AND/OR/NEAR)，短语查询包裹。
        """
        import re
        safe = query.replace('"', '').replace("'", '')
        reserved = {'not', 'and', 'or', 'near'}
        terms = []
        for t in safe.split():
            if t.lower() in reserved:
                continue
            terms.append(t)
        if not terms:
            return '""'
        return '"' + ' '.join(terms) + '"'

    async def search_entries(
        self,
        query: str,
        top_k: int = 5,
        memory_type: Optional[str] = None,
        min_win_rate: float = 0.0,
    ) -> List[MemoryEntry]:
        async with self._lock:
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
                where_sql = ""
                if where_clauses:
                    where_sql = "AND " + " AND ".join(where_clauses)
                safe_query = self._sanitize_fts5_query(query)
                cursor = conn.execute(
                    f"""SELECT m.* FROM memories m
                        JOIN memories_fts fts ON m.rowid = fts.rowid
                        WHERE memories_fts MATCH ? {where_sql}
                        ORDER BY rank
                        LIMIT ?""",
                    [safe_query] + params + [top_k],
                )
                rows = cursor.fetchall()
                return [MemoryEntry.from_db_row(dict(r)) for r in rows]
            except Exception as e:
                logger.error(f"[GE] FTS 搜索失败: {e}")
                return []
            finally:
                conn.close()

    async def get_all_memories(self, limit: int = 1000) -> List[MemoryEntry]:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = cursor.fetchall()
                return [MemoryEntry.from_db_row(dict(r)) for r in rows]
            finally:
                conn.close()

    async def get_memories_by_type(
        self,
        memory_type: str,
        limit: int = 500,
        order_by: str = "created_at DESC",
    ) -> List[MemoryEntry]:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                allowed_orders = {
                    "created_at DESC", "created_at ASC",
                    "win_rate DESC", "win_rate ASC",
                    "usage_count DESC", "usage_count ASC",
                    "updated_at DESC", "updated_at ASC",
                }
                if order_by not in allowed_orders:
                    order_by = "created_at DESC"
                cursor = conn.execute(
                    f"SELECT * FROM memories WHERE memory_type = ? ORDER BY {order_by} LIMIT ?",
                    (memory_type, limit),
                )
                rows = cursor.fetchall()
                return [MemoryEntry.from_db_row(dict(r)) for r in rows]
            finally:
                conn.close()

    async def get_entries_by_ids(self, entry_ids: List[str]) -> Dict[str, MemoryEntry]:
        if not entry_ids:
            return {}
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                result: Dict[str, MemoryEntry] = {}
                chunk_size = 500
                for i in range(0, len(entry_ids), chunk_size):
                    chunk = entry_ids[i:i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    cursor = conn.execute(
                        f"SELECT * FROM memories WHERE id IN ({placeholders})",
                        chunk,
                    )
                    for row in cursor:
                        entry = MemoryEntry.from_db_row(dict(row))
                        result[entry.id] = entry
                return result
            finally:
                conn.close()

    async def get_statistics(self) -> Dict[str, Any]:
        async with self._lock:
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
                by_category = {}
                for row in conn.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category"):
                    by_category[row["category"]] = row["cnt"]
                top_wr = []
                for row in conn.execute("SELECT id, win_rate, usage_count FROM memories ORDER BY win_rate DESC LIMIT 5"):
                    top_wr.append({"id": row["id"], "win_rate": row["win_rate"], "usage_count": row["usage_count"]})
                return {
                    "total_memories": total, "avg_win_rate": avg_wr,
                    "by_type": by_type, "by_judgement": by_judgement,
                    "by_category": by_category, "top_win_rate": top_wr,
                }
            finally:
                conn.close()

    async def insert_or_replace_distilled_rule(
        self,
        rule_id: str,
        content: str,
        source_memory_ids: str = "[]",
        avg_win_rate: float = 0.0,
    ) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                now = datetime.now().isoformat()
                cursor = conn.execute(
                    """INSERT OR REPLACE INTO distilled_rules
                       (id, content, source_memory_ids, avg_win_rate, created_at, updated_at)
                       VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM distilled_rules WHERE id = ?), ?), ?)""",
                    (rule_id, content, source_memory_ids, avg_win_rate,
                     rule_id, now, now),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def get_distilled_rules(
        self,
        min_win_rate: float = 0.0,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """SELECT id, content, source_memory_ids, avg_win_rate, created_at, updated_at
                       FROM distilled_rules
                       WHERE avg_win_rate >= ?
                       ORDER BY avg_win_rate DESC
                       LIMIT ?""",
                    (min_win_rate, limit),
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    async def update_distilled_rule(
        self,
        rule_id: str,
        content: Optional[str] = None,
        avg_win_rate: Optional[float] = None,
        source_memory_ids: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                set_clauses = []
                params: list[Any] = []
                if content is not None:
                    set_clauses.append("content = ?")
                    params.append(content)
                if avg_win_rate is not None:
                    set_clauses.append("avg_win_rate = ?")
                    params.append(avg_win_rate)
                if source_memory_ids is not None:
                    set_clauses.append("source_memory_ids = ?")
                    params.append(source_memory_ids)
                if not set_clauses:
                    return False
                set_clauses.append("updated_at = ?")
                params.append(datetime.now().isoformat())
                params.append(rule_id)
                sql = f"UPDATE distilled_rules SET {', '.join(set_clauses)} WHERE id = ?"
                cursor = conn.execute(sql, params)
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def delete_distilled_rule(self, rule_id: str) -> bool:
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute("DELETE FROM distilled_rules WHERE id = ?", (rule_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
