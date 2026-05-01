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
    使用 asyncio.Lock 防死锁（避免阻塞事件循环线程）。
    """

    def __init__(self, data_dir: str) -> None:
        self.db_path = os.path.join(data_dir, "evolution.db")
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构（单线程启动阶段，无需锁）"""
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

            # FTS5 虚拟表（content=memories 外部内容模式）
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(
                    question, content, category,
                    content='memories',
                    content_rowid='rowid'
                )
            """)

            # FTS 同步触发器
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
        """添加一条记忆，返回 entry_id"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                d = entry.to_db_dict()
                conn.execute(
                    """INSERT INTO memories
                       (id, memory_type, category, question, content,
                        trajectory, rules, judgement, usage_count, success_count,
                        win_rate, embedding, tags, related_ids, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        d["id"], d["memory_type"], d["category"], d["question"],
                        d["content"], d["trajectory"], d["rules"], d["judgement"],
                        d["usage_count"], d["success_count"], d["win_rate"],
                        d["embedding"], d["tags"], d["related_ids"],
                        d["created_at"], d["updated_at"],
                    ),
                )
                conn.commit()
                return entry.id
            finally:
                conn.close()

    async def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        """根据 ID 获取单条记忆"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (entry_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return MemoryEntry.from_db_row(dict(row))
            finally:
                conn.close()

    async def update_entry(self, entry_id: str, **kwargs: Any) -> bool:
        """更新记忆的指定字段"""
        if not kwargs:
            return False

        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                values = list(kwargs.values()) + [entry_id]
                cursor = conn.execute(
                    f"UPDATE memories SET {sets} WHERE id = ?", values
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def delete_entry(self, entry_id: str) -> bool:
        """删除一条记忆"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (entry_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """净化 FTS5 查询，防止特殊字符触发语法解析错误。

        FTS5 特殊字符：* " ( ) + -
        此外 . 等标点在未引号包裹时也可能导致 tokenizer 抛错。
        策略：去除已有双引号后，整体用双引号包裹为 phrase query。
        """
        # 去掉用户输入中可能存在的双引号，避免破坏 phrase 语法
        query = query.replace('"', '')
        # 整体包裹为 phrase query，FTS5 内部仍会按 tokenizer 分词匹配
        return f'"{query}"'

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

                # 净化查询，防止 v1.0.0 这类关键词触发 FTS5 语法错误
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

    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计快照"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row

                total = conn.execute(
                    "SELECT COUNT(*) as cnt FROM memories"
                ).fetchone()["cnt"]

                avg_wr = conn.execute(
                    "SELECT AVG(win_rate) as avg FROM memories"
                ).fetchone()["avg"] or 0

                # 按类型统计
                by_type = {}
                for row in conn.execute(
                    "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
                ):
                    by_type[row["memory_type"]] = row["cnt"]

                # 按评判统计
                by_judgement = {}
                for row in conn.execute(
                    "SELECT judgement, COUNT(*) as cnt FROM memories GROUP BY judgement"
                ):
                    by_judgement[row["judgement"]] = row["cnt"]

                # 胜率 Top 5
                top_wr = []
                for row in conn.execute(
                    "SELECT id, win_rate, usage_count FROM memories ORDER BY win_rate DESC LIMIT 5"
                ):
                    top_wr.append({
                        "id": row["id"],
                        "win_rate": row["win_rate"],
                        "usage_count": row["usage_count"],
                    })

                return {
                    "total_memories": total,
                    "avg_win_rate": avg_wr,
                    "by_type": by_type,
                    "by_judgement": by_judgement,
                    "top_win_rate": top_wr,
                }
            finally:
                conn.close()
