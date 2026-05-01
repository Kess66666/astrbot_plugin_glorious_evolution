"""
光荣进化系统 - 记忆管理器
MIA 风格的高层封装：add_memory / retrieve_relevant_memories + 向量化钩子

v1.0.11 修复:
- _id_counter 从 SQLite MAX(id) 恢复，避免重启碰撞
- 删除对 storage.update_win_rate() 的依赖（已从 storage 中删除）
- evict_low_quality 提高门限：usage_count >= 3 才参与淘汰，避免冷启动惩罚
"""

from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement
from .storage import Storage

DEDUP_THRESHOLD: float = 0.95
COSINE_WEIGHT: float = 0.7
WIN_RATE_WEIGHT: float = 0.3
MIN_FEEDBACK_COUNT: int = 3
CATEGORY_BOOST: float = 1.15

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
        self._vectors: Dict[str, Tuple[Any, float]] = {}
        self._id_counter: int = 0

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

    def _add_vector(self, entry_id: str, embedding: List[float], win_rate: float = 0.0) -> None:
        np = _lazy_import_numpy()
        if np is not None and embedding is not None:
            self._vectors[entry_id] = (np.array(embedding, dtype=np.float32), win_rate)

    async def load_vectors(self) -> None:
        entries = await self.storage.get_all_entries(limit=10000)
        loaded = 0
        for entry in entries:
            if entry.embedding is not None:
                self._add_vector(entry.id, entry.embedding, entry.win_rate)
                loaded += 1
        db_max = self.storage.get_max_id_counter()
        self._id_counter = max(len(entries), db_max)
        logger.info(
            f"[GE] 向量索引加载完成: {loaded}/{len(entries)} 条含向量, "
            f"counter={self._id_counter} (db_max={db_max})"
        )

    def _build_entry(self, entry_id: str, question: str, content: str, memory_type: str,
                     category: str, trajectory: str = "", rules: str = "",
                     tags: Optional[List[str]] = None,
                     embedding: Optional[List[float]] = None) -> MemoryEntry:
        return MemoryEntry(
            id=entry_id, memory_type=MemoryType(memory_type), category=category,
            question=question, content=content, trajectory=trajectory, rules=rules,
            judgement=Judgement.PENDING, usage_count=0, success_count=0, win_rate=0.0,
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
        vec_list = [v for v, _ in self._vectors.values()]
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

    async def retrieve_relevant_memories(self, query: str, top_k: int = 5,
                                          min_win_rate: float = 0.0,
                                          memory_type: Optional[str] = None) -> List[MemoryEntry]:
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"
                results = await self._vector_search(query_vec, top_k=top_k, min_win_rate=min_win_rate,
                                                     memory_type=memory_type, query_category=query_category)
                if results:
                    return results
        return await self.storage.search_entries(query=query, top_k=top_k,
                                                  memory_type=memory_type, min_win_rate=min_win_rate)

    async def retrieve_balanced_memories(self, query: str, pos_top_k: int = 2, neg_top_k: int = 2,
                                          min_win_rate: float = 0.0,
                                          memory_type: Optional[str] = None) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        positive: List[MemoryEntry] = []
        negative: List[MemoryEntry] = []
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"
                needed = (pos_top_k + neg_top_k) * 3
                candidates = await self._vector_search(query_vec, top_k=needed, min_win_rate=min_win_rate,
                                                        memory_type=memory_type, query_category=query_category)
                for entry in candidates:
                    if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                        positive.append(entry)
                    if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                        negative.append(entry)
                    if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                        break
                if positive or negative:
                    return positive, negative
        all_results = await self.storage.search_entries(query=query, top_k=(pos_top_k + neg_top_k) * 2,
                                                         memory_type=memory_type, min_win_rate=min_win_rate)
        for entry in all_results:
            if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                positive.append(entry)
            if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                negative.append(entry)
            if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                break
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
                              query_category: str = "general") -> List[MemoryEntry]:
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return []
        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return []
        ids_all = list(self._vectors.keys())
        vec_list = [v for v, _ in self._vectors.values()]
        wr_list = np.array([wr for _, wr in self._vectors.values()], dtype=np.float32)
        vec_matrix = np.stack(vec_list)
        vec_norms = np.linalg.norm(vec_matrix, axis=1)
        valid = vec_norms > 0
        if not valid.any():
            return []
        ids_valid = np.array(ids_all)[valid]
        vec_valid = vec_matrix[valid]
        wr_valid = wr_list[valid]
        norms_valid = vec_norms[valid]
        dots = np.dot(vec_valid, query_arr)
        cosine_sims = dots / (query_norm * norms_valid)
        cosine_normalized = (cosine_sims + 1.0) / 2.0
        base_scores = COSINE_WEIGHT * cosine_normalized + WIN_RATE_WEIGHT * wr_valid
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
        return [entry for _, entry in scored_entries[:top_k]]

    async def update_win_rate(self, entry_id: str, success: bool) -> bool:
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
            vec, _ = self._vectors[entry_id]
            self._vectors[entry_id] = (vec, entry.win_rate)
        return ok

    async def evict_low_quality(self) -> int:
        entries = await self.storage.get_all_entries(limit=10000)
        evicted = 0
        for entry in entries:
            if entry.usage_count >= self.EVICT_MIN_USAGE and entry.win_rate < self.EVICT_MAX_WIN_RATE:
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
        stats["category_boost"] = CATEGORY_BOOST
        stats["decay_factor"] = self.DECAY_FACTOR
        return stats