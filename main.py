#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.21 - Phase 2: 补全后台任务/进化入口/清理诊断钩子
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from astrbot.api.star import Star, Context
from astrbot.api import logger as astr_logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest

from .storage import Storage
from .memory_manager import MemoryManager
from .reasoning_engine import ReasoningEngine
from .evolution_task import EvolutionEngine
from .tool_sanitizer import sanitize_content, sanitize_tool_output, ENABLE_SANITIZATION, STRICT_TOOL_NAMES
from .tools import (
    inject_plugin,
    StoreMemoryTool, SearchMemoryTool, UpdateWinRateTool,
    EvictMemoriesTool, GetEvolutionStatsTool, TriggerEvolutionTool,
    BuildPlanTool, JudgeReplanTool, BuildReplanTool, RunAgentLoopTool,
)
from .agent_loop import AgentLoop

CST = timezone(timedelta(hours=8))

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/AstrBot/data/glorious_evolution"
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

OLD_DB_PATH = os.path.join(PLUGIN_DIR, "evolution.db")
OLD_CHROMA_PATH = os.path.join(PLUGIN_DIR, "chroma_db")
OLD_STATS_PATH = os.path.join(PLUGIN_DIR, "evolution_stats.json")

DB_PATH = os.path.join(DATA_DIR, "evolution.db")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma_db")
EVO_STATS_FILE = os.path.join(DATA_DIR, "evolution_stats.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory_store.json")

VERSION = "1.0.21"
DEFAULT_EVO_INTERVAL_HOURS = 6

logger = logging.getLogger("GloriousEvolution")

_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


def _ensure_data_dir() -> None:
    """Ensure data dir and migrate from old locations."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH) and os.path.exists(OLD_DB_PATH):
        shutil.copy2(OLD_DB_PATH, DB_PATH)
        logger.info(f"[GE] migrated: evolution.db -> {DATA_DIR}/")

    if not os.path.exists(EVO_STATS_FILE) and os.path.exists(OLD_STATS_PATH):
        shutil.copy2(OLD_STATS_PATH, EVO_STATS_FILE)
        logger.info(f"[GE] migrated: evolution_stats.json -> {DATA_DIR}/")

    if not os.path.exists(CHROMA_PATH) and os.path.exists(OLD_CHROMA_PATH):
        shutil.copytree(OLD_CHROMA_PATH, CHROMA_PATH)
        logger.info(f"[GE] migrated: chroma_db -> {DATA_DIR}/")

    old_mem = os.path.join(PLUGIN_DIR, "memory_store.json")
    if not os.path.exists(MEMORY_FILE) and os.path.exists(old_mem):
        shutil.copy2(old_mem, MEMORY_FILE)
        logger.info(f"[GE] migrated: memory_store.json -> {DATA_DIR}/")


async def _auto_backup(source_path: str, label: str) -> Optional[str]:
    """Async copy file to backup dir with timestamp."""
    if not os.path.exists(source_path):
        return None
    ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
    fname = os.path.basename(source_path)
    base, ext = os.path.splitext(fname)
    dest = os.path.join(BACKUP_DIR, f"{base}_{label}_{ts}{ext}")
    await asyncio.get_event_loop().run_in_executor(None, shutil.copy2, source_path, dest)
    size_kb = os.path.getsize(dest) / 1024
    logger.info(f"[GE] backup: {os.path.basename(dest)} ({size_kb:.0f} KB)")
    await _auto_rotate_backups(source_path)
    return dest


async def _auto_rotate_backups(source_path: str, keep: int = 5) -> None:
    """轮转清理：对同一 base name 的备份文件，只保留最近 `keep` 份。"""
    fname = os.path.basename(source_path)
    base, _ = os.path.splitext(fname)
    prefix = f"{base}_"

    def _clean():
        candidates = []
        for entry in os.scandir(BACKUP_DIR):
            if entry.is_file() and entry.name.startswith(prefix):
                candidates.append((entry.path, os.path.getmtime(entry.path)))
        candidates.sort(key=lambda x: x[1], reverse=True)
        for path, _ in candidates[keep:]:
            os.remove(path)
            logger.debug(f"[GE] rotated out: {os.path.basename(path)}")

    await asyncio.get_event_loop().run_in_executor(None, _clean)


async def _backup_all(label: str = "evo") -> None:
    """Async backup all critical data files."""
    tasks = []
    for path in [DB_PATH, EVO_STATS_FILE, MEMORY_FILE]:
        if os.path.exists(path):
            tasks.append(_auto_backup(path, label))
    if tasks:
        await asyncio.gather(*tasks)


class MemoryStore:
    """JSON file-backed memory store (v1.0.6: read-only archive, new data via MemoryManager/SQLite)."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._memories: Dict[str, dict] = {}
        self._counter = 0
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._memories = data.get("memories", {})
                self._counter = data.get("counter", 0)
                logger.info(f"[GE] loaded {len(self._memories)} memories from disk")
            except Exception:
                logger.warning("[GE] memory file corrupt, starting fresh")
                self._memories = {}
                self._counter = 0

    def _save(self):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump({"memories": self._memories, "counter": self._counter}, f, ensure_ascii=False, indent=2)

    async def _save_async(self):
        await asyncio.get_event_loop().run_in_executor(None, self._save)

    async def add(self, entry: dict) -> str:
        logger.warning("[GE] MemoryStore.add() deprecated (v1.0.6)")
        return entry.get("id", "")

    def get(self, entry_id: str) -> Optional[dict]:
        return self._memories.get(entry_id)

    def list_all(self) -> List[dict]:
        return list(self._memories.values())

    async def update(self, entry_id: str, updates: dict):
        logger.warning(f"[GE] MemoryStore.update() deprecated (v1.0.6), skipping {entry_id}")

    async def delete(self, entry_id: str):
        logger.warning(f"[GE] MemoryStore.delete() deprecated (v1.0.6), skipping {entry_id}")

    def count(self) -> int:
        return len(self._memories)


class VectorStore:
    """ChromaDB-based vector store."""

    def __init__(self, persist_dir: str):
        self.persist_dir = persist_dir
        self._client = None
        self._collection = None
        self._embed_fn = None

    @property
    def ready(self) -> bool:
        return self._collection is not None and self._embed_fn is not None

    def set_embed_fn(self, fn):
        self._embed_fn = fn

    def init_collection(self):
        try:
            import chromadb
            self._client = chromadb.PersistentClient(path=self.persist_dir)
            self._collection = self._client.get_or_create_collection(
                name="glorious_evolution_memories",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"[GE] ChromaDB collection ready ({self._collection.count()} vectors)")
        except Exception as e:
            logger.error(f"[GE] ChromaDB init failed: {e}")

    async def add(self, entry_id: str, text: str, metadata: Optional[dict] = None):
        if not self.ready:
            return
        try:
            vec = await self._embed_fn([text])
            self._collection.add(
                ids=[entry_id],
                embeddings=vec,
                metadatas=[metadata if metadata else {"source": "ge"}],
            )
        except Exception as e:
            logger.warning(f"[GE] vector add failed ({entry_id}): {e}")

    async def search(self, query: str, top_k: int = 5) -> List[dict]:
        if not self.ready:
            return []
        try:
            q_vec = await self._embed_fn([query])
            results = self._collection.query(query_embeddings=q_vec, n_results=top_k)
            out = []
            if results and results["ids"] and results["ids"][0]:
                for i, eid in enumerate(results["ids"][0]):
                    out.append({
                        "id": eid,
                        "distance": results["distances"][0][i] if results.get("distances") else None,
                        "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                    })
            return out
        except Exception as e:
            logger.warning(f"[GE] vector search failed: {e}")
            return []

    def count(self) -> int:
        return self._collection.count() if self._collection else 0

    async def delete(self, entry_id: str):
        if self.ready:
            try:
                self._collection.delete(ids=[entry_id])
            except Exception:
                pass

    async def load_vectors_from_store(self, store: MemoryStore):
        if not self.ready:
            return
        existing_ids = set()
        try:
            existing = self._collection.get()
            if existing and existing["ids"]:
                existing_ids = set(existing["ids"])
        except Exception:
            pass
        for mem in store.list_all():
            eid = mem["id"]
            if eid in existing_ids:
                continue
            text = _mem_to_text(mem)
            await self.add(eid, text)


def _mem_to_text(mem: dict) -> str:
    parts = []
    if mem.get("question"):
        parts.append(f"Q: {mem['question']}")
    if mem.get("content"):
        parts.append(f"A: {mem['content']}")
    if mem.get("category"):
        parts.append(f"Category: {mem['category']}")
    if mem.get("memory_type"):
        parts.append(f"Type: {mem['memory_type']}")
    return " | ".join(parts) if parts else json.dumps(mem, ensure_ascii=False)


def _entry_to_text(entry: "MemoryEntry") -> str:
    from .models import MemoryEntry
    parts = []
    if entry.question:
        parts.append(f"Q: {entry.question}")
    if entry.content:
        parts.append(f"A: {entry.content}")
    if entry.category:
        parts.append(f"Category: {entry.category}")
    mt = entry.memory_type.value if hasattr(entry.memory_type, 'value') else str(entry.memory_type)
    if mt:
        parts.append(f"Type: {mt}")
    return " | ".join(parts)


class EvolutionStats:
    """Evolution statistics persistence."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._stats: Dict[str, Any] = {
            "total_evolutions": 0,
            "total_insights": 0,
            "total_consolidations": 0,
            "total_evictions": 0,
            "last_evolution_at": None,
            "last_evolution_duration_sec": None,
        }
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self._stats.update(json.load(f))
            except Exception:
                pass

    def _save(self):
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self._stats, f, ensure_ascii=False, indent=2)

    def increment(self, key: str, amount: int = 1):
        self._stats[key] = self._stats.get(key, 0) + amount
        self._save()

    def set(self, key: str, value):
        self._stats[key] = value
        self._save()

    def get_summary(self) -> dict:
        return dict(self._stats)


CATEGORIES = [
    "general", "debugging", "deployment", "coding",
    "configuration", "security", "insight", "consolidated_rule",
]

MEMORY_TYPES = ["procedural", "declarative", "episodic"]


async def classify_memory(question: str, content: str, llm_call) -> dict:
    prompt = (
        "你是一个记忆分类助手。请分析以下记忆，返回 JSON。\n\n"
        f"问题: {question}\n"
        f"内容: {content}\n\n"
        "请返回严格 JSON 格式：\n"
        "{\n"
        '  "category": "分类标签",\n'
        '  "memory_type": "procedural/declarative/episodic",\n'
        '  "tags": ["标签1", "标签2"]\n'
        "}\n\n"
        "分类标签可选：" + ", ".join(CATEGORIES) + "\n"
        "记忆类型：\n"
        "- procedural: 操作步骤、命令、流程\n"
        "- declarative: 事实、知识、信息\n"
        "- episodic: 事件、经历、对话记录\n"
    )
    try:
        resp = await llm_call(prompt)
        json_start = resp.find("{")
        json_end = resp.rfind("}") + 1
        if json_start != -1 and json_end > json_start:
            parsed = json.loads(resp[json_start:json_end])
            return {
                "category": parsed.get("category", "general"),
                "memory_type": parsed.get("memory_type", "declarative"),
                "tags": parsed.get("tags", []),
            }
    except Exception:
        pass
    return {"category": "general", "memory_type": "declarative", "tags": []}


class GloriousEvolutionPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        _ensure_data_dir()
        self.memory_store = MemoryStore(MEMORY_FILE)
        self.vector_store = VectorStore(CHROMA_PATH)
        self.evo_stats = EvolutionStats(EVO_STATS_FILE)
        self._embedding_provider = None
        self._embedding_retry_task: Optional[asyncio.Task] = None
        self._classifier_retry_task: Optional[asyncio.Task] = None
        self._classify_task: Optional[asyncio.Task] = None
        self._evo_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._agent_loop_task: Optional[asyncio.Task] = None
        self._agent_loop: Optional[AgentLoop] = None
        self._classifier_llm = None
        self._storage = Storage(DB_PATH)
        self._memory_mgr = MemoryManager(self._storage)
        self._reasoning_engine = ReasoningEngine(self._memory_mgr, context)
        self._evo_engine = EvolutionEngine(self._memory_mgr, self._reasoning_engine, context)
        self._evo_engine.set_distillation_config(self.config.get("distillation", {}))
        self._agent_loop = AgentLoop(self._reasoning_engine, self._memory_mgr)
        global _plugin_cache
        _plugin_cache = self
        inject_plugin(self)
        self.context.add_llm_tools(
            StoreMemoryTool(), SearchMemoryTool(), UpdateWinRateTool(),
            EvictMemoriesTool(), GetEvolutionStatsTool(), TriggerEvolutionTool(),
            BuildPlanTool(), JudgeReplanTool(), BuildReplanTool(),
            RunAgentLoopTool(),
        )
        logger.info(f"[Glorious Evolution] v{VERSION} init (data: {DATA_DIR})")

    # ── on_llm_request: 脱敏 + 记忆注入 (query 用 event.message_str) ──

    async def _run_evolution(self) -> None:
        """执行一次完整进化周期，并更新统计。"""
        import time as _time
        t0 = _time.monotonic()
        result = await self._evo_engine.run_evolution_cycle()
        elapsed = _time.monotonic() - t0
        self.evo_stats.increment("total_evolutions")
        self.evo_stats.increment("total_consolidations", result.get("consolidated", 0))
        self.evo_stats.increment("total_insights", result.get("insights", 0))
        self.evo_stats.increment("total_evictions", result.get("evicted", 0))
        self.evo_stats.set("last_evolution_at", datetime.now(CST).isoformat())
        self.evo_stats.set("last_evolution_duration_sec", round(elapsed, 1))
        await _backup_all(label="evo")
        logger.info(
            f"[GE] evolution cycle done in {elapsed:.1f}s: "
            f"consolidated={result.get('consolidated', 0)} "
            f"insights={result.get('insights', 0)} "
            f"distilled={result.get('distilled', 0)} "
            f"evicted={result.get('evicted', 0)}"
        )

    def _start_background_tasks(self) -> None:
        """启动后台定时任务：进化周期 + 健康检查。"""
        import time as _time
        async def _evo_loop():
            """每 6 小时运行一次进化周期。"""
            while True:
                try:
                    await asyncio.sleep(DEFAULT_EVO_INTERVAL_HOURS * 3600)
                    await self._run_evolution()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[GE] evolution loop error: {e}", exc_info=True)
                    await asyncio.sleep(300)

        async def _health_loop():
            """每 30 分钟检查 embedding/classifier 状态。"""
            while True:
                try:
                    await asyncio.sleep(1800)
                    if self._memory_mgr._embed_func is None and self._embedding_provider is None:
                        await self._init_embedding()
                    if self._memory_mgr._classify_func is None and self._classifier_llm is None:
                        await self._init_classifier()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[GE] health check error: {e}")
                    await asyncio.sleep(600)

        self._evo_task = asyncio.create_task(_evo_loop())
        self._health_check_task = asyncio.create_task(_health_loop())
        logger.info(f"[GE] background tasks started: evo({DEFAULT_EVO_INTERVAL_HOURS}h) + health(30m)")

    async def _init_embedding(self) -> None:
        """尝试初始化 embedding provider（延迟绑定）。"""
        try:
            provider = self.context.get_using_provider()
            if provider and hasattr(provider, 'embed'):
                async def _embed_fn(texts):
                    result = await provider.embed(texts)
                    return result
                await self._memory_mgr.set_embed_func(_embed_fn, dim=1536)
                self._embedding_provider = provider
                logger.info("[GE] embedding provider initialized")
        except Exception as e:
            logger.debug(f"[GE] embedding init deferred: {e}")

    async def _init_classifier(self) -> None:
        """尝试初始化自动分类器（延迟绑定）。"""
        try:
            provider = self.context.get_using_provider()
            if provider:
                async def _classify_fn(question: str, content: str) -> str:
                    async def _llm_call(prompt: str) -> str:
                        resp = await provider.text_chat(prompt=prompt, system_prompt="", temperature=0.0)
                        return resp.completion_text
                    result = await classify_memory(question, content, _llm_call)
                    return result.get("category", "general")
                await self._memory_mgr.set_classify_func(_classify_fn)
                self._classifier_llm = provider
                logger.info("[GE] classifier initialized")
        except Exception as e:
            logger.debug(f"[GE] classifier init deferred: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """框架加载完成后，初始化向量化 + 分类器 + 启动后台任务。"""
        logger.info("[GE] on_astrbot_loaded: initializing embedding, classifier, background tasks...")
        await self._memory_mgr.load_vectors()
        await self._init_embedding()
        await self._init_classifier()
        self._start_background_tasks()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        logger.info("[GE] on_llm_request fired")
        try:
            astr_logger.info("[GE] on_llm_request fired")
        except Exception:
            pass
        if ENABLE_SANITIZATION:
            try:
                sp = getattr(req, "system_prompt", None)
                if isinstance(sp, str) and len(sp) > 10:
                    req.system_prompt = sanitize_content(sp)
                prompt = getattr(req, "prompt", None)
                if isinstance(prompt, str) and len(prompt) > 10:
                    tool_name = GloriousEvolutionPlugin._detect_last_tool(prompt)
                    if tool_name and tool_name in STRICT_TOOL_NAMES:
                        req.prompt = sanitize_tool_output(tool_name, prompt)
                    else:
                        req.prompt = sanitize_content(prompt)
                elif isinstance(prompt, list):
                    for msg in prompt:
                        if isinstance(msg, dict):
                            content = msg.get("content")
                            if isinstance(content, str) and len(content) > 10:
                                msg["content"] = sanitize_content(content)
            except Exception as e:
                logger.debug(f"[GE] ToolCallHook sanitize error (silenced): {e}")
        await self._inject_relevant_memories(event, req)

    async def _inject_relevant_memories(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """v1.0.19: query 改为 event.message_str（用户原始消息），减少语义噪音。"""
        if not self._memory_mgr:
            return

        query = getattr(event, "message_str", "")
        logger.info("[GE] inject start: query='{}'".format(query[:80] if query else ""))
        if not query or len(query) <= 5:
            return

        # 1. 向量检索
        try:
            entries = await self._memory_mgr.retrieve_relevant_memories(
                query=query, top_k=5
            )
        except Exception as e:
            logger.debug(f"[GE] memory retrieval error: {e}")
            entries = []

        # 2. 蒸馏规则
        distilled_rules: List[str] = []
        try:
            rules = await self._storage.get_distilled_rules(min_win_rate=0.7, limit=3)
            distilled_rules = [r["content"][:120] for r in rules if r.get("content")]
        except Exception:
            pass

        logger.info(f"[GE] retrieved {len(entries)} entries, {len(distilled_rules)} rules")

        # 3. 构建注入块（v1.0.19: 去掉 win_rate 门槛，让所有检索结果都能注入）
        mem_lines = []
        injected_ids: List[str] = []
        for e in entries:
            mem_lines.append(f"- [{e.category}] win={e.win_rate:.0%}: {e.content[:150]}")
            injected_ids.append(e.id)

        rule_lines = [f"- {r}" for r in distilled_rules]

        injection_parts = []
        if mem_lines:
            injection_parts.append("[相关记忆]\n" + "\n".join(mem_lines))
        if rule_lines:
            injection_parts.append("[蒸馏规则 (高胜率)]\n" + "\n".join(rule_lines))

        if not injection_parts:
            return

        injection = "\n\n" + "\n\n".join(injection_parts)
        sp = getattr(req, "system_prompt", None)
        if isinstance(sp, str):
            req.system_prompt = sp + injection
        else:
            req.system_prompt = injection

        # 4. usage_count +1
        for eid in injected_ids:
            try:
                await self._memory_mgr.increment_usage(eid)
            except Exception:
                pass

        # 5. ID 透传给 Agent Loop judge
        try:
            prev = getattr(req, "_ge_injected_mem_ids", None) or []
            req._ge_injected_mem_ids = prev + injected_ids
        except Exception:
            pass

        logger.info(f"[GE] memory injection done: {len(injected_ids)} memories, {len(distilled_rules)} rules")

    @staticmethod
    def _detect_last_tool(prompt: str) -> Optional[str]:
        patterns = [
            r'Tool Result \(([^)]+)\)',
            r'🔧 Tool:\s*(\S+)',
            r'"name":\s*"([^"]+)"',
        ]
        for pat in patterns:
            matches = list(re.finditer(pat, prompt))
            if matches:
                return matches[-1].group(1)
        return None

    async def terminate(self) -> None:
        """取消所有后台任务，最后备份一次。"""
        logger.info("[GE] terminate: cancelling background tasks & backing up...")
        for task_name, task in [
            ("_evo_task", self._evo_task),
            ("_classify_task", self._classify_task),
            ("_health_check_task", self._health_check_task),
            ("_embedding_retry_task", self._embedding_retry_task),
            ("_classifier_retry_task", self._classifier_retry_task),
            ("_agent_loop_task", self._agent_loop_task),
        ]:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.debug(f"[GE] cancelled {task_name}")
        self._evo_task = None
        self._classify_task = None
        self._health_check_task = None
        self._embedding_retry_task = None
        self._classifier_retry_task = None
        self._agent_loop_task = None
        try:
            await _backup_all(label="shutdown")
        except Exception as e:
            logger.warning(f"[GE] shutdown backup failed: {e}")

    @filter.command("ges")
    async def cmd_stats(self, event: AstrMessageEvent):
        mgr_stats = await self._memory_mgr.get_stats()
        mems = mgr_stats["total_memories"]
        win_rate = round(mgr_stats.get("avg_win_rate", 0) * 100) if mems else 0
        vec_ready = "✅" if mgr_stats.get("embedding_ready") else "⏳"
        cls_ready = "✅" if self._classifier_llm else "⏳"
        msg = (
            f"📊 Glorious Evolution v{VERSION}\n"
            f"📚 memories: {mems} | 📈 win_rate: {win_rate}% | 🧠 vector: {vec_ready} | 🏷️ classifier: {cls_ready}\n"
            f"💾 data: {DATA_DIR}\n"
            f"🧬 Full MIA: Memory + Reasoning + Evolution + Classification ✅"
        )
        yield event.plain_result(msg)