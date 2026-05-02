#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光荣进化系统 (Glorious Evolution) — MIA 风格的智能记忆与自改进框架
v1.0.13 - 记忆感知 judge：LLM 逐条评价记忆贡献度，替代粗糙二值映射
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

VERSION = "1.0.13"
DEFAULT_EVO_INTERVAL_HOURS = 6

logger = logging.getLogger("GloriousEvolution")

# ... rest of main.py unchanged from v1.0.12 ...

_plugin_cache: Optional["GloriousEvolutionPlugin"] = None