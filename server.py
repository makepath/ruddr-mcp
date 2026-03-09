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

import csv
import configparser
import io
import os
import re
import subprocess

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
2. Call `get_git_context` to get the repo URL, current branch, open PR URL, and any
   `.ruddr-project` hint. You will use this URL in the notes field of every time entry.
3. Call `list_projects` to find the matching project. If `get_git_context` returned a
   project_id hint from `.ruddr-project`, use that directly and skip the search.
4. Call `list_project_roles` for that project to see available roles. Infer the best
   role from the work:
   - Mostly changes under `assets/` or `frontend/` → Frontend / Frontend Developer
   - Mostly changes under `backend/` → Backend / Backend Developer
   - Mostly changes under `docs/` → Documentation / Technical Writer
   - Mixed → pick the dominant area or ask the user
5. Call `list_project_tasks` and try to match a task to the branch name, PR title,
   or GitHub issue number in the commit messages.
6. Draft a time entry with: date (today unless specified), duration, project, role,
   task, and a concise notes string summarising the commits.
   **Notes MUST include a URL** — prefer PR URL, then branch URL, then repo URL.
   Format: "Brief description of work — <url>"
   **Always round duration to the nearest 15 minutes** before presenting the draft.
7. **Always present the full draft to the user for approval before calling
   `create_time_entry`.** Show every field clearly. Let the user edit anything.
8. Only call `create_time_entry` after the user explicitly confirms.

## Workflow for bulk importing time entries

1. The user provides a CSV (pasted inline or as a file path).
2. Call `bulk_import_time_entries` with `dry_run=True` to parse and preview the entries.
3. Show the preview table to the user and ask for confirmation.
4. Only call `bulk_import_time_entries` with `dry_run=False` after the user confirms.
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


def _round_to_15(minutes: int) -> int:
    """Round a duration to the nearest 15-minute increment."""
    return round(minutes / 15) * 15


def _parse_duration(value: str) -> int:
    """
    Parse a duration string into minutes.

    Supported formats:
      90        → 90 minutes (bare integer = minutes)
      1.5       → 90 minutes (bare float = hours)
      1h 30m    → 90 minutes
      1h30m     → 90 minutes
      1h        → 60 minutes
      30m       → 30 minutes
      1:30      → 90 minutes
    """
    value = value.strip()

    # Bare integer → minutes
    if re.fullmatch(r"\d+", value):
        return int(value)

    # Bare float → hours
    if re.fullmatch(r"\d+\.\d+", value):
        return round(float(value) * 60)

    # HH:MM
    m = re.fullmatch(r"(\d+):(\d{2})", value)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # 1h 30m / 1h30m / 1h / 30m
    hours = re.search(r"(\d+(?:\.\d+)?)\s*h", value, re.IGNORECASE)
    mins = re.search(r"(\d+)\s*m(?!s)", value, re.IGNORECASE)
    total = 0
    if hours:
        total += round(float(hours.group(1)) * 60)
    if mins:
        total += int(mins.group(1))
    if total:
        return total

    raise ValueError(f"Cannot parse duration: {value!r}")


def _ssh_to_https(remote_url: str) -> str:
    """Convert a git SSH remote URL to HTTPS."""
    if remote_url.startswith("git@github.com:"):
        return "https://github.com/" + remote_url[len("git@github.com:"):].removesuffix(".git")
    if remote_url.startswith("git@"):
        # git@host:org/repo.git → https://host/org/repo
        host, path = remote_url[4:].split(":", 1)
        return f"https://{host}/{path.removesuffix('.git')}"
    return remote_url.removesuffix(".git")


def _resolve_project(name_or_id: str, projects: list) -> dict:
    """
    Match a project by UUID or case-insensitive name substring.
    Raises ValueError if no match or ambiguous.
    """
    # Exact UUID match first
    exact = [p for p in projects if p["id"] == name_or_id]
    if exact:
        return exact[0]

    needle = name_or_id.lower()
    matches = [p for p in projects if needle in p["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(p["name"] for p in matches)
        raise ValueError(f"Ambiguous project {name_or_id!r}: matches {names}")
    raise ValueError(f"No project found matching {name_or_id!r}")


def _resolve_role(name_or_id: str, roles: list) -> dict:
    exact = [r for r in roles if r["id"] == name_or_id]
    if exact:
        return exact[0]
    needle = name_or_id.lower()
    matches = [r for r in roles if needle in r["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(r["name"] for r in matches)
        raise ValueError(f"Ambiguous role {name_or_id!r}: matches {names}")
    raise ValueError(f"No role found matching {name_or_id!r}")


def _resolve_task(name_or_id: str, tasks: list) -> dict:
    exact = [t for t in tasks if t["id"] == name_or_id]
    if exact:
        return exact[0]
    needle = name_or_id.lower()
    matches = [t for t in tasks if needle in t["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(t["name"] for t in matches)
        raise ValueError(f"Ambiguous task {name_or_id!r}: matches {names}")
    raise ValueError(f"No task found matching {name_or_id!r}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_git_context(repo_path: str = ".") -> str:
    """
    Return the current git repo URL, branch, open PR URL, and any .ruddr-project hint.

    Always call this before drafting a time entry. It provides:
      - The best URL to include in notes (PR URL > branch URL > repo URL)
      - A project_id/project_name hint if a .ruddr-project file exists in the repo root
        (use the hint directly instead of calling list_projects)

    Args:
        repo_path: Path to the git repo (defaults to current directory)
    """

    def _run(cmd: list[str]) -> str:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=repo_path, timeout=5
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    remote_raw = _run(["git", "remote", "get-url", "origin"])
    if not remote_raw:
        return "Not a git repository or no remote configured."

    repo_url = _ssh_to_https(remote_raw)
    branch = _run(["git", "branch", "--show-current"])

    # Try gh CLI for PR URL
    pr_url = _run(["gh", "pr", "view", "--json", "url", "-q", ".url"])

    lines = [f"Repo URL:  {repo_url}"]
    if branch:
        branch_url = f"{repo_url}/tree/{branch}"
        lines.append(f"Branch:    {branch}")
        lines.append(f"Branch URL: {branch_url}")
    if pr_url:
        lines.append(f"PR URL:    {pr_url}")

    best = pr_url or (f"{repo_url}/tree/{branch}" if branch else repo_url)
    lines.append(f"\nBest URL for notes: {best}")

    # Check for .ruddr-project hint file
    import pathlib
    hint_path = pathlib.Path(repo_path) / ".ruddr-project"
    if hint_path.exists():
        cfg = configparser.RawConfigParser()
        cfg.read_string("[project]\n" + hint_path.read_text())
        project_id = cfg.get("project", "project_id", fallback="")
        project_name = cfg.get("project", "project_name", fallback="")
        if project_id or project_name:
            lines.append("\n.ruddr-project hint found:")
            if project_id:
                lines.append(f"  project_id:   {project_id}")
            if project_name:
                lines.append(f"  project_name: {project_name}")
            lines.append("  Use this project directly — no need to call list_projects.")

    return "\n".join(lines)


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
        minutes: Duration in minutes — will be rounded to the nearest 15-minute increment
        notes: Description of work done. MUST include a URL — use get_git_context to
               find the best one (PR URL > branch URL > repo URL).
               Format: "Brief description — <url>"
        role_id: UUID of the project role (from list_project_roles) — required by most projects
        task_id: UUID of the project task (from list_project_tasks) — include when matched
    """
    rounded = _round_to_15(minutes)
    payload: dict = {
        "typeId": "project_time",
        "memberId": member_id,
        "projectId": project_id,
        "date": date,
        "minutes": rounded,
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
    rounded_note = f" (rounded from {_fmt_minutes(minutes)})" if rounded != minutes else ""

    return (
        f"Time entry created successfully.\n"
        f"  Date:     {entry['date']}\n"
        f"  Duration: {duration}{rounded_note}\n"
        f"  Project:  {proj}\n"
        f"  ID:       {entry['id']}\n"
        f"  Status:   {entry.get('statusId', '—')}"
    )


@mcp.tool()
def bulk_import_time_entries(
    csv_text: str,
    member_id: str = "",
    dry_run: bool = True,
) -> str:
    """
    Parse a CSV of time entries, preview them as a table, and optionally submit all.

    CSV format (header row required):

      date,minutes,project,role,task,notes
      2026-03-01,90,NRC Easement,Backend,BDR report fix,Fixed layout bug — https://github.com/org/repo/pull/42
      2026-03-02,60,makepath Agency Ops,,,Team standup — https://github.com/org/repo

    Column details:
      date     — YYYY-MM-DD (required)
      minutes  — duration as integer minutes, float hours, "1h 30m", "1:30", etc. (required)
      project  — project name (partial match ok) or UUID (required)
      role     — role name (partial match ok) or UUID (optional)
      task     — task name (partial match ok) or UUID (optional)
      notes    — free text; include a URL when possible (optional)

    Workflow:
      1. Call with dry_run=True (default) to preview parsed entries as a table.
      2. Show the table to the user for review — they can correct any errors.
      3. Call again with dry_run=False to submit all entries.

    Args:
        csv_text: Raw CSV text including header row
        member_id: Member UUID (defaults to RUDDR_MEMBER_ID env var)
        dry_run: If True (default), preview only — nothing is submitted
    """
    resolved_member_id = member_id or os.environ.get("RUDDR_MEMBER_ID", "").strip()
    if not resolved_member_id:
        return "member_id is required (or set RUDDR_MEMBER_ID env var)."

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    rows = list(reader)
    if not rows:
        return "CSV is empty or has no data rows."

    required_cols = {"date", "minutes", "project"}
    missing = required_cols - {c.lower().strip() for c in (reader.fieldnames or [])}
    if missing:
        return f"CSV is missing required columns: {', '.join(sorted(missing))}"

    # Normalise column names to lowercase
    rows = [{k.lower().strip(): v.strip() for k, v in row.items()} for row in rows]

    # Load projects once
    all_projects = _paginate("projects")
    active_projects = [p for p in all_projects if p.get("recordStatusId") == "active"]

    # Resolve each row
    resolved: list[dict] = []
    errors: list[str] = []

    for i, row in enumerate(rows, start=2):  # row 1 = header
        try:
            minutes = _round_to_15(_parse_duration(row["minutes"]))
            project = _resolve_project(row["project"], active_projects)
            project_id = project["id"]

            role_id = ""
            role_name = ""
            if row.get("role"):
                roles = _paginate("project-roles", {"projectId": project_id})
                active_roles = [r for r in roles if r.get("isActive")]
                role = _resolve_role(row["role"], active_roles)
                role_id = role["id"]
                role_name = role["name"]

            task_id = ""
            task_name = ""
            if row.get("task"):
                tasks = _paginate("project-tasks", {"projectId": project_id})
                active_tasks = [t for t in tasks if t.get("recordStatusId") == "active"]
                task = _resolve_task(row["task"], active_tasks)
                task_id = task["id"]
                task_name = task["name"]

            resolved.append({
                "row": i,
                "date": row["date"],
                "minutes": minutes,
                "project_id": project_id,
                "project_name": project["name"],
                "role_id": role_id,
                "role_name": role_name,
                "task_id": task_id,
                "task_name": task_name,
                "notes": row.get("notes", ""),
            })
        except Exception as e:
            errors.append(f"Row {i}: {e}")

    # Build preview table
    col_widths = {
        "date": 10,
        "duration": 8,
        "project": max(len(r["project_name"]) for r in resolved) if resolved else 10,
        "role": max((len(r["role_name"]) for r in resolved), default=4),
        "task": max((len(r["task_name"]) for r in resolved), default=4),
        "notes": 50,
    }
    col_widths = {k: max(v, len(k)) for k, v in col_widths.items()}

    def _row_str(date, dur, proj, role, task, notes):
        return (
            f"  {date:<{col_widths['date']}}  "
            f"{dur:<{col_widths['duration']}}  "
            f"{proj:<{col_widths['project']}}  "
            f"{role:<{col_widths['role']}}  "
            f"{task:<{col_widths['task']}}  "
            f"{notes[:col_widths['notes']]}"
        )

    sep = "  " + "  ".join("-" * w for w in col_widths.values())
    header_row = _row_str("date", "duration", "project", "role", "task", "notes")

    table_lines = ["Preview:", header_row, sep]
    for r in resolved:
        table_lines.append(_row_str(
            r["date"],
            _fmt_minutes(r["minutes"]),
            r["project_name"],
            r["role_name"] or "—",
            r["task_name"] or "—",
            r["notes"] or "—",
        ))

    total_minutes = sum(r["minutes"] for r in resolved)
    table_lines.append(sep)
    table_lines.append(
        f"  {len(resolved)} entries  |  total: {_fmt_minutes(total_minutes)}"
    )

    output_lines = ["\n".join(table_lines)]

    if errors:
        output_lines.append("\nErrors (fix before submitting):\n" + "\n".join(errors))

    if dry_run:
        if errors:
            output_lines.append("\nFix the errors above, then call again with dry_run=True to re-preview.")
        else:
            output_lines.append(
                f"\n{len(resolved)} entries ready. "
                "Call again with dry_run=False to submit them all."
            )
        return "\n".join(output_lines)

    # --- Submit ---
    if errors:
        return "\n".join(output_lines) + "\n\nCannot submit: fix the errors above first."

    submitted = []
    submit_errors = []
    for r in resolved:
        payload: dict = {
            "typeId": "project_time",
            "memberId": resolved_member_id,
            "projectId": r["project_id"],
            "date": r["date"],
            "minutes": r["minutes"],
            "notes": r["notes"],
            "statusId": "not_submitted",
        }
        if r["role_id"]:
            payload["roleId"] = r["role_id"]
        if r["task_id"]:
            payload["taskId"] = r["task_id"]

        try:
            resp = httpx.post(
                f"{BASE_URL}/time-entries", headers=_headers(), json=payload, timeout=15
            )
            resp.raise_for_status()
            entry = resp.json()
            submitted.append(
                f"  ✓ {r['date']} | {_fmt_minutes(r['minutes'])} | {r['project_name']} (ID: {entry['id']})"
            )
        except Exception as e:
            submit_errors.append(f"  ✗ Row {r['row']} ({r['date']} / {r['project_name']}): {e}")

    result_lines = [f"Submitted {len(submitted)}/{len(resolved)} entries:"]
    result_lines.extend(submitted)
    if submit_errors:
        result_lines.append("\nFailed:")
        result_lines.extend(submit_errors)

    return "\n".join(output_lines) + "\n\n" + "\n".join(result_lines)


if __name__ == "__main__":
    mcp.run()
