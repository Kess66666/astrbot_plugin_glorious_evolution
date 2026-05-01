"""
光荣进化系统 - 存储层
SQLite + FTS5 全文搜索 + 向量存储
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement


class Storage:
    """
    SQLite 存储层，支持 FTS5 全文搜索。

    线程安全：所有写操作通过 _lock 串行化。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        # 确保父目录存在，避免 sqlite3.OperationalError: unable to open database file
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        logger.info(f"[GE] Storage db_path={db_path}, exists={os.path.exists(db_path)}")
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表（若不存在）。"""
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
            """)
            conn.commit()
        finally:
            conn.close()

    # ── CRUD ──

    async def insert_entry(self, entry: MemoryEntry) -> bool:
        """插入一条记忆（id 冲突时跳过）。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO memories
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
                return conn.total_changes > 0
            finally:
                conn.close()

    async def add_entry(self, entry: MemoryEntry) -> str:
        """添加一条记忆，返回 entry_id。"""
        await self.insert_entry(entry)
        return entry.id

    async def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        """按 ID 获取单条记忆。"""
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
        """按字段更新记忆条目。"""
        if not fields:
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
                conn.execute(sql, params)
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    async def delete_entry(self, entry_id: str) -> bool:
        """删除一条记忆。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    # ── 胜率 ──

    async def update_win_rate(self, entry_id: str, success: bool) -> bool:
        """更新胜率。返回 True 表示更新成功。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                now = datetime.now().isoformat()
                if success:
                    conn.execute(
                        """UPDATE memories
                           SET win_rate = MIN(1.0, win_rate + 0.05),
                               usage_count = usage_count + 1,
                               updated_at = ?
                           WHERE id = ?""",
                        (now, entry_id),
                    )
                else:
                    conn.execute(
                        """UPDATE memories
                           SET win_rate = MAX(0.0, win_rate - 0.1),
                               usage_count = usage_count + 1,
                               updated_at = ?
                           WHERE id = ?""",
                        (now, entry_id),
                    )
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    async def update_judgement(self, entry_id: str, judgement: Judgement) -> bool:
        """更新评判状态。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "UPDATE memories SET judgement = ?, updated_at = ? WHERE id = ?",
                    (judgement.value, datetime.now().isoformat(), entry_id),
                )
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    # ── 搜索 ──

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """净化 FTS5 查询字符串，避免特殊字符触发语法错误。"""
        import re
        safe = re.sub(r'[^\w\u4e00-\u9fff]', ' ', query)
        if not safe.strip():
            return '""'
        terms = safe.split()
        if not terms:
            return '""'
        return " OR ".join(terms)

    async def search_entries(
        self,
        query: str,
        top_k: int = 5,
        memory_type: Optional[str] = None,
        min_win_rate: float = 0.0,
    ) -> List[MemoryEntry]:
        """FTS5 全文搜索 + 过滤"""
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
                logger.error(f"[Glorious Evolution] FTS 搜索失败: {e}")
                return []
            finally:
                conn.close()

    # ── 批量读取 ──

    async def get_all_entries(self, limit: int = 1000) -> List[MemoryEntry]:
        """获取所有记忆条目"""
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

    async def get_entries_by_ids(self, entry_ids: List[str]) -> Dict[str, MemoryEntry]:
        """按 ID 列表批量获取记忆条目，返回 {id: MemoryEntry} 映射。"""
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

    # ── 统计 ──

    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计快照"""
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
