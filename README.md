# ruddr-mcp

An MCP server that connects Claude Code to [Ruddr](https://ruddr.io) for time tracking.
Lets you log time entries directly from a Claude Code conversation — Claude reads your
recent git activity, suggests the right project, role, task, and notes, then waits for
your approval before submitting anything.

## How it works

This is a **local MCP server** — it runs as a subprocess on your machine when Claude Code
starts. No hosting required. Each developer runs it with their own Ruddr API key.

Claude's typical flow when you ask it to log time:

1. Reads recent commits and `git diff --stat` on your current branch
2. Calls `list_projects` to find the matching Ruddr project
3. Infers the **role** from where the changes landed (frontend, backend, docs, etc.)
4. Calls `list_project_tasks` and tries to match a task to the branch name or issue refs
5. Drafts a complete time entry and presents it for your approval
6. Submits only after you confirm

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — used to run the server
  with automatic dependency management (no virtualenv setup needed)
- A Ruddr account with API access

## Setup

### 1. Get your Ruddr API key

In Ruddr: **Settings → API Keys → Create API Key**. Copy the key.

### 2. Find your Ruddr member ID

Run a quick curl to look yourself up:

```bash
curl -s -H "Authorization: Bearer <your-api-key>" \
  "https://www.ruddr.io/api/workspace/members?email=you@example.com" \
  | python3 -m json.tool | grep '"id"' | head -1
```

Or you can skip this step and let Claude look it up via `get_my_member_id` on first use.

### 3. Clone this repo

```bash
git clone git@github.com:makepath/ruddr-mcp.git ~/git/ruddr-mcp
# or wherever you keep your tools
```

### 4. Configure Claude Code

Add the server to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "ruddr": {
      "command": "uv",
      "args": ["run", "/home/you/git/ruddr-mcp/server.py"],
      "env": {
        "RUDDR_API_KEY": "your-ruddr-api-key-here",
        "RUDDR_MEMBER_ID": "your-member-uuid-here"
      }
    }
  }
}
```

> **Note:** `RUDDR_MEMBER_ID` is optional but recommended — it saves a lookup on every
> session. If omitted, ask Claude to run `get_my_member_id` once to find it.

Restart Claude Code (or run `/mcp` to reload servers). You should see `ruddr` listed.

## Usage

Just describe what you want in natural language. A few examples:

```
Log 2 hours of time for today's work on the satellite imagery feature.
```

```
I worked on the NRC project from about 9am to 11:30am fixing the BDR report layout.
Help me log that.
```

```
What time have I logged this week?
```

```
Log yesterday's work — I was mostly fixing backend bugs related to the geoenrichment service.
```

Claude will:
- Check your recent git commits for context
- Propose a complete entry (date, duration, project, role, task, notes)
- Ask you to confirm or edit before submitting

### Per-repo project hint (optional)

You can drop a `.ruddr-project` file at the root of any repo to help Claude pick the
right project without searching:

```
# .ruddr-project
project_id = 550e8400-e29b-41d4-a716-446655440000
project_name = NRC Easement Monitoring
```

Mention it exists when prompting Claude: *"Use the .ruddr-project file to find the project."*

## Available tools

| Tool | What it does |
|---|---|
| `get_my_member_id` | Returns your member ID (or lists all members if not configured) |
| `list_projects` | Lists all active projects with IDs, clients, and required fields |
| `list_project_roles` | Lists roles for a project (used to infer Frontend / Backend / etc.) |
| `list_project_tasks` | Lists tasks for a project, optionally filtered by name |
| `list_recent_time_entries` | Shows what you've logged recently (good for avoiding duplicates) |
| `create_time_entry` | Submits a time entry — **always asks for confirmation first** |

## Role inference

Claude infers the right role from `git diff --stat` output:

| Dominant change area | Typical role |
|---|---|
| `assets/`, `frontend/` | Frontend Developer |
| `backend/` | Backend Developer |
| `docs/`, `*.md` | Documentation / Technical Writer |
| Mixed | Claude will ask you to choose |

The actual role names come from your Ruddr project configuration, so they may differ.

## Troubleshooting

**"RUDDR_API_KEY environment variable is not set"**
Check that `RUDDR_API_KEY` is present in the `env` block of your MCP config.

**Server doesn't appear in `/mcp`**
Make sure `uv` is on your PATH. Test with `which uv`. If installed via curl, it's usually
at `~/.local/bin/uv` — use the full path in the `command` field if needed.

**"No active projects found"**
Your API key may belong to a user without access to projects. Confirm you can see projects
at `https://app.ruddr.io`.

**Time entry fails with 422**
The project may require a role or task. Claude should handle this automatically, but you
can check by running `list_project_roles` and `list_project_tasks` manually in the chat.
