"""Trivial FastMCP stdio server for the real-protocol integration test."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport
