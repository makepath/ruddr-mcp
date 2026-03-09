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
2. Calls `get_git_context` to get the repo URL, branch, open PR URL, and any `.ruddr-project` hint
3. Uses the `.ruddr-project` hint if present, otherwise calls `list_projects` to find the project
4. Infers the **role** from where the changes landed (frontend, backend, docs, etc.)
5. Calls `list_project_tasks` and tries to match a task to the branch name or issue refs
6. Drafts a complete time entry — **notes always include a URL** (PR > branch > repo),
   duration **rounded to the nearest 15 minutes**
7. Presents the draft for your approval
8. Submits only after you confirm

## Prerequisites

One of the following to run the server:

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — handles dependencies
  automatically, no virtualenv setup needed (recommended)
- **Python 3.10+** with `pip install "mcp[cli]>=1.0" httpx` — works if `uv` isn't available

A Ruddr account with API access is also required.

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

If you're using `python3` directly instead of `uv`:

```json
{
  "mcpServers": {
    "ruddr": {
      "command": "python3",
      "args": ["/home/you/git/ruddr-mcp/server.py"],
      "env": {
        "RUDDR_API_KEY": "your-ruddr-api-key-here",
        "RUDDR_MEMBER_ID": "your-member-uuid-here"
      }
    }
  }
}
```

Or register it via the CLI (user scope, available in all projects):

```bash
claude mcp add \
  -e RUDDR_API_KEY=your-key \
  -e RUDDR_MEMBER_ID=your-member-uuid \
  --scope user \
  ruddr -- python3 /home/you/git/ruddr-mcp/server.py
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
- Find the best URL to include in the notes (PR > branch > repo)
- Round the duration to the nearest 15 minutes
- Propose a complete entry (date, duration, project, role, task, notes)
- Ask you to confirm or edit before submitting

### Notes always include a URL

Every time entry Claude creates will include a link in the notes field. Claude calls
`get_git_context` to find the best available URL in priority order:

1. **Open PR URL** — if `gh` CLI is installed and there's an open PR on the current branch
2. **Branch URL** — `https://github.com/org/repo/tree/your-branch`
3. **Repo URL** — `https://github.com/org/repo`

Example notes: `Fixed layout bug in BDR report — https://github.com/makepath/nrc/pull/142`

### Bulk import

You can import multiple time entries at once from a CSV. Paste the CSV directly into the
chat or reference a file path.

#### CSV format

```csv
date,minutes,project,role,task,notes
2026-03-01,90,NRC Easement,Backend,BDR report fix,Fixed layout bug — https://github.com/org/repo/pull/42
2026-03-02,60,Agency Ops,,,Team standup — https://github.com/org/repo
2026-03-03,1h 30m,Battleship,Frontend,,Dashboard updates — https://github.com/org/repo/tree/feat/dashboard
```

Column reference:

| Column | Required | Format |
|--------|----------|--------|
| `date` | yes | YYYY-MM-DD |
| `minutes` | yes | Integer minutes (`90`), float hours (`1.5`), or `1h 30m` / `1:30` |
| `project` | yes | Project name (partial match ok) or UUID |
| `role` | no | Role name (partial match ok) or UUID |
| `task` | no | Task name (partial match ok) or UUID |
| `notes` | no | Free text; include a URL when possible |

#### Bulk import workflow

1. Paste the CSV and ask Claude to bulk import it
2. Claude calls `bulk_import_time_entries` with `dry_run=True` and shows you a preview table
3. Review the table — correct any errors in the CSV if needed
4. Confirm, and Claude submits all entries

Example prompt:

```
Bulk import these time entries:

date,minutes,project,notes
2026-03-01,90,NRC Easement,Fixed BDR report layout — https://github.com/makepath/nrc/pull/42
2026-03-02,30,Agency Ops,Team standup — https://github.com/makepath/ruddr-mcp
```

### 15-minute rounding

All durations are automatically rounded to the nearest 15-minute increment before
submission — both for single entries and bulk imports. If rounding changes the value,
the original duration is shown in the confirmation:

```
Duration: 1h 15m (rounded from 1h 11m)
```

### Per-repo project hint (optional)

Drop a `.ruddr-project` file at the root of any repo and Claude will automatically
use it — no need to mention it every time. Claude reads this file via `get_git_context`
before every time entry.

```
# .ruddr-project
project_id = 550e8400-e29b-41d4-a716-446655440000
project_name = NRC Easement Monitoring
```

When the file is present, Claude skips `list_projects` and uses the hint directly.

## Available tools

| Tool | What it does |
|---|---|
| `get_git_context` | Returns repo URL, branch, PR URL, and `.ruddr-project` hint for use in notes |
| `get_my_member_id` | Returns your member ID (or lists all members if not configured) |
| `list_projects` | Lists all active projects with IDs, clients, and required fields |
| `list_project_roles` | Lists roles for a project (used to infer Frontend / Backend / etc.) |
| `list_project_tasks` | Lists tasks for a project, optionally filtered by name |
| `list_recent_time_entries` | Shows what you've logged recently (good for avoiding duplicates) |
| `create_time_entry` | Submits a single time entry — **always asks for confirmation first** |
| `bulk_import_time_entries` | Parses a CSV, previews a table, then submits all entries |

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
If using `uv`, make sure it's on your PATH. Test with `which uv`. If installed via curl,
it's usually at `~/.local/bin/uv` — use the full path in the `command` field if needed.
Alternatively, switch to `python3` (see Setup above).

**"No active projects found"**
Your API key may belong to a user without access to projects. Confirm you can see projects
at `https://app.ruddr.io`.

**Time entry fails with 422**
The project may require a role or task. Claude should handle this automatically, but you
can check by running `list_project_roles` and `list_project_tasks` manually in the chat.

**Bulk import: "Ambiguous project" error**
The project name matched more than one project. Use a more specific substring or paste the
project UUID directly in the CSV.

**PR URL not appearing in notes**
`get_git_context` tries `gh pr view` to find the PR URL. Install the
[GitHub CLI](https://cli.github.com/) and run `gh auth login` once to enable this.
