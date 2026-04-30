"""
光荣进化系统 - 进化引擎
MIA Phase 3: 记忆合并提炼 + Insight 生成 + 后台进化循环

v1.0.5: 修复 text_chat 调用签名（统一使用 ProviderRequest）
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.provider import ProviderRequest

from .memory_manager import MemoryManager, _lazy_import_numpy
from .reasoning_engine import ReasoningEngine

logger = logging.getLogger("astrbot")


class EvolutionEngine:
    """
    后台进化引擎。

    不依赖消息事件（event），通过 Context 获取 LLM Provider。
    内置并发保护（asyncio.Lock）、规模上限、LLM 并发信号量。
    """

    CONSOLIDATION_SIM_THRESHOLD: float = 0.85
    MAX_CONSOLIDATE_CANDIDATES: int = 500
    LLM_CONCURRENCY: int = 3
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

        self._evo_lock: asyncio.Lock = asyncio.Lock()
        self._llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(self.LLM_CONCURRENCY)

    # ── Provider 获取 ──

    def _get_provider(self) -> Any:
        """获取 LLM Provider（优先 using_provider，降级 all_providers[0]）"""
        provider = self.context.get_using_provider()
        if not provider:
            providers = self.context.get_all_providers()
            if providers:
                provider = providers[0]
        if not provider:
            raise RuntimeError("No LLM provider for background evolution tasks")
        return provider

    # ── 受控 LLM 调用 (v1.0.5: 统一使用 ProviderRequest) ──

    async def _call_llm_consolidate(
        self,
        provider: Any,
        cluster_entries: List,
    ) -> Optional[str]:
        """带信号量保护的 LLM 合并调用"""
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
                req = ProviderRequest(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=0.0,
                )
                response = await provider.text_chat(req)
            return response.completion_text.strip()

    # ── Union-Find ──

    @staticmethod
    def _union_find(n: int, edges: List[Tuple[int, int]]) -> List[List[int]]:
        """Union-Find（路径压缩 + 按秩合并 + 迭代 find，避免递归深度问题）"""
        parent: Dict[int, int] = {i: i for i in range(n)}
        rank: Dict[int, int] = {i: 0 for i in range(n)}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
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

        clusters: Dict[int, List[int]] = {}
        for i in range(n):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        return list(clusters.values())

    # ── 情景记忆合并提炼 ──

    async def consolidate_episodic_memories(self) -> int:
        """记忆合并提炼"""
        np = _lazy_import_numpy()

        all_entries = await self.memory_mgr.storage.get_all_entries(limit=10000)
        episodic = [e for e in all_entries if e.memory_type.value == "episodic"]

        if len(episodic) < 2:
            logger.debug("[Evolution] episodic 记忆不足 2 条，跳过合并")
            return 0

        if len(episodic) > self.MAX_CONSOLIDATE_CANDIDATES:
            logger.info(
                f"[Evolution] episodic 候选 {len(episodic)} 条 → "
                f"截断至 {self.MAX_CONSOLIDATE_CANDIDATES}"
            )
            episodic = episodic[:self.MAX_CONSOLIDATE_CANDIDATES]

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

        if len(valid_entries) < 2 or np is None:
            logger.debug("[Evolution] 有效向量不足 2 条或 numpy 不可用，跳过合并")
            return 0

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
                if sim > 0 and sim > self.CONSOLIDATION_SIM_THRESHOLD:
                    edges.append((i, j))

        if not edges:
            logger.debug("[Evolution] 无相似记忆对，跳过合并")
            return 0

        clusters = self._union_find(n, edges)
        big_clusters = [c for c in clusters if len(c) >= 2]

        if not big_clusters:
            logger.debug("[Evolution] 无足够大的簇，跳过合并")
            return 0

        consolidated_count = 0
        provider = self._get_provider()
        logger.info(f"[Evolution] 开始合并 {len(big_clusters)} 个簇")

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

        # 分类漂移修正
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
        """受控调用 LLM 并存储合并结果"""
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

        source_ids = [e.id for e in cluster_entries]
        rules_json = json.dumps(source_ids, ensure_ascii=False)

        category = "consolidated_rule"
        if self.memory_mgr._classify_func is not None:
            try:
                new_cat = await self.memory_mgr._classify_func(rule_text[:200], rule_text)
                if new_cat in ("debugging", "deployment", "coding", "configuration",
                               "security", "general", "insight", "consolidated_rule"):
                    category = new_cat
            except Exception:
                pass

        entry_id = await self.memory_mgr.add_memory(
            question=rule_text[:200],
            content=rule_text,
            memory_type="declarative",
            category=category,
            rules=rules_json,
        )

        logger.info(
            f"[Evolution] 合并规则 {entry_id}: "
            f"来源 {len(source_ids)} 条 episodic ({', '.join(source_ids)}) category={category}"
        )
        return True

    # ── 胜率 Insight 生成 (v1.0.5: 统一 ProviderRequest) ──

    async def generate_insights(self) -> int:
        """胜率 Insight 生成"""
        stats = await self.memory_mgr.get_stats()
        if not stats or stats.get("total_memories", 0) == 0:
            logger.debug("[Evolution] 无记忆数据，跳过 Insight 生成")
            return 0

        all_entries = await self.memory_mgr.storage.get_all_entries(limit=10000)
        used_entries = [e for e in all_entries if e.usage_count > 0]

        if not used_entries:
            logger.debug("[Evolution] 无已使用的记忆，跳过 Insight 生成")
            return 0

        used_entries.sort(key=lambda e: e.win_rate, reverse=True)
        top5 = used_entries[:5]
        bottom5 = used_entries[-5:] if len(used_entries) >= 5 else used_entries

        stats_text_lines = [
            f"记忆总数: {stats.get('total_memories', 0)}",
            f"平均胜率: {stats.get('avg_win_rate', 0):.0%}",
            f"向量索引大小: {stats.get('vector_index_size', 0)}",
        ]
        for key, label in [("by_type", "类型分布"), ("by_judgement", "评判分布"), ("by_category", "分类分布")]:
            d = stats.get(key, {})
            if d:
                stats_text_lines.append(f"{label}: " + ", ".join(f"{k}={v}" for k, v in d.items()))

        top_lines = [f"{i}. {e.id} 胜率={e.win_rate:.0%} 使用={e.usage_count}次 [{e.category}] Q: {e.question[:80]}"
                     for i, e in enumerate(top5, 1)]
        bottom_lines = [f"{i}. {e.id} 胜率={e.win_rate:.0%} 使用={e.usage_count}次 [{e.category}] Q: {e.question[:80]}"
                        for i, e in enumerate(bottom5, 1)]

        provider = self._get_provider()

        system_prompt = (
            "你是系统分析师。基于记忆库统计数据分析系统演化趋势，生成可操作的洞察。"
            "输出格式：每条洞察一行，以数字编号开头，包含发现和建议。"
            "输出 3-5 条关键洞察，每条不超过 150 字。"
        )

        user_prompt = (
            "## 系统统计\n" + "\n".join(stats_text_lines)
            + "\n\n## 胜率 Top 5 记忆\n" + "\n".join(top_lines)
            + "\n\n## 胜率 Bottom 5 记忆\n" + "\n".join(bottom_lines)
            + "\n\n## 要求\n请分析以上数据，输出 3-5 条关键洞察（编号列表）"
        )

        insight_count = 0

        try:
            async with self._llm_semaphore:
                async with asyncio.timeout(30):
                    req = ProviderRequest(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=0.3,
                    )
                    response = await provider.text_chat(req)
                    text = response.completion_text.strip()

            if not text:
                return 0

            insights = [ins.strip() for ins in re.split(r"\n\s*\d+[\.\)、]\s*", text) if ins.strip()]

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
        """完整进化周期：合并 + 洞察 + 淘汰"""
        async with self._evo_lock:
            result = {"consolidated": 0, "insights": 0, "evicted": 0}
            result["consolidated"] = await self.consolidate_episodic_memories()
            result["insights"] = await self.generate_insights()
            result["evicted"] = await self.memory_mgr.evict_low_quality()
            return result
