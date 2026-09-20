# DMS-dead-man-s-spirit-
Dead Man's Spirit (dms)

Recover "ghost code" — code your AI coding agent claimed to write, but never actually landed in your workspace.

https://img.shields.io/badge/License-MIT-yellow.svg
https://img.shields.io/badge/python-3.x-blue.svg
https://img.shields.io/badge/dependencies-none-brightgreen.svg

---

The Problem

You ask Claude Code, Cursor, Aider, Cline, Codex, or Antigravity to write a file.

The agent says: "Done! I've created src/auth.py."

You check. It's not there.

Or worse — it's there, but it's the old version, and the agent's "successful edit" silently vanished into a sandbox, a git worktree, or a dangling blob. This is ghost code.

dms reads your agent's session history, finds every file write it claimed to perform, and checks whether each one actually landed on disk. Then it helps you get the lost ones back.

---

Features

· 🔍 Agent-agnostic — built-in profiles for Claude Code, Antigravity (agy), Aider, Cursor CLI, Cline, and Codex CLI
· 🧩 Extensible — point it at any agent's history dir with a JSON profile; no code changes needed
· 🪦 Grave search — hunts for lost code in git worktrees, sandboxes, scope-drifted project dirs, editor swap files, dangling git blobs, and the raw transcript payload itself
· ⚰️ --exhume — one command to restore ghost files back into your workspace
· 👁️ --watch — live monitor; alerts you the moment a claimed write doesn't land
· 📦 Zero dependencies — pure Python 3 stdlib. Runs on macOS, Linux, and Termux.

---

Install

Termux / Linux / macOS

```bash
curl -O https://raw.githubusercontent.com/YOURNAME/dms/main/dms.py
chmod +x dms.py
./dms.py --list-agents
```

Or clone:

```bash
git clone https://github.com/YOURNAME/dms.git
cd dms
./dms.py
```

No pip install. No virtualenv. Just Python 3.

---

Usage

```bash
# Auto-detect every known agent, reconcile, and display
./dms.py

# Only check one agent
./dms.py --agent claude-code

# Machine-readable output
./dms.py --json

# Recover ghost files into the workspace (prompts before overwrite)
./dms.py --exhume

# Recover and auto-confirm overwrites
./dms.py --exhume -y

# Live-watch session history and alert on missing writes
./dms.py --watch

# Point at a specific workspace
./dms.py --workspace ~/projects/myapp

# Custom / unknown agent
./dms.py --history-dir ~/.myagent --profile-json myagent.json

# See all built-in profiles
./dms.py --list-agents
```

---

How It Works

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Agent session  │───▶│  Extract claimed │───▶│  Reconcile vs.  │
│  history        │    │  file writes     │    │  real filesystem│
│  (JSONL / SQLite)│   │  (tool calls)    │    │                 │
└─────────────────┘    └──────────────────┘    └────────┬────────┘
                                                        │
                        ┌───────────────────────────────┘
                        ▼
              ┌──────────────────┐
              │  Search graves   │
              │  for lost code   │
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐
              │  Report / exhume │
              └──────────────────┘
```

Each claimed write gets one of three statuses:

Status Meaning
✅ OK File exists and mtime is at/after the claimed write time
⚠️ STALE File exists, but was last modified before the agent claimed to write it
❌ MISSING File does not exist at the claimed path

For every non-OK claim, dms searches these graves:

1. Git worktrees — git worktree list subagent checkouts
2. Sandbox dirs — agent-specific scratch areas (/tmp/claude-*, ~/.antigravity/sandbox, etc.)
3. Scope drift — other workspaces listed in the agent's settings.json
4. Editor swap files — .swp, .swo, .tmp, ~, #file#
5. Git dangling blobs — git fsck --lost-found
6. Transcript payload — the literal code from the tool call itself

If any grave has the code, --exhume will restore it.

---

Built-in Agent Profiles

Name Agent History Location
claude-code Claude Code ~/.claude/projects/*/*.jsonl
antigravity Google Antigravity CLI ~/.gemini/antigravity-cli/brain/...
aider Aider .aider.input.history in project
cursor Cursor CLI ~/.cursor/logs/*.jsonl
codex OpenAI Codex CLI ~/.codex/sessions/*.jsonl
cline Cline VS Code globalStorage
generic Anything else via --history-dir

Run ./dms.py --list-agents for the live list.

---

Custom Agent Profiles

Don't see your agent? Write a JSON profile:

```json
{
  "name": "myagent",
  "label": "My Custom Agent",
  "dirs": ["~/.myagent", "$XDG_DATA_HOME/myagent"],
  "transcript_glob": ["sessions/*.jsonl", "logs/**/*.jsonl"],
  "db_glob": [],
  "history_files": ["history.jsonl"],
  "tool_names": ["write_file", "apply_edit", "save"],
  "path_keys": ["path", "file_path", "target"],
  "content_keys": ["content", "new_str", "body"],
  "sandbox_dirs": ["/tmp/myagent-*"]
}
```

Then:

```bash
./dms.py --history-dir ~/.myagent --profile-json myagent.json
```

Profile field reference

Field Purpose
dirs Candidate history directories (support ~, $ENV, globs)
transcript_glob JSONL transcript patterns containing tool calls
db_glob SQLite conversation DBs
db_table / db_payload_col Table + column holding JSON tool-call payloads
history_files Flat JSONL history file names
tool_names Tool-call names that represent a file write
path_keys Arg keys holding the target path
content_keys Arg keys holding the written content
sandbox_dirs Extra "grave" glob patterns to search

---

Termux Notes

```bash
pkg install python git -y
termux-setup-storage   # if your workspace is on /sdcard
```

· Works out of the box — the shebang line auto-finds python3 or python
· --watch works, but bump --interval 5 or higher to save battery
· Git grave-searches need git installed (pkg install git)
· Windows-style paths ($LOCALAPPDATA) are safely ignored on Termux/Linux/macOS

---

Exit / Output Examples

```
=================================================================================
                  DEAD MAN'S SPIRIT (dms) - GHOST CODE RECOVERY REPORT
=================================================================================
Workspace: /home/user/projects/myapp
Inspected: 14 claimed writes
---------------------------------------------------------------------------------
CLAIMED PATH                       | STATUS         | AGENT        | FOUND AT
---------------------------------------------------------------------------------
src/auth.py                        | ✅ OK          | claude-code  | Workspace
src/handlers/webhook.py            | ⚠️ STALE       | claude-code  | Session Payload
lib/parser.go                      | ❌ MISSING     | codex        | Git Worktree: .../wt-1
tests/test_api.py                  | ❌ MISSING     | aider        | None
=================================================================================

Found 3 ghost file(s) (2 recoverable in graves).
To restore them to your workspace, run: ./dms.py --exhume
```

---

Roadmap

☐ --diff mode — show what changed between ghost and live version
☐ MCP server mode — expose dms checks to your agent directly
☐ Auto-hook — intercept writes and verify in real time
☐ More built-in profiles (Windsurf, Continue, Zed AI, OpenHands)

PRs welcome — especially new agent profiles. See CONTRIBUTING.md.

---

License

MIT — see LICENSE.

---

Disclaimer

dms reads local agent history files. It does not transmit anything anywhere. --exhume will overwrite files if you tell it to — back up your workspace if you're unsure. Use at your own risk.

---

If this saved a file your agent swore it wrote, ⭐ the repo.
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
