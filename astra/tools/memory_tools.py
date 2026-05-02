"""
MCP tools for Astra's memory system.

These tools are exposed to the Agent SDK via create_sdk_mcp_server(),
allowing Astra (the LLM) to store, recall, and manage its own memories.

Tool list:
- store_memory: Save a new memory
- recall_memories: Search memories by semantic similarity
- forget_memory: Delete a specific memory
- list_memories: List memories with filtering
- memory_stats: Get memory system statistics
"""

from claude_agent_sdk import tool, create_sdk_mcp_server

from astra.db.engine import async_session
from astra.memory.consolidation import get_memory_stats
from astra.memory.models import MemoryType
from astra.memory.retrieval import search_memories
from astra.memory.store import (
    delete_memory,
    list_memories,
    store_memory,
)


@tool(
    "store_memory",
    "Store a new memory in Astra's long-term memory. Use this to remember facts, "
    "events, procedures, preferences, or any information that should persist across "
    "conversations. Choose the right memory_type: 'semantic' for facts/preferences, "
    "'episodic' for events/interactions, 'procedural' for how-to knowledge, "
    "'working' for current session context.",
    {
        "content": str,
        "memory_type": str,
        "source": str,
        "tags": str,
        "importance": float,
    },
)
async def store_memory_tool(args: dict) -> dict:
    content = args["content"]
    memory_type_str = args.get("memory_type", "semantic")
    source = args.get("source", "agent")
    tags = args.get("tags", None)
    importance = args.get("importance", 0.5)

    try:
        memory_type = MemoryType(memory_type_str)
    except ValueError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Invalid memory_type '{memory_type_str}'. "
                    f"Must be one of: episodic, semantic, procedural, working",
                }
            ],
            "is_error": True,
        }

    async with async_session() as session:
        memory = await store_memory(
            session=session,
            content=content,
            memory_type=memory_type,
            source=source,
            tags=tags if tags else None,
            importance=importance,
        )

        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Memory stored (id={memory.id}, type={memory.memory_type.value}): {content[:100]}",
                }
            ]
        }


@tool(
    "recall_memories",
    "Search Astra's long-term memory by semantic similarity. Use this to recall "
    "relevant information before answering questions, making decisions, or taking "
    "actions. The search uses meaning-based matching, not just keywords.",
    {
        "query": str,
        "memory_type": str,
        "top_k": int,
    },
)
async def recall_memories_tool(args: dict) -> dict:
    query = args["query"]
    memory_type_str = args.get("memory_type", None)
    top_k = args.get("top_k", 5)

    memory_type = None
    if memory_type_str:
        try:
            memory_type = MemoryType(memory_type_str)
        except ValueError:
            pass

    async with async_session() as session:
        results = await search_memories(
            session=session,
            query=query,
            memory_type=memory_type,
            top_k=top_k,
        )

        if not results:
            return {
                "content": [
                    {"type": "text", "text": "No relevant memories found."}
                ]
            }

        lines = [f"Found {len(results)} relevant memories:\n"]
        for r in results:
            lines.append(
                f"[{r['memory_type']}] (relevance={r['similarity']}, "
                f"importance={r['importance']}) {r['content']}"
            )
            if r.get("tags"):
                lines.append(f"  tags: {r['tags']}")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "forget_memory",
    "Delete a specific memory by its ID. Use this when information is outdated, "
    "incorrect, or when the user explicitly asks to forget something.",
    {"memory_id": int},
)
async def forget_memory_tool(args: dict) -> dict:
    memory_id = args["memory_id"]

    async with async_session() as session:
        deleted = await delete_memory(session, memory_id)
        if deleted:
            return {
                "content": [
                    {"type": "text", "text": f"Memory {memory_id} deleted."}
                ]
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Memory {memory_id} not found.",
                }
            ],
            "is_error": True,
        }


@tool(
    "list_memories",
    "List memories with optional filtering by type, source, or tag. "
    "Use this to browse memories rather than searching by meaning.",
    {
        "memory_type": str,
        "source": str,
        "tag": str,
        "limit": int,
    },
)
async def list_memories_tool(args: dict) -> dict:
    memory_type_str = args.get("memory_type", None)
    source = args.get("source", None)
    tag = args.get("tag", None)
    limit = args.get("limit", 20)

    memory_type = None
    if memory_type_str:
        try:
            memory_type = MemoryType(memory_type_str)
        except ValueError:
            pass

    async with async_session() as session:
        memories = await list_memories(
            session=session,
            memory_type=memory_type,
            source=source,
            tag=tag,
            limit=limit,
        )

        if not memories:
            return {
                "content": [{"type": "text", "text": "No memories found."}]
            }

        lines = [f"Found {len(memories)} memories:\n"]
        for m in memories:
            lines.append(
                f"[id={m.id}] [{m.memory_type.value}] ({m.source}) "
                f"importance={m.importance:.2f} — {m.content[:100]}"
            )

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "memory_stats",
    "Get statistics about Astra's memory system. Shows total count, "
    "breakdown by type and source, average importance, and access patterns.",
    {},
)
async def memory_stats_tool(args: dict) -> dict:
    async with async_session() as session:
        stats = await get_memory_stats(session)

        lines = [
            f"Total memories: {stats['total_memories']}",
            f"By type: {stats['by_type']}",
            f"By source: {stats['by_source']}",
            f"Average importance: {stats['avg_importance']}",
            f"Average access count: {stats['avg_access_count']}",
        ]

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_memory_mcp_server():
    """Create the MCP server for memory tools."""
    return create_sdk_mcp_server(
        name="astra-memory",
        version="0.1.0",
        tools=[
            store_memory_tool,
            recall_memories_tool,
            forget_memory_tool,
            list_memories_tool,
            memory_stats_tool,
        ],
    )
