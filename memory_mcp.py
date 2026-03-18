"""记忆系统 MCP Server — 提供 search_knowledge / save_entity / save_relation / get_history"""
import json, os, sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from memory import MemoryDB

SESSIONS_DIR = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all"), "wecom-sessions")

# chatid 通过环境变量传入（每个 kiro 进程启动时设置）
CHATID = os.getenv("MEMORY_CHATID", "default")
DB_PATH = os.path.join(SESSIONS_DIR, CHATID, "memory.db")

db = MemoryDB(DB_PATH)
server = Server("memory-server")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_knowledge",
            description="从知识图谱中搜索实体和关系。用于回忆之前对话中提到的人、服务、项目、决策等信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或自然语言描述"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="save_entity",
            description="保存或更新一个实体到知识图谱。实体可以是人、服务、项目、工具、配置等。如果实体已存在会自动归档旧版本。",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "实体类型: person/service/project/tool/config/decision"},
                    "name": {"type": "string", "description": "实体名称"},
                    "description": {"type": "string", "description": "实体的自然语言描述"},
                    "properties": {"type": "object", "description": "结构化属性键值对"},
                    "reason": {"type": "string", "description": "更新原因（更新已有实体时填写）"}
                },
                "required": ["type", "name", "description"]
            }
        ),
        Tool(
            name="save_relation",
            description="保存实体之间的关系。如：张三-负责-订单服务，部署脚本-位于-/path/to/script",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_name": {"type": "string", "description": "关系起点实体名称"},
                    "relation": {"type": "string", "description": "关系类型: 负责/位于/依赖/偏好/创建/使用/..."},
                    "to_name": {"type": "string", "description": "关系终点实体名称"},
                    "from_type": {"type": "string", "description": "起点实体类型（可选）"},
                    "to_type": {"type": "string", "description": "终点实体类型（可选）"}
                },
                "required": ["from_name", "relation", "to_name"]
            }
        ),
        Tool(
            name="get_history",
            description="查看某个实体的版本变更历史",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string", "description": "实体名称"}
                },
                "required": ["entity_name"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "search_knowledge":
        results = db.search(arguments["query"], top_k=arguments.get("top_k", 5))
        return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

    elif name == "save_entity":
        eid = db.save_entity(
            type=arguments["type"],
            name=arguments["name"],
            description=arguments["description"],
            properties=arguments.get("properties"),
            source_chatid=CHATID,
            reason=arguments.get("reason", "")
        )
        return [TextContent(type="text", text=f"已保存实体: {eid}")]

    elif name == "save_relation":
        db.save_relation(
            from_name=arguments["from_name"],
            relation=arguments["relation"],
            to_name=arguments["to_name"],
            from_type=arguments.get("from_type", ""),
            to_type=arguments.get("to_type", ""),
            source_chatid=CHATID
        )
        return [TextContent(type="text", text=f"已保存关系: {arguments['from_name']} -{arguments['relation']}-> {arguments['to_name']}")]

    elif name == "get_history":
        history = db.get_history(arguments["entity_name"])
        return [TextContent(type="text", text=json.dumps(history, ensure_ascii=False, indent=2))]

    return [TextContent(type="text", text=f"未知工具: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
