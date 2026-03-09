#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.0",
#   "httpx>=0.27",
# ]
# ///
"""
Ruddr MCP Server

Exposes Ruddr time-tracking as MCP tools for Claude Code.

Required env vars:
  RUDDR_API_KEY    - Ruddr API key (Settings > API Keys in Ruddr)

Optional env vars:
  RUDDR_MEMBER_ID  - Your Ruddr member UUID (avoids a lookup each session)
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://www.ruddr.io/api/workspace"

mcp = FastMCP(
    "ruddr",
    instructions="""
You are helping a software developer log time entries to Ruddr.

## Workflow for logging time

1. Use `git log` and `git diff --stat` (via bash) to understand recent work on the
   current branch — commits, files changed, rough split between frontend/backend/docs.
2. Call `list_projects` to find the matching project. Match on client name, project
   name, or the current git repo name.
3. Call `list_project_roles` for that project to see available roles. Infer the best
   role from the work:
   - Mostly changes under `assets/` or `frontend/` → Frontend / Frontend Developer
   - Mostly changes under `backend/` → Backend / Backend Developer
   - Mostly changes under `docs/` → Documentation / Technical Writer
   - Mixed → pick the dominant area or ask the user
4. Call `list_project_tasks` and try to match a task to the branch name, PR title,
   or GitHub issue number in the commit messages.
5. Draft a time entry with: date (today unless specified), duration, project, role,
   task, and a concise notes string summarising the commits.
6. **Always present the full draft to the user for approval before calling
   `create_time_entry`.** Show every field clearly. Let the user edit anything.
7. Only call `create_time_entry` after the user explicitly confirms.
""",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers() -> dict:
    api_key = os.environ.get("RUDDR_API_KEY", "").strip()
    if not api_key:
        raise ValueError("RUDDR_API_KEY environment variable is not set.")
    return {"Authorization": f"Bearer {api_key}"}


def _paginate(endpoint: str, params: dict | None = None) -> list:
    """Fetch all pages from a cursor-paginated Ruddr endpoint."""
    params = dict(params or {})
    params["limit"] = 100
    results: list = []

    while True:
        resp = httpx.get(f"{BASE_URL}/{endpoint}", headers=_headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("hasMore"):
            break
        params["startingAfter"] = results[-1]["id"]

    return results


def _fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_my_member_id() -> str:
    """
    Return the configured member ID from RUDDR_MEMBER_ID env var, or list all
    active members so the user can find theirs.

    Run this once to confirm your member ID, then add RUDDR_MEMBER_ID to your
    Claude Code MCP config to skip this lookup in future sessions.
    """
    member_id = os.environ.get("RUDDR_MEMBER_ID", "").strip()
    if member_id:
        return f"Configured member ID (from RUDDR_MEMBER_ID): {member_id}"

    members = _paginate("members")
    active = [m for m in members if m.get("isActive")]
    lines = [f"- {m['name']} <{m['email']}> — ID: {m['id']}" for m in active]
    header = (
        "RUDDR_MEMBER_ID is not set. Active members:\n\n"
        + "\n".join(lines)
        + "\n\nAdd RUDDR_MEMBER_ID=<your-id> to your MCP env config to avoid this lookup."
    )
    return header


@mcp.tool()
def list_projects(include_archived: bool = False) -> str:
    """
    List all Ruddr projects with their IDs, clients, and relevant settings
    (whether notes/tasks/roles are required).

    Use this to find the right project ID before logging time.
    """
    projects = _paginate("projects")
    if not include_archived:
        projects = [p for p in projects if p.get("recordStatusId") == "active"]

    if not projects:
        return "No active projects found."

    lines = []
    for p in sorted(projects, key=lambda x: (x.get("client", {}) or {}).get("name", "")):
        client = (p.get("client") or {}).get("name", "—")
        flags = []
        if p.get("requiresNotes"):
            flags.append("notes required")
        if p.get("requiresTasks"):
            flags.append("task required")
        if p.get("useRoles"):
            flags.append("role required")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"- [{client}] {p['name']}{flag_str}\n  ID: {p['id']}")

    return "\n".join(lines)


@mcp.tool()
def list_project_roles(project_id: str) -> str:
    """
    List the active roles available for a specific project.

    Use the project ID from list_projects. Match a role to the type of work done
    (frontend, backend, documentation, etc.) to populate the roleId in a time entry.
    """
    roles = _paginate("project-roles", {"projectId": project_id})
    active = [r for r in roles if r.get("isActive")]

    if not active:
        return "No active roles found for this project."

    lines = [f"- {r['name']} — ID: {r['id']}" for r in active]
    return "\n".join(lines)


@mcp.tool()
def list_project_tasks(project_id: str, name_contains: str = "") -> str:
    """
    List active tasks for a specific project, optionally filtered by name.

    Use the project ID from list_projects. Try to match a task to the current
    branch name, GitHub issue number, or PR title to populate taskId.
    """
    params: dict = {"projectId": project_id}
    if name_contains:
        params["nameContains"] = name_contains

    tasks = _paginate("project-tasks", params)
    active = [t for t in tasks if t.get("recordStatusId") == "active"]

    if not active:
        return "No active tasks found for this project."

    lines = []
    for t in active:
        status = t.get("statusId", "—")
        lines.append(f"- {t['name']} (status: {status}) — ID: {t['id']}")

    return "\n".join(lines)


@mcp.tool()
def list_recent_time_entries(member_id: str, days_back: int = 7) -> str:
    """
    List recent time entries for a member (default: last 7 days).

    Useful for reviewing what has already been logged before adding a new entry,
    and for checking the format of previous notes.
    """
    from datetime import date, timedelta

    since = (date.today() - timedelta(days=days_back)).isoformat()
    params = {"memberId": member_id, "dateOnAfter": since, "limit": 50}

    resp = httpx.get(f"{BASE_URL}/time-entries", headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    entries = resp.json().get("results", [])

    if not entries:
        return f"No time entries found in the last {days_back} days."

    lines = []
    for e in entries:
        proj = (e.get("project") or {}).get("name", "—")
        task = (e.get("task") or {}).get("name", "")
        role = (e.get("role") or {}).get("name", "")
        task_str = f" / {task}" if task else ""
        role_str = f" [{role}]" if role else ""
        duration = _fmt_minutes(e.get("minutes", 0))
        notes = (e.get("notes") or "")[:80]
        notes_str = f' — "{notes}"' if notes else ""
        lines.append(f"- {e['date']} | {duration} | {proj}{task_str}{role_str}{notes_str}")

    return "\n".join(lines)


@mcp.tool()
def create_time_entry(
    member_id: str,
    project_id: str,
    date: str,
    minutes: int,
    notes: str,
    role_id: str = "",
    task_id: str = "",
) -> str:
    """
    Submit a time entry to Ruddr.

    IMPORTANT: You MUST present the complete draft to the user and receive explicit
    confirmation before calling this tool. Show all fields:
      - Date
      - Duration (convert minutes to hours/minutes for readability)
      - Project name (not just ID)
      - Role name (not just ID)
      - Task name (not just ID)
      - Notes text

    Only call this after the user says "yes", "submit", "looks good", or similar.

    Args:
        member_id: UUID of the member (from get_my_member_id)
        project_id: UUID of the project (from list_projects)
        date: Date in YYYY-MM-DD format
        minutes: Duration in minutes (e.g. 90 = 1h 30m)
        notes: Description of work done — be specific, mention features/fixes worked on
        role_id: UUID of the project role (from list_project_roles) — required by most projects
        task_id: UUID of the project task (from list_project_tasks) — include when matched
    """
    payload: dict = {
        "typeId": "project_time",
        "memberId": member_id,
        "projectId": project_id,
        "date": date,
        "minutes": minutes,
        "notes": notes,
        "statusId": "not_submitted",
    }
    if role_id:
        payload["roleId"] = role_id
    if task_id:
        payload["taskId"] = task_id

    resp = httpx.post(f"{BASE_URL}/time-entries", headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    entry = resp.json()

    proj = (entry.get("project") or {}).get("name", "Unknown")
    duration = _fmt_minutes(entry.get("minutes", 0))

    return (
        f"Time entry created successfully.\n"
        f"  Date:     {entry['date']}\n"
        f"  Duration: {duration}\n"
        f"  Project:  {proj}\n"
        f"  ID:       {entry['id']}\n"
        f"  Status:   {entry.get('statusId', '—')}"
    )


if __name__ == "__main__":
    mcp.run()
