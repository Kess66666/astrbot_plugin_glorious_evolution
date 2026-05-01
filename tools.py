from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .models import Action, Phase


# ── 插件引用（由 main.py 注入） ──
_plugin_cache: Optional["GloriousEvolutionPlugin"] = None


def inject_plugin(plugin) -> None:
    """由 main.py 在 plugin 启动时调用，注入插件实例。"""
    global _plugin_cache
    _plugin_cache = plugin


def _get_plugin() -> Optional["GloriousEvolutionPlugin"]:
    global _plugin_cache
    if _plugin_cache is not None:
        return _plugin_cache
    raise RuntimeError("GloriousEvolutionPlugin not initialized")