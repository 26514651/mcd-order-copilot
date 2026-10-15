"""点单参谋 · McDonald's order nutrition copilot.

A skill built on the McDonald's China MCP server. Every nutrition figure comes
from the official interface at request time -- nothing is hard-coded, and gaps
are disclosed rather than estimated.

Quick start (no token required):

    python -m mcd_order_copilot --demo
"""

from .demo_data import DemoData
from .mcp_client import MCPClient, MCPError, MCPTransportError, ToolResult
from .normalize import Match, NutritionIndex, nutrition_of
from .nutrition import Item, annotate_tags, build_menu, meets, rank, summarise
from .profiles import PRESETS, Target, build_target

__version__ = "0.1.0"

__all__ = [
    "DemoData",
    "MCPClient",
    "MCPError",
    "MCPTransportError",
    "ToolResult",
    "Match",
    "NutritionIndex",
    "nutrition_of",
    "Item",
    "annotate_tags",
    "build_menu",
    "meets",
    "rank",
    "summarise",
    "PRESETS",
    "Target",
    "build_target",
    "__version__",
]
