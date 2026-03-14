"""MCP Server: 提供 reply_user 工具，让 kiro-cli 主动发送最终回复"""
import os
import httpx
from mcp.server.fastmcp import FastMCP

BRIDGE_URL = os.getenv("KIRO_BRIDGE_URL", "http://localhost:8900")

mcp = FastMCP("kiro-bridge")


@mcp.tool()
async def reply_user(request_id: str, message: str) -> str:
    """将最终回复发送给用户。当你得出最终结论或需要向用户提问时，必须调用此工具发送回复。

    Args:
        request_id: 请求ID，从用户消息中获取
        message: 要发送给用户的消息内容
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BRIDGE_URL}/reply", json={
            "request_id": request_id,
            "message": message,
        }, timeout=10)
        return "消息已发送" if r.status_code == 200 else f"发送失败: {r.status_code}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
