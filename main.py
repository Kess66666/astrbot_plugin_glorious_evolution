#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.1 - 修复 initialize() 生命周期钩子
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from astrbot.api.star import Star, Context
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest

# 上海时区
CST = timezone(timedelta(hours=8))

# ── 本地存储路径 ──
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(PLUGIN_DIR, "memory_store.json")
EVO_STATS_FILE = os.path.join(PLUGIN_DIR, "evolution_stats.json")
CHROMA_PATH = os.path.join(PLUGIN_DIR, "chroma_db")

# ── 常量 ──
VERSION = "1.0.1"
DEFAULT_EVO_INTERVAL_HOURS = 6

logger = logging.getLogger("GloriousEvolution")


# ═══════════════════════════════════════════════════════════════
# 内存记忆引擎
# ═══════════════════════════════════════════════════════════════

class MemoryStore:
    """JSON 文件持久化的记忆存储"""

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

    def add(self, entry: dict) -> str:
        self._counter += 1
        entry_id = entry.get("id") or f"MEM-{datetime.now(CST).strftime('%Y%m%d')}-{self._counter:03d}"
        entry["id"] = entry_id
        entry["created_at"] = entry.get("created_at") or datetime.now(CST).isoformat()
        entry["last_accessed_at"] = entry["created_at"]
        entry["access_count"] = 0
        entry["win_rate"] = 0.0
        self._memories[entry_id] = entry
        self._save()
        return entry_id

    def get(self, entry_id: str) -> Optional[dict]:
        entry = self._memories.get(entry_id)
        if entry:
            entry["last_accessed_at"] = datetime.now(CST).isoformat()
            entry["access_count"] = entry.get("access_count", 0) + 1
            self._save()
        return entry

    def list_all(self) -> List[dict]:
        return list(self._memories.values())

    def update(self, entry_id: str, updates: dict):
        if entry_id in self._memories:
            self._memories[entry_id].update(updates)
            self._save()

    def delete(self, entry_id: str):
        if entry_id in self._memories:
            del self._memories[entry_id]
            self._save()

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
    """将记忆字典转为可向量化的文本"""
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
        # 提取 JSON
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

        # 运行时状态
        self._embedding_provider = None  # 延迟注入
        self._evo_task: Optional[asyncio.Task] = None
        self._classifier_llm = None  # LLM 调用函数

        logger.info(f"[Glorious Evolution] v{VERSION} __init__ 完成")

    # ── Embedding 供应商注入 (参考 livingmemory 用 context.get_all_embedding_providers) ──

    async def _init_embedding_provider(self) -> None:
        """轻量重试获取 EmbeddingProvider，最多 3 次，绝不阻塞主进程"""

        MAX_RETRIES = 3
        RETRY_DELAY = 1.0  # 固定 1 秒间隔，不用指数退避

        try:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    emb_providers = self.context.get_all_embedding_providers()
                    if emb_providers and len(emb_providers) > 0:
                        self._embedding_provider = emb_providers[0]
                        pid = getattr(self._embedding_provider, 'id', 'unknown')
                        logger.info(f"[GE] EmbeddingProvider 就绪 (attempt {attempt}): {pid}")
                    else:
                        raise RuntimeError("get_all_embedding_providers 返回空列表")
                except Exception as e:
                    logger.warning(f"[GE] EmbeddingProvider 未就绪 (attempt {attempt}/{MAX_RETRIES}): {e}")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)
                    continue

                # 封装 embed 函数
                async def embed_fn(texts):
                    req = ProviderRequest(
                        prompt="",
                        image_urls=[],
                        urls=[],
                        func_tool=None,
                        embedding_input=texts,
                        session=None,
                        context_compress=False,
                    )
                    resp = await self._embedding_provider.text_to_embedding(req)
                    if resp and resp.embeddings:
                        return resp.embeddings
                    return []

                self.vector_store.set_embed_fn(embed_fn)
                self.vector_store.init_collection()
                await self.vector_store.load_vectors_from_store(self.memory_store)
                logger.info(f"[GE] 向量化引擎就绪 (现有 {self.vector_store.count()} 条向量)")
                return

            logger.warning(f"[GE] {MAX_RETRIES}次重试后仍未找到 EmbeddingProvider，向量功能不可用")
        except Exception as e:
            logger.error(f"[GE] EmbeddingProvider 初始化异常 (已跳过，不阻塞主进程): {e}")

    # ── 分类器初始化 ──

    async def _init_classifier(self):
        """初始化分类器：获取 LLM 调用函数"""
        try:
            all_providers = self.context.get_all_providers()
            for p in all_providers:
                if hasattr(p, "text_chat"):
                    async def llm_call(prompt):
                        req = ProviderRequest(
                            prompt=prompt,
                            image_urls=[],
                            urls=[],
                            func_tool=None,
                            session=None,
                            context_compress=False,
                        )
                        resp = await p.text_chat(req)
                        return resp.completion_text if resp else ""
                    self._classifier_llm = llm_call
                    pid = getattr(p, 'id', str(p))
                    logger.info(f"[GE] 分类器 LLM 就绪: {pid}")
                    return
            logger.warning("[GE] 未找到可用 chat provider，分类器功能不可用")
        except Exception as e:
            logger.warning(f"[Glorious Evolution] 分类器初始化失败: {e}")

    # ── 生命周期 (AstrBot 标准钩子) ──

    async def initialize(self) -> None:
        """AstrBot 标准生命周期钩子：插件加载完成后调用。初始化失败不阻塞主流程"""
        try:
            await self._init_embedding_provider()
        except Exception as e:
            logger.error(f"[GE] 向量化初始化失败（已跳过）: {e}")

        try:
            await self._init_classifier()
        except Exception as e:
            logger.error(f"[GE] 分类器初始化失败（已跳过）: {e}")

        # 立即执行一次索引扫描（initialize 完成即触发）
        try:
            await self._scan_and_index()
        except Exception as e:
            logger.error(f"[GE] 初始 scan_and_index 失败（已跳过）: {e}")

        self._evo_task = asyncio.create_task(self._evolution_loop())
        logger.info("[Glorious Evolution] v1.0.1 启动完成 ✅")

    async def terminate(self) -> None:
        """AstrBot 标准生命周期钩子：插件卸载时调用"""
        if self._evo_task:
            self._evo_task.cancel()
            try:
                await self._evo_task
            except asyncio.CancelledError:
                pass
            self._evo_task = None

    async def _evolution_loop(self) -> None:
        """后台进化循环 — 固定 360 分钟触发 scan_and_index + 合并淘汰"""
        INTERVAL_SECONDS = 360 * 60  # 固定 6 小时

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
        """扫描 MemoryStore 所有条目，确保 ChromaDB 向量索引完整（幂等操作）"""
        if not self.vector_store.ready:
            logger.debug("[GE] scan_and_index 跳过: VectorStore 未就绪")
            return

        mems = self.memory_store.list_all()
        if not mems:
            logger.debug("[GE] scan_and_index: 记忆库为空，跳过")
            return

        # 获取 ChromaDB 中已有向量 ID 集合
        existing_ids: set = set()
        try:
            existing = self.vector_store._collection.get()
            if existing and existing.get("ids"):
                existing_ids = set(existing["ids"])
        except Exception:
            pass

        missing = [m for m in mems if m["id"] not in existing_ids]
        if not missing:
            logger.debug(f"[GE] scan_and_index: 全部 {len(mems)} 条已索引，无需回填")
            return

        indexed = 0
        for m in missing:
            text = _mem_to_text(m)
            await self.vector_store.add(m["id"], text)
            indexed += 1

        logger.info(
            f"[GE] scan_and_index 完成: {indexed}/{len(mems)} 条新索引 "
            f"(总计 {self.vector_store.count()} 条向量)"
        )

    async def _run_evolution(self):
        """执行一次进化周期：合并洞察 + 淘汰"""
        start_ts = time.time()
        logger.info("[GE] 🧬 进化周期开始...")

        # ── 洞察合并 ──
        insights = 0
        try:
            memories = self.memory_store.list_all()
            if len(memories) >= 3:
                by_category = {}
                for m in memories:
                    cat = m.get("category", "general")
                    by_category.setdefault(cat, []).append(m)
                for cat, mems in by_category.items():
                    if len(mems) >= 3 and cat != "consolidated_rule":
                        combined = "\n".join([
                            f"- [{m.get('memory_type', '?')}] {m.get('question', '')} → {m.get('content', '')[:200]}"
                            for m in mems[-5:]
                        ])
                        self.memory_store.add({
                            "question": f"{cat} 类经验总结",
                            "content": f"以下经验已合并:\n{combined}",
                            "memory_type": "declarative",
                            "category": "consolidated_rule",
                        })
                        insights += 1
        except Exception as e:
            logger.warning(f"[GE] 洞察合并失败: {e}")

        # ── 低胜率淘汰 ──
        evictions = 0
        try:
            for m in self.memory_store.list_all():
                if m.get("win_rate", 1.0) < 0.1 and m.get("access_count", 0) >= 3:
                    await self.vector_store.delete(m["id"])
                    self.memory_store.delete(m["id"])
                    evictions += 1
        except Exception as e:
            logger.warning(f"[GE] 淘汰失败: {e}")

        # ── 统计更新 ──
        duration = time.time() - start_ts
        self.evo_stats.increment("total_evolutions")
        self.evo_stats.increment("total_insights", insights)
        self.evo_stats.increment("total_evictions", evictions)
        self.evo_stats.set("last_evolution_at", datetime.now(CST).isoformat())
        self.evo_stats.set("last_evolution_duration_sec", round(duration, 2))

        logger.info(f"[GE] 🧬 进化完成: insights={insights} evictions={evictions} duration={duration:.1f}s")

    # ── 命令 ──

    async def _send(self, text: str):
        if hasattr(self, 'context') and self.context:
            await self.context.send_message(text)

    @filter.command("ges")
    async def cmd_stats(self, event: AstrMessageEvent):
        """📊 查看进化统计"""
        mems = self.memory_store.count()
        wins = sum(1 for m in self.memory_store.list_all() if m.get("win_rate", 0) >= 0.5)
        win_rate = round(wins / mems * 100) if mems else 0
        vec_ready = "✅" if self.vector_store.ready else "⏳"
        cls_ready = "✅" if self._classifier_llm else "⏳"

        msg = (
            f"📊 光荣进化 v{VERSION}\n"
            f"📚 记忆: {mems} | 📈 胜率: {win_rate}% | 🧠 向量化: {vec_ready} | 🏷️ 分类器: {cls_ready}\n"
            f"🧬 Full MIA: Memory + Reasoning + Evolution + Classification ✅"
        )
        yield event.plain_result(msg)


# ── 工具函数 ──

async def store_memory(question: str, content: str, memory_type: str = "declarative", category: str = "general") -> str:
    """存储一条智能记忆。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    entry = {
        "question": question,
        "content": content,
        "memory_type": memory_type,
        "category": category,
    }

    if plugin._classifier_llm:
        try:
            classification = await classify_memory(question, content, plugin._classifier_llm)
            entry["category"] = classification.get("category", category)
            entry["memory_type"] = classification.get("memory_type", memory_type)
            entry["tags"] = classification.get("tags", [])
        except Exception:
            pass

    eid = plugin.memory_store.add(entry)
    asyncio.create_task(plugin.vector_store.add(eid, _mem_to_text(entry)))
    logger.info(f"[GE] 记忆已存储: {eid} ({entry.get('category')}/{entry.get('memory_type')})")
    return f"✅ 记忆已存储: {eid}"


async def search_memory(query: str, top_k: int = 5) -> str:
    """搜索相关记忆，检索过往经验、规则或知识。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    results = []
    if plugin.vector_store.ready:
        vec_results = await plugin.vector_store.search(query, top_k)
        for vr in vec_results:
            mem = plugin.memory_store.get(vr["id"])
            if mem:
                results.append({
                    "id": mem["id"],
                    "question": mem.get("question", ""),
                    "content": mem.get("content", ""),
                    "category": mem.get("category", ""),
                    "win_rate": mem.get("win_rate", 0),
                    "distance": vr.get("distance"),
                })

    if not results:
        query_lower = query.lower()
        for m in plugin.memory_store.list_all():
            if query_lower in _mem_to_text(m).lower():
                results.append({
                    "id": m["id"],
                    "question": m.get("question", ""),
                    "content": m.get("content", ""),
                    "category": m.get("category", ""),
                    "win_rate": m.get("win_rate", 0),
                    "distance": None,
                })
                if len(results) >= top_k:
                    break

    if not results:
        return "🔍 未找到相关记忆"

    out = "🧠 相关记忆:\n"
    for i, r in enumerate(results[:top_k], 1):
        dist_str = f" [dist={r['distance']:.3f}]" if r["distance"] is not None else ""
        out += f"{i}. [{r['id']}] ({r['category']}) win={r['win_rate']:.0%}{dist_str}\n"
        out += f"   Q: {r['question'][:80]}\n"
        out += f"   A: {r['content'][:120]}\n"
    return out


async def update_win_rate(entry_id: str, success: bool) -> str:
    """更新某条记忆的胜率，标记其是否有效。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    mem = plugin.memory_store.get(entry_id)
    if not mem:
        return f"❌ 记忆 {entry_id} 不存在"

    old_rate = mem.get("win_rate", 0)
    new_rate = round((old_rate * 0.7 + (1.0 if success else 0.0) * 0.3), 3)
    plugin.memory_store.update(entry_id, {"win_rate": new_rate})

    logger.info(f"[GE] win_rate 更新: {entry_id} {old_rate:.2f} → {new_rate:.2f} ({'✅' if success else '❌'})")
    return f"📈 {entry_id} win_rate: {old_rate:.2f} → {new_rate:.2f}"


async def evict_memories() -> str:
    """淘汰低胜率、低使用的记忆，保持记忆库健康。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    evicted = []
    for m in plugin.memory_store.list_all():
        if m.get("win_rate", 1.0) < 0.1 and m.get("access_count", 0) >= 3:
            await plugin.vector_store.delete(m["id"])
            plugin.memory_store.delete(m["id"])
            evicted.append(m["id"])

    plugin.evo_stats.increment("total_evictions", len(evicted))
    if not evicted:
        return "🧹 无需淘汰"
    logger.info(f"[GE] 已淘汰 {len(evicted)} 条记忆: {evicted}")
    return f"🧹 已淘汰 {len(evicted)} 条: {', '.join(evicted)}"


async def get_evolution_stats() -> str:
    """获取光荣进化系统的统计概览。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    stats = plugin.evo_stats.get_summary()
    mems = plugin.memory_store.count()
    vecs = plugin.vector_store.count()
    vec_ok = "✅" if plugin.vector_store.ready else "⚠️"
    return (
        f"📊 进化统计:\n"
        f"📚 记忆: {mems} | 🧬 向量: {vecs} {vec_ok}\n"
        f"🔄 总进化: {stats['total_evolutions']} | 💡 洞察: {stats['total_insights']} | 🗑️ 淘汰: {stats['total_evictions']}\n"
        f"⏱️ 上次进化: {stats.get('last_evolution_at', 'N/A')} ({stats.get('last_evolution_duration_sec', 'N/A')}s)"
    )


async def trigger_evolution() -> str:
    """手动触发一次完整的进化周期。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    try:
        await asyncio.wait_for(plugin._run_evolution(), timeout=120)
        return "🧬 进化周期完成 ✅"
    except asyncio.TimeoutError:
        return "⚠️ 进化周期超时 (2min)"


async def build_plan(question: str, extra_context: str = "") -> str:
    """基于记忆库生成行动计划。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    relevant = []
    if plugin.vector_store.ready:
        vec_results = await plugin.vector_store.search(question, 5)
        for vr in vec_results:
            mem = plugin.memory_store.get(vr["id"])
            if mem and mem.get("win_rate", 0) > 0.3:
                relevant.append(mem)

    if not relevant:
        for m in plugin.memory_store.list_all():
            if question.lower() in _mem_to_text(m).lower() and m.get("win_rate", 0) > 0.3:
                relevant.append(m)
                if len(relevant) >= 5:
                    break

    context_parts = [f"[{r['id']}] win={r.get('win_rate',0):.0%}: {r.get('content', '')[:200]}" for r in relevant]
    context_text = "\n".join(context_parts) if context_parts else "无相关记忆"

    return (
        f"📋 计划 (基于 {len(relevant)} 条记忆):\n"
        f"目标: {question}\n"
        f"{'额外上下文: ' + extra_context if extra_context else ''}\n"
        f"────────────────\n"
        f"📚 相关经验:\n{context_text}\n"
        f"────────────────\n"
        f"💡 建议步骤:\n"
        f"1. 审查相关记忆中的成功/失败模式\n"
        f"2. 优先采用高胜率 (>50%) 策略\n"
        f"3. 执行后调用 update_win_rate 记录结果"
    )


async def judge_replan(execution_trace: str) -> str:
    """评估执行轨迹，判断是否需要重新规划。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    failure_keywords = ["error", "failed", "❌", "exception", "timeout", "refused", "denied"]
    has_failure = any(kw in execution_trace.lower() for kw in failure_keywords)
    return "🔄 建议重新规划（检测到失败标志）" if has_failure else "✅ 无需重新规划"


async def build_replan(question: str, execution_trace: str) -> str:
    """基于失败经验生成补充计划。"""
    plugin = _get_plugin()
    if not plugin:
        return "❌ 插件未就绪"

    failure_mems = [m for m in plugin.memory_store.list_all()
                    if m.get("win_rate", 0) < 0.5 and question.lower() in _mem_to_text(m).lower()]
    avoid_list = "\n".join([f"- ❌ {m.get('content', '')[:150]}" for m in failure_mems[:3]]) if failure_mems else "无已知失败模式"

    return (
        f"🔄 补充计划:\n"
        f"原始目标: {question}\n"
        f"失败轨迹: {execution_trace[:200]}\n"
        f"────────────────\n"
        f"⚠️ 应避免的策略:\n{avoid_list}\n"
        f"────────────────\n"
        f"💡 建议:\n"
        f"1. 换用未被标记为失败的方案\n"
        f"2. 尝试更简单的替代路径\n"
        f"3. 成功/失败后调用 update_win_rate"
    )


def _get_plugin() -> Optional[GloriousEvolutionPlugin]:
    """获取插件实例"""
    try:
        from astrbot.api.star import GlobalStarMap
        star_map = GlobalStarMap()
        for v in star_map.star_map.values():
            if isinstance(v, GloriousEvolutionPlugin):
                return v
    except Exception:
        pass
    return None