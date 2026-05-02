"""
光荣进化系统 - 混合 Agent 循环
State-driven + LLM-assisted

v1.0.12: 反馈闭环 — JUDGING 阶段自动更新所用记忆的 win_rate
v1.0.13: 记忆感知 judge — 传入片段内容，按记忆粒度逐条评分，替代粗糙二值映射
"""

import asyncio
import hashlib
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import (
    Action,
    AgentLoopState,
    MemoryEntry,
    Phase,
    PHASE_TRANSITIONS,
    PHASE_TO_ACTION,
)


class StateMachine:
    """状态机：决定 phase 之间的确定性转换。LLM 无权修改。"""

    def next_phase(self, state: AgentLoopState, judge_result: str = "") -> None:
        if state.phase == Phase.JUDGING:
            self._handle_judge_branch(state, judge_result)
            return

        if state.phase == Phase.DONE or state.phase == Phase.FAILED:
            state.done = True
            return

        next_phase = PHASE_TRANSITIONS.get(state.phase)
        if next_phase is None:
            logger.warning(f"[AgentLoop] 未知 phase: {state.phase}，标记失败")
            state.phase = Phase.FAILED
            state.action = Action.FINISH
            state.done = True
            return

        state.phase = next_phase
        state.action = PHASE_TO_ACTION.get(next_phase, Action.FINISH)

        if state.action in (Action.EXECUTE_PLAN, Action.EXECUTE_REPLAN):
            state.done = True

    def _handle_judge_branch(self, state: AgentLoopState, judge_result: str) -> None:
        if judge_result.strip().lower().startswith("yes"):
            state.phase = Phase.REPLANNING
            state.action = Action.BUILD_REPLAN
            state.done = False
            logger.info(f"[AgentLoop] JUDGE=yes → REPLANNING (迭代 {state.iteration})")
        else:
            state.phase = Phase.DONE
            state.action = Action.FINISH
            state.done = True
            state.result = f"## ✅ 目标达成\n\n{state.goal}\n\n计划执行成功 — 无需重新规划。"
            logger.info(f"[AgentLoop] JUDGE=no → DONE")


class AgentLoop:
    """
    混合 Agent 循环 — State-driven + LLM-assisted

    v1.0.13: judge 拿到记忆片段 → 逐条贡献评分 → 精确 win_rate 反馈
    """

    SNIPPET_MAX_LEN = 200  # 每个记忆传给 judge 的最大字符数

    def __init__(self, reasoning_engine, memory_mgr=None, max_iterations: int = 3):
        self._engine = reasoning_engine
        self._memory_mgr = memory_mgr
        self._sm = StateMachine()
        self._loops: Dict[str, AgentLoopState] = {}
        self._max_iterations = max_iterations
        self._lock = asyncio.Lock()

    @staticmethod
    def _goal_id(goal: str) -> str:
        return hashlib.md5(goal.encode()).hexdigest()[:8]

    # ── 工具模式 ──

    async def process(self, goal: str, execution_trace: str = "",
                      max_iterations: int = 3) -> str:
        gid = self._goal_id(goal)

        async with self._lock:
            state = self._get_or_create_state(gid, goal, max_iterations)

            if execution_trace:
                state.execution_trace = execution_trace
                state.done = False
                if state.phase == Phase.EXECUTING:
                    self._sm.next_phase(state)
                elif state.phase == Phase.EXECUTING_REPLAN:
                    self._sm.next_phase(state)

            result = await self._run_until_pause(state)
            return result

    def _get_or_create_state(self, gid: str, goal: str,
                              max_iterations: int) -> AgentLoopState:
        if gid in self._loops:
            return self._loops[gid]
        state = AgentLoopState(goal=goal, max_iterations=max_iterations)
        self._loops[gid] = state
        return state

    async def _run_until_pause(self, state: AgentLoopState) -> str:
        while not state.done and state.iteration < state.max_iterations:
            state.iteration += 1
            logger.info(
                f"[AgentLoop] step {state.iteration}: "
                f"phase={state.phase.value} action={state.action.value}"
            )

            action = state.action

            if action == Action.BUILD_PLAN:
                await self._do_build_plan(state)
            elif action == Action.BUILD_REPLAN:
                await self._do_build_replan(state)
            elif action == Action.JUDGE_RESULT:
                await self._do_judge(state)
            elif action in (Action.EXECUTE_PLAN, Action.EXECUTE_REPLAN):
                return self._fmt_pause_result(state)
            elif action == Action.FINISH:
                state.done = True
                break
            else:
                logger.warning(f"[AgentLoop] 未知 action: {action}")
                state.error = f"未知 action: {action}"
                state.phase = Phase.FAILED
                state.done = True

            if state.done:
                break

            self._sm.next_phase(state)

        return self._fmt_final_result(state)

    # ── 各动作实现 ──

    async def _do_build_plan(self, state: AgentLoopState) -> None:
        try:
            plan_text, pos, neg = await self._engine.build_plan(
                event=None, question=state.goal, extra_context=""
            )
            state.plan = plan_text
            state.used_memory_ids = [e.id for e in pos]
            state.used_neg_memory_ids = [e.id for e in neg]
            # v1.0.13: 存储记忆内容摘要，供 judge 评分
            state.used_memory_snippets = self._build_snippets(pos + neg)
            state.result = (
                f"## 🎯 目标\n{state.goal}\n\n"
                f"## 📋 初始计划\n{plan_text}\n\n"
                f"📊 检索到 {len(pos)} 正面 + {len(neg)} 负面记忆\n\n"
            )
            logger.info(
                f"[AgentLoop] build_plan OK: pos={len(pos)} neg={len(neg)} "
                f"ids={state.used_memory_ids + state.used_neg_memory_ids}"
            )
        except Exception as e:
            logger.error(f"[AgentLoop] build_plan failed: {e}")
            state.error = str(e)
            state.phase = Phase.FAILED
            state.done = True

    async def _do_build_replan(self, state: AgentLoopState) -> None:
        try:
            replan, pos, neg = await self._engine.build_replan(
                event=None, question=state.goal,
                execution_trace=state.execution_trace,
            )
            state.plan = replan
            # 累积追踪：replan 也注入了记忆
            new_ids = [e.id for e in pos] + [e.id for e in neg]
            for mid in new_ids:
                if mid not in state.used_memory_ids and mid not in state.used_neg_memory_ids:
                    state.used_memory_ids.append(mid)
            # v1.0.13: 合并新记忆的 snippet
            state.used_memory_snippets.update(self._build_snippets(pos + neg))
            state.result = (
                f"## 🔄 修订计划\n\n{replan}\n\n"
                f"💡 基于失败轨迹重新规划"
            )
            logger.info(f"[AgentLoop] build_replan OK: pos={len(pos)} neg={len(neg)}")
        except Exception as e:
            logger.error(f"[AgentLoop] build_replan failed: {e}")
            state.error = str(e)
            state.phase = Phase.FAILED
            state.done = True

    async def _do_judge(self, state: AgentLoopState) -> None:
        """v1.0.13: LLM 评估 + 逐条记忆贡献评分 → 精确 win_rate 反馈"""
        try:
            judge_result = await self._engine.judge_replan(
                event=None,
                execution_trace=state.execution_trace,
                memory_snippets=state.used_memory_snippets if state.used_memory_snippets else None,
            )
            need_replan = judge_result["need_replan"]
            memory_scores = judge_result.get("memory_contributions", {})
        except RuntimeError:
            # LLM Provider 完全不可用 → 关键词兜底，不评记忆
            failure_kw = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
            need_replan = "yes" if any(k in state.execution_trace.lower() for k in failure_kw) else "no"
            memory_scores = {}

        # ── v1.0.13 逐条记忆反馈 ──
        await self._record_feedback(state, memory_scores)

        state.result = f"🔍 评估结果: {'需要重规划' if need_replan == 'yes' else '完成'}"
        if memory_scores:
            scored = len(memory_scores)
            pos_count = sum(1 for s in memory_scores.values() if s > 0)
            neg_count = sum(1 for s in memory_scores.values() if s < 0)
            state.result += f" (记忆评分: {pos_count}✅/{neg_count}❌/{scored}总)"
        self._sm.next_phase(state, judge_result=need_replan)

    async def _record_feedback(self, state: AgentLoopState,
                                memory_scores: Dict[str, float]) -> None:
        """v1.0.13: 按记忆粒度更新 win_rate。

        - score > 0  → 标记成功
        - score < 0  → 标记失败
        - score == 0 → 跳过（中性，不改动）
        """
        if not self._memory_mgr:
            logger.warning("[AgentLoop] 无 memory_mgr，跳过反馈闭环")
            return

        all_ids = state.used_memory_ids + state.used_neg_memory_ids
        if not all_ids:
            logger.info("[AgentLoop] 本轮未使用记忆，跳过反馈")
            return

        updated = 0
        skipped = 0
        for mid in all_ids:
            score = memory_scores.get(mid, 0.0)
            if score == 0.0:
                skipped += 1
                continue
            try:
                await self._memory_mgr.update_win_rate(mid, score > 0)
                updated += 1
            except Exception as e:
                logger.warning(f"[AgentLoop] update_win_rate {mid} failed: {e}")

        logger.info(
            f"[AgentLoop] 反馈闭环: {updated}/{len(all_ids)} 条记忆更新, "
            f"{skipped} 条跳过 (judge=LLM逐条评分)"
        )

    # ── snippet 构建 ──

    @classmethod
    def _build_snippets(cls, memories: List[MemoryEntry]) -> Dict[str, str]:
        """从 MemoryEntry 列表构建 id → 内容摘要的映射。"""
        snippets: Dict[str, str] = {}
        for mem in memories:
            content = (mem.content or mem.rules or mem.question)[:cls.SNIPPET_MAX_LEN]
            snippets[mem.id] = content
        return snippets

    # ── 格式化输出 ──

    def _fmt_pause_result(self, state: AgentLoopState) -> str:
        header = "⚡ 执行原始计划" if state.action == Action.EXECUTE_PLAN else "🔄 执行修订计划"
        return (
            f"{state.result}"
            f"## {header}\n\n"
            f"{state.plan}\n\n"
            f"---\n"
            f"💡 **下一步**: 执行以上步骤，然后将执行轨迹作为 `execution_trace` 传入。\n"
            f"📍 当前状态: phase={state.phase.value} | 迭代 {state.iteration}/{state.max_iterations}"
        )

    def _fmt_final_result(self, state: AgentLoopState) -> str:
        if state.error:
            return f"## ❌ 循环失败\n\n{state.error}\n\n{state.to_display()}"
        if not state.result:
            state.result = "（空结果）"
        return (
            f"{state.result}\n\n"
            f"---\n"
            f"📊 循环统计: {state.iteration} 次迭代 | 最终 phase: {state.phase.value}"
        )

    # ── 后台模式 ──

    async def run_in_background(self, goal: str,
                                 executor_fn=None,
                                 max_iterations: int = 3) -> AgentLoopState:
        gid = self._goal_id(goal)
        state = AgentLoopState(goal=goal, max_iterations=max_iterations)
        self._loops[gid] = state

        logger.info(f"[AgentLoop] 后台循环启动: {goal[:60]}")

        while not state.done and state.iteration < state.max_iterations:
            state.iteration += 1

            action = state.action

            if action == Action.BUILD_PLAN:
                await self._do_build_plan(state)
            elif action == Action.BUILD_REPLAN:
                await self._do_build_replan(state)
            elif action == Action.JUDGE_RESULT:
                await self._do_judge(state)
            elif action in (Action.EXECUTE_PLAN, Action.EXECUTE_REPLAN):
                if executor_fn:
                    try:
                        trace = await executor_fn(state)
                        state.execution_trace = trace
                    except Exception as e:
                        state.execution_trace = f"Execution failed: {e}"
                else:
                    state.execution_trace = f"（后台自动执行，迭代 {state.iteration}）"
            elif action == Action.FINISH:
                state.done = True
                break

            if state.done:
                break

            self._sm.next_phase(state)

        logger.info(f"[AgentLoop] 后台循环结束: phase={state.phase.value} iter={state.iteration}")
        return state
