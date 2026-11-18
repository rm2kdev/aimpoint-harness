"""Tiny MCP server used by the test-suite (stdio by default, `--http PORT` for streamable HTTP)."""
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def vault_word() -> str:
    """Return today's vault word. Only this tool knows it."""
    return "pomegranate-42"


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--http":
        mcp.settings.port = int(sys.argv[2])
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
