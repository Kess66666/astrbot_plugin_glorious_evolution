"""
光荣进化系统 - 记忆管理器
MIA 风格的高层封装：add_memory / retrieve_relevant_memories + 向量化钩子

重构内容（对齐 MIA 原型）：
1. 记忆去重 (dedup) — add_memory 存储前按余弦相似度去重
2. 正负记忆分离检索 — retrieve_balanced_memories
3. 检索分数加权 — 0.7*cosine + 0.3*win_rate（MIA 混合评分）
4. 胜率公式修正 — 默认用 MIA 简单比值，可选衰减
5. 分类进化 — 自动分类 + 同类桶优先检索 (v1.0.0)
"""

from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import MemoryEntry, MemoryType, Judgement
from .storage import Storage

# ── 常量 ──
DEDUP_THRESHOLD: float = 0.95      # 去重余弦相似度阈值
COSINE_WEIGHT: float = 0.7         # 混合评分：余弦相似度权重
WIN_RATE_WEIGHT: float = 0.3       # 混合评分：胜率权重
MIN_FEEDBACK_COUNT: int = 3        # 负面记忆判定最小反馈次数门槛
CATEGORY_BOOST: float = 1.15       # 同类桶加权系数

# ── 向量化钩子类型 ──
# 签名: async (text: str) -> list[float]
EmbedFunc = Callable[[str], Coroutine[Any, None, List[float]]]

# ── 分类器钩子类型 ──
# 签名: async (question: str, content: str) -> str
ClassifyFunc = Callable[[str, str], Coroutine[Any, None, str]]


def _lazy_import_numpy() -> Optional[Any]:
    """懒加载 numpy，避免容器未安装时顶层 import 崩溃"""
    try:
        import numpy as np
        return np
    except ImportError:
        logger.warning("[Glorious Evolution] numpy 未安装，向量检索不可用")
        return None


class MemoryManager:
    """
    MIA MemoryBucket 的高层封装。

    职责：
    1. add_memory          — 存储记忆 + 可选向量化 + 去重 + 自动分类
    2. retrieve_relevant   — 向量相似度检索（同类桶优先） + FTS 兜底 + 胜率过滤
    3. retrieve_balanced   — 正负记忆分离检索
    4. update_win_rate     — MIA 胜率更新（简单比值 / 可选衰减）
    5. evict_low_quality   — 低质量记忆淘汰
    6. embed_text 钩子     — 接入 AstrBot EmbeddingProvider
    7. classify 钩子       — 接入 LLM 自动分类器
    """

    # 默认衰减因子：0 = 使用 MIA 简单比值，>0 时使用衰减公式
    DECAY_FACTOR: float = 0.0
    # 淘汰阈值：使用次数 < 阈值 且 胜率 < 阈值
    EVICT_MIN_USAGE: int = 3
    EVICT_MAX_WIN_RATE: float = 0.2

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

        # ── 向量化钩子 ──
        self._embed_func: Optional[EmbedFunc] = None
        self._embed_dim: int = 0

        # ── 分类器钩子 ──
        self._classify_func: Optional[ClassifyFunc] = None
        self._valid_categories: List[str] = [
            "general", "debugging", "deployment", "coding",
            "configuration", "security", "insight", "consolidated_rule",
        ]

        # 内存向量索引：{ entry_id: (np.ndarray, win_rate) }
        self._vectors: Dict[str, Tuple[Any, float]] = {}

        # 生成 ID 的计数器
        self._id_counter: int = 0

    # ── 向量化钩子 ──

    async def set_embed_func(self, func: EmbedFunc, dim: int) -> None:
        """
        注入向量化函数。

        由 main.py 在初始化时调用：
            embed_func = embedding_provider.get_embedding
            dim = embedding_provider.get_dim()
            await memory_mgr.set_embed_func(embed_func, dim)
        """
        self._embed_func = func
        self._embed_dim = dim
        logger.info(f"[Glorious Evolution] 向量化钩子已注入, dim={dim}")

    # ── 分类器钩子 ──

    async def set_classify_func(
        self, func: ClassifyFunc, valid_categories: Optional[List[str]] = None,
    ) -> None:
        """
        注入自动分类函数。

        由 main.py 在初始化时调用，用于对新增/检索记忆进行自动分类。
        """
        self._classify_func = func
        self._valid_categories = valid_categories or [
            "general", "debugging", "deployment", "coding",
            "configuration", "security", "insight", "consolidated_rule",
        ]
        logger.info("[Glorious Evolution] 自动分类器已注入")

    async def embed_text(self, text: str) -> Optional[List[float]]:
        """
        对文本进行向量化。

        - 若已注入 embed_func → 调用 AstrBot EmbeddingProvider
        - 否则 → 返回 None
        """
        if self._embed_func is None:
            logger.debug("[Glorious Evolution] embed_text: 向量化钩子未注入，跳过向量化")
            return None
        try:
            vec = await self._embed_func(text)
            return vec
        except Exception as e:
            logger.error(f"[Glorious Evolution] 向量化失败: {e}")
            return None

    # ── 向量索引辅助 ──

    def _add_vector(self, entry_id: str, embedding: List[float], win_rate: float = 0.0) -> None:
        """将向量与胜率一起写入内存索引"""
        np = _lazy_import_numpy()
        if np is not None and embedding is not None:
            self._vectors[entry_id] = (np.array(embedding, dtype=np.float32), win_rate)

    # ── 启动时加载向量索引 ──

    async def load_vectors(self) -> None:
        """从 SQLite 加载已有 embedding 到内存向量索引"""
        entries = await self.storage.get_all_entries(limit=10000)
        loaded = 0
        for entry in entries:
            if entry.embedding is not None:
                self._add_vector(entry.id, entry.embedding, entry.win_rate)
                loaded += 1
        self._id_counter = len(entries)
        logger.info(
            f"[Glorious Evolution] 向量索引加载完成: "
            f"{loaded}/{len(entries)} 条含向量, counter={self._id_counter}"
        )

    # ── 记忆条目构建（消除重复） ──

    def _build_entry(
        self,
        entry_id: str,
        question: str,
        content: str,
        memory_type: str,
        category: str,
        trajectory: str = "",
        rules: str = "",
        tags: Optional[List[str]] = None,
        embedding: Optional[List[float]] = None,
    ) -> MemoryEntry:
        """构建 MemoryEntry 实例，消除多处重复的字段赋值"""
        return MemoryEntry(
            id=entry_id,
            memory_type=MemoryType(memory_type),
            category=category,
            question=question,
            content=content,
            trajectory=trajectory,
            rules=rules,
            judgement=Judgement.PENDING,
            usage_count=0,
            success_count=0,
            win_rate=0.0,
            embedding=embedding,
            tags=tags or [],
            related_ids=[],
        )

    # ── 去重辅助 ──

    async def _find_duplicate(self, query_vec: List[float]) -> Optional[Tuple[str, float, MemoryEntry]]:
        """
        在内存向量索引中查找与 query_vec 最相似的条目。

        Returns:
            (entry_id, similarity, entry) — 最相似的匹配条目；
            若无匹配或 numpy 不可用则返回 None。
        """
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return None

        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return None

        best_id: Optional[str] = None
        best_score: float = -1.0

        # 仅在向量索引上遍历，不查 DB
        for entry_id, (vec, _win_rate) in self._vectors.items():
            vec_norm = np.linalg.norm(vec)
            if vec_norm == 0:
                continue
            similarity = float(np.dot(query_arr, vec) / (query_norm * vec_norm))
            if similarity > best_score:
                best_score = similarity
                best_id = entry_id

        if best_id is None or best_score < DEDUP_THRESHOLD:
            return None

        # 找到候选后只查一次 DB
        entry = await self.storage.get_entry(best_id)
        if entry is None:
            return None

        return (best_id, best_score, entry)

    async def _replace_entry(self, old_id: str, new_entry: MemoryEntry) -> None:
        """
        替换旧记忆：先写入新条目，成功后再删除旧条目（避免先删后写的数据丢失风险）。
        旧条目删除失败仅记录日志，不抛异常。
        """
        # 先写入新条目
        await self.storage.add_entry(new_entry)
        if new_entry.embedding is not None:
            self._add_vector(new_entry.id, new_entry.embedding, new_entry.win_rate)

        # 后删除旧条目（失败只记日志）
        try:
            await self.storage.delete_entry(old_id)
        except Exception as e:
            logger.warning(
                f"[Glorious Evolution] 删除旧记忆 {old_id} 失败（已不影响新条目）: {e}"
            )
        self._vectors.pop(old_id, None)

    # ── 核心 API ──

    async def add_memory(
        self,
        question: str,
        content: str,
        memory_type: str = "procedural",
        category: str = "general",
        trajectory: str = "",
        rules: str = "",
        tags: Optional[List[str]] = None,
    ) -> str:
        """
        MIA 风格的记忆存储接口（含去重 + 自动分类逻辑）。

        去重流程：
        1. 对 question 向量化
        2. 在内存向量索引中计算余弦相似度
        3. 若最大相似度 >= DEDUP_THRESHOLD (0.95)：
           - 旧记忆 judgement == "incorrect" → 直接替换
           - 旧记忆 judgement == "correct" → 保留 content 更短的
           - 否则 → 新增
        4. 若相似度 < DEDUP_THRESHOLD → 正常新增

        自动分类流程：
        - 若 category == "general" 且分类器已注入，调用分类器自动分类
        - 分类失败静默降级为 "general"
        """
        # 生成 ID
        self._id_counter += 1
        date_str = datetime.now().strftime("%Y%m%d")
        entry_id = f"MEM-{date_str}-{self._id_counter:03d}"

        # ── 自动分类（仅对 category == "general" 且分类器可用时） ──
        if category == "general" and self._classify_func is not None:
            try:
                category = await self._classify_func(question, content)
                if category not in self._valid_categories:
                    category = "general"
                logger.debug(f"[Glorious Evolution] 自动分类: {entry_id} → {category}")
            except Exception:
                category = "general"

        # 向量化：对 question 做嵌入
        embedding = None
        if self._embed_func is not None:
            embedding = await self.embed_text(question)

        # ── 去重检查 ──
        if embedding is not None:
            dup_result = await self._find_duplicate(embedding)
            if dup_result is not None:
                old_id, similarity, old_entry = dup_result
                return await self._handle_duplicate(
                    old_id, similarity, old_entry,
                    entry_id, question, content,
                    memory_type, category, trajectory, rules,
                    tags, embedding,
                )

        # ── 无重复 → 正常新增 ──
        entry = self._build_entry(
            entry_id, question, content, memory_type, category,
            trajectory, rules, tags, embedding,
        )

        saved_id = await self.storage.add_entry(entry)

        if embedding is not None:
            self._add_vector(entry_id, embedding)

        logger.info(
            f"[Glorious Evolution] 新增记忆: {entry_id} "
            f"type={memory_type} category={category} "
            f"embedded={'yes' if embedding else 'no'}"
        )
        return saved_id

    async def _handle_duplicate(
        self,
        old_id: str,
        similarity: float,
        old_entry: MemoryEntry,
        new_id: str,
        question: str,
        content: str,
        memory_type: str,
        category: str,
        trajectory: str,
        rules: str,
        tags: Optional[List[str]],
        embedding: Optional[List[float]],
    ) -> str:
        """
        处理去重决策。

        Args:
            old_id: 旧记忆 ID
            similarity: 余弦相似度
            old_entry: 旧记忆条目
            new_id: 新记忆 ID
            其余参数：新记忆的各字段

        Returns:
            最终保留的 entry_id
        """
        logger.info(
            f"[Glorious Evolution] 去重触发: new={new_id} vs old={old_id} "
            f"similarity={similarity:.4f} old_judgement={old_entry.judgement.value}"
        )

        # 旧记忆被判定为无效 → 直接替换
        if old_entry.judgement == Judgement.INCORRECT:
            new_entry = self._build_entry(
                new_id, question, content, memory_type, category,
                trajectory, rules, tags, embedding,
            )
            await self._replace_entry(old_id, new_entry)
            logger.info(
                f"[Glorious Evolution] 去重替换: 旧记忆 {old_id} 为无效，已替换为 {new_id}"
            )
            return new_id

        # 旧记忆被判定为有效 → 保留 content 更短的那个
        if old_entry.judgement == Judgement.CORRECT:
            if len(content) < len(old_entry.content):
                # 新 content 更短 → 替换
                new_entry = self._build_entry(
                    new_id, question, content, memory_type, category,
                    trajectory, rules, tags, embedding,
                )
                await self._replace_entry(old_id, new_entry)
                logger.info(
                    f"[Glorious Evolution] 去重替换: 新内容更短，"
                    f"替换 {old_id} → {new_id}"
                )
                return new_id
            else:
                # 旧 content 更短或相同 → 保留旧的
                logger.info(
                    f"[Glorious Evolution] 去重保留: 旧记忆 {old_id} content 更短或相同，跳过新增"
                )
                return old_id

        # 旧记忆为 pending → 直接新增（不去重）
        logger.info(
            f"[Glorious Evolution] 去重跳过: 旧记忆 {old_id} 状态为 pending，新增 {new_id}"
        )

        entry = self._build_entry(
            new_id, question, content, memory_type, category,
            trajectory, rules, tags, embedding,
        )
        saved_id = await self.storage.add_entry(entry)
        if embedding is not None:
            self._add_vector(new_id, embedding)
        return saved_id

    async def retrieve_relevant_memories(
        self,
        query: str,
        top_k: int = 5,
        min_win_rate: float = 0.0,
        memory_type: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """
        MIA 风格的记忆检索接口（含同类桶优先）。

        策略：
        1. 若有向量索引 → 先分类查询 → MIA 混合评分检索 + 胜率过滤 + 同类桶加权
        2. 否则 → FTS5 全文搜索 + 胜率过滤

        混合评分公式：score = COSINE_WEIGHT * cosine_similarity + WIN_RATE_WEIGHT * win_rate
        同类桶加权：匹配 category 的条目 score *= CATEGORY_BOOST
        """
        # 向量检索路径
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                # 先分类查询
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"

                results = await self._vector_search(
                    query_vec, top_k=top_k, min_win_rate=min_win_rate,
                    memory_type=memory_type, query_category=query_category,
                )
                if results:
                    return results

        # FTS5 兜底路径
        return await self.storage.search_entries(
            query=query,
            top_k=top_k,
            memory_type=memory_type,
            min_win_rate=min_win_rate,
        )

    async def retrieve_balanced_memories(
        self,
        query: str,
        pos_top_k: int = 2,
        neg_top_k: int = 2,
        min_win_rate: float = 0.0,
        memory_type: Optional[str] = None,
    ) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        """
        正负记忆分离检索（MIA 风格）。

        返回 (positive_memories, negative_memories)：
        - positive: 有效记忆（judgement == correct 或 win_rate > 0.5）
        - negative: 无效记忆（judgement == incorrect 或 win_rate <= 0.5）

        检索策略：
        1. 向量检索路径 → MIA 混合评分，按正/负分别取 top_k
        2. FTS5 兜底 → 按 judgement 分类
        """
        positive: List[MemoryEntry] = []
        negative: List[MemoryEntry] = []

        # 向量检索路径
        if self._embed_func is not None and self._vectors:
            query_vec = await self.embed_text(query)
            if query_vec is not None:
                # 先分类查询
                query_category = "general"
                if self._classify_func is not None:
                    try:
                        query_category = await self._classify_func(query, query[:200])
                    except Exception:
                        query_category = "general"

                # 多取一些，留分类余量
                needed = (pos_top_k + neg_top_k) * 3
                candidates = await self._vector_search(
                    query_vec, top_k=needed, min_win_rate=min_win_rate,
                    memory_type=memory_type, query_category=query_category,
                )
                if candidates:
                    for entry in candidates:
                        # 独立 if：一个 entry 可同时满足两个条件，但优先放入高优先级桶
                        if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                            positive.append(entry)
                        if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                            negative.append(entry)
                        if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                            break

                    if positive or negative:
                        logger.debug(
                            f"[Glorious Evolution] balanced 检索: "
                            f"pos={len(positive)} neg={len(negative)}"
                        )
                        return positive, negative

        # FTS5 兜底路径
        needed = (pos_top_k + neg_top_k) * 2
        all_results = await self.storage.search_entries(
            query=query,
            top_k=needed,
            memory_type=memory_type,
            min_win_rate=min_win_rate,
        )
        for entry in all_results:
            if self._is_positive_memory(entry) and len(positive) < pos_top_k:
                positive.append(entry)
            if self._is_negative_memory(entry) and len(negative) < neg_top_k:
                negative.append(entry)
            if len(positive) >= pos_top_k and len(negative) >= neg_top_k:
                break

        logger.debug(
            f"[Glorious Evolution] balanced 检索(FTS): "
            f"pos={len(positive)} neg={len(negative)}"
        )
        return positive, negative

    @staticmethod
    def _is_positive_memory(entry: MemoryEntry) -> bool:
        """判断是否为正面记忆：judgement == correct 或 win_rate > 0.5"""
        return entry.judgement == Judgement.CORRECT or entry.win_rate > 0.5

    @staticmethod
    def _is_negative_memory(entry: MemoryEntry) -> bool:
        """
        判断是否为负面记忆。

        优先级：
        1. judgement == incorrect → 一定为负面
        2. win_rate <= 0.5 且已使用过足够次数（>= MIN_FEEDBACK_COUNT）→ 为负面
           （使用次数太少时不判定为负面，避免冷启动误判）
        """
        if entry.judgement == Judgement.INCORRECT:
            return True
        return entry.usage_count >= MIN_FEEDBACK_COUNT and entry.win_rate <= 0.5

    async def _vector_search(
        self,
        query_vec: List[float],
        top_k: int = 5,
        min_win_rate: float = 0.0,
        memory_type: Optional[str] = None,
        query_category: str = "general",
    ) -> List[MemoryEntry]:
        """
        内存向量相似度检索（MIA 混合评分 + 同类桶优先）。

        评分公式：
        - base_score = COSINE_WEIGHT * cosine_similarity + WIN_RATE_WEIGHT * win_rate
        - 若 entry.category == query_category 且 query_category != "general"：
            score = base_score * CATEGORY_BOOST

        两阶段排序：
        1. 快速粗排：从 _vectors 计算 base_score，多取 top_k*3 候选
        2. 精排：获取 MemoryEntry 后应用同类桶加权，重排序取 top_k
        """
        np = _lazy_import_numpy()
        if np is None or not self._vectors:
            return []

        query_arr = np.array(query_vec, dtype=np.float32)
        query_norm = np.linalg.norm(query_arr)
        if query_norm == 0:
            return []

        # ── 第一阶段：粗排（仅基于向量索引，无 DB 查询） ──
        scores: List[Tuple[str, float]] = []
        for entry_id, (vec, win_rate) in self._vectors.items():
            vec_norm = np.linalg.norm(vec)
            if vec_norm == 0:
                continue
            cosine_sim = float(np.dot(query_arr, vec) / (query_norm * vec_norm))
            cosine_normalized = (cosine_sim + 1.0) / 2.0  # [-1,1] → [0,1]
            base_score = COSINE_WEIGHT * cosine_normalized + WIN_RATE_WEIGHT * win_rate
            scores.append((entry_id, base_score))

        # 按 base_score 降序排列
        scores.sort(key=lambda x: x[1], reverse=True)

        # ── 第二阶段：精排（获取完整 MemoryEntry，应用同类桶加权） ──
        results: List[MemoryEntry] = []
        seen_ids: set = set()
        scored_entries: List[Tuple[float, MemoryEntry]] = []

        for entry_id, base_score in scores[:top_k * 3]:
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entry = await self.storage.get_entry(entry_id)
            if entry is None:
                continue
            if min_win_rate > 0 and entry.win_rate < min_win_rate:
                continue
            if memory_type and entry.memory_type.value != memory_type:
                continue

            # 同类桶加权
            final_score = base_score
            if query_category != "general" and entry.category == query_category:
                final_score *= CATEGORY_BOOST

            scored_entries.append((final_score, entry))

        # 按最终评分重排序
        scored_entries.sort(key=lambda x: x[0], reverse=True)

        for _, entry in scored_entries[:top_k]:
            results.append(entry)

        return results

    async def update_win_rate(self, entry_id: str, success: bool) -> bool:
        """
        MIA 胜率更新。

        公式（由 DECAY_FACTOR 控制）：
        - DECAY_FACTOR == 0（默认）：win_rate = success_count / usage_count（MIA 简单比值）
        - DECAY_FACTOR > 0：new = decay * old + (1 - decay) * (success_count / usage_count)（衰减）
        """
        entry = await self.storage.get_entry(entry_id)
        if entry is None:
            logger.warning(f"[Glorious Evolution] 胜率更新失败，记忆不存在: {entry_id}")
            return False

        entry.usage_count += 1
        if success:
            entry.success_count += 1

        # 除零防御：使用 max(1, usage_count)
        safe_usage = max(1, entry.usage_count)

        if self.DECAY_FACTOR <= 0:
            # MIA 原版：简单比值
            entry.win_rate = entry.success_count / safe_usage
        else:
            # 衰减公式
            entry.win_rate = (
                self.DECAY_FACTOR * entry.win_rate
                + (1 - self.DECAY_FACTOR) * (entry.success_count / safe_usage)
            )

        entry.judgement = Judgement.CORRECT if success else Judgement.INCORRECT
        entry.updated_at = datetime.now().isoformat()

        # 写回 SQLite
        ok = await self.storage.update_entry(
            entry_id,
            usage_count=entry.usage_count,
            success_count=entry.success_count,
            win_rate=entry.win_rate,
            judgement=entry.judgement.value,
            updated_at=entry.updated_at,
        )
        if ok:
            # 同步更新内存向量索引中的 win_rate
            if entry_id in self._vectors:
                vec, _old_wr = self._vectors[entry_id]
                self._vectors[entry_id] = (vec, entry.win_rate)

            logger.debug(
                f"[Glorious Evolution] 胜率更新: {entry_id} "
                f"win_rate={entry.win_rate:.4f} "
                f"(usage={entry.usage_count}, success={entry.success_count}) "
                f"mode={'decay' if self.DECAY_FACTOR > 0 else 'ratio'}"
            )
        return ok

    async def evict_low_quality(self) -> int:
        """
        MIA 风格的记忆淘汰。

        策略：使用次数 < EVICT_MIN_USAGE 且 胜率 < EVICT_MAX_WIN_RATE
        返回被淘汰的数量。
        """
        entries = await self.storage.get_all_entries(limit=10000)
        evicted_count = 0
        for entry in entries:
            if (
                entry.usage_count > 0
                and entry.usage_count < self.EVICT_MIN_USAGE
                and entry.win_rate < self.EVICT_MAX_WIN_RATE
            ):
                deleted = await self.storage.delete_entry(entry.id)
                if deleted:
                    self._vectors.pop(entry.id, None)
                    evicted_count += 1
                    logger.debug(f"[Glorious Evolution] 淘汰低质量记忆: {entry.id}")

        if evicted_count > 0:
            logger.info(f"[Glorious Evolution] 本轮淘汰 {evicted_count} 条低质量记忆")
        return evicted_count

    # ── 统计 ──

    async def get_stats(self) -> Dict[str, Any]:
        """获取统计快照（代理 storage.get_statistics）"""
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
