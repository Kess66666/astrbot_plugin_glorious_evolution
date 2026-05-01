from typing import Optional

from astrbot.core.agent.tool import FunctionTool

from .models import Phase

# ── 插件引用（由 main.py 注入） ──
_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


def inject_plugin(plugin) -> None:
    """由 main.py 在 plugin 启动时调用，注入插件实例。"""
    global _plugin_cache
    _plugin_cache = plugin


def _get_plugin() -> Optional["GloriousEvolutionPlugin"]:
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    raise RuntimeError("GloriousEvolutionPlugin not initialized")


# ── Tool 类定义 ──

class StoreMemoryTool(FunctionTool):
    """存储一条新记忆到知识库。"""
    def __init__(self):
        super().__init__(
            name="store_memory",
            description="Store a new memory in the Glorious Evolution knowledge base. Use this to remember important facts, code snippets, error messages, successful solutions, and configuration details for future reference. The memory will be vector-indexed for semantic retrieval and automatically classified by category and memory type.",
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question or problem this memory addresses.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full memory content — answer, solution, code, or knowledge.",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["procedural", "declarative", "episodic"],
                        "description": "Memory type: procedural (steps/commands/how-to), declarative (facts/knowledge), episodic (events/conversations/logs).",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["general", "debugging", "deployment", "coding", "configuration", "security", "insight", "consolidated_rule"],
                        "description": "Category for organizing memories.",
                    },
                },
                "required": ["question", "content"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(question: str, content: str, memory_type: str = "declarative", category: str = "general") -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        eid = await plugin._memory_mgr.add_memory(
            question=question, content=content, memory_type=memory_type, category=category,
        )
        return f"✅ memory stored: {eid}"


class SearchMemoryTool(FunctionTool):
    """检索相关记忆。"""
    def __init__(self):
        super().__init__(
            name="search_memory",
            description="Search the Glorious Evolution memory bank for relevant past experiences. Uses hybrid retrieval: vector similarity (ChromaDB) + full-text search (FTS5). Results are ranked by a mix of cosine similarity and historical win_rate. Use this BEFORE attempting any complex task to learn from past successes and failures.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — describe what knowledge you need.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(query: str, top_k: int = 5) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        entries = await plugin._memory_mgr.retrieve_relevant_memories(query=query, top_k=top_k)
        if not entries:
            return "🔍 no results"
        out = "🧠 relevant memories:\n"
        for i, e in enumerate(entries[:top_k], 1):
            out += f"{i}. [{e.id}] ({e.category}) win={e.win_rate:.0%}\n"
            out += f"   Q: {e.question[:80]}\n"
            out += f"   A: {e.content[:120]}\n"
        return out


class UpdateWinRateTool(FunctionTool):
    """更新记忆胜率。"""
    def __init__(self):
        super().__init__(
            name="update_win_rate",
            description="Record whether a previously stored memory led to success or failure. This feedback updates the memory's win_rate, which affects future retrieval ranking. ALWAYS call this after using a memory's advice — it makes the system smarter over time.",
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "The memory entry ID (e.g., 'MEM-20260501-001') to rate.",
                    },
                    "success": {
                        "type": "boolean",
                        "description": "True if the memory was helpful, False if it led to errors.",
                    },
                },
                "required": ["entry_id", "success"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(entry_id: str, success: bool) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        ok = await plugin._memory_mgr.update_win_rate(entry_id, success)
        return f"📈 {entry_id} win_rate updated" if ok else f"❌ {entry_id} not found"


class EvictMemoriesTool(FunctionTool):
    """淘汰低质量记忆。"""
    def __init__(self):
        super().__init__(
            name="evict_memories",
            description="Evict low-quality memories from the knowledge base. Removes entries with low usage count AND low win_rate. Call this periodically to keep the memory bank clean and efficient.",
            parameters={"type": "object", "properties": {}},
            handler=self._run,
        )

    @staticmethod
    async def _run() -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        n = await plugin._memory_mgr.evict_low_quality()
        plugin.evo_stats.increment("total_evictions", n)
        return f"🧹 evicted {n} low-quality memories" if n else "🧹 nothing to evict"


class GetEvolutionStatsTool(FunctionTool):
    """获取进化系统统计信息。"""
    def __init__(self):
        super().__init__(
            name="get_evolution_stats",
            description="Get statistics about the Glorious Evolution system: total memories, win rates, vector index status, evolution cycle count, and insights generated. Use this to understand the system's current knowledge state.",
            parameters={"type": "object", "properties": {}},
            handler=self._run,
        )

    @staticmethod
    async def _run() -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        stats = plugin.evo_stats.get_summary()
        mgr_stats = await plugin._memory_mgr.get_stats()
        return (
            f"📊 evolution stats:\n"
            f"📚 memories: {mgr_stats['total_memories']} | 🧬 vectors: {mgr_stats['vector_index_size']}"
            f" {'✅' if mgr_stats.get('embedding_ready') else '⚠️'}\n"
            f"🔄 evolutions: {stats['total_evolutions']} | 💡 insights: {stats['total_insights']}"
            f" | 🗑️ evicted: {stats['total_evictions']}\n"
            f"⏱️ last: {stats.get('last_evolution_at', 'N/A')}"
            f" ({stats.get('last_evolution_duration_sec', 'N/A')}s)\n"
            f"💾 data: {plugin.DATA_DIR if hasattr(plugin, 'DATA_DIR') else '/AstrBot/data/glorious_evolution'}"
        )


class TriggerEvolutionTool(FunctionTool):
    """手动触发进化周期。"""
    def __init__(self):
        super().__init__(
            name="trigger_evolution",
            description="Manually trigger a full evolution cycle: consolidate episodic memories into declarative rules, generate insights from win_rate patterns, and evict low-quality memories. The system auto-runs this every 6 hours, but you can call it on-demand after storing many new memories.",
            parameters={"type": "object", "properties": {}},
            handler=self._run,
        )

    @staticmethod
    async def _run() -> str:
        plugin = _get_plugin()
        if not plugin:
            return "❌ plugin not ready"
        import asyncio
        try:
            await asyncio.wait_for(plugin._run_evolution(), timeout=120)
            return "🧬 evolution cycle complete ✅"
        except asyncio.TimeoutError:
            return "⚠️ evolution timeout (2min)"


class BuildPlanTool(FunctionTool):
    """构建行动计划。"""
    def __init__(self):
        super().__init__(
            name="build_plan",
            description="【MIA Phase 2】Build an action plan for a given goal using past experience. Retrieves relevant positive (high win_rate) and negative (low win_rate) memories, then uses LLM reasoning to generate a structured step-by-step plan. ALWAYS call this BEFORE attempting complex tasks like debugging, deployment, code changes, or configuration updates.",
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The goal or problem to plan for."},
                    "extra_context": {"type": "string", "description": "Additional context: error logs, current state, constraints."},
                },
                "required": ["question"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(question: str, extra_context: str = "") -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        pos, neg = await plugin._memory_mgr.retrieve_balanced_memories(
            query=question, pos_top_k=3, neg_top_k=2,
        )
        ctx_parts = [f"[{e.id}] win={e.win_rate:.0%}: {e.content[:150]}" for e in pos + neg]
        ctx = "\n".join(ctx_parts) if ctx_parts else "no relevant memories"
        return (
            f"📋 plan (pos={len(pos)} neg={len(neg)}):\n"
            f"goal: {question}\n"
            f"{'extra context: ' + extra_context if extra_context else ''}\n"
            f"────────────────\n📚 experience:\n{ctx}\n────────────────\n"
            f"💡 1.review success/failure patterns 2.prefer high-win strategies 3.record win_rate"
        )


class JudgeReplanTool(FunctionTool):
    """判断是否需要重新规划。"""
    def __init__(self):
        super().__init__(
            name="judge_replan",
            description="【MIA Phase 2】Analyze the execution trace to determine if replanning is needed. Uses LLM reasoning (not simple keyword matching) to evaluate whether the plan succeeded or if a different approach is required. Call this AFTER attempting to execute a plan from `build_plan`.",
            parameters={
                "type": "object",
                "properties": {
                    "execution_trace": {"type": "string", "description": "Full execution trace: what was attempted, what happened, errors encountered, outputs."},
                },
                "required": ["execution_trace"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(execution_trace: str) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "❌ plugin not ready"
        if plugin._reasoning_engine:
            try:
                result = await plugin._reasoning_engine.judge_replan(
                    event=None, execution_trace=execution_trace,
                )
                return "🔄 replan suggested" if result == "yes" else "✅ no replan needed"
            except RuntimeError:
                pass
        failure_keywords = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
        has_failure = any(kw in execution_trace.lower() for kw in failure_keywords)
        return "🔄 replan suggested" if has_failure else "✅ no replan needed"


class BuildReplanTool(FunctionTool):
    """构建修订计划。"""
    def __init__(self):
        super().__init__(
            name="build_replan",
            description="【MIA Phase 2】Build a revised action plan after a failed execution attempt. Analyzes the failure trace, retrieves relevant negative memories to avoid, and generates an alternative approach. Call this when `judge_replan` recommends replanning.",
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The original goal."},
                    "execution_trace": {"type": "string", "description": "The execution trace that led to failure."},
                },
                "required": ["question", "execution_trace"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(question: str, execution_trace: str) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"
        _, neg = await plugin._memory_mgr.retrieve_balanced_memories(
            query=question, pos_top_k=2, neg_top_k=3,
        )
        avoid_lines = [f"- ❌ {e.content[:150]}" for e in neg[:3]]
        avoid = "\n".join(avoid_lines) if avoid_lines else "no known failure patterns"
        return (
            f"🔄 replan:\noriginal: {question}\nfailure trace: {execution_trace[:200]}\n"
            f"⚠️ avoid: {avoid}\n💡 1.try different approach 2.simplify 3.record win_rate"
        )


class RunAgentLoopTool(FunctionTool):
    """运行 Agent 循环。"""
    def __init__(self):
        super().__init__(
            name="run_agent_loop",
            description="【State-Driven Agent Loop】状态机驱动的混合推理循环。\nUsage (manual mode):\n1. `run_agent_loop(goal='...')` → 返回计划，等待你执行\n2. 执行步骤后 → `run_agent_loop(goal='...', execution_trace='...')` → 评判 + (重规划 | 完成)\n3. 重复直到完成\nUsage (background mode):\n`run_agent_loop(goal='...', mode='background')` → 后台自主运行，不返回中间结果\n\nFixed Action flow: BUILD_PLAN → EXECUTE_PLAN → JUDGE_RESULT → (BUILD_REPLAN → EXECUTE_REPLAN → JUDGE_RESULT | FINISH)\nState machine controls the flow; LLM only assists with planning & judging.",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "The goal or problem to solve."},
                    "execution_trace": {"type": "string", "description": "Previous execution trace. Omit for the initial call. Include after executing the returned plan."},
                    "max_iterations": {"type": "integer", "description": "Maximum plan-replan iterations (default 3)."},
                    "mode": {"type": "string", "enum": ["manual", "background"], "description": "manual: pause at execution phases (default). background: run full loop autonomously."},
                },
                "required": ["goal"],
            },
            handler=self._run,
        )

    @staticmethod
    async def _run(goal: str, execution_trace: str = "", max_iterations: int = 3, mode: str = "manual") -> str:
        plugin = _get_plugin()
        if not plugin:
            return "❌ plugin not ready"
        loop = plugin._agent_loop
        if not loop:
            return "❌ agent loop not initialized"
        if mode == "background":
            import asyncio
            async def _bg():
                return await loop.run_in_background(goal, max_iterations=max_iterations)
            asyncio.create_task(_bg())
            return f"🔄 Agent loop started in background: {goal[:80]}..."
        return await loop.process(goal, execution_trace=execution_trace, max_iterations=max_iterations)
