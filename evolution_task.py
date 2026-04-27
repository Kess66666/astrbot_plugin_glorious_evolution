"""
光荣进化系统 - 进化引擎
MIA Phase 3: 记忆合并提炼 + Insight 生成 + 后台进化循环

职责：
1. consolidate_episodic_memories — 情景记忆向量聚簇 → 陈述性规则提炼
2. generate_insights — 胜率统计分析 → 可操作洞察
3. run_evolution_cycle — 合并 + 洞察 + 淘汰完整周期
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .memory_manager import MemoryManager, _lazy_import_numpy
from .reasoning_engine import ReasoningEngine

logger = logging.getLogger("astrbot")


class EvolutionEngine:
    """
    后台进化引擎。

    不依赖消息事件（event），通过 Context 获取 LLM Provider。
    内置并发保护（asyncio.Lock）、规模上限、LLM 并发信号量。
    """

    # 记忆合并余弦相似度阈值
    CONSOLIDATION_SIM_THRESHOLD: float = 0.85

    # 规模保护：单次合并最多处理的 episodic 候选数（防止 O(n²) 爆炸）
    MAX_CONSOLIDATE_CANDIDATES: int = 500

    # LLM 调用并发上限（防止 API 配额耗尽 / 429）
    LLM_CONCURRENCY: int = 3

    # 单周期超时（秒）
    CYCLE_TIMEOUT: int = 300

    def __init__(
        self,
        memory_mgr: MemoryManager,
        reasoning_engine: ReasoningEngine,
        context: Any,
    ) -> None:
        self.memory_mgr = memory_mgr
        self.reasoning = reasoning_engine
        self.context = context

        # 并发保护：保证同一时间只有一个进化周期
        self._evo_lock: asyncio.Lock = asyncio.Lock()

        # LLM 并发信号量
        self._llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(self.LLM_CONCURRENCY)

    # ── Provider 获取（后台任务专用，无 event） ──

    def _get_provider(self) -> Any:
        """
        后台任务获取 LLM Provider（无 event.unified_msg_origin）。

        优先 get_using_provider()，降级 get_all_providers()[0]。
        """
        provider = self.context.get_using_provider()
        if not provider:
            providers = self.context.get_all_providers()
            if providers:
                provider = providers[0]
        if not provider:
            raise RuntimeError("No LLM provider for background evolution tasks")
        return provider

    # ── 受控 LLM 调用 ──

    async def _call_llm_consolidate(
        self,
        provider: Any,
        cluster_entries: List,
    ) -> Optional[str]:
        """
        带信号量保护的 LLM 合并调用。

        Args:
            provider: LLM provider 实例
            cluster_entries: 簇内 MemoryEntry 列表

        Returns:
            提炼后的规则文本，失败返回 None
        """
        memory_lines = []
        for idx, entry in enumerate(cluster_entries, 1):
            memory_lines.append(
                f"记忆{idx}: Q: {entry.question}\nA: {entry.content}"
            )

        user_prompt = (
            "以下是多条相似的情景记忆，请将它们合并提炼为一条通用的陈述性规则：\n\n"
            + "\n\n".join(memory_lines)
            + "\n\n请输出提炼后的规则（纯文本）："
        )

        system_prompt = (
            "你是一个知识提炼专家。将多条情景记忆合并为一条简洁的陈述性规则。"
            "规则应包含：(1)核心模式 (2)决策准则 (3)适用场景。"
            "输出格式为纯文本规则，不要包含标题或编号。"
        )

        async with self._llm_semaphore:
            async with asyncio.timeout(30):
                response = await provider.text_chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=0.0,
                )
            return response.completion_text.strip()

    # ── Union-Find（纯 Python dict 实现，路径压缩 + 按秩合并） ──

    @staticmethod
    def _union_find(n: int, edges: List[Tuple[int, int]]) -> List[List[int]]:
        """
        简易 Union-Find（路径压缩 + 按秩合并）：返回连通分量列表。

        Args:
            n: 节点总数
            edges: 需要合并的 (i, j) 对

        Returns:
            连通分量列表，每个分量为节点索引列表
        """
        parent: Dict[int, int] = {i: i for i in range(n)}
        rank: Dict[int, int] = {i: 0 for i in range(n)}

        def find(x: int) -> int:
            # Path compression (iterative)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            # Union-by-rank
            rx, ry = find(x), find(y)
            if rx == ry:
                return
            if rank[rx] < rank[ry]:
                rx, ry = ry, rx
            parent[ry] = rx
            if rank[rx] == rank[ry]:
                rank[rx] += 1

        for i, j in edges:
            union(i, j)

        # 收集连通分量
        clusters: Dict[int, List[int]] = {}
        for i in range(n):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        return list(clusters.values())

    # ── 情景记忆合并提炼 ──

    async def consolidate_episodic_memories(self) -> int:
        """
        记忆合并提炼。

        流程：
        1. 获取 episodic 类型记忆（受 MAX_CONSOLIDATE_CANDIDATES 上限）
        2. 对每条的 question+content 做向量化
        3. 计算 pairwise 余弦相似度矩阵（省去一半计算 + 规模保护）
        4. 用 Union-Find 聚簇（相似度 > CONSOLIDATION_SIM_THRESHOLD 且 > 0）
        5. 对每个簇（>=2 条）并发调用 LLM 提炼规则（受 LLM_CONCURRENCY 控制）
        6. 存储合并后的 declarative 规则
        7. 对未合并的 episodic 条目做分类漂移修正
        """
        np = _lazy_import_numpy()

        # 1. 获取 episodic 记忆（硬上限截断）
        all_entries = await self.memory_mgr.storage.get_all_entries(limit=10000)
        episodic = [e for e in all_entries if e.memory_type.value == "episodic"]

        if len(episodic) < 2:
            logger.debug("[Evolution] episodic 记忆不足 2 条，跳过合并")
            return 0

        # 规模保护：只取最近 N 条，防止 O(n²) 爆炸
        if len(episodic) > self.MAX_CONSOLIDATE_CANDIDATES:
            logger.info(
                f"[Evolution] episodic 候选 {len(episodic)} 条 → "
                f"截断至 {self.MAX_CONSOLIDATE_CANDIDATES}"
            )
            episodic = episodic[:self.MAX_CONSOLIDATE_CANDIDATES]

        # 2. 向量化
        vectors: List[Optional[List[float]]] = []
        valid_indices: List[int] = []
        valid_entries: List = []

        for i, entry in enumerate(episodic):
            text = f"{entry.question} {entry.content}"
            vec = await self.memory_mgr.embed_text(text)
            if vec is not None:
                vectors.append(vec)
                valid_indices.append(i)
                valid_entries.append(entry)
            else:
                vectors.append(None)
                logger.warning(f"[Evolution] 向量化失败，跳过 {entry.id}")

        if len(valid_entries) < 2 or np is None:
            logger.debug("[Evolution] 有效向量不足 2 条或 numpy 不可用，跳过合并")
            return 0

        # 3. 计算 pairwise 余弦相似度（O(n²/2)，但受规模限制）
        vec_arrs = [np.array(v, dtype=np.float32) for v in vectors if v is not None]
        n = len(vec_arrs)

        norms = [np.linalg.norm(v) for v in vec_arrs]
        edges: List[Tuple[int, int]] = []

        for i in range(n):
            if norms[i] == 0:
                continue
            for j in range(i + 1, n):
                if norms[j] == 0:
                    continue
                sim = float(np.dot(vec_arrs[i], vec_arrs[j]) / (norms[i] * norms[j]))
                # 仅当正相似度超过阈值时才连接（排除反义向量误导）
                if sim > 0 and sim > self.CONSOLIDATION_SIM_THRESHOLD:
                    edges.append((i, j))

        if not edges:
            logger.debug("[Evolution] 无相似记忆对，跳过合并")
            return 0

        # 4. Union-Find 聚簇
        clusters = self._union_find(n, edges)
        big_clusters = [c for c in clusters if len(c) >= 2]

        if not big_clusters:
            logger.debug("[Evolution] 无足够大的簇，跳过合并")
            return 0

        # 5. 对每个簇并发调用 LLM 提炼规则（受信号量限制）
        consolidated_count = 0
        provider = self._get_provider()

        logger.info(f"[Evolution] 开始合并 {len(big_clusters)} 个簇")

        # 并发执行所有合并任务
        tasks = []
        for cluster_indices in big_clusters:
            cluster_entries = [valid_entries[ci] for ci in cluster_indices]
            tasks.append(self._call_llm_and_store(provider, cluster_entries))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"[Evolution] 合并任务异常: {result}", exc_info=result)
            elif result is True:
                consolidated_count += 1

        # ── 6. 分类漂移修正：对未被合并的 episodic 条目做分类修正 ──
        if self.memory_mgr._classify_func is not None and consolidated_count > 0:
            for entry in episodic:
                if entry.category == "general":
                    try:
                        new_cat = await self.memory_mgr._classify_func(
                            entry.question, entry.content,
                        )
                        if new_cat != "general":
                            await self.memory_mgr.storage.update_entry(
                                entry.id, category=new_cat,
                            )
                            logger.debug(
                                f"[Evolution] 类别漂移修正: {entry.id} general → {new_cat}"
                            )
                    except Exception:
                        pass

        return consolidated_count

    async def _call_llm_and_store(
        self,
        provider: Any,
        cluster_entries: List,
    ) -> bool:
        """
        受控调用 LLM 并存储合并结果。

        合并后使用分类器修正 category（替代默认的 "consolidated_rule"）。

        Returns:
            True 表示成功生成并存储规则
        """
        try:
            rule_text = await self._call_llm_consolidate(provider, cluster_entries)
        except asyncio.TimeoutError:
            logger.error("[Evolution] LLM 合并调用超时 (30s)")
            return False
        except Exception as e:
            logger.error(f"[Evolution] LLM 合并调用失败: {e}", exc_info=True)
            return False

        if not rule_text:
            logger.warning("[Evolution] LLM 返回空规则，跳过簇")
            return False

        # 原始条目 ID 列表
        source_ids = [e.id for e in cluster_entries]
        rules_json = json.dumps(source_ids, ensure_ascii=False)

        # ── 分类修正：用分类器对合并后的规则重新分类 ──
        category = "consolidated_rule"
        if self.memory_mgr._classify_func is not None:
            try:
                new_cat = await self.memory_mgr._classify_func(rule_text[:200], rule_text)
                if new_cat in ("debugging", "deployment", "coding", "configuration",
                               "security", "general", "insight", "consolidated_rule"):
                    category = new_cat
                    logger.info(
                        f"[Evolution] 分类修正: {rule_text[:50]}... → {category}"
                    )
            except Exception:
                pass

        # 存储合并后的规则（使用修正后的 category）
        entry_id = await self.memory_mgr.add_memory(
            question=rule_text[:200],
            content=rule_text,
            memory_type="declarative",
            category=category,
            rules=rules_json,
        )

        logger.info(
            f"[Evolution] 合并规则 {entry_id}: "
            f"来源 {len(source_ids)} 条 episodic "
            f"({', '.join(source_ids)}) category={category}"
        )
        return True

    # ── 胜率 Insight 生成 ──

    async def generate_insights(self) -> int:
        """
        胜率 Insight 生成。

        流程：
        1. 获取统计数据和 top/bottom 胜率记忆
        2. 格式化为结构化 prompt
        3. LLM 生成 3-5 条关键洞察
        4. 每条洞察存储为 declarative 记忆 (category='insight')
        """
        stats = await self.memory_mgr.get_stats()
        if not stats or stats.get("total_memories", 0) == 0:
            logger.debug("[Evolution] 无记忆数据，跳过 Insight 生成")
            return 0

        # 获取 top 5 和 bottom 5 胜率记忆
        all_entries = await self.memory_mgr.storage.get_all_entries(limit=10000)

        # 过滤已使用过的记忆
        used_entries = [e for e in all_entries if e.usage_count > 0]

        if not used_entries:
            logger.debug("[Evolution] 无已使用的记忆，跳过 Insight 生成")
            return 0

        # 排序
        used_entries.sort(key=lambda e: e.win_rate, reverse=True)
        top5 = used_entries[:5]
        bottom5 = used_entries[-5:] if len(used_entries) >= 5 else used_entries

        # 格式化统计数据
        stats_text_lines = [
            f"记忆总数: {stats.get('total_memories', 0)}",
            f"平均胜率: {stats.get('avg_win_rate', 0):.0%}",
            f"向量索引大小: {stats.get('vector_index_size', 0)}",
        ]

        by_type = stats.get("by_type", {})
        if by_type:
            stats_text_lines.append("类型分布: " + ", ".join(
                f"{t}={c}" for t, c in by_type.items()
            ))

        by_judgement = stats.get("by_judgement", {})
        if by_judgement:
            stats_text_lines.append("评判分布: " + ", ".join(
                f"{j}={c}" for j, c in by_judgement.items()
            ))

        by_category = stats.get("by_category", {})
        if by_category:
            stats_text_lines.append("分类分布: " + ", ".join(
                f"{cat}={c}" for cat, c in by_category.items()
            ))

        # 格式化 top/bottom
        top_lines = []
        for i, e in enumerate(top5, 1):
            top_lines.append(
                f"{i}. {e.id} 胜率={e.win_rate:.0%} "
                f"使用={e.usage_count}次 [{e.category}] "
                f"Q: {e.question[:80]}"
            )

        bottom_lines = []
        for i, e in enumerate(bottom5, 1):
            bottom_lines.append(
                f"{i}. {e.id} 胜率={e.win_rate:.0%} "
                f"使用={e.usage_count}次 [{e.category}] "
                f"Q: {e.question[:80]}"
            )

        provider = self._get_provider()

        system_prompt = (
            "你是系统分析师。基于记忆库统计数据分析系统演化趋势，生成可操作的洞察。"
            "输出格式：每条洞察一行，以数字编号开头，包含发现和建议。"
            "输出 3-5 条关键洞察，每条不超过 150 字。"
        )

        user_prompt = (
            "## 系统统计\n"
            + "\n".join(stats_text_lines)
            + "\n\n## 胜率 Top 5 记忆\n"
            + "\n".join(top_lines)
            + "\n\n## 胜率 Bottom 5 记忆\n"
            + "\n".join(bottom_lines)
            + "\n\n## 要求\n"
            "请分析以上数据，输出 3-5 条关键洞察（编号列表）：\n"
            "1. 哪些类型的记忆最有效？为什么？\n"
            "2. 哪些类别需要改进？\n"
            "3. 系统整体演化趋势如何？\n"
            "4. 建议下一步优化方向\n"
        )

        insight_count = 0

        try:
            async with self._llm_semaphore:
                async with asyncio.timeout(30):
                    response = await provider.text_chat(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=0.3,
                    )
                    text = response.completion_text.strip()

            if not text:
                return 0

            # 按编号拆分洞察
            insights = re.split(r"\n\s*\d+[\.\)、]\s*", text)
            insights = [ins.strip() for ins in insights if ins.strip()]

            for insight in insights:
                if len(insight) < 10:
                    continue
                entry_id = await self.memory_mgr.add_memory(
                    question=f"Insight: {insight[:100]}",
                    content=insight,
                    memory_type="declarative",
                    category="insight",
                )
                insight_count += 1
                logger.info(f"[Evolution] 洞察生成: {entry_id}")

        except asyncio.TimeoutError:
            logger.error("[Evolution] LLM Insight 调用超时 (30s)")
        except Exception as e:
            logger.error(f"[Evolution] Insight 生成失败: {e}", exc_info=True)

        return insight_count

    # ── 完整进化周期 ──

    async def run_evolution_cycle(self) -> Dict[str, int]:
        """
        完整进化周期：合并 + 洞察 + 淘汰。

        使用 _evo_lock 保证同一时间只有一个周期运行。

        Returns:
            {"consolidated": N, "insights": N, "evicted": N}
        """
        async with self._evo_lock:
            result = {
                "consolidated": 0,
                "insights": 0,
                "evicted": 0,
            }
            result["consolidated"] = await self.consolidate_episodic_memories()
            result["insights"] = await self.generate_insights()
            result["evicted"] = await self.memory_mgr.evict_low_quality()
            return result
