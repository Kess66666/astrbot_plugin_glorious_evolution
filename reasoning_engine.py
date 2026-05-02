"""
光荣进化系统 - 推理引擎
MIA 风格的 Plan-Judge-Replan 循环（Phase 2）

v1.0.12: build_replan 返回 (replan_text, pos, neg) 以支持反馈闭环
"""

from typing import Any, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import AstrMessageEvent

from .memory_manager import MemoryManager
from .models import MemoryEntry


class ReasoningEngine:
    """MIA 风格的推理引擎"""

    def __init__(self, memory_mgr: MemoryManager, context: Context) -> None:
        self.memory_mgr = memory_mgr
        self.context = context

    def _get_provider(self, event: Optional[AstrMessageEvent] = None) -> Any:
        """获取 LLM Provider（兼容 event=None 的后台调用场景）。"""
        if event is not None:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if provider:
                return provider
        provider = self.context.get_using_provider()
        if not provider:
            providers = self.context.get_all_providers()
            if providers:
                provider = providers[0]
        if not provider:
            raise RuntimeError("No LLM provider available")
        return provider

    async def _call_llm(self, event: Optional[AstrMessageEvent], system_prompt: str,
                        user_prompt: str, temperature: float = 0.0) -> str:
        provider = self._get_provider(event)
        response = await provider.text_chat(
            prompt=user_prompt, system_prompt=system_prompt, temperature=temperature)
        return response.completion_text

    def _format_memories_for_prompt(self, pos_memories: List[MemoryEntry],
                                    neg_memories: List[MemoryEntry]) -> str:
        lines = []
        if pos_memories:
            lines.append("## ✅ 正面策略（成功经验）")
            for i, mem in enumerate(pos_memories, 1):
                lines.append(f"{i}. [胜率: {mem.win_rate:.0%}] {mem.content[:200]}")
            lines.append("")
        if neg_memories:
            lines.append("## ❌ 反面教训（失败经验）")
            for i, mem in enumerate(neg_memories, 1):
                lines.append(f"{i}. [胜率: {mem.win_rate:.0%}] {mem.content[:200]}")
            lines.append("")
        return "\n".join(lines) if lines else "（暂无相关记忆）"

    async def build_plan(self, event: Optional[AstrMessageEvent], question: str,
                         extra_context: str = "") -> Tuple[str, List[MemoryEntry], List[MemoryEntry]]:
        pos, neg = await self.memory_mgr.retrieve_balanced_memories(query=question, pos_top_k=2, neg_top_k=2)
        memories_text = self._format_memories_for_prompt(pos, neg)
        system_prompt = ("You are a strategic planning assistant. "
                         "Given memories of past successes and failures, create a clear step-by-step action plan.")
        user_prompt = f"## 问题\n{question}\n\n## 相关记忆\n{memories_text}\n\n## 要求\n请生成分步行动计划（核心目标、2-5具体步骤、风险应对、成功标准）"
        if extra_context:
            user_prompt = f"## 问题\n{question}\n\n## 额外上下文\n{extra_context}\n\n## 相关记忆\n{memories_text}\n\n## 要求\n请生成分步行动计划"
        plan_text = await self._call_llm(event, system_prompt, user_prompt)
        logger.info(f"[Glorious Evolution] build_plan: pos={len(pos)} neg={len(neg)}")
        return plan_text, pos, neg

    async def judge_replan(self, event: Optional[AstrMessageEvent], execution_trace: str) -> str:
        system_prompt = "You are a critical evaluation assistant. Analyze the execution trace and determine if replanning is needed."
        user_prompt = f"## 执行轨迹\n{execution_trace}\n\n## 输出要求\n请只输出 'yes' 或 'no'"
        result = await self._call_llm(event, system_prompt, user_prompt)
        return "yes" if result.strip().lower().startswith("yes") else "no"

    async def build_replan(self, event: Optional[AstrMessageEvent], question: str,
                           execution_trace: str) -> Tuple[str, List[MemoryEntry], List[MemoryEntry]]:
        """v1.0.12: 返回 (replan_text, pos_memories, neg_memories) 以支持反馈闭环"""
        pos, neg = await self.memory_mgr.retrieve_balanced_memories(query=question, pos_top_k=4, neg_top_k=4)
        memories_text = self._format_memories_for_prompt(pos, neg)
        system_prompt = ("You are a strategic replanning assistant. "
                         "Analyze failures and provide supplementary strategies. Do not repeat completed steps.")
        user_prompt = (f"## 原始问题\n{question}\n\n## 执行轨迹\n{execution_trace}\n\n"
                       f"## 相关记忆\n{memories_text}\n\n## 要求\n提供补充计划（失败原因、改进建议、避免措施、新行动）")
        replan_text = await self._call_llm(event, system_prompt, user_prompt)
        logger.info(f"[Glorious Evolution] build_replan: pos={len(pos)} neg={len(neg)}")
        return replan_text, pos, neg
