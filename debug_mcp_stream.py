import asyncio
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "http://1.13.18.167:18060/mcp"

async def main():
    print(f"Connecting to MCP Server via StreamableHTTP: {URL}")
    try:
        async with streamable_http_client(URL) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print("\n--- Available Tools ---")
                for tool in tools.tools:
                    print(json.dumps({
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.inputSchema,
                    }, ensure_ascii=False, indent=2))
                print("Session ID:", get_session_id())
    except Exception as e:
        print(f"Connection Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
