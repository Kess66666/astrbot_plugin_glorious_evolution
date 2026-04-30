#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.7 - ToolCallHook: 敏感信息脱敏拦截发往 LLM 的工具返回值
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from astrbot.api.star import Star, Context
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest

# ── MIA 引擎（完整版进化循环） ──
from .storage import Storage
from .memory_manager import MemoryManager
from .reasoning_engine import ReasoningEngine
from .evolution_task import EvolutionEngine
from .tool_sanitizer import sanitize_content, sanitize_tool_output, ENABLE_SANITIZATION, STRICT_TOOL_NAMES

# 上海时区
CST = timezone(timedelta(hours=8))

# ── 本地存储路径 ──
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(PLUGIN_DIR, "memory_store.json")
EVO_STATS_FILE = os.path.join(PLUGIN_DIR, "evolution_stats.json")
CHROMA_PATH = os.path.join(PLUGIN_DIR, "chroma_db")

# ── 常量 ──
VERSION = "1.0.7"
DEFAULT_EVO_INTERVAL_HOURS = 6

logger = logging.getLogger("GloriousEvolution")

# ── 全局缓存：避免每次工具函数调用都遍历 GlobalStarMap ──
_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


# ═══════════════════════════════════════════════════════════════
# 内存记忆引擎 (v1.0.6: JSON 降级为只读历史存档，新数据走 MemoryManager/SQLite)
# ═══════════════════════════════════════════════════════════════

class MemoryStore:
    """JSON 文件持久化的记忆存储（v1.0.6: 降级为只读存档）"""

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
                logger.info(f"[GE] 从磁盘加载 {len(self._memories)} 条记忆")
            except Exception:
                logger.warning("[GE] 记忆文件损坏，从空库开始")
                self._memories = {}
                self._counter = 0

    def _save(self):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump({"memories": self._memories, "counter": self._counter}, f, ensure_ascii=False, indent=2)

    async def _save_async(self):
        """非阻塞写入（通过线程池），避免阻塞事件循环 (v1.0.5)"""
        await asyncio.get_event_loop().run_in_executor(None, self._save)

    async def add(self, entry: dict) -> str:
        """已弃用 (v1.0.6)：只读归档，新数据请走 MemoryManager"""
        logger.warning("[GE] MemoryStore.add() 已弃用 (v1.0.6)，仅返回 ID，未写入 JSON")
        return entry.get("id", "")

    def get(self, entry_id: str) -> Optional[dict]:
        """v1.0.5: 不再每次 get 触发写盘，减少同步 I/O"""
        return self._memories.get(entry_id)

    def list_all(self) -> List[dict]:
        return list(self._memories.values())

    async def update(self, entry_id: str, updates: dict):
        """已弃用 (v1.0.6)：只读归档"""
        logger.warning(f"[GE] MemoryStore.update() 已弃用 (v1.0.6)，跳过 {entry_id}")

    async def delete(self, entry_id: str):
        """已弃用 (v1.0.6)：只读归档"""
        logger.warning(f"[GE] MemoryStore.delete() 已弃用 (v1.0.6)，跳过 {entry_id}")

    def count(self) -> int:
        return len(self._memories)


# ═══════════════════════════════════════════════════════════════
# 向量化引擎
# ═══════════════════════════════════════════════════════════════

class VectorStore:
    """基于 ChromaDB 的向量存储"""

    def __init__(self, persist_dir: str):
        self.persist_dir = persist_dir
        self._client = None
        self._collection = None
        self._embed_fn = None  # async callable(texts: List[str]) -> List[List[float]]

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
            logger.info(f"[GE] ChromaDB 集合就绪 (现有 {self._collection.count()} 条向量)")
        except Exception as e:
            logger.error(f"[GE] ChromaDB 初始化失败: {e}")

    async def add(self, entry_id: str, text: str, metadata: Optional[dict] = None):
        if not self.ready:
            logger.warning("[GE] VectorStore 未就绪，跳过向量化")
            return
        try:
            vec = await self._embed_fn([text])
            self._collection.add(
                ids=[entry_id],
                embeddings=vec,
                metadatas=[metadata or {}],
            )
        except Exception as e:
            logger.warning(f"[GE] 向量化添加失败 ({entry_id}): {e}")

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
            logger.warning(f"[GE] 向量检索失败: {e}")
            return []

    def count(self) -> int:
        if self._collection:
            return self._collection.count()
        return 0

    async def delete(self, entry_id: str):
        if self.ready:
            try:
                self._collection.delete(ids=[entry_id])
            except Exception:
                pass

    async def load_vectors_from_store(self, store: MemoryStore):
        """从 MemoryStore 回填所有缺失的向量"""
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
    """将记忆字典转为可向量化的文本（v1.0.6: 仅用于旧数据兼容，新数据走 _entry_to_text）"""
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
    """将 MemoryEntry (SQLite) 转为可向量化的文本"""
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


# ═══════════════════════════════════════════════════════════════
# 进化统计
# ═══════════════════════════════════════════════════════════════

class EvolutionStats:
    """进化统计持久化"""

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


# ═══════════════════════════════════════════════════════════════
# 简易分类器 (LLM-powered)
# ═══════════════════════════════════════════════════════════════

CATEGORIES = [
    "general",
    "debugging",
    "deployment",
    "coding",
    "configuration",
    "security",
    "insight",
    "consolidated_rule",
]

MEMORY_TYPES = ["procedural", "declarative", "episodic"]


async def classify_memory(question: str, content: str, llm_call) -> dict:
    """调用 LLM 对记忆进行分类"""
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


# ═══════════════════════════════════════════════════════════════
# Plugin Entry
# ═══════════════════════════════════════════════════════════════

class GloriousEvolutionPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 存储引擎
        self.memory_store = MemoryStore(MEMORY_FILE)
        self.vector_store = VectorStore(CHROMA_PATH)
        self.evo_stats = EvolutionStats(EVO_STATS_FILE)

        # ── 运行时状态 ──
        self._embedding_provider = None
        self._embedding_retry_task: Optional[asyncio.Task] = None
        self._classifier_retry_task: Optional[asyncio.Task] = None
        self._classify_task: Optional[asyncio.Task] = None  # v1.0.5: 跟踪分类器注入任务
        self._evo_task: Optional[asyncio.Task] = None
        self._classifier_llm = None

        # ── MIA 完整版进化引擎（EvolutionEngine） ──
        self._storage = Storage(PLUGIN_DIR)
        self._memory_mgr = MemoryManager(self._storage)
        self._reasoning_engine = ReasoningEngine(self._memory_mgr, context)
        self._evo_engine = EvolutionEngine(self._memory_mgr, self._reasoning_engine, context)

        # ── v1.0.5: 注册到全局缓存 ──
        global _plugin_cache
        _plugin_cache = self

        logger.info(f"[Glorious Evolution] v{VERSION} __init__ 完成")

    # ── Embedding 供应商注入 (v1.0.5: 优先选 Qwen3-Embedding-8B) ──

    @staticmethod
    def _pick_embedding_provider(providers: list) -> Optional[Any]:
        """从可用 provider 中优先选 Qwen3-Embedding-8B，降级到第一个可用的"""
        if not providers:
            return None
        # 优先级匹配：先精确后模糊
        PREFERRED_STRICT = ["Qwen3-Embedding-8B", "Qwen3-VL-Embedding-8B"]
        PREFERRED_FUZZY = ["Qwen3"]
        for pref in PREFERRED_STRICT + PREFERRED_FUZZY:
            for p in providers:
                pid = getattr(p, 'id', '') + ' ' + getattr(p, 'model', '')
                if pref.lower() in pid.lower():
                    return p
        # 降级：第一个
        return providers[0]

    async def _init_embedding_provider(self) -> None:
        if self.vector_store.ready:
            return

        emb_providers = self.context.get_all_embedding_providers()
        if emb_providers and len(emb_providers) > 0:
            self._embedding_provider = self._pick_embedding_provider(emb_providers)
            pid = getattr(self._embedding_provider, 'id', 'unknown')
            dim = self._embedding_provider.get_dim() if hasattr(self._embedding_provider, 'get_dim') else '?'
            logger.info(f"[GE] EmbeddingProvider 就绪 (即时): {pid} (dim={dim})")
            await self._setup_embed_fn_and_collection()
            return

        if (not hasattr(self, '_embedding_retry_task') or
                self._embedding_retry_task is None or self._embedding_retry_task.done()):
            logger.info("[GE] EmbeddingProvider 暂未就绪，启动后台重试...")
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
                logger.info(f"[GE] EmbeddingProvider 就绪 (attempt {attempt}): {pid} (dim={dim})")
                await self._setup_embed_fn_and_collection()
                return

            if attempt % log_interval == 0:
                next_delay = min(delay * 1.5, max_delay)
                logger.info(
                    f"[GE] 等待 EmbeddingProvider... (attempt {attempt}/{MAX_RETRIES}, "
                    f"next delay {next_delay:.1f}s)"
                )

        logger.error(f"[GE] {MAX_RETRIES} 次重试后仍未找到 EmbeddingProvider")

    async def _setup_embed_fn_and_collection(self):
        ep = self._embedding_provider

        async def embed_fn(texts):
            return await ep.get_embeddings(texts)

        self.vector_store.set_embed_fn(embed_fn)
        self.vector_store.init_collection()
        dim = ep.get_dim() if hasattr(ep, 'get_dim') else '?'
        logger.info(f"[GE] ChromaDB 集合就绪 (维度={dim})")

        await self.vector_store.load_vectors_from_store(self.memory_store)
        logger.info(f"[GE] 向量回填完成 (现有 {self.vector_store.count()} 条向量)")

        # ── 注入到 MemoryManager（供 EvolutionEngine 使用） ──
        dim = ep.get_dim() if hasattr(ep, 'get_dim') else 0
        if dim > 0:
            async def _mem_mgr_embed(text):
                result = await ep.get_embeddings([text])
                return result[0] if result else None
            await self._memory_mgr.set_embed_func(_mem_mgr_embed, dim)
            await self._memory_mgr.load_vectors()
            logger.info("[GE] MemoryManager 向量化钩子已注入 ✅")

    # ── 分类器初始化 (v1.0.5: 修复闭包捕获 + ensure_future 追踪) ──

    async def _init_classifier(self):
        if self._classifier_llm is not None:
            return

        if self._try_setup_classifier():
            return

        if (not hasattr(self, '_classifier_retry_task') or
                self._classifier_retry_task is None or self._classifier_retry_task.done()):
            logger.info("[GE] 分类器 LLM 暂未就绪，启动后台重试...")
            self._classifier_retry_task = asyncio.create_task(self._retry_init_classifier())

    def _try_setup_classifier(self) -> bool:
        try:
            all_providers = self.context.get_all_providers()
            if not all_providers:
                logger.debug("[GE] get_all_providers 返回空列表")
                return False

            # v1.0.5: 用索引替代 for 循环中的闭包捕获，消除 _p=p 模式歧义
            for idx, p in enumerate(all_providers):
                if not hasattr(p, "text_chat"):
                    continue

                pid = getattr(p, 'id', str(p))

                # 闭包安全：用默认参数捕获当前 provider
                async def llm_call(prompt, _provider=p):
                    req = ProviderRequest(
                        prompt=prompt,
                        image_urls=[],
                        urls=[],
                        func_tool=None,
                        session=None,
                        context_compress=False,
                    )
                    resp = await _provider.text_chat(req)
                    return resp.completion_text if resp else ""

                self._classifier_llm = llm_call
                logger.info(f"[GE] 分类器 LLM 就绪: {pid}")

                # ── 注入分类器到 MemoryManager ──
                async def _classify_mem(q, c):
                    classification = await classify_memory(q, c, llm_call)
                    return classification.get("category", "general")

                self._classify_task = asyncio.ensure_future(
                    self._memory_mgr.set_classify_func(_classify_mem)
                )
                logger.info("[GE] MemoryManager 分类器钩子已注入 ✅")
                return True

            logger.warning("[GE] get_all_providers 返回了列表但无 text_chat 方法")
        except Exception as e:
            logger.warning(f"[GE] 分类器探测异常: {e}")
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
                logger.info(
                    f"[GE] 等待分类器 LLM... (attempt {attempt}/{MAX_RETRIES}, "
                    f"next delay {next_delay:.1f}s)"
                )

        logger.error(f"[GE] {MAX_RETRIES} 次重试后仍未找到分类器 LLM")

    # ── 生命周期 ──

    async def initialize(self) -> None:
        try:
            await self._init_embedding_provider()
        except Exception as e:
            logger.error(f"[GE] 向量化初始化失败: {e}")

        try:
            await self._init_classifier()
        except Exception as e:
            logger.error(f"[GE] 分类器初始化失败: {e}")

        try:
            await self._scan_and_index()
        except Exception as e:
            logger.error(f"[GE] 初始 scan_and_index 失败: {e}")

        asyncio.create_task(self._delayed_scan_and_index())

        self._evo_task = asyncio.create_task(self._evolution_loop())
        logger.info("[Glorious Evolution] v1.0.7 启动完成 ✅ (含 ToolCallHook 脱敏)")

    async def _delayed_scan_and_index(self):
        await asyncio.sleep(30)
        try:
            await self._scan_and_index()
        except Exception as e:
            logger.error(f"[GE] delayed_scan_and_index 异常: {e}", exc_info=True)

    async def terminate(self) -> None:
        if self._evo_task:
            self._evo_task.cancel()
            try:
                await self._evo_task
            except asyncio.CancelledError:
                pass
            self._evo_task = None
        # v1.0.5: 清理分类器注入任务
        if self._classify_task and not self._classify_task.done():
            self._classify_task.cancel()
            try:
                await asyncio.shield(self._classify_task)
            except asyncio.CancelledError:
                pass
            self._classify_task = None

    async def _evolution_loop(self) -> None:
        INTERVAL_SECONDS = 360 * 60

        logger.info(f"[GE] 进化循环就绪，首轮将于 {INTERVAL_SECONDS//60} 分钟后执行")
        await asyncio.sleep(INTERVAL_SECONDS)

        while True:
            try:
                await self._scan_and_index()
                await self._run_evolution()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[GE] 进化循环异常: {e}")
            await asyncio.sleep(INTERVAL_SECONDS)

    async def _scan_and_index(self):
        """v1.0.6: 从 SQLite 读取记忆（非 JSON MemoryStore），消除双存储割裂"""
        if not self.vector_store.ready:
            await self._init_embedding_provider()
        if not self.vector_store.ready:
            logger.warning("[GE] scan_and_index 跳过: VectorStore 仍未就绪")
            return

        mem_entries = await self._storage.get_all_entries(limit=10000)
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

        logger.info(f"[GE] scan_and_index (SQLite): {indexed}/{len(mem_entries)} 条新索引 (总计 {self.vector_store.count()} 条)")

    async def _run_evolution(self):
        start_ts = time.time()
        logger.info("[GE] 🧬 进化周期开始 (EvolutionEngine 完整版)...")

        try:
            result = await asyncio.wait_for(
                self._evo_engine.run_evolution_cycle(),
                timeout=self._evo_engine.CYCLE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[GE] ⚠️ EvolutionEngine 超时 (300s)，跳过本轮")
            return
        except Exception as e:
            logger.error(f"[GE] EvolutionEngine 异常: {e}", exc_info=True)
            return

        duration = time.time() - start_ts

        self.evo_stats.increment("total_evolutions")
        self.evo_stats.increment("total_insights", result.get("insights", 0))
        self.evo_stats.increment("total_consolidations", result.get("consolidated", 0))
        self.evo_stats.increment("total_evictions", result.get("evicted", 0))
        self.evo_stats.set("last_evolution_at", datetime.now(CST).isoformat())
        self.evo_stats.set("last_evolution_duration_sec", round(duration, 2))

        logger.info(
            f"[GE] 🧬 进化完成: "
            f"consolidated={result.get('consolidated', 0)} "
            f"insights={result.get('insights', 0)} "
            f"evicted={result.get('evicted', 0)} "
            f"duration={duration:.1f}s"
        )

    # ── ToolCallHook: 拦截所有工具返回值，脱敏敏感数据 (v1.0.7) ──

    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入到 AstrBot 的 LLM 请求钩子，对上下文中的工具返回结果做脱敏。

        AstrBot 会在每次 LLM 调用前自动调用此方法，包括：
        - 用户消息后的首轮 LLM 调用
        - 工具执行完成后的后续 LLM 调用（此时上下文中含工具返回值）
        """
        if not ENABLE_SANITIZATION:
            return

        try:
            # 脱敏 system_prompt
            sp = getattr(req, "system_prompt", None)
            if isinstance(sp, str) and len(sp) > 10:
                req.system_prompt = sanitize_content(sp)

            # 脱敏 prompt（可能是 str 或 list[dict]）
            prompt = getattr(req, "prompt", None)
            if isinstance(prompt, str) and len(prompt) > 10:
                # 检测最近一次工具调用名，做针对性脱敏
                tool_name = self._detect_last_tool(prompt)
                if tool_name and tool_name in STRICT_TOOL_NAMES:
                    req.prompt = sanitize_tool_output(tool_name, prompt)
                else:
                    req.prompt = sanitize_content(prompt)

            elif isinstance(prompt, list):
                # messages 格式：逐条脱敏
                for msg in prompt:
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, str) and len(content) > 10:
                            msg["content"] = sanitize_content(content)

        except Exception as e:
            logger.debug(f"[GE] ToolCallHook 脱敏异常 (已静默): {e}")

    @staticmethod
    def _detect_last_tool(prompt: str) -> Optional[str]:
        """从 prompt 字符串中检测最近一次工具调用名。

        匹配模式：
        - "Tool Result (dev_read_file):" 
        - "🔧 Tool: dev_read_file"
        - "tool_call_id": "...", "name": "dev_read_file"
        """
        patterns = [
            r'Tool Result \(([^)]+)\)',
            r'🔧 Tool:\s*(\S+)',
            r'"name":\s*"([^"]+)"',
        ]
        for pat in patterns:
            matches = list(re.finditer(pat, prompt))
            if matches:
                return matches[-1].group(1)  # 取最后一次匹配
        return None

    @filter.command("ges")
    async def cmd_stats(self, event: AstrMessageEvent):
        """v1.0.5: 统一走 MemoryManager 统计，消除 JSON/SQLite 数据割裂"""
        mgr_stats = await self._memory_mgr.get_stats()
        mems = mgr_stats["total_memories"]
        total_wins = mgr_stats.get("total_memories", 0)
        win_rate = round(mgr_stats.get("avg_win_rate", 0) * 100) if mems else 0
        vec_ready = "✅" if mgr_stats.get("embedding_ready") else "⏳"
        cls_ready = "✅" if self._classifier_llm else "⏳"

        msg = (
            f"📊 光荣进化 v{VERSION}\n"
            f"📚 记忆: {mems} | 📈 胜率: {win_rate}% | 🧠 向量化: {vec_ready} | 🏷️ 分类器: {cls_ready}\n"
            f"🧬 Full MIA: Memory + Reasoning + Evolution + Classification ✅"
        )
        yield event.plain_result(msg)


# ═══════════════════════════════════════════════════════════════
# 工具函数 (v1.0.5: 统一走 MemoryManager，消除 JSON/SQLite 双存储割裂)
# ═══════════════════════════════════════════════════════════════

async def store_memory(question: str, content: str, memory_type: str = "declarative", category: str = "general") -> str:
    """统一走 MemoryManager (SQLite)，消除双存储割裂 (v1.0.5)"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    eid = await plugin._memory_mgr.add_memory(
        question=question, content=content,
        memory_type=memory_type, category=category,
    )
    logger.info(f"[GE] 记忆已存储: {eid}")
    return f"✅ 记忆已存储: {eid}"


async def search_memory(query: str, top_k: int = 5) -> str:
    """统一走 MemoryManager 检索 (v1.0.5)"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    entries = await plugin._memory_mgr.retrieve_relevant_memories(query=query, top_k=top_k)
    if not entries:
        return "🔍 未找到相关记忆"
    out = "🧠 相关记忆:\n"
    for i, e in enumerate(entries[:top_k], 1):
        out += f"{i}. [{e.id}] ({e.category}) win={e.win_rate:.0%}\n"
        out += f"   Q: {e.question[:80]}\n"
        out += f"   A: {e.content[:120]}\n"
    return out


async def update_win_rate(entry_id: str, success: bool) -> str:
    """统一走 MemoryManager 更新胜率 (v1.0.5)"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    ok = await plugin._memory_mgr.update_win_rate(entry_id, success)
    return f"📈 {entry_id} win_rate 已更新" if ok else f"❌ 记忆 {entry_id} 不存在"


async def evict_memories() -> str:
    """统一走 MemoryManager 淘汰 (v1.0.5)"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    n = await plugin._memory_mgr.evict_low_quality()
    plugin.evo_stats.increment("total_evictions", n)
    return f"🧹 已淘汰 {n} 条低质量记忆" if n else "🧹 无需淘汰"


async def get_evolution_stats() -> str:
    """v1.0.5: 从 MemoryManager 获取统计，与进化引擎一致"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    stats = plugin.evo_stats.get_summary()
    mgr_stats = await plugin._memory_mgr.get_stats()
    return (
        f"📊 进化统计:\n"
        f"📚 记忆: {mgr_stats['total_memories']} | 🧬 向量: {mgr_stats['vector_index_size']}"
        f" {'✅' if mgr_stats.get('embedding_ready') else '⚠️'}\n"
        f"🔄 总进化: {stats['total_evolutions']} | 💡 洞察: {stats['total_insights']}"
        f" | 🗑️ 淘汰: {stats['total_evictions']}\n"
        f"⏱️ 上次进化: {stats.get('last_evolution_at', 'N/A')}"
        f" ({stats.get('last_evolution_duration_sec', 'N/A')}s)"
    )


async def trigger_evolution() -> str:
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    try:
        await asyncio.wait_for(plugin._run_evolution(), timeout=120)
        return "🧬 进化周期完成 ✅"
    except asyncio.TimeoutError:
        return "⚠️ 进化周期超时 (2min)"


async def build_plan(question: str, extra_context: str = "") -> str:
    """v1.0.5: 统一走 MemoryManager 检索"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    pos, neg = await plugin._memory_mgr.retrieve_balanced_memories(
        query=question, pos_top_k=3, neg_top_k=2,
    )
    ctx_parts = []
    for e in pos + neg:
        ctx_parts.append(f"[{e.id}] win={e.win_rate:.0%}: {e.content[:150]}")
    ctx = "\n".join(ctx_parts) if ctx_parts else "无相关记忆"
    return (
        f"📋 计划 (正={len(pos)} 负={len(neg)}):\n"
        f"目标: {question}\n"
        f"{'额外上下文: ' + extra_context if extra_context else ''}\n"
        f"────────────────\n📚 相关经验:\n{ctx}\n────────────────\n"
        f"💡 1.审查成功/失败模式 2.优先高胜率策略 3.执行后记录win_rate"
    )


async def judge_replan(execution_trace: str) -> str:
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    failure_keywords = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
    has_failure = any(kw in execution_trace.lower() for kw in failure_keywords)
    return "🔄 建议重新规划" if has_failure else "✅ 无需重新规划"


async def build_replan(question: str, execution_trace: str) -> str:
    """v1.0.5: 统一走 MemoryManager 检索"""
    plugin = _get_plugin()
    if not plugin or not plugin._memory_mgr:
        return "❌ 插件未就绪"
    _, neg = await plugin._memory_mgr.retrieve_balanced_memories(
        query=question, pos_top_k=2, neg_top_k=3,
    )
    avoid_lines = [f"- ❌ {e.content[:150]}" for e in neg[:3]]
    avoid = "\n".join(avoid_lines) if avoid_lines else "无已知失败模式"
    return (
        f"🔄 补充计划:\n原始目标: {question}\n失败轨迹: {execution_trace[:200]}\n"
        f"⚠️ 应避免: {avoid}\n💡 1.换用未标记失败方案 2.尝试更简替代 3.记录win_rate"
    )


def _get_plugin() -> Optional[GloriousEvolutionPlugin]:
    """v1.0.5: 带缓存，避免每次全量遍历 GlobalStarMap"""
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    try:
        from astrbot.api.star import GlobalStarMap
        star_map = GlobalStarMap()
        for v in star_map.star_map.values():
            if isinstance(v, GloriousEvolutionPlugin):
                _plugin_cache = v
                return v
    except Exception:
        pass
    return None
