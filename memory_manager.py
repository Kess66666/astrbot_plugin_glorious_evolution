"""
光荣进化系统 - 记忆管理器
MIA 风格的高层封装：add_memory / retrieve_relevant_memories + 向量化钩子
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

EmbedFunc = Callable[[str], Coroutine[Any, None, List[float]]]


def _lazy_import_numpy() -> Optional[Any]:
    try:
        import numpy as np
        return np
    except ImportError:
        logger.warning("[Glorious Evolution] numpy 未安装，向量检索不可用")
        return None


class MemoryManager:
    """MIA MemoryBucket 的高层封装"""

    DECAY_FACTOR: float = 0.0
    EVICT_MIN_USAGE: int = 3
    EVICT_MAX_WIN_RATE: float = 0.2

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._embed_func: Optional[EmbedFunc] = None
        self._embed_dim: int = 0
        self._vectors: Dict[str, Tuple[Any, float]] = {}
        self._id_counter: int = 0

    async def set_embed_func(self, func: EmbedFunc, dim: int) -> None:
        self._embed_func = func
        self._embed_dim = dim
        logger.info(f"[Glorious Evolution] 向量化钩子已注入, dim={dim}")

    async def embed_text(self, text: str) -> Optional[List[float]]:
        if self._embed_func is None:
            return None
        try:
            return await self._embed_func(text)
        except Exception as e:
            logger.error(f"[Glorious Evolution] 向量化失败: {e}")
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
        self._id_counter = len(entries)
        logger.info(f"[Glorious Evolution] 向量索引加载: {loaded}/{len(entries)}, counter={self._id_counter}")

    def _build_entry(self, entry_id, question, content, memory_type, category,
                     trajectory="", rules="", tags=None, embedding=None) -> MemoryEntry:
        return MemoryEntry(
            id=entry_id, memory_type=MemoryType(memory_type), category=category,
            question=question, content=content, trajectory=trajectory, rules=rules,
            judgement=Judgement.PENDING, usage_count=0, success_count=0, win_rate=0.0,
            embedding=embedding, tags=tags or [], related_ids=[])

    async def _find_duplicate(self, query_vec: List[float]) -> Optional[Tuple[str, float, MemoryEntry]]:
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return None
        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return None
        best_id, best_score = None, -1.0
        for entry_id, (vec, _) in self._vectors.items():
            vec_norm = np.linalg.norm(vec)
            if vec_norm == 0:
                continue
            similarity = float(np.dot(query_arr, vec) / (query_norm * vec_norm))
            if similarity > best_score:
                best_score, best_id = similarity, entry_id
        if best_id is None or best_score < DEDUP_THRESHOLD:
            return None
        entry = await self.storage.get_entry(best_id)
        return (best_id, best_score, entry) if entry else None

    async def _replace_entry(self, old_id: str, new_entry: MemoryEntry) -> None:
        await self.storage.add_entry(new_entry)
        if new_entry.embedding is not None:
            self._add_vector(new_entry.id, new_entry.embedding, new_entry.win_rate)
        try:
            await self.storage.delete_entry(old_id)
        except Exception as e:
            logger.warning(f"[Glorious Evolution] 删除旧记忆 {old_id} 失败: {e}")
        self._vectors.pop(old_id, None)

    async def add_memory(self, question, content, memory_type="procedural",
                         category="general", trajectory="", rules="", tags=None) -> str:
        self._id_counter += 1
        date_str = datetime.now().strftime("%Y%m%d")
        entry_id = f"MEM-{date_str}-{self._id_counter:03d}"
        embedding = await self.embed_text(question) if self._embed_func else None
        if embedding is not None:
            dup = await self._find_duplicate(embedding)
            if dup:
                return await self._handle_duplicate(dup[0], dup[1], dup[2],
                    entry_id, question, content, memory_type, category,
                    trajectory, rules, tags, embedding)
        entry = self._build_entry(entry_id, question, content, memory_type, category,
                                  trajectory, rules, tags, embedding)
        saved_id = await self.storage.add_entry(entry)
        if embedding is not None:
            self._add_vector(entry_id, embedding)
        logger.info(f"[Glorious Evolution] 新增记忆: {entry_id} type={memory_type} embedded={'yes' if embedding else 'no'}")
        return saved_id

    async def _handle_duplicate(self, old_id, similarity, old_entry, new_id, question,
                                content, memory_type, category, trajectory, rules, tags, embedding) -> str:
        logger.info(f"[Glorious Evolution] 去重: new={new_id} old={old_id} sim={similarity:.4f} jud={old_entry.judgement.value}")
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
        entry = self._build_entry(new_id, question, content, memory_type, category,
                                  trajectory, rules, tags, embedding)
        saved_id = await self.storage.add_entry(entry)
        if embedding is not None:
            self._add_vector(new_id, embedding)
        return saved_id

    async def retrieve_relevant_memories(self, query, top_k=5, min_win_rate=0.0, memory_type=None) -> List[MemoryEntry]:
        if self._embed_func and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec:
                results = await self._vector_search(query_vec, top_k, min_win_rate, memory_type)
                if results:
                    return results
        return await self.storage.search_entries(query=query, top_k=top_k, memory_type=memory_type, min_win_rate=min_win_rate)

    async def retrieve_balanced_memories(self, query, pos_top_k=2, neg_top_k=2,
                                         min_win_rate=0.0, memory_type=None) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        positive, negative = [], []
        if self._embed_func and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec:
                candidates = await self._vector_search(query_vec, (pos_top_k+neg_top_k)*3, min_win_rate, memory_type)
                if candidates:
                    for entry in candidates:
                        if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                            positive.append(entry)
                        if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                            negative.append(entry)
                        if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                            break
                    if positive or negative:
                        return positive, negative
        all_results = await self.storage.search_entries(query=query, top_k=(pos_top_k+neg_top_k)*2,
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

    async def _vector_search(self, query_vec, top_k=5, min_win_rate=0.0, memory_type=None) -> List[MemoryEntry]:
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return []
        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return []
        scores: List[Tuple[str, float]] = []
        for entry_id, (vec, win_rate) in self._vectors.items():
            vec_norm = np.linalg.norm(vec)
            if vec_norm == 0:
                continue
            cosine_sim = float(np.dot(query_arr, vec) / (query_norm * vec_norm))
            cosine_norm = (cosine_sim + 1.0) / 2.0
            scores.append((entry_id, COSINE_WEIGHT * cosine_norm + WIN_RATE_WEIGHT * win_rate))
        scores.sort(key=lambda x: x[1], reverse=True)
        results, seen = [], set()
        for entry_id, _ in scores[:top_k*3]:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            entry = await self.storage.get_entry(entry_id)
            if not entry:
                continue
            if min_win_rate > 0 and entry.win_rate < min_win_rate:
                continue
            if memory_type and entry.memory_type.value != memory_type:
                continue
            results.append(entry)
            if len(results) >= top_k:
                break
        return results

    async def update_win_rate(self, entry_id: str, success: bool) -> bool:
        entry = await self.storage.get_entry(entry_id)
        if not entry:
            return False
        entry.usage_count += 1
        if success:
            entry.success_count += 1
        safe_usage = max(1, entry.usage_count)
        if self.DECAY_FACTOR <= 0:
            entry.win_rate = entry.success_count / safe_usage
        else:
            entry.win_rate = self.DECAY_FACTOR * entry.win_rate + (1-self.DECAY_FACTOR) * (entry.success_count / safe_usage)
        entry.judgement = Judgement.CORRECT if success else Judgement.INCORRECT
        entry.updated_at = datetime.now().isoformat()
        ok = await self.storage.update_entry(entry_id, usage_count=entry.usage_count,
            success_count=entry.success_count, win_rate=entry.win_rate,
            judgement=entry.judgement.value, updated_at=entry.updated_at)
        if ok and entry_id in self._vectors:
            vec, _ = self._vectors[entry_id]
            self._vectors[entry_id] = (vec, entry.win_rate)
        return ok

    async def evict_low_quality(self) -> int:
        entries = await self.storage.get_all_entries(limit=10000)
        evicted = 0
        for entry in entries:
            if entry.usage_count > 0 and entry.usage_count < self.EVICT_MIN_USAGE and entry.win_rate < self.EVICT_MAX_WIN_RATE:
                if await self.storage.delete_entry(entry.id):
                    self._vectors.pop(entry.id, None)
                    evicted += 1
        if evicted:
            logger.info(f"[Glorious Evolution] 淘汰 {evicted} 条低质量记忆")
        return evicted

    async def get_stats(self) -> Dict[str, Any]:
        stats = await self.storage.get_statistics()
        stats["vector_index_size"] = len(self._vectors)
        stats["embedding_ready"] = self._embed_func is not None
        stats["embedding_dim"] = self._embed_dim
        return stats
