"""
光荣进化系统 - 记忆管理器
MIA 风格的高层封装：add_memory / retrieve_relevant_memories + 向量化钩子

v1.0.31 - Unified Scoring 三维度:
- 四元组 (vec, win_rate, retrieved_ts, feedback_ts) 解耦检索时间与反馈时间
- recency 指数衰减 (30天半衰期)，基于 retrieved_ts 而非 feedback_ts
- FTS5 候选接入统一评分公式，merge-before-rerank
- debug_recall 输出三维得分 + 来源标记 [VEC]/[FTS]
"""

import math
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement
from .storage import Storage

DEDUP_THRESHOLD: float = 0.95
COSINE_WEIGHT: float = 0.60      # v1.0.31: 0.7→0.60，为 recency 腾空间
WIN_RATE_WEIGHT: float = 0.25    # v1.0.31: 0.3→0.25
RECENCY_WEIGHT: float = 0.15
RECENCY_HALFLIFE_DAYS: float = 30.0
RECENCY_LAMBDA: float = math.log(2) / RECENCY_HALFLIFE_DAYS  # ≈ 0.0231
MIN_FEEDBACK_COUNT: int = 3
CATEGORY_BOOST: float = 1.15

# FTS5 保底配额：merge 池中 FTS 候选最少保留数
FTS_CANDIDATE_BUDGET: int = 5

EmbedFunc = Callable[[str], Coroutine[Any, None, List[float]]]
ClassifyFunc = Callable[[str, str], Coroutine[Any, None, str]]


def _lazy_import_numpy() -> Optional[Any]:
    try:
        import numpy as np
        return np
    except ImportError:
        logger.warning("[GE] numpy 未安装，向量检索不可用")
        return None


class MemoryManager:
    DECAY_FACTOR: float = 0.0
    EVICT_MIN_USAGE: int = 3
    EVICT_MAX_WIN_RATE: float = 0.2

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._embed_func: Optional[EmbedFunc] = None
        self._embed_dim: int = 0
        self._classify_func: Optional[ClassifyFunc] = None
        self._valid_categories: List[str] = [
            "general", "debugging", "deployment", "coding",
            "configuration", "security", "insight", "consolidated_rule",
        ]
        # 四元组: (numpy_vec, win_rate, retrieved_ts, feedback_ts)
        self._vectors: Dict[str, Tuple[Any, float, float, float]] = {}
        self._id_counter: int = 0

    # ──── Recency 计算 ────

    @staticmethod
    def _compute_recency(retrieved_ts: float) -> float:
        """指数衰减: exp(-λ·days)，30天半衰期。"""
        now = datetime.now().timestamp()
        days = max(0.0, (now - retrieved_ts) / 86400.0)
        return math.exp(-RECENCY_LAMBDA * days)

    @staticmethod
    def _now_ts() -> float:
        return datetime.now().timestamp()

    # ──── 向量管理 ────

    async def set_embed_func(self, func: EmbedFunc, dim: int) -> None:
        self._embed_func = func
        self._embed_dim = dim
        logger.info(f"[GE] 向量化钩子已注入, dim={dim}")

    async def set_classify_func(self, func: ClassifyFunc, valid_categories: Optional[List[str]] = None) -> None:
        self._classify_func = func
        self._valid_categories = valid_categories or self._valid_categories
        logger.info("[GE] 自动分类器已注入")

    async def embed_text(self, text: str) -> Optional[List[float]]:
        if self._embed_func is None:
            return None
        try:
            return await self._embed_func(text)
        except Exception as e:
            logger.error(f"[GE] 向量化失败: {e}")
            return None

    def _add_vector(self, entry_id: str, embedding: List[float], win_rate: float = 0.0,
                    retrieved_ts: Optional[float] = None, feedback_ts: Optional[float] = None) -> None:
        np = _lazy_import_numpy()
        if np is not None and embedding is not None:
            now = self._now_ts()
            self._vectors[entry_id] = (
                np.array(embedding, dtype=np.float32),
                win_rate,
                retrieved_ts or now,
                feedback_ts or now,
            )

    def _touch_retrieved(self, entry_id: str) -> None:
        """更新 retrieved_ts，表示该记忆被检索命中。"""
        if entry_id in self._vectors:
            vec, wr, _, fb_ts = self._vectors[entry_id]
            self._vectors[entry_id] = (vec, wr, self._now_ts(), fb_ts)

    async def load_vectors(self) -> None:
        """加载向量索引 + 迁移旧数据到四元组 + 恢复 _id_counter。"""
        entries = await self.storage.get_all_memories(limit=10000)
        loaded = 0
        for entry in entries:
            if entry.embedding is not None:
                # 迁移：updated_at → feedback_ts, retrieved_ts 新设为 now（首日所有记忆活性=1.0）
                fb_ts = entry.updated_at.timestamp() if entry.updated_at else self._now_ts()
                self._add_vector(entry.id, entry.embedding, entry.win_rate,
                                retrieved_ts=self._now_ts(), feedback_ts=fb_ts)
                loaded += 1
        db_max = self.storage.get_max_id_counter()
        self._id_counter = max(len(entries), db_max)
        logger.info(
            f"[GE] 向量索引加载完成 (v1.0.31): {loaded}/{len(entries)} 条含向量, "
            f"counter={self._id_counter} (db_max={db_max})"
        )

    def _build_entry(self, entry_id: str, question: str, content: str, memory_type: str,
                     category: str, trajectory: str = "", rules: str = "",
                     tags: Optional[List[str]] = None,
                     embedding: Optional[List[float]] = None) -> MemoryEntry:
        return MemoryEntry(
            id=entry_id, memory_type=MemoryType(memory_type), category=category,
            question=question, content=content, trajectory=trajectory, rules=rules,
            judgement=Judgement.PENDING, usage_count=0, success_count=0, win_rate=0.5,
            embedding=embedding, tags=tags or [], related_ids=[],
        )

    async def _find_duplicate(self, query_vec: List[float]) -> Optional[Tuple[str, float, MemoryEntry]]:
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return None
        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return None
        ids_all = list(self._vectors.keys())
        vec_list = [v for v, _, _, _ in self._vectors.values()]
        vec_matrix = np.stack(vec_list)
        vec_norms = np.linalg.norm(vec_matrix, axis=1)
        valid = vec_norms > 0
        if not valid.any():
            return None
        dots = np.dot(vec_matrix[valid], query_arr)
        similarities = dots / (query_norm * vec_norms[valid])
        best_idx = int(np.argmax(similarities))
        best_sim = float(similarities[best_idx])
        if best_sim < DEDUP_THRESHOLD:
            return None
        valid_indices = np.where(valid)[0]
        best_eid = ids_all[valid_indices[best_idx]]
        entry = await self.storage.get_entry(best_eid)
        if entry is None:
            return None
        return (best_eid, best_sim, entry)

    async def _replace_entry(self, old_id: str, new_entry: MemoryEntry) -> None:
        await self.storage.add_entry(new_entry)
        if new_entry.embedding is not None:
            self._add_vector(new_entry.id, new_entry.embedding, new_entry.win_rate)
        try:
            await self.storage.delete_entry(old_id)
        except Exception as e:
            logger.warning(f"[GE] 删除旧记忆 {old_id} 失败: {e}")
        self._vectors.pop(old_id, None)

    async def add_memory(self, question: str, content: str, memory_type: str = "procedural",
                         category: str = "general", trajectory: str = "", rules: str = "",
                         tags: Optional[List[str]] = None) -> str:
        self._id_counter += 1
        date_str = datetime.now().strftime("%Y%m%d")
        entry_id = f"MEM-{date_str}-{self._id_counter:03d}"
        if category == "general" and self._classify_func is not None:
            try:
                category = await self._classify_func(question, content)
                if category not in self._valid_categories:
                    category = "general"
            except Exception:
                category = "general"
        embedding = None
        if self._embed_func is not None:
            embedding = await self.embed_text(question)
        if embedding is not None:
            dup = await self._find_duplicate(embedding)
            if dup is not None:
                old_id, sim, old_entry = dup
                return await self._handle_duplicate(old_id, sim, old_entry, entry_id, question,
                                                     content, memory_type, category, trajectory,
                                                     rules, tags, embedding)
        entry = self._build_entry(entry_id, question, content, memory_type, category,
                                  trajectory, rules, tags, embedding)
        saved_id = await self.storage.add_entry(entry)
        if embedding is not None:
            self._add_vector(entry_id, embedding)
        logger.info(f"[GE] 新增记忆: {entry_id} type={memory_type} category={category}")
        return saved_id

    async def _handle_duplicate(self, old_id: str, similarity: float, old_entry: MemoryEntry,
                                 new_id: str, question: str, content: str, memory_type: str,
                                 category: str, trajectory: str, rules: str,
                                 tags: Optional[List[str]], embedding: Optional[List[float]]) -> str:
        logger.info(f"[GE] 去重触发: new={new_id} vs old={old_id} similarity={similarity:.4f}")
        if old_entry.judgement == Judgement.INCORRECT:
            new_entry = self._build_entry(new_id, question, content, memory_type, category,
                                          trajectory, rules, tags, embedding)
            await self._replace_entry(old_id, new_entry)
            return new_id
        if old_entry.judgement == Judgement.CORRECT:
            if len(content) < len(old_entry.content):
                new_entry = self._build_entry(new_id, question, content, memory_type, category,
                                              trajectory, rules, tags, embedding)
                await self._replace_entry(old_id, new_entry)
                return new_id
            return old_id
        new_entry = self._build_entry(new_id, question, content, memory_type, category,
                                      trajectory, rules, tags, embedding)
        await self._replace_entry(old_id, new_entry)
        return new_id

    # ──── 统一评分公式 ────

    @staticmethod
    def _unified_score(cosine_norm: float, win_rate: float, recency: float) -> float:
        """三维统一评分: 语义 + 行为 + 时间。"""
        return COSINE_WEIGHT * cosine_norm + WIN_RATE_WEIGHT * win_rate + RECENCY_WEIGHT * recency

    def _get_entry_recency(self, entry_id: str) -> float:
        """获取某条记忆的 recency 值，不在向量库返回默认 0.5。"""
        if entry_id in self._vectors:
            _, _, retrieved_ts, _ = self._vectors[entry_id]
            return self._compute_recency(retrieved_ts)
        return 0.5

    # ──── 检索 ────

    async def retrieve_relevant_memories(self, query: str, top_k: int = 5,
                                          min_win_rate: float = 0.0,
                                          memory_type: Optional[str] = None) -> List[MemoryEntry]:
        """Hybrid retrieval: vector + FTS5 候选统一评分，merge-before-rerank。"""
        import asyncio

        fetch_top_k = max(top_k, 5) * 3
        all_candidates: Dict[str, Tuple[float, MemoryEntry, str]] = {}  # (score, entry, source_tag)

        # ── 向量搜索 ──
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"
                vec_scored = await self._vector_search(
                    query_vec, top_k=fetch_top_k, min_win_rate=min_win_rate,
                    memory_type=memory_type, query_category=query_category,
                )
                for score, entry in vec_scored:
                    if entry.id not in all_candidates or score > all_candidates[entry.id][0]:
                        all_candidates[entry.id] = (score, entry, "[VEC]")

        # ── FTS5 全文搜索（并行） ──
        fts_task = asyncio.create_task(
            self.storage.search_entries(
                query=query, top_k=max(fetch_top_k, FTS_CANDIDATE_BUDGET + 5),
                memory_type=memory_type, min_win_rate=min_win_rate,
            )
        )
        fts_entries = await fts_task

        # FTS5 统一评分
        for i, entry in enumerate(fts_entries):
            pos_score = max(0.0, 1.0 - 0.05 * i)          # 位置代理 cosine
            rec = self._get_entry_recency(entry.id)
            score = self._unified_score(pos_score, entry.win_rate, rec)
            if entry.id not in all_candidates or score > all_candidates[entry.id][0]:
                all_candidates[entry.id] = (score, entry, "[FTS]")

        # ── merge-before-rerank → top_k ──
        merged = sorted(all_candidates.values(), key=lambda x: x[0], reverse=True)
        results = merged[:top_k]

        # Touch retrieved_ts for all returned entries
        for _, entry, _ in results:
            self._touch_retrieved(entry.id)

        return [entry for _, entry, _ in results]

    async def retrieve_balanced_memories(self, query: str, pos_top_k: int = 2, neg_top_k: int = 2,
                                          min_win_rate: float = 0.0,
                                          memory_type: Optional[str] = None) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        """Hybrid balanced retrieval: merge-before-rerank, then split by judgement."""
        import asyncio

        fetch_top_k = (pos_top_k + neg_top_k) * 4
        all_candidates: Dict[str, Tuple[float, MemoryEntry, str]] = {}

        # ── 向量搜索 ──
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"
                vec_scored = await self._vector_search(
                    query_vec, top_k=fetch_top_k, min_win_rate=min_win_rate,
                    memory_type=memory_type, query_category=query_category,
                )
                for score, entry in vec_scored:
                    if entry.id not in all_candidates or score > all_candidates[entry.id][0]:
                        all_candidates[entry.id] = (score, entry, "[VEC]")

        # ── FTS5 ──
        fts_entries = await self.storage.search_entries(
            query=query, top_k=max(fetch_top_k, FTS_CANDIDATE_BUDGET + 5),
            memory_type=memory_type, min_win_rate=min_win_rate,
        )
        for i, entry in enumerate(fts_entries):
            pos_score = max(0.0, 1.0 - 0.05 * i)
            rec = self._get_entry_recency(entry.id)
            score = self._unified_score(pos_score, entry.win_rate, rec)
            if entry.id not in all_candidates or score > all_candidates[entry.id][0]:
                all_candidates[entry.id] = (score, entry, "[FTS]")

        # ── merge → split ──
        merged = sorted(all_candidates.values(), key=lambda x: x[0], reverse=True)
        positive: List[MemoryEntry] = []
        negative: List[MemoryEntry] = []
        touched: set = set()

        for _, entry, _ in merged:
            if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                positive.append(entry)
                touched.add(entry.id)
            if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                negative.append(entry)
                touched.add(entry.id)
            if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                break

        for eid in touched:
            self._touch_retrieved(eid)

        return positive, negative

    @staticmethod
    def _is_positive_memory(entry: MemoryEntry) -> bool:
        return entry.judgement == Judgement.CORRECT or entry.win_rate > 0.5

    @staticmethod
    def _is_negative_memory(entry: MemoryEntry) -> bool:
        if entry.judgement == Judgement.INCORRECT:
            return True
        return entry.usage_count >= MIN_FEEDBACK_COUNT and entry.win_rate <= 0.5

    async def _vector_search(self, query_vec: List[float], top_k: int = 5,
                              min_win_rate: float = 0.0, memory_type: Optional[str] = None,
                              query_category: str = "general") -> List[Tuple[float, MemoryEntry]]:
        """纯向量搜索，三因子统一评分 (cosine + win_rate + recency)。"""
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return []
        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return []
        ids_all = list(self._vectors.keys())
        vec_list = [v for v, _, _, _ in self._vectors.values()]
        wr_list = np.array([wr for _, wr, _, _ in self._vectors.values()], dtype=np.float32)
        rt_list = np.array([rt for _, _, rt, _ in self._vectors.values()], dtype=np.float32)

        vec_matrix = np.stack(vec_list)
        vec_norms = np.linalg.norm(vec_matrix, axis=1)
        valid = vec_norms > 0
        if not valid.any():
            return []

        ids_valid = np.array(ids_all)[valid]
        vec_valid = vec_matrix[valid]
        wr_valid = wr_list[valid]
        rt_valid = rt_list[valid]
        norms_valid = vec_norms[valid]

        dots = np.dot(vec_valid, query_arr)
        cosine_sims = dots / (query_norm * norms_valid)
        cosine_normalized = (cosine_sims + 1.0) / 2.0

        # recency: 批量向量化计算
        now = self._now_ts()
        days_since = np.maximum(0.0, (now - rt_valid) / 86400.0)
        recency_scores = np.exp(-RECENCY_LAMBDA * days_since)

        # 三因子统一评分
        base_scores = (COSINE_WEIGHT * cosine_normalized
                      + WIN_RATE_WEIGHT * wr_valid
                      + RECENCY_WEIGHT * recency_scores)

        sorted_i = np.argsort(base_scores)[::-1]
        candidate_count = min(len(sorted_i), top_k * 3)
        candidate_ids = ids_valid[sorted_i[:candidate_count]].tolist()
        entry_map = await self.storage.get_entries_by_ids(candidate_ids)

        scored_entries: List[Tuple[float, MemoryEntry]] = []
        for idx in sorted_i[:candidate_count]:
            eid = str(ids_valid[idx])
            entry = entry_map.get(eid)
            if entry is None:
                continue
            if min_win_rate > 0 and entry.win_rate < min_win_rate:
                continue
            if memory_type and entry.memory_type.value != memory_type:
                continue
            final_score = float(base_scores[idx])
            if query_category != "general" and entry.category == query_category:
                final_score *= CATEGORY_BOOST
            scored_entries.append((final_score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        return scored_entries[:top_k]

    # ──── 胜率 & 使用计数 ────

    async def update_win_rate(self, entry_id: str, success: bool) -> bool:
        """统一胜率更新入口（唯一公式来源）。

        v1.0.31: 只更新 feedback_ts，不动 retrieved_ts。
        """
        entry = await self.storage.get_entry(entry_id)
        if entry is None:
            logger.warning(f"[GE] 胜率更新失败，记忆不存在: {entry_id}")
            return False
        entry.usage_count += 1
        if success:
            entry.success_count += 1
        safe_usage = max(1, entry.usage_count)
        if self.DECAY_FACTOR <= 0:
            entry.win_rate = entry.success_count / safe_usage
        else:
            entry.win_rate = self.DECAY_FACTOR * entry.win_rate + (1 - self.DECAY_FACTOR) * (entry.success_count / safe_usage)
        entry.judgement = Judgement.CORRECT if success else Judgement.INCORRECT
        entry.updated_at = datetime.now().isoformat()
        ok = await self.storage.update_entry(entry_id, usage_count=entry.usage_count,
                                              success_count=entry.success_count,
                                              win_rate=entry.win_rate,
                                              judgement=entry.judgement.value,
                                              updated_at=entry.updated_at)
        if ok and entry_id in self._vectors:
            vec, _, retrieved_ts, _ = self._vectors[entry_id]
            self._vectors[entry_id] = (vec, entry.win_rate, retrieved_ts, self._now_ts())
        return ok

    async def increment_usage(self, entry_id: str) -> bool:
        """纯递增 usage_count + 刷新 retrieved_ts（注入 prompt = 被检索使用）。"""
        entry = await self.storage.get_entry(entry_id)
        if entry is None:
            return False
        self._touch_retrieved(entry_id)
        return await self.storage.update_entry(
            entry_id, usage_count=entry.usage_count + 1
        )

    # ──── debug_recall ────

    async def debug_recall(self, query: str, top_k: int = 5,
                           verbose: bool = False) -> List[Dict[str, Any]]:
        """调试用：返回带三维得分和来源标记的检索结果。"""
        import asyncio

        fetch_top_k = max(top_k, 5) * 3
        all_candidates: Dict[str, Dict[str, Any]] = {}

        # 向量搜索
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                vec_scored = await self._vector_search(query_vec, top_k=fetch_top_k)
                for score, entry in vec_scored:
                    if entry.id not in all_candidates or score > all_candidates[entry.id]["score"]:
                        cos = 0.0
                        wr = entry.win_rate
                        rec = self._get_entry_recency(entry.id)
                        all_candidates[entry.id] = {
                            "score": score, "entry": entry, "source": "[VEC]",
                            "sim": cos, "wr": wr, "rec": rec,
                        }

        # FTS5
        fts_entries = await self.storage.search_entries(
            query=query, top_k=max(fetch_top_k, FTS_CANDIDATE_BUDGET + 5),
        )
        for i, entry in enumerate(fts_entries):
            pos_score = max(0.0, 1.0 - 0.05 * i)
            wr = entry.win_rate
            rec = self._get_entry_recency(entry.id)
            score = self._unified_score(pos_score, wr, rec)
            if entry.id not in all_candidates or score > all_candidates[entry.id]["score"]:
                all_candidates[entry.id] = {
                    "score": score, "entry": entry, "source": "[FTS]",
                    "sim": pos_score, "wr": wr, "rec": rec,
                }

        merged = sorted(all_candidates.values(), key=lambda x: x["score"], reverse=True)
        results = merged[:top_k]

        # Touch retrieved
        for r in results:
            self._touch_retrieved(r["entry"].id)

        if verbose:
            return results

        # 紧凑输出
        output = []
        for r in results:
            e = r["entry"]
            output.append({
                "id": e.id,
                "source": r["source"],
                "score": round(r["score"], 4),
                "sim": round(r["sim"], 4),
                "wr": round(r["wr"], 4),
                "rec": round(r["rec"], 4),
                "category": e.category,
                "question": e.question[:80],
            })
        return output

    # ──── 管理接口 ────

    async def get_all_memories(self, limit: int = 10000) -> List[MemoryEntry]:
        return await self.storage.get_all_memories(limit=limit)

    async def get_memories_by_type(self, memory_type: str, limit: int = 500) -> List[MemoryEntry]:
        return await self.storage.get_memories_by_type(memory_type=memory_type, limit=limit)

    async def evict_low_quality(self) -> int:
        entries = await self.storage.get_all_memories(limit=10000)
        evicted = 0
        for entry in entries:
            if (entry.usage_count >= self.EVICT_MIN_USAGE 
                and entry.win_rate < self.EVICT_MAX_WIN_RATE
                and entry.judgement != Judgement.PENDING):
                if await self.storage.delete_entry(entry.id):
                    self._vectors.pop(entry.id, None)
                    evicted += 1
        if evicted:
            logger.info(f"[GE] 本轮淘汰 {evicted} 条低质量记忆")
        return evicted

    async def get_stats(self) -> Dict[str, Any]:
        stats = await self.storage.get_statistics()
        stats["vector_index_size"] = len(self._vectors)
        stats["embedding_ready"] = self._embed_func is not None
        stats["embedding_dim"] = self._embed_dim
        stats["classifier_ready"] = self._classify_func is not None
        stats["dedup_threshold"] = DEDUP_THRESHOLD
        stats["cosine_weight"] = COSINE_WEIGHT
        stats["win_rate_weight"] = WIN_RATE_WEIGHT
        stats["recency_weight"] = RECENCY_WEIGHT
        stats["recency_halflife_days"] = RECENCY_HALFLIFE_DAYS
        stats["category_boost"] = CATEGORY_BOOST
        stats["decay_factor"] = self.DECAY_FACTOR
        stats["fts_candidate_budget"] = FTS_CANDIDATE_BUDGET
        return stats
