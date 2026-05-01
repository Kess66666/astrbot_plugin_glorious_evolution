"""
光荣进化系统 - 混合 Agent 循环
State-driven + LLM-assisted（ChatGPT 建议的 Hybrid Agent 模式）

核心设计：
- 状态机控制主流程（下一步做什么，不由 LLM 决定）
- LLM 只在策略阶段提供补充（planning / judging / replanning）
- 固定 Action 枚举约束所有行为
- 支持后台异步循环（run_in_background）
"""

import asyncio
import hashlib
from typing import Dict, Optional, Tuple

from astrbot.api import logger

from .models import (
    Action,
    AgentLoopState,
    Phase,
    PHASE_TRANSITIONS,
    PHASE_TO_ACTION,
)


class StateMachine:
    """状态机：决定 phase 之间的确定性转换。LLM 无权修改。"""

    def next_phase(self, state: AgentLoopState, judge_result: str = "") -> None:
        """
        根据当前 phase 和 judge 结果推进状态机。
        - 大部分 transition 是确定性的
        - JUDGING 是唯一的分支点（LLM 判断 yes/no → REPLANNING/DONE）
        """
        if state.phase == Phase.JUDGING:
            self._handle_judge_branch(state, judge_result)
            return

        if state.phase == Phase.DONE or state.phase == Phase.FAILED:
            state.done = True
            return

        # 确定性转换
        next_phase = PHASE_TRANSITIONS.get(state.phase)
        if next_phase is None:
            logger.warning(f"[AgentLoop] 未知 phase: {state.phase}，标记失败")
            state.phase = Phase.FAILED
            state.action = Action.FINISH
            state.done = True
            return

        state.phase = next_phase
        state.action = PHASE_TO_ACTION.get(next_phase, Action.FINISH)

        # 执行类阶段暂停循环，等调用方执行完毕后继续
        if state.action in (Action.EXECUTE_PLAN, Action.EXECUTE_REPLAN):
            state.done = True  # 暂停，外部继续时会重置 done=False

    def _handle_judge_branch(self, state: AgentLoopState, judge_result: str) -> None:
        """JUDGING 分支：LLM 返回 yes → 重规划，no → 完成"""
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

    用法（工具模式）：
        loop = AgentLoop(reasoning_engine)
        result = await loop.process("分析这个仓库")

    用法（后台模式）：
        loop = AgentLoop(reasoning_engine)
        task = loop.run_in_background("分析这个仓库")
    """

    def __init__(self, reasoning_engine, max_iterations: int = 3):
        self._engine = reasoning_engine
        self._sm = StateMachine()
        self._loops: Dict[str, AgentLoopState] = {}  # goal_id → state
        self._max_iterations = max_iterations
        self._lock = asyncio.Lock()

    @staticmethod
    def _goal_id(goal: str) -> str:
        return hashlib.md5(goal.encode()).hexdigest()[:8]

    # ── 工具模式：每次调用推进状态机 ──

    async def process(self, goal: str, execution_trace: str = "",
                      max_iterations: int = 3) -> str:
        """
        工具模式：LLM Agent 每次调用时推进一步或多步。
        - 第一次调用：创建新循环 → 推进到 EXECUTING 暂停
        - 后续调用（带 execution_trace）：从上次暂停处继续
        """
        gid = self._goal_id(goal)

        async with self._lock:
            state = self._get_or_create_state(gid, goal, max_iterations)

            if execution_trace:
                # 恢复循环：上次停在 EXECUTING，现在拿到执行结果
                state.execution_trace = execution_trace
                state.done = False  # 重置暂停标记
                if state.phase == Phase.EXECUTING:
                    self._sm.next_phase(state)  # EXECUTING → JUDGING
                elif state.phase == Phase.EXECUTING_REPLAN:
                    self._sm.next_phase(state)  # EXECUTING_REPLAN → JUDGING

            # 推进循环（直到遇到执行阶段暂停或结束）
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
        """推进状态机直到暂停或结束。返回给调用方的结果文本。"""
        while not state.done and state.iteration < state.max_iterations:
            state.iteration += 1
            logger.info(f"[AgentLoop] step {state.iteration}: phase={state.phase.value} action={state.action.value}")

            action = state.action

            if action == Action.BUILD_PLAN:
                await self._do_build_plan(state)

            elif action == Action.BUILD_REPLAN:
                await self._do_build_replan(state)

            elif action == Action.JUDGE_RESULT:
                await self._do_judge(state)

            elif action in (Action.EXECUTE_PLAN, Action.EXECUTE_REPLAN):
                # 暂停：等调用方执行后继续
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

            # 推进状态机
            self._sm.next_phase(state)

        # 循环结束
        return self._fmt_final_result(state)

    # ── 各动作实现 ──

    async def _do_build_plan(self, state: AgentLoopState) -> None:
        """LLM 生成初始计划"""
        try:
            plan_text, pos, neg = await self._engine.build_plan(
                event=None, question=state.goal, extra_context=""
            )
            state.plan = plan_text
            state.result = (
                f"## 🎯 目标\n{state.goal}\n\n"
                f"## 📋 初始计划\n{plan_text}\n\n"
                f"📊 检索到 {len(pos)} 正面 + {len(neg)} 负面记忆\n\n"
            )
            logger.info(f"[AgentLoop] build_plan OK: pos={len(pos)} neg={len(neg)}")
        except Exception as e:
            logger.error(f"[AgentLoop] build_plan failed: {e}")
            state.error = str(e)
            state.phase = Phase.FAILED
            state.done = True

    async def _do_build_replan(self, state: AgentLoopState) -> None:
        """LLM 生成修订计划"""
        try:
            replan = await self._engine.build_replan(
                event=None, question=state.goal, execution_trace=state.execution_trace,
            )
            state.plan = replan
            state.result = (
                f"## 🔄 修订计划\n\n{replan}\n\n"
                f"💡 基于失败轨迹重新规划"
            )
            logger.info("[AgentLoop] build_replan OK")
        except Exception as e:
            logger.error(f"[AgentLoop] build_replan failed: {e}")
            state.error = str(e)
            state.phase = Phase.FAILED
            state.done = True

    async def _do_judge(self, state: AgentLoopState) -> None:
        """LLM 评估执行结果 → yes/no（状态机根据结果分支）"""
        try:
            need_replan = await self._engine.judge_replan(
                event=None, execution_trace=state.execution_trace,
            )
        except RuntimeError:
            # LLM 降级：关键词判断
            failure_kw = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
            need_replan = "yes" if any(k in state.execution_trace.lower() for k in failure_kw) else "no"

        state.result = f"🔍 评估结果: {'需要重规划' if need_replan == 'yes' else '完成'}"
        self._sm.next_phase(state, judge_result=need_replan)

    # ── 格式化输出 ──

    def _fmt_pause_result(self, state: AgentLoopState) -> str:
        """暂停时的输出：提示调用方执行计划"""
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
        """循环结束时的输出"""
        if state.error:
            return f"## ❌ 循环失败\n\n{state.error}\n\n{state.to_display()}"
        if not state.result:
            state.result = "（空结果）"
        return (
            f"{state.result}\n\n"
            f"---\n"
            f"📊 循环统计: {state.iteration} 次迭代 | 最终 phase: {state.phase.value}"
        )

    # ── 后台模式：自主运行完整循环 ──

    async def run_in_background(self, goal: str,
                                 executor_fn=None,
                                 max_iterations: int = 3) -> AgentLoopState:
        """
        后台模式：自主运行完整循环，直到完成或超过最大迭代。
        
        executor_fn: async callable(state) → execution_trace (str)
        如果不提供 executor_fn，则在 EXECUTE 阶段返回空轨迹（调试用）。
        """
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
                # 后台模式：如果有执行函数就调用，否则模拟
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
