"""
光荣进化系统 - 推理引擎
MIA 风格的 Plan-Judge-Replan 循环（Phase 2）

v1.0.12: build_replan 返回 (replan_text, pos, neg) 以支持反馈闭环
v1.0.13: judge_replan 升级为记忆感知评分 — 拿到 memory_snippets 后逐条评价贡献度
"""

import json
from typing import Any, Dict, List, Optional, Tuple

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

    async def judge_replan(self, event: Optional[AstrMessageEvent], execution_trace: str,
                           memory_snippets: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """v1.0.13: 记忆感知判断 — 输出 need_replan + 逐条贡献评分。

        Returns:
            dict: {"need_replan": "yes"/"no",
                   "memory_contributions": {mem_id: float ([-1.0, 1.0]), ...}}

        当 memory_snippets 为 None 时走简单 yes/no 兼容模式（Tools 调用路径）。
        """
        if memory_snippets:
            snippets_block = "\n".join(
                f"  - {mid}: {snippet[:150]}" for mid, snippet in memory_snippets.items()
            )
            system_prompt = (
                "You are a critical evaluation assistant. "
                "Analyze the execution trace alongside the memories that were used to build the plan. "
                "Determine: 1) whether replanning is needed, "
                "2) how much each memory contributed to the outcome."
            )
            user_prompt = (
                f"## 执行轨迹\n{execution_trace}\n\n"
                f"## 使用的记忆（ID: 内容摘要）\n{snippets_block}\n\n"
                f"## 输出要求\n"
                f"只输出一行 JSON（不要 markdown 代码块，不要解释）：\n"
                f'{{"need_replan": "yes"|"no", "memory_contributions": {{"MEM-xxx": 贡献度, ...}}}}\n\n'
                f"贡献度: -1.0 = 严重误导, 0 = 无贡献, 1.0 = 关键帮助。只列出有显著贡献(≠0)的记忆。"
            )
            result = await self._call_llm(event, system_prompt, user_prompt)
            return self._parse_judge_response(result)
        else:
            # 兼容模式: 简单 yes/no（Tools.py 调用路径）
            system_prompt = (
                "You are a critical evaluation assistant. "
                "Analyze the execution trace and determine if replanning is needed."
            )
            user_prompt = f"## 执行轨迹\n{execution_trace}\n\n## 输出要求\n请只输出 'yes' 或 'no'"
            result = await self._call_llm(event, system_prompt, user_prompt)
            return {
                "need_replan": "yes" if result.strip().lower().startswith("yes") else "no",
                "memory_contributions": {},
            }

    def _parse_judge_response(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 的 JSON 输出，剥离 markdown 包裹，处理格式异常。"""
        json_str = raw.strip()
        # 剥离 ```json ... ``` 包裹
        if json_str.startswith("```"):
            end = json_str.rfind("```")
            if end > 3:
                start = json_str.index("\n") if "\n" in json_str else 3
                json_str = json_str[start:end].strip()
            else:
                json_str = json_str[3:].strip()

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning(f"[GE] judge_replan JSON 解析失败, raw={raw[:200]}")
            return {"need_replan": "no", "memory_contributions": {}}

        need_replan = str(parsed.get("need_replan", "no")).strip().lower()
        if need_replan not in ("yes", "no"):
            need_replan = "no"

        raw_contrib = parsed.get("memory_contributions", {})
        memory_contributions: Dict[str, float] = {}
        for mid, score in raw_contrib.items():
            try:
                s = max(-1.0, min(1.0, float(score)))
                memory_contributions[mid.strip()] = s
            except (TypeError, ValueError):
                memory_contributions[mid.strip()] = 0.0

        return {"need_replan": need_replan, "memory_contributions": memory_contributions}

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
