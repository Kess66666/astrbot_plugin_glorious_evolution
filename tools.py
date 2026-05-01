"""
光荣进化系统 — LLM Function Tools
按 AstrBot FunctionTool 规范注册，让大模型真正能调用记忆/推理/进化系统。

工具清单:
- store_memory / search_memory / update_win_rate — 记忆 CRUD
- evict_memories / get_evolution_stats / trigger_evolution — 进化操作
- build_plan / judge_replan / build_replan — MIA 风格的 Plan-Judge-Replan 推理
- run_agent_loop — 状态机驱动的混合 Agent 循环
"""

import asyncio
from typing import Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .models import Action, Phase


# ── 插件引用（由 main.py 注入） ──
_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


def _get_plugin() -> Optional["GloriousEvolutionPlugin"]:
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    raise RuntimeError("GloriousEvolutionPlugin not initialized")


# ──────────────────────────────────────────
# 记忆 CRUD 工具
# ──────────────────────────────────────────

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
            return "❌ plugin not ready"
        try:
            question = str(kwargs.get("question", ""))
            content = str(kwargs.get("content", ""))
            memory_type = str(kwargs.get("memory_type", "procedural"))
            category = str(kwargs.get("category", "general"))
            entry = await plugin._memory_mgr.store_memory(
                question=question, content=content,
                memory_type=memory_type, category=category,
            )
            return f"✅ 记忆已存储: {entry.id} (type={entry.memory_type.value}, category={entry.category})"
        except Exception as e:
            logger.error(f"store_memory error: {e}")
            return f"❌ 存储失败: {e}"


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
        try:
            query = str(kwargs.get("query", ""))
            top_k = int(kwargs.get("top_k", 5))
            results = await plugin._memory_mgr.search_memories(query=query, top_k=min(top_k, 10))
            if not results:
                return "📭 未找到相关记忆"
            lines = [f"## 🔍 检索结果 ({len(results)} 条) for: {query[:60]}\n"]
            for i, mem in enumerate(results, 1):
                lines.append(
                    f"{i}. **[{mem.win_rate:.0%}]** ({mem.memory_type.value}) "
                    f"{mem.content[:120]}..."
                )
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"search_memory error: {e}")
            return f"❌ 搜索失败: {e}"


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
        try:
            entry_id = str(kwargs.get("entry_id", ""))
            success = bool(kwargs.get("success", False))
            await plugin._memory_mgr.update_win_rate(entry_id=entry_id, success=success)
            return f"✅ 胜率更新: {entry_id} → {'success' if success else 'failure'}"
        except Exception as e:
            logger.error(f"update_win_rate error: {e}")
            return f"❌ 更新失败: {e}"


# ──────────────────────────────────────────
# 进化操作工具
# ──────────────────────────────────────────

@dataclass
class EvictMemoriesTool(FunctionTool[AstrAgentContext]):
    name: str = "evict_memories"
    description: str = (
        "Evict low-quality memories from the knowledge base. "
        "Removes entries with low usage count AND low win_rate. "
        "Call this periodically to keep the memory bank clean and efficient."
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._evo_engine:
            return "❌ plugin not ready"
        try:
            count = await plugin._evo_engine.evict_memories()
            return f"🧹 清理完成: 驱逐了 {count} 条低质量记忆"
        except Exception as e:
            logger.error(f"evict_memories error: {e}")
            return f"❌ 清理失败: {e}"


@dataclass
class GetEvolutionStatsTool(FunctionTool[AstrAgentContext]):
    name: str = "get_evolution_stats"
    description: str = (
        "Get statistics about the Glorious Evolution system: "
        "total memories, win rates, vector index status, evolution cycle count, and insights generated. "
        "Use this to understand the system's current knowledge state."
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._evo_engine:
            return "❌ plugin not ready"
        try:
            stats = await plugin._evo_engine.get_stats()
            return (
                f"## 📊 光荣进化统计\n"
                f"- 总记忆: {stats.get('total_memories', 0)}\n"
                f"- 进化周期: {stats.get('evolution_cycles', 0)}\n"
                f"- 平均胜率: {stats.get('avg_win_rate', 0):.1%}\n"
                f"- 正面记忆: {stats.get('positive_count', 0)}\n"
                f"- 负面记忆: {stats.get('negative_count', 0)}\n"
                f"- 向量索引: {'✅ ready' if stats.get('vector_ready', False) else '⚠️ not ready'}"
            )
        except Exception as e:
            logger.error(f"get_evolution_stats error: {e}")
            return f"❌ 获取统计失败: {e}"


@dataclass
class TriggerEvolutionTool(FunctionTool[AstrAgentContext]):
    name: str = "trigger_evolution"
    description: str = (
        "Manually trigger a full evolution cycle: consolidate episodic memories "
        "into declarative rules, generate insights from win_rate patterns, and evict "
        "low-quality memories. The system auto-runs this every 6 hours, but you can "
        "call it on-demand after storing many new memories."
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin or not plugin._evo_engine:
            return "❌ plugin not ready"
        try:
            result = await plugin._evo_engine.run_evolution_cycle()
            return f"🧬 进化周期完成: {result}"
        except Exception as e:
            logger.error(f"trigger_evolution error: {e}")
            return f"❌ 进化失败: {e}"


# ═══════════════════════════════════════════
# MIA Plan-Judge-Replan 推理工具（原子操作用）
# ═══════════════════════════════════════════

@dataclass
class BuildPlanTool(FunctionTool[AstrAgentContext]):
    name: str = "build_plan"
    description: str = (
        "【MIA Phase 2】Build an action plan for a given goal using past experience. "
        "Retrieves relevant positive (high win_rate) and negative (low win_rate) memories, "
        "then uses LLM reasoning to generate a structured step-by-step plan. "
        "ALWAYS call this BEFORE attempting complex tasks like debugging, deployment, code changes, or configuration updates."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The goal or problem to plan for."},
                "extra_context": {"type": "string", "description": "Additional context: error logs, current state, constraints."},
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
            return f"❌ LLM planning failed: {e}"
        return (
            f"## 📋 Plan for: {question[:80]}\n\n{plan_text}\n\n"
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
        try:
            result = await plugin._reasoning_engine.judge_replan(
                event=None, execution_trace=execution_trace,
            )
        except RuntimeError:
            failure_kw = ["error", "failed", "❌", "exception", "timeout"]
            result = "yes" if any(k in execution_trace.lower() for k in failure_kw) else "no"
        if result == "yes":
            return "🔄 replan suggested — call `build_replan` with the original goal and this trace."
        else:
            return "✅ plan succeeded — no replan needed."


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
                "question": {"type": "string", "description": "The original goal."},
                "execution_trace": {"type": "string", "description": "The execution trace that led to failure."},
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
            return f"❌ Replanning failed: {e}"
        return (
            f"## 🔄 Revised Plan\n\n{replan}\n\n"
            f"💡 Next: execute the revised plan, then call `judge_replan` again."
        )


# ═══════════════════════════════════════════
# Agent Loop — 状态机驱动的混合循环
# ═══════════════════════════════════════════

@dataclass
class RunAgentLoopTool(FunctionTool[AstrAgentContext]):
    """
    状态机驱动的混合 Agent 循环。

    核心变化 (v2):
    - 状态机控制 phase 转换，LLM 只在 planning/judging 阶段做策略补充
    - 固定 Action 枚举：BUILD_PLAN → EXECUTE_PLAN → JUDGE_RESULT → (BUILD_REPLAN | FINISH)
    - 每次调用推进到下一个 EXECUTE 暂停点，等待调用方执行后继续
    - 支持 mode="background" 自主运行
    """
    name: str = "run_agent_loop"
    description: str = (
        "【State-Driven Agent Loop】状态机驱动的混合推理循环。\n"
        "Usage (manual mode):\n"
        "1. `run_agent_loop(goal='...')` → 返回计划，等待你执行\n"
        "2. 执行步骤后 → `run_agent_loop(goal='...', execution_trace='...')` → 评判 + (重规划 | 完成)\n"
        "3. 重复直到完成\n"
        "Usage (background mode):\n"
        "`run_agent_loop(goal='...', mode='background')` → 后台自主运行，不返回中间结果\n\n"
        "Fixed Action flow: BUILD_PLAN → EXECUTE_PLAN → JUDGE_RESULT → (BUILD_REPLAN → EXECUTE_REPLAN → JUDGE_RESULT | FINISH)\n"
        "State machine controls the flow; LLM only assists with planning & judging."
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
                    "description": "Previous execution trace. Omit for the initial call. Include after executing the returned plan.",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum plan-replan iterations (default 3).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["manual", "background"],
                    "description": "manual: pause at execution phases (default). background: run full loop autonomously.",
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
        max_iterations = int(kwargs.get("max_iterations", 3))
        mode = str(kwargs.get("mode", "manual"))

        agent_loop = plugin._agent_loop
        if agent_loop is None:
            return "❌ AgentLoop not initialized"

        if mode == "background":
            return await self._run_background(plugin, goal, max_iterations)

        # 工具模式：状态机推进
        try:
            result = await agent_loop.process(
                goal=goal,
                execution_trace=execution_trace,
                max_iterations=max_iterations,
            )
            return result
        except Exception as e:
            logger.error(f"[RunAgentLoopTool] error: {e}")
            return f"❌ AgentLoop 异常: {e}"

    async def _run_background(self, plugin, goal: str, max_iterations: int) -> str:
        """后台模式：启动异步循环，立即返回状态。"""
        agent_loop = plugin._agent_loop
        if plugin._agent_loop_task and not plugin._agent_loop_task.done():
            return "⚠️ 已有后台循环在运行，请等待完成"

        plugin._agent_loop_task = asyncio.create_task(
            agent_loop.run_in_background(goal=goal, max_iterations=max_iterations)
        )

        def _on_done(t):
            try:
                state = t.result()
                logger.info(f"[RunAgentLoopTool] 后台循环完成: phase={state.phase.value}")
            except Exception as e:
                logger.error(f"[RunAgentLoopTool] 后台循环异常: {e}")

        plugin._agent_loop_task.add_done_callback(_on_done)

        return (
            f"🚀 **后台循环已启动**\n\n"
            f"🎯 目标: {goal[:100]}\n"
            f"🔄 最大迭代: {max_iterations}\n"
            f"📌 状态: Phase={Phase.INIT.value}\n\n"
            f"💡 循环将自动运行 plan→judge→replan，完成后可查询结果。"
        )
