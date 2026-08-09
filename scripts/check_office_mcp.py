"""Small real-protocol smoke check for the office MCP demo server."""

import asyncio
import json

from langchain_mcp_adapters.client import MultiServerMCPClient


async def main() -> None:
    client = MultiServerMCPClient(
        {
            "office-mcp": {
                "url": "http://127.0.0.1:8013/sse",
                "transport": "sse",
            }
        }
    )
    tools = await client.get_tools(server_name="office-mcp")
    print(f"tools/list={len(tools)}")
    search = next(tool for tool in tools if tool.name == "search_courses")
    result = await search.ainvoke({"query": "大模型"})
    print("tools/call=" + json.dumps(json.loads(result), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
