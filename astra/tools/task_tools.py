"""
MCP tools for task management.

Three tools deliberately, not five: the minimum Astra needs to keep
a flat to-do list honest. Anything fancier (recurrence, projects,
reminders) is a later expansion.
"""

from datetime import datetime

from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

from astra.tasks.store import add_task, complete_task, list_tasks


@tool(
    "add_task",
    "Add a task Astra should remember. Use when the user asks you to "
    "track, remember, or follow up on something actionable. Keeps a "
    "persistent todo list that survives across conversations.",
    {
        "title": str,
        "note": str,
        "due_iso": str,
        "priority": int,
        "tags": str,
    },
)
async def add_task_tool(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    if not title:
        return {"content": [{"type": "text", "text": "add_task: title required"}]}
    note = args.get("note") or ""
    tags = args.get("tags") or ""
    priority = int(args.get("priority") or 1)
    due: datetime | None = None
    due_iso = args.get("due_iso")
    if due_iso:
        try:
            due = datetime.fromisoformat(str(due_iso))
        except ValueError:
            pass

    task = await add_task(
        title=title,
        note=note,
        due_at=due,
        priority=priority,
        tags=tags,
        source="chat",
    )
    due_line = f" · due {task['due_at'][:10]}" if task.get("due_at") else ""
    return {
        "content": [
            {
                "type": "text",
                "text": f"Task #{task['id']} added: {task['title']}{due_line}",
            }
        ]
    }


@tool(
    "list_tasks",
    "List the user's tasks. By default, returns only open tasks, most "
    "recent first. Pass include_done=true for completed ones too.",
    {"include_done": bool, "limit": int},
)
async def list_tasks_tool(args: dict) -> dict:
    include_done = bool(args.get("include_done", False))
    limit = int(args.get("limit") or 50)
    rows = await list_tasks(limit=limit, include_done=include_done)
    if not rows:
        msg = "No open tasks." if not include_done else "No tasks yet."
        return {"content": [{"type": "text", "text": msg}]}

    lines = [f"{len(rows)} task{'s' if len(rows) != 1 else ''}:"]
    for t in rows:
        pri = ["·", "·", "!", "!!"][t["priority"]]
        state = "✓" if t["status"] == "done" else ("✗" if t["status"] == "cancelled" else "○")
        due = f" due {t['due_at'][:10]}" if t.get("due_at") else ""
        lines.append(f"  {state} #{t['id']}  {pri}  {t['title']}{due}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "complete_task",
    "Mark a task as done. Use the task id returned by list_tasks.",
    {"task_id": int},
)
async def complete_task_tool(args: dict) -> dict:
    task_id = int(args.get("task_id") or 0)
    if not task_id:
        return {"content": [{"type": "text", "text": "complete_task: task_id required"}]}
    task = await complete_task(task_id)
    if not task:
        return {"content": [{"type": "text", "text": f"Task #{task_id} not found"}]}
    return {
        "content": [
            {"type": "text", "text": f"Task #{task['id']} completed: {task['title']}"}
        ]
    }


def create_task_mcp_server():
    return create_sdk_mcp_server(
        name="astra-tasks",
        version="0.1.0",
        tools=[add_task_tool, list_tasks_tool, complete_task_tool],
    )
