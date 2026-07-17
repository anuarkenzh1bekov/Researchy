"""`research-mcp` entry point: run the FastMCP server over stdio."""

from research_assistant.mcp.server import mcp


def main() -> None:
    mcp.run()  # stdio transport — what MCP hosts spawn


if __name__ == "__main__":
    main()
