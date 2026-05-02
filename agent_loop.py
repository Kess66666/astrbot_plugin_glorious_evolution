"""
光荣进化系统 - 混合 Agent 循环
State-driven + LLM-assisted

v1.0.12: 反馈闭环 — JUDGING 阶段自动更新所用记忆的 win_rate
         (ChatGPT 建议 + Claude 指出的优先级 #1)
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

    v1.0.12: 自动反馈闭环 — judge 结果自动写回所用记忆的 win_rate
    """

    def __init__(self, reasoning_engine, memory_mgr=None, max_iterations: int = 3):
        self._engine = reasoning_engine
        self._memory_mgr = memory_mgr  # v1.0.12: 反馈闭环
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
            # v1.0.12: 追踪本轮所用记忆 ID
            state.used_memory_ids = [e.id for e in pos]
            state.used_neg_memory_ids = [e.id for e in neg]
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
            # v1.0.12: build_replan 现在也返回 pos/neg
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
        """LLM 评估 + v1.0.12 反馈闭环：自动更新所用记忆的 win_rate"""
        try:
            need_replan = await self._engine.judge_replan(
                event=None, execution_trace=state.execution_trace,
            )
        except RuntimeError:
            failure_kw = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
            need_replan = "yes" if any(k in state.execution_trace.lower() for k in failure_kw) else "no"

        success = need_replan.strip().lower().startswith("no")

        # ── v1.0.12 反馈闭环 ──
        await self._record_feedback(state, success)

        state.result = f"🔍 评估结果: {'需要重规划' if need_replan == 'yes' else '完成'}"
        self._sm.next_phase(state, judge_result=need_replan)

    async def _record_feedback(self, state: AgentLoopState, success: bool) -> None:
        """v1.0.12: 根据 judge 结果自动更新本轮所用记忆的 win_rate。"""
        if not self._memory_mgr:
            logger.warning("[AgentLoop] 无 memory_mgr，跳过反馈闭环")
            return

        all_ids = state.used_memory_ids + state.used_neg_memory_ids
        if not all_ids:
            logger.info("[AgentLoop] 本轮未使用记忆，跳过反馈")
            return

        updated = 0
        for mid in all_ids:
            try:
                await self._memory_mgr.update_win_rate(mid, success)
                updated += 1
            except Exception as e:
                logger.warning(f"[AgentLoop] update_win_rate {mid} failed: {e}")

        logger.info(
            f"[AgentLoop] 反馈闭环: {updated}/{len(all_ids)} 条记忆 "
            f"→ {'成功' if success else '失败'} (judge={'no' if success else 'yes'})"
        )

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
