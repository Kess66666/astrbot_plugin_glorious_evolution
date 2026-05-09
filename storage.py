"""
光荣进化系统 - 存储层
SQLite + FTS5 全文搜索 + 向量存储

v1.0.11 修复:
- INSERT OR IGNORE → INSERT，冲突时抛异常而非静默丢数据
- add_entry 检查 insert_entry 返回值
- update_entry key 白名单校验，防 SQL 注入
- 删除废弃的 storage.update_win_rate()（统一走 MemoryManager.update_win_rate）
- insert_entry/add_entry 返回值语义修正
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement

# update_entry 允许的字段白名单（防 SQL 注入）
_ALLOWED_UPDATE_FIELDS = frozenset({
    "question", "content", "memory_type", "category", "judgement",
    "win_rate", "usage_count", "success_count", "trajectory", "rules",
    "embedding", "embedding_version", "tags", "related_ids", "updated_at",
})


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
                    embedding_version TEXT NOT NULL DEFAULT '',
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

            # ── ALTER TABLE 兜底：为已有数据库补列 ──
            try:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN embedding_version TEXT NOT NULL DEFAULT ''"
                )
                conn.commit()
                logger.info("[GE] ALTER TABLE: 已补列 embedding_version")
            except sqlite3.OperationalError:
                pass  # 列已存在，忽略

        finally:
            conn.close()

    # ── ID 计数器恢复 ──

    def get_max_id_counter(self) -> int:
        """从 SQLite 查询当前最大 ID 序号，用于 _id_counter 安全恢复。"""
        conn = sqlite3.connect(self.db_path)
        try:
            # ID 格式: MEM-YYYYMMDD-NNN，取 NNN 部分的全局最大值
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

    # ── CRUD ──

    async def insert_entry(self, entry: MemoryEntry) -> bool:
        """插入一条记忆。ID 冲突时抛 IntegrityError 而非静默跳过。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                db_dict = entry.to_db_dict()
                cursor = conn.execute(
                    """INSERT INTO memories
                       (id, question, content, memory_type, category, judgement,
                        win_rate, usage_count, success_count, trajectory, rules,
                        embedding, embedding_version, tags, related_ids,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        entry.id, entry.question, entry.content,
                        entry.memory_type.value, entry.category, entry.judgement.value,
                        entry.win_rate, entry.usage_count, entry.success_count,
                        entry.trajectory, entry.rules,
                        db_dict.get("embedding"), db_dict.get("embedding_version", ""),
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
        """添加一条记忆，返回 entry_id。插入失败时记录错误日志。"""
        ok = await self.insert_entry(entry)
        if not ok:
            logger.error(f"[GE] add_entry 失败: {entry.id} 可能已存在")
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
        """按字段更新记忆条目。key 须在白名单内，否则拒绝。"""
        if not fields:
            return False
        # ── 白名单校验：防止 SQL 注入 ──
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
        """删除一条记忆。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    # ── 评判 ──

    async def update_judgement(self, entry_id: str, judgement: Judgement) -> bool:
        """更新评判状态。"""
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

    # ── 搜索 ──

    @staticmethod
    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """净化 FTS5 查询字符串。
        v1.0.32: 过滤 FTS5 保留关键字 (NOT/AND/OR/NEAR)，短语查询包裹。
        """
        import re
        # 去除用户输入中的引号
        safe = query.replace('"', '').replace("'", '')
        # 剔除 FTS5 保留关键字（整词匹配，大小写不敏感）
        reserved = {'not', 'and', 'or', 'near'}
        terms = []
        for t in safe.split():
            if t.lower() in reserved:
                continue
            terms.append(t)
        if not terms:
            return '""'
        # 双引号包裹 = 短语查询，。等特殊字符不再报错
        return '"' + ' '.join(terms) + '"'

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
                logger.error(f"[GE] FTS 搜索失败: {e}")
                return []
            finally:
                conn.close()

    # ── 批量读取 ──

    async def get_all_memories(self, limit: int = 1000) -> List[MemoryEntry]:
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

    async def get_memories_by_type(
        self,
        memory_type: str,
        limit: int = 500,
        order_by: str = "created_at DESC",
    ) -> List[MemoryEntry]:
        """按 memory_type 过滤获取记忆条目（SQL 层过滤，省内存）。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                # 白名单校验 order_by，防 SQL 注入
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

    # ── 蒸馏规则 CRUD (v1.0.14) ──

    async def insert_or_replace_distilled_rule(
        self,
        rule_id: str,
        content: str,
        source_memory_ids: str = "[]",
        avg_win_rate: float = 0.0,
    ) -> bool:
        """插入或替换一条蒸馏规则。ID 冲突时 REPLACE。"""
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
        """获取蒸馏规则列表，按 avg_win_rate 降序。"""
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
        """更新蒸馏规则的部分字段。"""
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
        """删除一条蒸馏规则。"""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute("DELETE FROM distilled_rules WHERE id = ?", (rule_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
