"""Print every MCP tool this server exposes. No CapCut project needed."""

import asyncio

from capcut_mcp.server import mcp


def main() -> None:
    tools = asyncio.run(mcp.list_tools())
    print(f"{len(tools)} tools registered\n")
    for t in tools:
        first_line = (t.description or "").strip().splitlines()[0]
        required = (t.input_schema or {}).get("required", [])
        print(f"  {t.name:<26} {first_line[:56]}")
        print(f"  {'':<26} required: {', '.join(required) or '-'}")


if __name__ == "__main__":
    main()
