"""
光荣进化系统 v0.4.0 - AstrBot 插件
融合 MIA 智能记忆框架与自改进机制的进化系统
"""

import os

from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

from .storage import Storage
from .memory_manager import MemoryManager
from .reasoning_engine import ReasoningEngine
from .models import MemoryType, Judgement


class GloriousEvolutionPlugin(Star):
    """光荣进化系统主插件类"""

    def __init__(self, context: Context, **kwargs):
        super().__init__(context)
        self.config = kwargs.get("config") or {}
        data_dir = os.path.dirname(__file__)
        self.storage = Storage(data_dir)
        self.memory_mgr = MemoryManager(self.storage)
        self.reasoning = ReasoningEngine(self.memory_mgr, self.context)
        logger.info("[Glorious Evolution] v0.4.0 初始化完成")

    async def _init_embedding_provider(self) -> None:
        try:
            emb_providers = self.context.get_all_embedding_providers()
            if emb_providers:
                provider = emb_providers[0]
                dim = provider.get_dim()
                await self.memory_mgr.set_embed_func(provider.get_embedding, dim)
                logger.info(f"[Glorious Evolution] EmbeddingProvider 注入成功 (dim={dim})")
            else:
                logger.warning("[Glorious Evolution] 未找到 EmbeddingProvider")
        except Exception as e:
            logger.error(f"[Glorious Evolution] EmbeddingProvider 注入失败: {e}")

    async def start(self) -> None:
        await self._init_embedding_provider()
        await self.memory_mgr.load_vectors()
        logger.info("[Glorious Evolution] 启动完成")

    async def terminate(self) -> None:
        pass

    @filter.llm_tool(name="store_memory", description="存储一条智能记忆。")
    async def store_memory(self, event: AstrMessageEvent, question: str, content: str,
                           memory_type: str = "procedural", category: str = "general"):
        entry_id = await self.memory_mgr.add_memory(question=question, content=content,
                                                      memory_type=memory_type, category=category)
        yield event.plain_result(f"✅ 记忆已存储: {entry_id}")

    @filter.llm_tool(name="search_memory", description="搜索相关记忆，检索过往经验、规则或知识。")
    async def search_memory(self, event: AstrMessageEvent, query: str, top_k: int = 5,
                            min_win_rate: float = 0.0):
        results = await self.memory_mgr.retrieve_relevant_memories(query=query, top_k=top_k, min_win_rate=min_win_rate)
        if not results:
            yield event.plain_result("🔍 未找到相关记忆")
            return
        lines = [f"🔍 找到 {len(results)} 条相关记忆:\n"]
        for i, entry in enumerate(results, 1):
            bar = "█" * int(entry.win_rate * 10) + "░" * (10 - int(entry.win_rate * 10))
            lines.append(f"{i}. [{entry.memory_type.value}] {entry.question[:60]}\n"
                         f"   📊 {bar} {entry.win_rate:.0%} ({entry.usage_count}次)\n   📝 {entry.content[:100]}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="update_win_rate", description="更新某条记忆的胜率，标记其是否有效。")
    async def update_win_rate_tool(self, event: AstrMessageEvent, entry_id: str, success: bool):
        ok = await self.memory_mgr.update_win_rate(entry_id, success)
        if ok:
            entry = await self.storage.get_entry(entry_id)
            wr = entry.win_rate if entry else 0
            yield event.plain_result(f"📊 胜率已更新: {entry_id} → {wr:.0%} ({'✅' if success else '❌'})")
        else:
            yield event.plain_result(f"⚠️ 更新失败: {entry_id}")

    @filter.llm_tool(name="evict_memories", description="淘汰低胜率、低使用的记忆。")
    async def evict_memories(self, event: AstrMessageEvent):
        count = await self.memory_mgr.evict_low_quality()
        yield event.plain_result(f"🗑️ 淘汰 {count} 条" if count > 0 else "✨ 记忆库健康")

    @filter.llm_tool(name="get_evolution_stats", description="获取光荣进化系统的统计概览。")
    async def get_evolution_stats_tool(self, event: AstrMessageEvent):
        stats = await self.memory_mgr.get_stats()
        if not stats:
            yield event.plain_result("📊 暂无数据")
            return
        yield event.plain_result(self._format_stats(stats))

    @filter.llm_tool(name="build_plan", description="基于记忆库生成行动计划。")
    async def build_plan_tool(self, event: AstrMessageEvent, question: str, extra_context: str = ""):
        plan, pos, neg = await self.reasoning.build_plan(event, question, extra_context)
        yield event.plain_result(f"📋 行动计划\n{'━'*24}\n{plan}\n\n📚 参考记忆: {len(pos)}正面/{len(neg)}反面")

    @filter.llm_tool(name="judge_replan", description="评估执行轨迹是否需要重新规划。")
    async def judge_replan_tool(self, event: AstrMessageEvent, execution_trace: str):
        result = await self.reasoning.judge_replan(event, execution_trace)
        yield event.plain_result(f"{'🔄 需要重规划' if result=='yes' else '✅ 无需重规划'} ({result})")

    @filter.llm_tool(name="build_replan", description="基于失败经验生成补充计划。")
    async def build_replan_tool(self, event: AstrMessageEvent, question: str, execution_trace: str):
        replan = await self.reasoning.build_replan(event, question, execution_trace)
        yield event.plain_result(f"🔄 补充计划\n{'━'*24}\n{replan}")

    @filter.command("ges", alias=["evolution_stats"])
    async def get_evolution_stats_cmd(self, event: AstrMessageEvent):
        stats = await self.memory_mgr.get_stats()
        if not stats:
            yield event.plain_result("📊 暂无数据")
            return
        total = stats.get("total_memories", 0)
        avg_wr = stats.get("avg_win_rate", 0)
        emb = stats.get("embedding_ready", False)
        yield event.plain_result(f"📊 光荣进化 v0.4.0\n📚 记忆:{total} | 📈 胜率:{avg_wr:.0%} | 🧠 向量化:{'✅' if emb else '⏳'}\n🔧 推理引擎: ✅ 已启用")

    @filter.command("store")
    async def store_cmd(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        if "|" not in msg:
            yield event.plain_result("用法: /store 问题 | 内容")
            return
        q, c = msg.split("|", 1)
        q, c = q.strip(), c.strip()
        if not q or not c:
            yield event.plain_result("❌ 问题和内容不能为空")
            return
        eid = await self.memory_mgr.add_memory(question=q, content=c)
        yield event.plain_result(f"✅ 记忆已存储: {eid}")

    @staticmethod
    def _format_stats(stats: dict) -> str:
        total = stats.get("total_memories", 0)
        avg_wr = stats.get("avg_win_rate", 0)
        vsize = stats.get("vector_index_size", 0)
        emb = stats.get("embedding_ready", False)
        dim = stats.get("embedding_dim", 0)
        lines = [f"📊 光荣进化统计\n{'━'*24}", f"📚 记忆总数: {total}", f"📈 平均胜率: {avg_wr:.0%}",
                 f"🔢 向量索引: {vsize} 条", f"🧠 向量化: {'✅' if emb else '⏳'}" + (f" (dim={dim})" if emb else "")]
        by_type = stats.get("by_type", {})
        if by_type:
            labels = {"procedural": "过程性", "declarative": "陈述性", "episodic": "情景性"}
            lines.append("\n📂 按类型:")
            for t, c in by_type.items():
                lines.append(f"  • {labels.get(t,t)}: {c}")
        by_j = stats.get("by_judgement", {})
        if by_j:
            jl = {"correct": "✅有效", "incorrect": "❌无效", "pending": "⏳待定"}
            lines.append("\n⚖️ 按评判:")
            for j, c in by_j.items():
                lines.append(f"  • {jl.get(j,j)}: {c}")
        top = stats.get("top_win_rate", [])
        if top:
            lines.append("\n🏆 胜率 Top 5:")
            for i, r in enumerate(top, 1):
                lines.append(f"  {i}. {r['id']} — {r['win_rate']:.0%} ({r['usage_count']}次)")
        return "\n".join(lines)
