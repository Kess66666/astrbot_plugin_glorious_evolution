"""
光荣进化系统 v1.0.0 - AstrBot 插件
融合 MIA 智能记忆框架与自改进机制的进化系统

三层架构（v1.0.0 Full MIA）：
  Memory    → storage.py + memory_manager.py  (SQLite + 向量索引 + 胜率)
  Reasoning → reasoning_engine.py            (Plan → Judge → Replan)
  Evolution → evolution_task.py              (合并提炼 + Insight + 淘汰)
"""

import asyncio
import os

from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

from .storage import Storage
from .memory_manager import MemoryManager
from .reasoning_engine import ReasoningEngine
from .evolution_task import EvolutionEngine
from .models import MemoryType, Judgement


class GloriousEvolutionPlugin(Star):
    """光荣进化系统主插件类"""

    def __init__(self, context: Context, **kwargs):
        super().__init__(context)
        self.config = kwargs.get("config") or {}

        # ── 数据层：SQLite + MemoryManager ──
        data_dir = os.path.dirname(__file__)
        self.storage = Storage(data_dir)
        self.memory_mgr = MemoryManager(self.storage)

        # ── 推理层：ReasoningEngine ──
        self.reasoning = ReasoningEngine(self.memory_mgr, self.context)

        # ── 进化层：EvolutionEngine ──
        self.evolution = EvolutionEngine(self.memory_mgr, self.reasoning, self.context)

        # 后台进化任务句柄
        self._evo_task = None

        logger.info("[Glorious Evolution] v1.0.0 初始化完成 (Memory + Reasoning + Evolution)")

    # ── Embedding 供应商注入 ──

    async def _init_embedding_provider(self) -> None:
        """
        注入 AstrBot EmbeddingProvider 作为向量化钩子。

        通过 context.get_all_embedding_providers() 获取用户在
        AstrBot 后台配置的 embedding 供应商，取第一个可用的。
        """
        try:
            emb_providers = self.context.get_all_embedding_providers()
            if emb_providers:
                provider = emb_providers[0]
                dim = provider.get_dim()
                await self.memory_mgr.set_embed_func(
                    provider.get_embedding,
                    dim,
                )
                logger.info(
                    f"[Glorious Evolution] EmbeddingProvider 钩子注入成功 "
                    f"(dim={dim})"
                )
            else:
                logger.warning(
                    "[Glorious Evolution] 未找到 EmbeddingProvider，"
                    "向量检索不可用，仅使用 FTS5 全文搜索"
                )
        except Exception as e:
            logger.error(f"[Glorious Evolution] EmbeddingProvider 注入失败: {e}")

    # ── 生命周期 ──

    async def start(self) -> None:
        """插件启动后：注入 Embedding 供应商 + 加载向量索引 + 启动后台进化"""
        await self._init_embedding_provider()
        await self.memory_mgr.load_vectors()

        # 启动后台进化循环：每 6 小时运行一次
        self._evo_task = asyncio.create_task(self._evolution_loop())

        logger.info("[Glorious Evolution] v1.0.0 启动完成 (Memory + Reasoning + Evolution)")

    async def terminate(self) -> None:
        """插件关闭时清理资源"""
        if self._evo_task:
            self._evo_task.cancel()

    async def _evolution_loop(self) -> None:
        """后台进化循环，每 6 小时运行一次"""
        # 首次启动后等 5 分钟再跑第一次（给系统预热时间）
        await asyncio.sleep(300)

        while True:
            try:
                logger.info("[Glorious Evolution] 进化周期开始...")
                result = await self.evolution.run_evolution_cycle()
                logger.info(
                    f"[Glorious Evolution] 进化周期完成: "
                    f"consolidated={result['consolidated']} "
                    f"insights={result['insights']} "
                    f"evicted={result['evicted']}"
                )
            except Exception as e:
                logger.error(f"[Glorious Evolution] 进化周期异常: {e}", exc_info=True)

            # 每 6 小时 (21600 秒)
            await asyncio.sleep(21600)

    # ── LLM Tool：存储记忆 ──

    @filter.llm_tool(name="store_memory", description="存储一条智能记忆，用于积累经验、规则或知识。")
    async def store_memory(
        self,
        event: AstrMessageEvent,
        question: str,
        content: str,
        memory_type: str = "procedural",
        category: str = "general",
    ):
        """存储一条智能记忆。

        Args:
            question (str): 触发内容或原始问题
            content (str): 记忆正文、执行计划或经验总结
            memory_type (str): 记忆类型 — procedural/declarative/episodic
            category (str): 分类标签，如 general, debugging, deployment, coding 等
        """
        entry_id = await self.memory_mgr.add_memory(
            question=question,
            content=content,
            memory_type=memory_type,
            category=category,
        )
        yield event.plain_result(f"✅ 记忆已存储: {entry_id}")

    # ── LLM Tool：搜索记忆 ──

    @filter.llm_tool(name="search_memory", description="搜索相关记忆，检索过往经验、规则或知识。")
    async def search_memory(
        self,
        event: AstrMessageEvent,
        query: str,
        top_k: int = 5,
        min_win_rate: float = 0.0,
    ):
        """搜索相关记忆，检索过往经验、规则或知识。

        Args:
            query (str): 搜索关键词或自然语言描述
            top_k (int): 返回最大条数，默认 5
            min_win_rate (float): 最低胜率过滤，0.0 不过滤
        """
        results = await self.memory_mgr.retrieve_relevant_memories(
            query=query,
            top_k=top_k,
            min_win_rate=min_win_rate,
        )
        if not results:
            yield event.plain_result("🔍 未找到相关记忆")
            return

        lines = [f"🔍 找到 {len(results)} 条相关记忆:\n"]
        for i, entry in enumerate(results, 1):
            win_rate_bar = "█" * int(entry.win_rate * 10) + "░" * (10 - int(entry.win_rate * 10))
            lines.append(
                f"{i}. [{entry.memory_type.value}] {entry.question[:60]}\n"
                f"   📊 胜率: {win_rate_bar} {entry.win_rate:.0%} "
                f"(使用{entry.usage_count}次)\n"
                f"   📝 {entry.content[:100]}"
            )
        yield event.plain_result("\n".join(lines))

    # ── LLM Tool：更新胜率 ──

    @filter.llm_tool(name="update_win_rate", description="更新某条记忆的胜率，标记其是否有效。")
    async def update_win_rate_tool(
        self,
        event: AstrMessageEvent,
        entry_id: str,
        success: bool,
    ):
        """更新某条记忆的胜率，标记其是否有效。

        Args:
            entry_id (str): 记忆条目 ID（如 MEM-20260427-001）
            success (bool): 该记忆是否在本次使用中有效
        """
        ok = await self.memory_mgr.update_win_rate(entry_id, success)
        if ok:
            entry = await self.storage.get_entry(entry_id)
            win_rate = entry.win_rate if entry else 0
            yield event.plain_result(
                f"📊 胜率已更新: {entry_id} → {win_rate:.0%}"
                f" ({'✅ 有效' if success else '❌ 无效'})"
            )
        else:
            yield event.plain_result(f"⚠️ 更新失败，记忆不存在: {entry_id}")

    # ── LLM Tool：淘汰低质量记忆 ──

    @filter.llm_tool(name="evict_memories", description="淘汰低胜率、低使用的记忆，保持记忆库健康。")
    async def evict_memories(self, event: AstrMessageEvent):
        """淘汰低胜率、低使用的记忆，保持记忆库健康。"""
        count = await self.memory_mgr.evict_low_quality()
        yield event.plain_result(
            f"🗑️ 本轮淘汰 {count} 条低质量记忆"
            if count > 0
            else "✨ 记忆库状态健康，无需淘汰"
        )

    # ── LLM Tool：进化统计 ──

    @filter.llm_tool(name="get_evolution_stats", description="获取光荣进化系统的统计概览。")
    async def get_evolution_stats_tool(self, event: AstrMessageEvent):
        """获取光荣进化系统的统计概览。"""
        stats = await self.memory_mgr.get_stats()
        if not stats:
            yield event.plain_result("📊 暂无数据")
            return

        yield event.plain_result(self._format_stats(stats))

    # ── LLM Tool：手动触发进化 ──

    @filter.llm_tool(name="trigger_evolution", description="手动触发一次进化周期，包括记忆合并、洞察生成和淘汰。")
    async def trigger_evolution_tool(self, event: AstrMessageEvent):
        """手动触发一次完整的进化周期。"""
        result = await self.evolution.run_evolution_cycle()
        yield event.plain_result(
            f"🧬 进化周期完成\n"
            f"📦 合并规则: {result['consolidated']} 条\n"
            f"💡 生成洞察: {result['insights']} 条\n"
            f"🗑️ 淘汰记忆: {result['evicted']} 条"
        )

    # ── LLM Tool：生成行动计划 ──

    @filter.llm_tool(name="build_plan", description="基于记忆库生成行动计划。输入问题，返回基于过往成功/失败经验的分步策略。")
    async def build_plan_tool(
        self,
        event: AstrMessageEvent,
        question: str,
        extra_context: str = "",
    ):
        """基于记忆库生成行动计划。

        Args:
            question (str): 用户问题或目标
            extra_context (str): 额外上下文信息（可选）
        """
        plan, pos, neg = await self.reasoning.build_plan(event, question, extra_context)

        result_lines = [
            "📋 行动计划",
            "━" * 24,
            plan,
            "",
            f"📚 参考记忆: {len(pos)}条正面 / {len(neg)}条反面",
        ]

        yield event.plain_result("\n".join(result_lines))

    # ── LLM Tool：评估是否需要重规划 ──

    @filter.llm_tool(name="judge_replan", description="评估执行轨迹，判断是否需要重新规划。返回yes/no。")
    async def judge_replan_tool(
        self,
        event: AstrMessageEvent,
        execution_trace: str,
    ):
        """评估执行轨迹，判断是否需要重新规划。

        Args:
            execution_trace (str): 执行轨迹文本
        """
        result = await self.reasoning.judge_replan(event, execution_trace)

        if result == "yes":
            yield event.plain_result("🔄 评估结果: 需要重新规划 (yes)")
        else:
            yield event.plain_result("✅ 评估结果: 无需重新规划 (no)")

    # ── LLM Tool：生成补充计划 ──

    @filter.llm_tool(name="build_replan", description="基于失败经验生成补充计划。输入原始问题和执行轨迹，返回修正后的策略。")
    async def build_replan_tool(
        self,
        event: AstrMessageEvent,
        question: str,
        execution_trace: str,
    ):
        """基于失败经验生成补充计划。

        Args:
            question (str): 原始问题
            execution_trace (str): 执行轨迹
        """
        replan = await self.reasoning.build_replan(event, question, execution_trace)

        result_lines = [
            "🔄 补充计划",
            "━" * 24,
            replan,
        ]

        yield event.plain_result("\n".join(result_lines))

    # ── 指令：/ges 查看统计 ──

    @filter.command("ges", alias=["evolution_stats"])
    async def get_evolution_stats_cmd(self, event: AstrMessageEvent):
        """查看系统进化状态统计"""
        stats = await self.memory_mgr.get_stats()
        if not stats:
            yield event.plain_result("📊 暂无数据，记忆库为空")
            return

        total = stats.get("total_memories", 0)
        avg_win_rate = stats.get("avg_win_rate", 0)
        embedding_ready = stats.get("embedding_ready", False)

        yield event.plain_result(
            f"📊 光荣进化 v1.0.0\n"
            f"📚 记忆: {total} | 📈 胜率: {avg_win_rate:.0%} | "
            f"🧠 向量化: {'✅' if embedding_ready else '⏳'}\n"
            f"🧬 Full MIA: Memory + Reasoning + Evolution ✅"
        )

    # ── 指令：/store 手动存储记忆 ──

    @filter.command("store")
    async def store_cmd(self, event: AstrMessageEvent):
        """手动存储一条记忆。用法: /store 问题 | 内容"""
        msg = event.message_str.strip()
        if not msg or "|" not in msg:
            yield event.plain_result("用法: /store 问题 | 内容")
            return

        parts = msg.split("|", 1)
        question = parts[0].strip()
        content = parts[1].strip()

        if not question or not content:
            yield event.plain_result("❌ 问题和内容不能为空")
            return

        entry_id = await self.memory_mgr.add_memory(
            question=question, content=content,
        )
        yield event.plain_result(f"✅ 记忆已存储: {entry_id}")

    # ── 内部工具方法 ──

    @staticmethod
    def _format_stats(stats: dict) -> str:
        """格式化统计信息为可读文本"""
        total = stats.get("total_memories", 0)
        avg_win_rate = stats.get("avg_win_rate", 0)
        vector_index_size = stats.get("vector_index_size", 0)
        embedding_ready = stats.get("embedding_ready", False)
        embedding_dim = stats.get("embedding_dim", 0)

        lines = [
            "📊 光荣进化统计报告",
            "━" * 24,
            f"📚 记忆总数: {total}",
            f"📈 平均胜率: {avg_win_rate:.0%}",
            f"🔢 向量索引: {vector_index_size} 条",
            f"🧠 向量化: {'✅ 已接入' if embedding_ready else '⏳ 待接入'}"
            + (f" (dim={embedding_dim})" if embedding_ready else ""),
        ]

        by_type = stats.get("by_type", {})
        if by_type:
            lines.append("\n📂 按类型分布:")
            type_labels = {
                "procedural": "过程性",
                "declarative": "陈述性",
                "episodic": "情景性",
            }
            for t, c in by_type.items():
                lines.append(f"  • {type_labels.get(t, t)}: {c}")

        by_judgement = stats.get("by_judgement", {})
        if by_judgement:
            lines.append("\n⚖️ 按评判分布:")
            j_labels = {"correct": "✅ 有效", "incorrect": "❌ 无效", "pending": "⏳ 待定"}
            for j, c in by_judgement.items():
                lines.append(f"  • {j_labels.get(j, j)}: {c}")

        top = stats.get("top_win_rate", [])
        if top:
            lines.append("\n🏆 胜率 Top 5:")
            for i, r in enumerate(top, 1):
                lines.append(
                    f"  {i}. {r['id']} — {r['win_rate']:.0%} "
                    f"(使用{r['usage_count']}次)"
                )

        return "\n".join(lines)
