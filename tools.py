"""
光荣进化系统 — LLM Function Tools
按 AstrBot FunctionTool 规范注册，让大模型真正能调用记忆/推理/进化系统。

工具清单:
- store_memory / search_memory / update_win_rate — 记忆 CRUD
- evict_memories / get_evolution_stats / trigger_evolution — 进化操作
- build_plan / judge_replan / build_replan — MIA 风格的 Plan-Judge-Replan 推理
- run_agent_loop — 完整推理循环入口
"""

import asyncio
from typing import Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


# ── 插件引用（由 main.py 注入） ──
_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


def _get_plugin() -> Optional["GloriousEvolutionPlugin"]:
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    try:
        from astrbot.api.star import GlobalStarMap
        star_map = GlobalStarMap()
        from .main import GloriousEvolutionPlugin
        for v in star_map.star_map.values():
            if isinstance(v, GloriousEvolutionPlugin):
                _plugin_cache = v
                return v
    except Exception:
        pass
    return None


def inject_plugin(plugin: "GloriousEvolutionPlugin") -> None:
    global _plugin_cache
    _plugin_cache = plugin


# ── 辅助：安全截断 ──
def _trunc(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + "..."


# ═══════════════════════════════════════════
# 记忆工具
# ═══════════════════════════════════════════

@dataclass
class StoreMemoryTool(FunctionTool[AstrAgentContext]):
    name: str = "store_memory"
    description: str = (
        "Store a new memory in the Glorious Evolution knowledge base. "
        "Use this to remember important facts, code snippets, error messages, "
        "successful solutions, and configuration details for future reference. "
        "The memory will be vector-indexed for semantic retrieval and automatically "
        "classified by category and memory type."
    )
    parameters: dict = Field(
        default_factory=lambda: {
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
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ Glorious Evolution plugin not ready"

        question = str(kwargs.get("question", ""))
        content = str(kwargs.get("content", ""))
        memory_type = str(kwargs.get("memory_type", "declarative"))
        category = str(kwargs.get("category", "general"))

        eid = await plugin._memory_mgr.add_memory(
            question=question, content=content,
            memory_type=memory_type, category=category,
        )
        logger.info(f"[GE] Tool: store_memory → {eid}")
        return f"✅ memory stored: {eid}"


@dataclass
class SearchMemoryTool(FunctionTool[AstrAgentContext]):
    name: str = "search_memory"
    description: str = (
        "Search the Glorious Evolution memory bank for relevant past experiences. "
        "Uses hybrid retrieval: vector similarity (ChromaDB) + full-text search (FTS5). "
        "Results are ranked by a mix of cosine similarity and historical win_rate. "
        "Use this BEFORE attempting any complex task to learn from past successes and failures."
    )
    parameters: dict = Field(
        default_factory=lambda: {
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
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"

        query = str(kwargs.get("query", ""))
        top_k = int(kwargs.get("top_k", 5))
        top_k = max(1, min(top_k, 10))

        entries = await plugin._memory_mgr.retrieve_relevant_memories(
            query=query, top_k=top_k,
        )
        if not entries:
            return "🔍 no relevant memories found"

        out = "🧠 relevant memories:\n"
        for i, e in enumerate(entries[:top_k], 1):
            out += f"{i}. [{e.id}] ({e.category}) win={e.win_rate:.0%} use={e.usage_count}\n"
            out += f"   Q: {_trunc(e.question, 80)}\n"
            out += f"   A: {_trunc(e.content, 120)}\n"
        return out


@dataclass
class UpdateWinRateTool(FunctionTool[AstrAgentContext]):
    name: str = "update_win_rate"
    description: str = (
        "Record whether a previously stored memory led to success or failure. "
        "This feedback updates the memory's win_rate, which affects future retrieval ranking. "
        "ALWAYS call this after using a memory's advice — it makes the system smarter over time."
    )
    parameters: dict = Field(
        default_factory=lambda: {
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
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"

        entry_id = str(kwargs.get("entry_id", ""))
        success = bool(kwargs.get("success", True))

        ok = await plugin._memory_mgr.update_win_rate(entry_id, success)
        return f"📈 {entry_id} win_rate updated" if ok else f"❌ {entry_id} not found"


@dataclass
class EvictMemoriesTool(FunctionTool[AstrAgentContext]):
    name: str = "evict_memories"
    description: str = (
        "Evict low-quality memories from the knowledge base. "
        "Removes entries with low usage count AND low win_rate. "
        "Call this periodically to keep the memory bank clean and efficient."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._memory_mgr:
            return "❌ plugin not ready"

        n = await plugin._memory_mgr.evict_low_quality()
        plugin.evo_stats.increment("total_evictions", n)
        return f"🧹 evicted {n} low-quality memories" if n else "🧹 nothing to evict"


@dataclass
class GetEvolutionStatsTool(FunctionTool[AstrAgentContext]):
    name: str = "get_evolution_stats"
    description: str = (
        "Get statistics about the Glorious Evolution system: "
        "total memories, win rates, vector index status, evolution cycle count, "
        "and insights generated. Use this to understand the system's current knowledge state."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
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
            f"💾 data: {plugin.DATA_DIR}"
        )


@dataclass
class TriggerEvolutionTool(FunctionTool[AstrAgentContext]):
    name: str = "trigger_evolution"
    description: str = (
        "Manually trigger a full evolution cycle: consolidate episodic memories into "
        "declarative rules, generate insights from win_rate patterns, and evict low-quality "
        "memories. The system auto-runs this every 6 hours, but you can call it on-demand "
        "after storing many new memories."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "❌ plugin not ready"

        try:
            await asyncio.wait_for(plugin._run_evolution(), timeout=120)
            asyncio.create_task(plugin._backup_all(label="manual"))
            return "🧬 evolution cycle complete ✅"
        except asyncio.TimeoutError:
            return "⚠️ evolution timeout (2min)"


# ═══════════════════════════════════════════
# 推理工具 (Plan-Judge-Replan)
# ═══════════════════════════════════════════

@dataclass
class BuildPlanTool(FunctionTool[AstrAgentContext]):
    name: str = "build_plan"
    description: str = (
        "【MIA Phase 2】Build an action plan for a given goal using past experience. "
        "Retrieves relevant positive (high win_rate) and negative (low win_rate) memories, "
        "then uses LLM reasoning to generate a structured step-by-step plan. "
        "ALWAYS call this BEFORE attempting complex tasks like debugging, deployment, "
        "code changes, or configuration updates."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The goal or problem to plan for.",
                },
                "extra_context": {
                    "type": "string",
                    "description": "Additional context: error logs, current state, constraints.",
                },
            },
            "required": ["question"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._reasoning_engine:
            return "❌ plugin not ready"

        question = str(kwargs.get("question", ""))
        extra_context = str(kwargs.get("extra_context", ""))

        try:
            plan_text, pos, neg = await plugin._reasoning_engine.build_plan(
                event=None, question=question, extra_context=extra_context,
            )
        except RuntimeError as e:
            return f"❌ LLM plan failed: {e}"

        return (
            f"## 🎯 Goal\n{question}\n\n## 📋 Plan\n{plan_text}\n\n"
            f"📊 Retrieved: {len(pos)} positive + {len(neg)} negative memories\n"
            f"💡 Next: execute the plan, then call `judge_replan` with the execution trace."
        )


@dataclass
class JudgeReplanTool(FunctionTool[AstrAgentContext]):
    name: str = "judge_replan"
    description: str = (
        "【MIA Phase 2】Analyze the execution trace to determine if replanning is needed. "
        "Uses LLM reasoning (not simple keyword matching) to evaluate whether the plan "
        "succeeded or if a different approach is required. "
        "Call this AFTER attempting to execute a plan from `build_plan`."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "execution_trace": {
                    "type": "string",
                    "description": "Full execution trace: what was attempted, what happened, errors encountered, outputs.",
                },
            },
            "required": ["execution_trace"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._reasoning_engine:
            return "❌ plugin not ready"

        execution_trace = str(kwargs.get("execution_trace", ""))

        # 优先 LLM 判断
        try:
            result = await plugin._reasoning_engine.judge_replan(
                event=None, execution_trace=execution_trace,
            )
            if result == "yes":
                return "🔄 replan suggested — call `build_replan` with the original goal and this trace."
            else:
                return "✅ no replan needed — plan succeeded."
        except RuntimeError:
            # 降级：关键词匹配
            failure_keywords = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
            has_failure = any(kw in execution_trace.lower() for kw in failure_keywords)
            return "🔄 replan suggested" if has_failure else "✅ no replan needed"


@dataclass
class BuildReplanTool(FunctionTool[AstrAgentContext]):
    name: str = "build_replan"
    description: str = (
        "【MIA Phase 2】Build a revised action plan after a failed execution attempt. "
        "Analyzes the failure trace, retrieves relevant negative memories to avoid, "
        "and generates an alternative approach. "
        "Call this when `judge_replan` recommends replanning."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The original goal.",
                },
                "execution_trace": {
                    "type": "string",
                    "description": "The execution trace that led to failure.",
                },
            },
            "required": ["question", "execution_trace"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._reasoning_engine:
            return "❌ plugin not ready"

        question = str(kwargs.get("question", ""))
        execution_trace = str(kwargs.get("execution_trace", ""))

        try:
            replan = await plugin._reasoning_engine.build_replan(
                event=None, question=question, execution_trace=execution_trace,
            )
        except RuntimeError as e:
            return f"❌ LLM replan failed: {e}"

        return (
            f"## 🔄 Revised Plan\n\n{replan}\n\n"
            f"💡 Next: execute the revised plan, then call `judge_replan` again."
        )


# ═══════════════════════════════════════════
# Agent Loop (完整推理循环)
# ═══════════════════════════════════════════

@dataclass
class RunAgentLoopTool(FunctionTool[AstrAgentContext]):
    name: str = "run_agent_loop"
    description: str = (
        "【MIA Full Cycle】Run the complete Plan-Judge-Replan reasoning loop. "
        "Usage pattern:\n"
        "1. First call: `run_agent_loop(goal='your goal')` → returns a plan\n"
        "2. Execute the plan steps\n"
        "3. Second call: `run_agent_loop(goal='your goal', execution_trace='what happened')` → judges & replans if needed\n"
        "4. Repeat until 'no replan needed'\n"
        "This tool combines build_plan + judge_replan + build_replan in one interface."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The goal or problem to solve.",
                },
                "execution_trace": {
                    "type": "string",
                    "description": "Previous execution trace. Omit for the initial plan. Include to get replanning.",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum plan-replan iterations (default 3). Used for loop control.",
                },
            },
            "required": ["goal"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._reasoning_engine:
            return "❌ plugin not ready"

        goal = str(kwargs.get("goal", ""))
        execution_trace = str(kwargs.get("execution_trace", ""))
        engine = plugin._reasoning_engine

        if not execution_trace:
            # Phase 1: 初始计划
            try:
                plan_text, pos, neg = await engine.build_plan(
                    event=None, question=goal, extra_context="",
                )
            except RuntimeError as e:
                return f"❌ LLM planning failed: {e}"

            return (
                f"## 🎯 Goal\n{goal}\n\n## 📋 Initial Plan\n{plan_text}\n\n"
                f"📊 Retrieved: {len(pos)} positive + {len(neg)} negative memories\n"
                f"💡 **Next**: Execute the plan, then call `run_agent_loop` again "
                f"with `execution_trace` set to what happened."
            )

        # Phase 2: 判断 + (可能) 重新规划
        try:
            need_replan = await engine.judge_replan(
                event=None, execution_trace=execution_trace,
            )
        except RuntimeError:
            # 降级关键词判断
            failure_keywords = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
            need_replan = "yes" if any(kw in execution_trace.lower() for kw in failure_keywords) else "no"

        if need_replan == "yes":
            try:
                replan = await engine.build_replan(
                    event=None, question=goal, execution_trace=execution_trace,
                )
            except RuntimeError as e:
                return f"⚠️ Replanning LLM failed, using fallback.\n❌ {e}"

            return (
                f"## 🔄 Plan Revision Needed\n\n{replan}\n\n"
                f"💡 **Next**: Execute the revised plan, then call `run_agent_loop` again "
                f"with the updated execution trace."
            )
        else:
            return f"## ✅ Goal Achieved\n\n{goal}\n\nPlan executed successfully — no replan needed."
