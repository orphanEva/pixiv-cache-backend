"""Run with python -m app.mcp, or the pixiv-cache-mcp console command."""
import argparse
import sys

from app.mcp import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="Pixiv cache MCP server (stdio)")
    parser.add_argument("--version", action="version", version=f"pixiv-cache-mcp {__version__}")
    parser.add_argument("--read-only", action="store_true", help="Expose only query tools")
    parser.add_argument("--library-only", action="store_true", help="Force local archived-library tools only (default)")
    options = parser.parse_args()

    try:
        from pydantic import ValidationError
        from app.mcp.config import MCPSettings
        from app.mcp.server import create_server
    except ImportError:
        print('MCP dependencies are missing. Install with: python -m pip install -e ".[mcp]"', file=sys.stderr)
        raise SystemExit(2) from None

    try:
        overrides = {}
        if options.read_only:
            overrides["read_only"] = True
        if options.library_only:
            overrides["library_only"] = True
        settings = MCPSettings(**overrides)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors(include_input=False) if error["loc"]})
        print("Invalid MCP configuration: " + ", ".join(fields)
              + ". Check PIXIV_MCP_* settings in docs/MCP.md; values omitted.", file=sys.stderr)
        raise SystemExit(2) from None

    # stdout is exclusively the MCP JSON-RPC stream. SDK logging uses stderr.
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()

