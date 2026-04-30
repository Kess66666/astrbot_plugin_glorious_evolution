#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.8 - 数据持久化硬化：全局数据目录 + 自动备份 + 卸载安全
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

# ── MIA 引擎（完整版进化循环） ──
from .storage import Storage
from .memory_manager import MemoryManager
from .reasoning_engine import ReasoningEngine
from .evolution_task import EvolutionEngine
from .tool_sanitizer import sanitize_content, sanitize_tool_output, ENABLE_SANITIZATION, STRICT_TOOL_NAMES

# 上海时区
CST = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════
# v1.0.8: 全局数据目录 — 与插件目录解耦，卸载不再清空数据库
# ═══════════════════════════════════════════════════════════════
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/AstrBot/data/glorious_evolution"
BACKUP_DIR = "/AstrBot/data/workspaces/telegram_FriendMessage_7223158438/glorious-evolution/backups"

# 旧路径（迁移用）
OLD_DB_PATH = os.path.join(PLUGIN_DIR, "evolution.db")
OLD_CHROMA_PATH = os.path.join(PLUGIN_DIR, "chroma_db")
OLD_STATS_PATH = os.path.join(PLUGIN_DIR, "evolution_stats.json")

# 新路径
DB_PATH = os.path.join(DATA_DIR, "evolution.db")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma_db")
EVO_STATS_FILE = os.path.join(DATA_DIR, "evolution_stats.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory_store.json")

# ── 常量 ──
VERSION = "1.0.8"
DEFAULT_EVO_INTERVAL_HOURS = 6

logger = logging.getLogger("GloriousEvolution")