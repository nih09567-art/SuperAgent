"""Entrypoint for the office MCP prototype server."""

from src.tools.office_mcp.server import mcp


if __name__ == "__main__":
    mcp.run(transport="sse")
