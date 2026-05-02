#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.12 - 反馈闭环：Agent Loop 的 JUDGING 阶段自动更新所用记忆的 win_rate
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

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from astrbot.api.star import Star, Context
from astrbot.api import logger
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

VERSION = "1.0.12"
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

    @staticmethod
    def _pick_embedding_provider(providers: list) -> Optional[Any]:
        if not providers:
            return None
        PREFERRED_STRICT = ["Qwen3-Embedding-8B", "Qwen3-VL-Embedding-8B"]
        PREFERRED_FUZZY = ["Qwen3"]
        for pref in PREFERRED_STRICT + PREFERRED_FUZZY:
            for p in providers:
                pid = getattr(p, 'id', '') + ' ' + getattr(p, 'model', '')
                if pref.lower() in pid.lower():
                    return p
        return providers[0]

    async def _init_embedding_provider(self) -> None:
        if self.vector_store.ready:
            return
        emb_providers = self.context.get_all_embedding_providers()
        if emb_providers and len(emb_providers) > 0:
            self._embedding_provider = self._pick_embedding_provider(emb_providers)
            pid = getattr(self._embedding_provider, 'id', 'unknown')
            dim = self._embedding_provider.get_dim() if hasattr(self._embedding_provider, 'get_dim') else '?'
            logger.info(f"[GE] EmbeddingProvider ready (instant): {pid} (dim={dim})")
            await self._setup_embed_fn_and_collection()
            return
        if (not hasattr(self, '_embedding_retry_task') or
                self._embedding_retry_task is None or self._embedding_retry_task.done()):
            logger.info("[GE] EmbeddingProvider not ready, starting background retry...")
            self._embedding_retry_task = asyncio.create_task(self._retry_init_embedding())

    async def _retry_init_embedding(self):
        MAX_RETRIES = 60
        base_delay = 2.0
        max_delay = 30.0
        log_interval = 10
        for attempt in range(1, MAX_RETRIES + 1):
            delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
            await asyncio.sleep(delay)
            emb_providers = self.context.get_all_embedding_providers()
            if emb_providers and len(emb_providers) > 0:
                self._embedding_provider = self._pick_embedding_provider(emb_providers)
                pid = getattr(self._embedding_provider, 'id', 'unknown')
                dim = self._embedding_provider.get_dim() if hasattr(self._embedding_provider, 'get_dim') else '?'
                logger.info(f"[GE] EmbeddingProvider ready (attempt {attempt}): {pid} (dim={dim})")
                await self._setup_embed_fn_and_collection()
                return
            if attempt % log_interval == 0:
                next_delay = min(delay * 1.5, max_delay)
                logger.info(f"[GE] waiting EmbeddingProvider... (attempt {attempt}/{MAX_RETRIES}, next delay {next_delay:.1f}s)")
        logger.error(f"[GE] {MAX_RETRIES} retries, no EmbeddingProvider found")

    async def _setup_embed_fn_and_collection(self):
        ep = self._embedding_provider
        async def embed_fn(texts):
            return await ep.get_embeddings(texts)
        self.vector_store.set_embed_fn(embed_fn)
        self.vector_store.init_collection()
        dim = ep.get_dim() if hasattr(ep, 'get_dim') else '?'
        logger.info(f"[GE] ChromaDB collection ready (dim={dim})")
        await self.vector_store.load_vectors_from_store(self.memory_store)
        logger.info(f"[GE] vector backfill done ({self.vector_store.count()} total)")
        dim = ep.get_dim() if hasattr(ep, 'get_dim') else 0
        if dim > 0:
            async def _mem_mgr_embed(text):
                result = await ep.get_embeddings([text])
                return result[0] if result else None
            await self._memory_mgr.set_embed_func(_mem_mgr_embed, dim)
            await self._memory_mgr.load_vectors()
            logger.info("[GE] MemoryManager embed hook injected")

    async def _init_classifier(self):
        if self._classifier_llm is not None:
            return
        if self._try_setup_classifier():
            return
        if (not hasattr(self, '_classifier_retry_task') or
                self._classifier_retry_task is None or self._classifier_retry_task.done()):
            logger.info("[GE] classifier LLM not ready, starting background retry...")
            self._classifier_retry_task = asyncio.create_task(self._retry_init_classifier())

    def _try_setup_classifier(self) -> bool:
        try:
            all_providers = self.context.get_all_providers()
            if not all_providers:
                return False
            for idx, p in enumerate(all_providers):
                if not hasattr(p, "text_chat"):
                    continue
                pid = getattr(p, 'id', str(p))
                async def llm_call(prompt, _provider=p):
                    req = ProviderRequest(
                        prompt=prompt, image_urls=[], urls=[],
                        func_tool=None, session=None, context_compress=False,
                    )
                    resp = await _provider.text_chat(req)
                    return resp.completion_text if resp else ""
                self._classifier_llm = llm_call
                logger.info(f"[GE] classifier LLM ready: {pid}")
                async def _classify_mem(q, c):
                    classification = await classify_memory(q, c, llm_call)
                    return classification.get("category", "general")
                self._classify_task = asyncio.ensure_future(
                    self._memory_mgr.set_classify_func(_classify_mem)
                )
                logger.info("[GE] MemoryManager classify hook injected")
                return True
            logger.warning("[GE] get_all_providers returned list but no text_chat")
        except Exception as e:
            logger.warning(f"[GE] classifier probe error: {e}")
        return False

    async def _retry_init_classifier(self):
        MAX_RETRIES = 60
        base_delay = 2.0
        max_delay = 30.0
        log_interval = 10
        for attempt in range(1, MAX_RETRIES + 1):
            delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
            await asyncio.sleep(delay)
            if self._try_setup_classifier():
                return
            if attempt % log_interval == 0:
                next_delay = min(delay * 1.5, max_delay)
                logger.info(f"[GE] waiting classifier LLM... (attempt {attempt}/{MAX_RETRIES}, next delay {next_delay:.1f}s)")
        logger.error(f"[GE] {MAX_RETRIES} retries, no classifier LLM found")

    async def initialize(self) -> None:
        try:
            await self._init_embedding_provider()
        except Exception as e:
            logger.error(f"[GE] embedding init failed: {e}")
        asyncio.create_task(self._init_classifier())
        try:
            await self._scan_and_index()
        except Exception as e:
            logger.error(f"[GE] scan_and_index failed: {e}")
        asyncio.create_task(self._delayed_scan_and_index())
        self._evo_task = asyncio.create_task(self._evolution_loop())
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info(f"[Glorious Evolution] v{VERSION} started (data: {DATA_DIR})")

    async def _delayed_scan_and_index(self):
        await asyncio.sleep(30)
        try:
            await self._scan_and_index()
        except Exception as e:
            logger.error(f"[GE] delayed_scan_and_index error: {e}", exc_info=True)

    async def terminate(self) -> None:
        logger.info("[GE] final backup before shutdown...")
        try:
            await _backup_all(label="shutdown")
        except Exception as e:
            logger.warning(f"[GE] shutdown backup failed: {e}")
        if self._evo_task:
            self._evo_task.cancel()
            try:
                await self._evo_task
            except asyncio.CancelledError:
                pass
            self._evo_task = None
        if self._classify_task and not self._classify_task.done():
            self._classify_task.cancel()
            try:
                await asyncio.shield(self._classify_task)
            except asyncio.CancelledError:
                pass
            self._classify_task = None
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None

    async def _evolution_loop(self) -> None:
        INTERVAL_SECONDS = 360 * 60
        logger.info(f"[GE] evolution loop ready, first cycle in {INTERVAL_SECONDS//60} min")
        await asyncio.sleep(INTERVAL_SECONDS)
        while True:
            try:
                await self._scan_and_index()
                await self._run_evolution()
                asyncio.create_task(_backup_all(label="evo"))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[GE] evolution loop error: {e}")
            await asyncio.sleep(INTERVAL_SECONDS)

    REQUIRED_FILES = [
        "main.py", "__init__.py", "models.py", "storage.py",
        "memory_manager.py", "reasoning_engine.py", "evolution_task.py",
        "tool_sanitizer.py", "metadata.yaml", "_conf_schema.json",
    ]

    async def _health_check_loop(self) -> None:
        CHECK_INTERVAL = 30 * 60
        logger.info("[GE] health check loop ready (interval: 30 min)")
        await asyncio.sleep(CHECK_INTERVAL)
        while True:
            try:
                violations = []
                for fname in self.REQUIRED_FILES:
                    fpath = os.path.join(PLUGIN_DIR, fname)
                    if not os.path.exists(fpath):
                        violations.append(f"MISSING: {fname}")
                    elif os.path.getsize(fpath) == 0:
                        violations.append(f"EMPTY: {fname}")
                if os.path.exists(DB_PATH):
                    try:
                        import sqlite3
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute("SELECT COUNT(*) FROM memories")
                        conn.close()
                    except Exception as e:
                        violations.append(f"DB_CORRUPT: {e}")
                global _plugin_cache
                if _plugin_cache is None:
                    try:
                        from astrbot.api.star import GlobalStarMap
                        star_map = GlobalStarMap()
                        found = False
                        for v in star_map.star_map.values():
                            if isinstance(v, GloriousEvolutionPlugin):
                                _plugin_cache = v
                                found = True
                                break
                        if not found:
                            violations.append("UNREGISTERED: plugin not in star_map")
                    except Exception:
                        violations.append("UNREGISTERED: GlobalStarMap inaccessible")
                if violations:
                    logger.error(f"[GE] HEALTH CHECK FAILED: {'; '.join(violations)}")
                    for v in violations:
                        if v.startswith("MISSING:"):
                            fname = v.split(": ", 1)[1]
                            fpath = os.path.join(PLUGIN_DIR, fname)
                            if not os.path.exists(fpath):
                                with open(fpath, "w") as f:
                                    f.write("")
                                logger.warning(f"[GE] auto-repair: created empty {fname}")
                else:
                    logger.debug("[GE] health check passed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[GE] health check error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _scan_and_index(self):
        if not self.vector_store.ready:
            await self._init_embedding_provider()
        if not self.vector_store.ready:
            logger.warning("[GE] scan_and_index skip: VectorStore not ready")
            return
        mem_entries = await self._storage.get_all_memories(limit=10000)
        if not mem_entries:
            return
        existing_ids: set = set()
        try:
            existing = self.vector_store._collection.get()
            if existing and existing.get("ids"):
                existing_ids = set(existing["ids"])
        except Exception:
            pass
        indexed = 0
        for entry in mem_entries:
            eid = entry.id
            if eid in existing_ids:
                continue
            text = _entry_to_text(entry)
            await self.vector_store.add(eid, text)
            indexed += 1
        logger.info(f"[GE] scan_and_index: {indexed}/{len(mem_entries)} new (total {self.vector_store.count()})")

    async def _run_evolution(self):
        start_ts = time.time()
        logger.info("[GE] evolution cycle started (EvolutionEngine)...")
        try:
            result = await asyncio.wait_for(
                self._evo_engine.run_evolution_cycle(),
                timeout=self._evo_engine.CYCLE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[GE] EvolutionEngine timeout (300s), skip")
            return
        except Exception as e:
            logger.error(f"[GE] EvolutionEngine error: {e}", exc_info=True)
            return
        duration = time.time() - start_ts
        self.evo_stats.increment("total_evolutions")
        self.evo_stats.increment("total_insights", result.get("insights", 0))
        self.evo_stats.increment("total_consolidations", result.get("consolidated", 0))
        self.evo_stats.increment("total_evictions", result.get("evicted", 0))
        self.evo_stats.set("last_evolution_at", datetime.now(CST).isoformat())
        self.evo_stats.set("last_evolution_duration_sec", round(duration, 2))
        logger.info(
            f"[GE] evolution done: "
            f"consolidated={result.get('consolidated', 0)} "
            f"insights={result.get('insights', 0)} "
            f"evicted={result.get('evicted', 0)} "
            f"duration={duration:.1f}s"
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if ENABLE_SANITIZATION:
            try:
                sp = getattr(req, "system_prompt", None)
                if isinstance(sp, str) and len(sp) > 10:
                    req.system_prompt = sanitize_content(sp)
                prompt = getattr(req, "prompt", None)
                if isinstance(prompt, str) and len(prompt) > 10:
                    tool_name = self._detect_last_tool(prompt)
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
        await self._inject_relevant_memories(req)

    async def _inject_relevant_memories(self, req: ProviderRequest) -> None:
        if not self._memory_mgr:
            return
        prompt = getattr(req, "prompt", "")
        if isinstance(prompt, str) and len(prompt) > 5:
            try:
                entries = await self._memory_mgr.retrieve_relevant_memories(
                    query=prompt, top_k=3
                )
                if entries:
                    mem_lines = [
                        f"- [{e.category}] win={e.win_rate:.0%}: {e.content[:150]}"
                        for e in entries
                    ]
                    mem_block = "\n".join(mem_lines)
                    injection = f"\n\n[相关记忆]\n{mem_block}"
                    sp = getattr(req, "system_prompt", None)
                    if isinstance(sp, str):
                        req.system_prompt = sp + injection
                    else:
                        req.system_prompt = injection
            except Exception as e:
                logger.debug(f"[GE] memory injection error: {e}")

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
