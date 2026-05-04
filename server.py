#!/usr/bin/env python3
"""TTRPG MCP Server — AD&D 2nd Edition campaign manager.

Register in Claude Code settings:
  {
    "mcpServers": {
      "ttrpg": {
        "command": "python3",
        "args": ["/home/raf/claude/ttrpg2/server.py"],
        "cwd": "/home/raf/claude/ttrpg2"
      }
    }
  }
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from tools import dice, party, combat, combat_map, world, lore, log, lookup, images, campaign_mgmt

mcp = FastMCP("TTRPG Campaign Manager")

dice.register(mcp)
party.register(mcp)
combat.register(mcp)
combat_map.register(mcp)
world.register(mcp)
lore.register(mcp)
log.register(mcp)
lookup.register(mcp)
images.register(mcp)
campaign_mgmt.register(mcp)

if __name__ == "__main__":
    mcp.run()
