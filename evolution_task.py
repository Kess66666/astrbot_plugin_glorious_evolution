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
from typing import Any, Dict, List, Optional, Tuple

from .memory_manager import MemoryManager, _lazy_import_numpy
from .reasoning_engine import ReasoningEngine

logger = logging.getLogger("astrbot")


class EvolutionEngine:
    """
    后台进化引擎。

    不依赖消息事件（event），通过 Context 获取 LLM Provider。
    """

    # 记忆合并余弦相似度阈值
    CONSOLIDATION_SIM_THRESHOLD: float = 0.85

    def __init__(
        self,
        memory_mgr: MemoryManager,
        reasoning_engine: ReasoningEngine,
        context: Any,
    ) -> None:
        self.memory_mgr = memory_mgr
        self.reasoning = reasoning_engine
        self.context = context

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

    # ── Union-Find（纯 Python dict 实现） ──

    @staticmethod
    def _union_find(n: int, edges: List[Tuple[int, int]]) -> List[List[int]]:
        """
        简易 Union-Find：返回连通分量列表。

        Args:
            n: 节点总数
            edges: 需要合并的 (i, j) 对

        Returns:
            连通分量列表，每个分量为节点索引列表
        """
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
        1. 获取所有 episodic 类型记忆
        2. 对每条的 question+content 做向量化
        3. 计算 pairwise 余弦相似度矩阵
        4. 用 Union-Find 聚簇（相似度 > CONSOLIDATION_SIM_THRESHOLD）
        5. 对每个簇（>=2 条）调用 LLM 提炼规则
        6. 存储合并后的 declarative 规则
        """
        np = _lazy_import_numpy()

        # 1. 获取所有 episodic 记忆
        all_entries = await self.memory_mgr.storage.get_all_entries(limit=10000)
        episodic = [e for e in all_entries if e.memory_type.value == "episodic"]

        if len(episodic) < 2:
            logger.debug("[Evolution] episodic 记忆不足 2 条，跳过合并")
            return 0

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

        # 3. 计算 pairwise 余弦相似度
        vec_arrs = [np.array(v, dtype=np.float32) for v in vectors if v is not None]
        n = len(vec_arrs)

        # 归一化
        norms = [np.linalg.norm(v) for v in vec_arrs]
        edges: List[Tuple[int, int]] = []

        for i in range(n):
            if norms[i] == 0:
                continue
            for j in range(i + 1, n):
                if norms[j] == 0:
                    continue
                sim = float(np.dot(vec_arrs[i], vec_arrs[j]) / (norms[i] * norms[j]))
                if sim > self.CONSOLIDATION_SIM_THRESHOLD:
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

        # 5. 对每个簇调用 LLM 提炼规则
        consolidated_count = 0
        provider = self._get_provider()

        system_prompt = (
            "你是一个知识提炼专家。将多条情景记忆合并为一条简洁的陈述性规则。"
            "规则应包含：(1)核心模式 (2)决策准则 (3)适用场景。"
            "输出格式为纯文本规则，不要包含标题或编号。"
        )

        for cluster_indices in big_clusters:
            # 构建簇内记忆文本
            cluster_entries = [valid_entries[ci] for ci in cluster_indices]
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

            try:
                async with asyncio.timeout(30):
                    response = await provider.text_chat(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=0.0,
                    )
                    rule_text = response.completion_text.strip()

                if not rule_text:
                    logger.warning(f"[Evolution] LLM 返回空规则，跳过簇")
                    continue

                # 原始条目 ID 列表
                source_ids = [e.id for e in cluster_entries]
                rules_json = json.dumps(source_ids, ensure_ascii=False)

                # 存储合并后的规则
                entry_id = await self.memory_mgr.add_memory(
                    question=rule_text[:200],
                    content=rule_text,
                    memory_type="declarative",
                    category="consolidated_rule",
                    rules=rules_json,
                )

                consolidated_count += 1
                logger.info(
                    f"[Evolution] 合并规则 {entry_id}: "
                    f"来源 {len(source_ids)} 条 episodic "
                    f"({', '.join(source_ids)})"
                )

            except asyncio.TimeoutError:
                logger.error("[Evolution] LLM 合并调用超时 (30s)")
            except Exception as e:
                logger.error(f"[Evolution] 合并簇失败: {e}", exc_info=True)

        return consolidated_count

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
            import re
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

        Returns:
            {"consolidated": N, "insights": N, "evicted": N}
        """
        result = {
            "consolidated": 0,
            "insights": 0,
            "evicted": 0,
        }
        result["consolidated"] = await self.consolidate_episodic_memories()
        result["insights"] = await self.generate_insights()
        result["evicted"] = await self.memory_mgr.evict_low_quality()
        return result
