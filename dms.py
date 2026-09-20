#!/bin/sh
''''exec "$(command -v python3 || command -v python)" "$0" "$@" # '''
"""
=============================================================================
Dead Man's Spirit (abbreviated: dms)
=============================================================================
CLI tool to recover "ghost code" — code that an AI coding CLI (Claude Code,
agy/Antigravity, Aider, Cursor CLI, Cline, Codex CLI, etc.) claims to have
written or patched, but never actually landed in your workspace.

The tool is agent-agnostic. It ships with built-in "profiles" that know
where a given agent stores its session history and how its tool-call
payloads are shaped, but you can also point it at any arbitrary log
directory and describe the shape yourself via a JSON config, or just let
it auto-detect every known profile at once.

How to run:
    ./dms.py [OPTIONS]

Examples:
    ./dms.py                              # Auto-detect all known agents, reconcile & display
    ./dms.py --agent claude-code          # Only check a specific agent's history
    ./dms.py --json                       # Export findings in machine-readable JSON format
    ./dms.py --exhume                     # Recover ghost files into workspace (prompts before overwrite)
    ./dms.py --exhume -y                  # Recover ghost files and auto-confirm overwriting
    ./dms.py --watch                      # Monitor live session history and alert when writes don't land
    ./dms.py --workspace /dir             # Specify primary workspace directory (defaults to cwd)
    ./dms.py --history-dir ~/.myagent     # Point at a custom/unknown agent's history directory
    ./dms.py --list-agents                # Show all built-in agent profiles

Dependencies:
    Zero external dependencies. Runs on macOS, Linux, and Termux with Python 3.
=============================================================================
"""

import os
import sys
import json
import glob
import subprocess
import time
import datetime
import argparse
import shutil
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# ANSI Terminal Formatting
# ---------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty()

def color(text: str, code: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(text: str) -> str: return color(text, "32;1")
def yellow(text: str) -> str: return color(text, "33;1")
def red(text: str) -> str: return color(text, "31;1")
def cyan(text: str) -> str: return color(text, "36;1")
def bold(text: str) -> str: return color(text, "1")
def dim(text: str) -> str: return color(text, "2")


# ---------------------------------------------------------------------------
# 0. Agent Profiles — pluggable knowledge of where each CLI keeps history
#    and what its tool-call / write payloads look like.
#
# A profile is just a dict. To support a brand-new agent, either:
#   (a) add an entry to BUILTIN_PROFILES below, or
#   (b) pass --history-dir plus (optionally) --profile-json pointing at a
#       JSON file with the same shape as one of these dicts.
#
# Fields:
#   name            - short identifier
#   dirs            - list of candidate directories (may use ~ and glob *)
#   transcript_glob - list of glob patterns (relative to a found dir) for
#                     JSONL transcript files containing tool calls
#   db_glob         - list of glob patterns for sqlite3 conversation DBs
#   db_table        - table name expected to hold step/message payloads
#   db_payload_col  - column name in that table holding a JSON blob
#   history_files   - list of flat JSONL history file names (relative to dir)
#   tool_names      - set of tool-call names that represent a file write
#   path_keys       - set of arg-key names that hold the target file path
#   content_keys    - set of arg-key names that hold the written content
#   sandbox_dirs    - extra glob patterns to search as "graves" for lost code
# ---------------------------------------------------------------------------

DEFAULT_TOOL_NAMES = {
    "write_file", "edit_file", "create_file", "apply_patch",
    "write_to_file", "replace_file_content", "patch_file",
    "modify_file", "append_file", "save_file", "str_replace",
    "str_replace_editor", "fs_write", "update_file",
}

DEFAULT_PATH_KEYS = {
    "targetfile", "target_file", "path", "file_path", "filepath",
    "file", "filename", "destination", "dest", "target",
}

DEFAULT_CONTENT_KEYS = {
    "codecontent", "code_content", "content", "replacementcontent",
    "replacement_content", "patch", "diff", "text", "body", "code",
    "new_str", "new_string", "file_text",
}

BUILTIN_PROFILES = {
    "antigravity": {
        "name": "antigravity",
        "label": "Google Antigravity CLI (agy)",
        "dirs": [
            "~/.gemini/antigravity-cli",
            "~/.antigravity",
            "~/.config/antigravity-cli",
            "~/.local/share/antigravity-cli",
            "$LOCALAPPDATA/antigravity-cli",
            "$LOCALAPPDATA/Google/antigravity-cli",
        ],
        "transcript_glob": [
            "brain/*/.system_generated/logs/transcript*.jsonl",
            "brain/*/.system_generated/logs/chunks/*/*.jsonl",
            "brain/*/transcript*.jsonl",
            "logs/*.jsonl",
        ],
        "db_glob": ["conversations/*.db"],
        "db_table": "steps",
        "db_payload_col": "step_payload",
        "history_files": ["history.jsonl"],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [
            "~/.antigravity/sandbox",
            "/tmp/agy-*",
            "/tmp/antigravity-*",
            "~/.gemini/antigravity-cli/brain/*/scratch",
        ],
    },
    "claude-code": {
        "name": "claude-code",
        "label": "Claude Code",
        "dirs": [
            "~/.claude",
            "~/.config/claude-code",
        ],
        "transcript_glob": [
            "projects/*/*.jsonl",
            "logs/*.jsonl",
        ],
        "db_glob": [],
        "db_table": None,
        "db_payload_col": None,
        "history_files": ["history.jsonl"],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [
            "/tmp/claude-*",
        ],
    },
    "aider": {
        "name": "aider",
        "label": "Aider",
        "dirs": [
            "~/.aider",
            ".",  # aider keeps .aider.chat.history.md in the project itself
        ],
        "transcript_glob": [
            "*.jsonl",
        ],
        "db_glob": [],
        "db_table": None,
        "db_payload_col": None,
        "history_files": [".aider.input.history"],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [],
    },
    "cursor": {
        "name": "cursor",
        "label": "Cursor CLI",
        "dirs": [
            "~/.cursor",
            "~/.config/Cursor",
        ],
        "transcript_glob": [
            "logs/*.jsonl",
            "sessions/*/*.jsonl",
        ],
        "db_glob": ["**/state.vscdb"],
        "db_table": None,
        "db_payload_col": None,
        "history_files": [],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [],
    },
    "codex": {
        "name": "codex",
        "label": "OpenAI Codex CLI",
        "dirs": [
            "~/.codex",
        ],
        "transcript_glob": [
            "sessions/*.jsonl",
            "logs/*.jsonl",
        ],
        "db_glob": [],
        "db_table": None,
        "db_payload_col": None,
        "history_files": ["history.jsonl"],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": ["/tmp/codex-*"],
    },
    "cline": {
        "name": "cline",
        "label": "Cline",
        "dirs": [
            "~/.cline",
            "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev",
            "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev",
        ],
        "transcript_glob": [
            "tasks/*/*.json",
            "tasks/*/*.jsonl",
        ],
        "db_glob": [],
        "db_table": None,
        "db_payload_col": None,
        "history_files": [],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [],
    },
    "generic": {
        "name": "generic",
        "label": "Generic / user-specified",
        "dirs": [],
        "transcript_glob": ["*.jsonl", "**/*.jsonl"],
        "db_glob": ["*.db", "**/*.db"],
        "db_table": "steps",
        "db_payload_col": "step_payload",
        "history_files": ["history.jsonl"],
        "tool_names": DEFAULT_TOOL_NAMES,
        "path_keys": DEFAULT_PATH_KEYS,
        "content_keys": DEFAULT_CONTENT_KEYS,
        "sandbox_dirs": [],
    },
}


def expand_dir(d: str) -> str:
    d = os.path.expandvars(d)
    d = os.path.expanduser(d)
    return d


def resolve_profile_dirs(profile, override_dir=None):
    """Finds candidate history directories for a given profile."""
    candidates = []
    if override_dir:
        candidates.append(os.path.abspath(os.path.expanduser(override_dir)))

    for d in profile.get("dirs", []):
        expanded = expand_dir(d)
        if "*" in expanded:
            candidates.extend(glob.glob(expanded))
        else:
            candidates.append(expanded)

    existing = []
    for c in candidates:
        c_abs = os.path.abspath(c)
        if os.path.isdir(c_abs) and c_abs not in existing:
            existing.append(c_abs)
    return existing


def load_profiles(selected_agent=None, profile_json_path=None):
    """
    Returns the list of profile dicts to scan. If --agent is given, only
    that one. If --profile-json is given, it's merged in as an additional
    'custom' profile (or replaces 'generic' fields if it shares the name).
    Otherwise, all builtin profiles are scanned (auto-detect mode).
    """
    profiles = dict(BUILTIN_PROFILES)

    if profile_json_path:
        try:
            with open(profile_json_path, "r", encoding="utf-8") as f:
                custom = json.load(f)
            custom.setdefault("name", "custom")
            custom.setdefault("label", custom["name"])
            custom.setdefault("tool_names", list(DEFAULT_TOOL_NAMES))
            custom.setdefault("path_keys", list(DEFAULT_PATH_KEYS))
            custom.setdefault("content_keys", list(DEFAULT_CONTENT_KEYS))
            custom.setdefault("transcript_glob", ["*.jsonl", "**/*.jsonl"])
            custom.setdefault("db_glob", [])
            custom.setdefault("history_files", [])
            custom.setdefault("sandbox_dirs", [])
            custom.setdefault("dirs", [])
            # normalize sets
            custom["tool_names"] = set(n.lower() for n in custom["tool_names"])
            custom["path_keys"] = set(k.lower() for k in custom["path_keys"])
            custom["content_keys"] = set(k.lower() for k in custom["content_keys"])
            profiles[custom["name"]] = custom
        except Exception as e:
            print(red(f"[!] Failed to load --profile-json: {e}"), file=sys.stderr)

    if selected_agent:
        if selected_agent not in profiles:
            print(red(f"[!] Unknown agent profile: {selected_agent}"), file=sys.stderr)
            print(dim(f"    Known profiles: {', '.join(sorted(profiles.keys()))}"), file=sys.stderr)
            sys.exit(1)
        return [profiles[selected_agent]]

    # Auto-detect mode: everything except the bare 'generic' template,
    # unless the user pointed us at a custom --history-dir (handled by caller).
    return [p for name, p in profiles.items() if name != "generic"]


def parse_timestamp(val):
    """Parses timestamps from ISO 8601 string, integer/float milliseconds or seconds."""
    if not val:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(val, (int, float)):
        if val > 1e11:  # Milliseconds
            val = val / 1000.0
        return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
    if isinstance(val, str):
        val = val.strip().strip('"').strip("'")
        try:
            f = float(val)
            if f > 1e11:
                f = f / 1000.0
            return datetime.datetime.fromtimestamp(f, tz=datetime.timezone.utc)
        except ValueError:
            pass
        val = val.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            pass
    return datetime.datetime.now(datetime.timezone.utc)


def clean_arg(val):
    """Clean quoted strings or stringified JSON arguments."""
    if val is None:
        return None
    if isinstance(val, str):
        v = val.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        return v
    return str(val)


def extract_from_tool_call(tool_call, default_ts, profile, session_id="", agent_name=""):
    """Extracts claimed file write information from a single tool call dictionary,
    using the given profile's tool/path/content key vocabulary."""
    if not isinstance(tool_call, dict):
        return None

    name = (tool_call.get("name") or tool_call.get("function", {}).get("name") or "").lower()
    if name not in profile["tool_names"]:
        return None

    args = tool_call.get("args") or tool_call.get("arguments") or tool_call.get("input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        return None

    claimed_path = None
    claimed_content = None

    for k, v in args.items():
        k_lower = str(k).lower().replace("-", "_").replace(" ", "_")
        if k_lower in profile["path_keys"] and not claimed_path:
            cleaned = clean_arg(v)
            if cleaned:
                claimed_path = cleaned
        elif k_lower in profile["content_keys"] and not claimed_content:
            if isinstance(v, str):
                claimed_content = v

    if not claimed_path:
        return None

    return {
        "tool": name,
        "agent": agent_name,
        "claimed_path": claimed_path,
        "content": claimed_content,
        "timestamp": default_ts,
        "session_id": session_id,
        "raw_args": args,
    }


# ---------------------------------------------------------------------------
# 1. History Parsing (profile-driven, works across any number of agents)
# ---------------------------------------------------------------------------
def parse_history_for_profile(profile, base_dirs, target_workspace):
    """
    Parses a single agent profile's session history from:
      - transcript_glob JSONL files (tool_calls arrays)
      - db_glob SQLite conversation databases (payload column with tool_calls)
      - history_files flat JSONL history logs
    """
    claimed_records = []
    seen_keys = set()
    agent_name = profile["name"]

    for base_dir in base_dirs:
        if not os.path.exists(base_dir):
            continue

        # 1. Transcript JSONL files
        for pattern in profile.get("transcript_glob", []):
            full_pattern = os.path.join(base_dir, pattern)
            for t_file in glob.glob(full_pattern, recursive=True):
                if not os.path.isfile(t_file):
                    continue
                session_id = Path(t_file).stem
                try:
                    with open(t_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue
                            if not isinstance(obj, dict):
                                continue

                            ts = parse_timestamp(obj.get("created_at") or obj.get("timestamp"))
                            tool_calls = obj.get("tool_calls") or obj.get("toolCalls") or []
                            if isinstance(tool_calls, dict):
                                tool_calls = [tool_calls]
                            for tc in tool_calls:
                                rec = extract_from_tool_call(tc, ts, profile, session_id=session_id, agent_name=agent_name)
                                if rec:
                                    p = rec["claimed_path"]
                                    if not os.path.isabs(p):
                                        p = os.path.abspath(os.path.join(target_workspace, p))
                                    rec["resolved_path"] = os.path.normpath(p)

                                    key = (rec["resolved_path"], rec["timestamp"].isoformat(), rec["tool"], agent_name)
                                    if key not in seen_keys:
                                        seen_keys.add(key)
                                        claimed_records.append(rec)
                except Exception:
                    pass

        # 2. SQLite conversation databases
        if profile.get("db_glob") and profile.get("db_table") and profile.get("db_payload_col"):
            for db_pattern in profile["db_glob"]:
                full_pattern = os.path.join(base_dir, db_pattern)
                for db_file in glob.glob(full_pattern, recursive=True):
                    session_id = Path(db_file).stem
                    try:
                        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                        cur = conn.cursor()
                        tables = [r[0] for r in cur.execute(
                            "SELECT name FROM sqlite_master WHERE type='table';"
                        ).fetchall()]
                        if profile["db_table"] in tables:
                            rows = cur.execute(
                                f"SELECT {profile['db_payload_col']} FROM {profile['db_table']} "
                                f"WHERE {profile['db_payload_col']} IS NOT NULL;"
                            ).fetchall()
                            for (payload,) in rows:
                                if not payload:
                                    continue
                                try:
                                    data = json.loads(payload) if isinstance(payload, str) else payload
                                except Exception:
                                    continue
                                if isinstance(data, dict):
                                    ts = parse_timestamp(data.get("created_at") or data.get("timestamp"))
                                    tcs = data.get("tool_calls") or data.get("toolCalls") or []
                                    if isinstance(tcs, dict):
                                        tcs = [tcs]
                                    for tc in tcs:
                                        rec = extract_from_tool_call(tc, ts, profile, session_id=session_id, agent_name=agent_name)
                                        if rec:
                                            p = rec["claimed_path"]
                                            if not os.path.isabs(p):
                                                p = os.path.abspath(os.path.join(target_workspace, p))
                                            rec["resolved_path"] = os.path.normpath(p)
                                            key = (rec["resolved_path"], rec["timestamp"].isoformat(), rec["tool"], agent_name)
                                            if key not in seen_keys:
                                                seen_keys.add(key)
                                                claimed_records.append(rec)
                        conn.close()
                    except Exception:
                        pass

        # 3. Flat history JSONL files
        for hf_name in profile.get("history_files", []):
            history_file = os.path.join(base_dir, hf_name)
            if not os.path.isfile(history_file):
                continue
            try:
                with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        ts = parse_timestamp(obj.get("timestamp"))
                        tool_calls = obj.get("tool_calls") or obj.get("toolCalls") or []
                        if isinstance(tool_calls, dict):
                            tool_calls = [tool_calls]
                        for tc in tool_calls:
                            rec = extract_from_tool_call(tc, ts, profile, session_id="history", agent_name=agent_name)
                            if rec:
                                p = rec["claimed_path"]
                                if not os.path.isabs(p):
                                    p = os.path.abspath(os.path.join(target_workspace, p))
                                rec["resolved_path"] = os.path.normpath(p)
                                key = (rec["resolved_path"], rec["timestamp"].isoformat(), rec["tool"], agent_name)
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    claimed_records.append(rec)
            except Exception:
                pass

    claimed_records.sort(key=lambda x: x["timestamp"], reverse=True)
    return claimed_records


def parse_history(profile_dir_pairs, target_workspace):
    """profile_dir_pairs: list of (profile, [base_dirs]) tuples."""
    all_records = []
    for profile, base_dirs in profile_dir_pairs:
        all_records.extend(parse_history_for_profile(profile, base_dirs, target_workspace))
    all_records.sort(key=lambda x: x["timestamp"], reverse=True)
    return all_records


# ---------------------------------------------------------------------------
# 2. Reconcile Claimed Writes against Real Filesystem
# ---------------------------------------------------------------------------
def reconcile_claims(claimed_records):
    """
    Checks each claimed path against the filesystem:
      - OK: Exists and mtime >= claimed timestamp (within 2s grace)
      - STALE: Exists, but last modified before the claimed write timestamp
      - MISSING: File does not exist at the claimed path
    """
    reconciled = []
    latest_claims = {}
    for r in claimed_records:
        rp = r["resolved_path"]
        if rp not in latest_claims or r["timestamp"] > latest_claims[rp]["timestamp"]:
            latest_claims[rp] = r

    for resolved_path, claim in latest_claims.items():
        if os.path.exists(resolved_path):
            mtime = os.path.getmtime(resolved_path)
            file_mtime_dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
            claim_ts = claim["timestamp"]

            if file_mtime_dt >= (claim_ts - datetime.timedelta(seconds=2)):
                status = "OK"
                badge = "✅ OK"
            else:
                status = "STALE"
                badge = "⚠️ STALE"
            claim["file_mtime"] = file_mtime_dt.isoformat()
        else:
            status = "MISSING"
            badge = "❌ MISSING"
            claim["file_mtime"] = None

        claim["status"] = status
        claim["badge"] = badge
        reconciled.append(claim)

    reconciled.sort(key=lambda x: (x["status"] == "OK", x["resolved_path"]))
    return reconciled


# ---------------------------------------------------------------------------
# 3. Search Known "Graves" for Ghost Files
# ---------------------------------------------------------------------------
def search_graves(claim, workspace_root, all_base_dirs, profiles_by_name):
    """
    Searches for ghost files in known graves:
      1. Git subagent worktrees (git worktree list)
      2. Sandbox directories (agent-specific, from profile's sandbox_dirs)
      3. Project scope drift: other folders mapped in settings.json
      4. Editor swap / temp backup files (.swp, .tmp, ~)
      5. Git dangling blobs: git fsck --lost-found
      6. In-transcript tool call payload (direct code extraction)
    """
    found_locations = []
    resolved_path = claim["resolved_path"]
    filename = os.path.basename(resolved_path)
    rel_path = os.path.relpath(resolved_path, workspace_root) if resolved_path.startswith(workspace_root) else filename

    # 1. Git Subagent Worktrees
    try:
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if res.returncode == 0:
            worktree_paths = []
            for line in res.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_dir = line.split(" ", 1)[1].strip()
                    if os.path.abspath(wt_dir) != os.path.abspath(workspace_root):
                        worktree_paths.append(wt_dir)

            for wt in worktree_paths:
                candidate = os.path.join(wt, rel_path)
                if os.path.isfile(candidate):
                    found_locations.append({
                        "type": "git_worktree",
                        "path": candidate,
                        "description": f"Git Worktree: {candidate}",
                        "source": "worktree"
                    })
    except Exception:
        pass

    # 2. Sandbox Directories — pulled from the claim's own agent profile
    agent_name = claim.get("agent")
    sandbox_patterns = []
    if agent_name and agent_name in profiles_by_name:
        sandbox_patterns.extend(profiles_by_name[agent_name].get("sandbox_dirs", []))
    for pattern in sandbox_patterns:
        for s_dir in glob.glob(expand_dir(pattern)):
            if not os.path.isdir(s_dir):
                continue
            for cand in [os.path.join(s_dir, rel_path), os.path.join(s_dir, filename)]:
                if os.path.isfile(cand):
                    found_locations.append({
                        "type": "sandbox",
                        "path": cand,
                        "description": f"Sandbox: {cand}",
                        "source": "sandbox"
                    })

    # 3. Project Scope Drift (settings.json trustedWorkspaces / projects)
    for b in all_base_dirs:
        s_file = os.path.join(b, "settings.json")
        if os.path.isfile(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                workspaces = s_data.get("trustedWorkspaces") or s_data.get("projects") or []
                if isinstance(workspaces, list):
                    for other_ws in workspaces:
                        if isinstance(other_ws, str) and os.path.isdir(other_ws):
                            if os.path.abspath(other_ws) != os.path.abspath(workspace_root):
                                cand = os.path.join(other_ws, rel_path)
                                if os.path.isfile(cand):
                                    found_locations.append({
                                        "type": "scope_drift",
                                        "path": cand,
                                        "description": f"Project Drift: {cand}",
                                        "source": "scope_drift"
                                    })
            except Exception:
                pass

    # 4. Editor Swap Files (.swp, .tmp, ~ backups)
    parent_dir = os.path.dirname(resolved_path)
    if os.path.isdir(parent_dir):
        swap_patterns = [
            os.path.join(parent_dir, f".{filename}.swp"),
            os.path.join(parent_dir, f".{filename}.swo"),
            os.path.join(parent_dir, f"{filename}.tmp"),
            os.path.join(parent_dir, f"{filename}~"),
            os.path.join(parent_dir, f"#{filename}#"),
        ]
        for sp in swap_patterns:
            if os.path.isfile(sp):
                found_locations.append({
                    "type": "editor_swap",
                    "path": sp,
                    "description": f"Editor Swap/Backup: {sp}",
                    "source": "swap"
                })

    # 5. Git Dangling Blobs (git fsck --lost-found)
    git_dir = os.path.join(workspace_root, ".git")
    if os.path.isdir(git_dir):
        try:
            lost_found_dir = os.path.join(git_dir, "lost-found", "other")
            if not os.path.isdir(lost_found_dir):
                subprocess.run(
                    ["git", "fsck", "--lost-found"],
                    cwd=workspace_root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5
                )

            if os.path.isdir(lost_found_dir):
                for blob_file in glob.glob(os.path.join(lost_found_dir, "*")):
                    try:
                        with open(blob_file, "rb") as bf:
                            blob_bytes = bf.read(1024 * 1024)
                            if filename.encode("utf-8") in blob_bytes:
                                found_locations.append({
                                    "type": "git_blob",
                                    "path": blob_file,
                                    "description": f"Git Dangling Blob: {blob_file}",
                                    "source": "git_lost_found"
                                })
                    except Exception:
                        pass
        except Exception:
            pass

    # 6. In-Transcript Tool Call Payload (Recoverable Ghost Code)
    if claim.get("content"):
        found_locations.append({
            "type": "session_payload",
            "path": f"<session-payload:{claim.get('session_id') or 'transcript'}>",
            "content": claim["content"],
            "description": f"Session Payload (in tool call: {claim['tool']})",
            "source": "transcript_payload"
        })

    return found_locations


# ---------------------------------------------------------------------------
# 4. Report Findings
# ---------------------------------------------------------------------------
def print_report_table(reconciled, workspace_root):
    """Prints a styled CLI reconciliation and recovery table."""
    print()
    print(bold("================================================================================="))
    print(bold(cyan("                  DEAD MAN'S SPIRIT (dms) - GHOST CODE RECOVERY REPORT          ")))
    print(bold("================================================================================="))
    print(f"{dim('Workspace:')} {bold(workspace_root)}")
    print(f"{dim('Inspected:')} {len(reconciled)} claimed writes")
    print("---------------------------------------------------------------------------------")

    col1_w = 34
    col2_w = 14
    col3_w = 12
    col4_w = 30

    header = f"{'CLAIMED PATH':<{col1_w}} | {'STATUS':<{col2_w}} | {'AGENT':<{col3_w}} | {'FOUND AT':<{col4_w}}"
    print(bold(header))
    print("-" * (col1_w + col2_w + col3_w + col4_w + 9))

    for item in reconciled:
        p = item["resolved_path"]
        try:
            disp_path = os.path.relpath(p, workspace_root)
            if disp_path.startswith(".."):
                disp_path = p
        except Exception:
            disp_path = p

        if len(disp_path) > col1_w:
            disp_path = "..." + disp_path[-(col1_w - 3):]

        agent_disp = item.get("agent", "?")
        if len(agent_disp) > col3_w:
            agent_disp = agent_disp[:col3_w - 3] + "..."

        graves = item.get("graves", [])
        if graves:
            g_desc = graves[0]["description"]
            if len(graves) > 1:
                g_desc += f" (+{len(graves) - 1} more)"
        else:
            g_desc = "None" if item["status"] != "OK" else "Workspace"

        if len(g_desc) > col4_w:
            g_desc = g_desc[:col4_w - 3] + "..."

        if item["status"] == "OK":
            st_text = green(f"{'✅ OK':<{col2_w}}")
        elif item["status"] == "STALE":
            st_text = yellow(f"{'⚠️ STALE':<{col2_w}}")
        else:
            st_text = red(f"{'❌ MISSING':<{col2_w}}")

        print(f"{disp_path:<{col1_w}} | {st_text} | {agent_disp:<{col3_w}} | {cyan(g_desc):<{col4_w}}")

    print("=================================================================================")
    print()


# ---------------------------------------------------------------------------
# 5. Support Recovery (--exhume)
# ---------------------------------------------------------------------------
def perform_exhume(reconciled, workspace_root, auto_confirm=False):
    """
    Exhumes ghost code by copying recovered files or writing payload back
    into the primary workspace. Prompts before overwriting existing files.
    """
    to_recover = [item for item in reconciled if item["status"] in ("MISSING", "STALE")]
    if not to_recover:
        print(green("✨ No ghost files to exhume! All claimed writes are ✅ OK."))
        return

    print(bold(cyan(f"Exhuming {len(to_recover)} ghost file(s) back into workspace: {workspace_root}\n")))
    recovered_count = 0

    for item in to_recover:
        dest_path = item["resolved_path"]
        graves = item.get("graves", [])
        if not graves:
            print(dim(f"[-] No grave found for: {dest_path} (cannot exhume)"))
            continue

        grave = graves[0]
        exists = os.path.exists(dest_path)

        if exists and not auto_confirm:
            answer = input(yellow(f"[?] File '{dest_path}' exists ({item['status']}). Overwrite? [y/N]: ")).strip().lower()
            if answer not in ("y", "yes"):
                print(dim(f"    Skipping '{dest_path}'"))
                continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        try:
            if grave.get("content") is not None:
                with open(dest_path, "w", encoding="utf-8") as df:
                    df.write(grave["content"])
                print(green(f"[+] Exhumed from {grave['description']} -> {dest_path}"))
                recovered_count += 1
            elif grave.get("path") and os.path.isfile(grave["path"]):
                shutil.copy2(grave["path"], dest_path)
                print(green(f"[+] Exhumed from {grave['path']} -> {dest_path}"))
                recovered_count += 1
            else:
                print(red(f"[!] Unable to extract content from {grave}"))
        except Exception as e:
            print(red(f"[!] Error exhuming {dest_path}: {e}"))

    print()
    print(bold(green(f"Exhumation complete. Recovered {recovered_count} ghost file(s).")))


# ---------------------------------------------------------------------------
# 5b. Live Watch Mode (--watch)
# ---------------------------------------------------------------------------
def run_watch_mode(profile_dir_pairs, workspace_root, all_base_dirs, profiles_by_name, interval=2.0):
    """
    Monitors session history files in real-time, across all active profiles.
    Alerts immediately whenever a claimed write does not land on the filesystem.
    """
    agent_list = ", ".join(p["name"] for p, _ in profile_dir_pairs)
    print(bold(cyan("Entering --watch mode. Monitoring session writes...")))
    print(dim(f"Workspace: {workspace_root} | Agents: {agent_list} | Polling interval: {interval}s (Press Ctrl+C to stop)\n"))

    seen_claims = set()
    initial_claims = parse_history(profile_dir_pairs, workspace_root)
    for c in initial_claims:
        seen_claims.add((c["resolved_path"], c["timestamp"].isoformat(), c["agent"]))

    try:
        while True:
            time.sleep(interval)
            current_claims = parse_history(profile_dir_pairs, workspace_root)
            new_claims = []
            for c in current_claims:
                key = (c["resolved_path"], c["timestamp"].isoformat(), c["agent"])
                if key not in seen_claims:
                    seen_claims.add(key)
                    new_claims.append(c)

            if new_claims:
                time.sleep(1.0)  # Grace pause for file flush
                reconciled = reconcile_claims(new_claims)
                for item in reconciled:
                    for g in search_graves(item, workspace_root, all_base_dirs, profiles_by_name):
                        item.setdefault("graves", []).append(g)

                    now_str = datetime.datetime.now().strftime("%H:%M:%S")
                    if item["status"] == "OK":
                        print(f"[{now_str}] {green('WRITE LANDED')}: {item['resolved_path']} (tool: {item['tool']}, agent: {item['agent']})")
                    else:
                        print(f"[{now_str}] {red('⚠️ GHOST CODE DETECTED!')} Status: {item['badge']}")
                        print(f"         Claimed Path: {bold(item['resolved_path'])}")
                        print(f"         Agent:        {item['agent']}")
                        print(f"         Tool Call:    {item['tool']} at {item['timestamp'].isoformat()}")
                        graves = item.get("graves", [])
                        if graves:
                            print(f"         Grave:        {cyan(graves[0]['description'])}")
                            print(dim("         Run './dms.py --exhume' to recover this file."))
                        else:
                            print(f"         Grave:        {red('No grave found yet.')}")
                        print()
    except KeyboardInterrupt:
        print("\n" + dim("Exiting watch mode."))


# ---------------------------------------------------------------------------
# CLI Argument Parsing & Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="dms",
        description="Dead Man's Spirit — Ghost Code Recovery CLI for any AI coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  ./dms.py
  ./dms.py --agent claude-code
  ./dms.py --json
  ./dms.py --exhume
  ./dms.py --watch
  ./dms.py --history-dir ~/.my-custom-agent --profile-json myagent.json
"""
    )
    parser.add_argument("--workspace", default=os.getcwd(), help="Primary workspace directory (default: cwd)")
    parser.add_argument("--history-dir", default=None, help="Custom session/history directory (used with --agent or --profile-json)")
    parser.add_argument("--agent", default=None,
                         help=f"Restrict to one built-in agent profile. Choices: {', '.join(sorted(BUILTIN_PROFILES.keys()))}. "
                              "Omit to auto-detect all known agents.")
    parser.add_argument("--profile-json", default=None,
                         help="Path to a JSON file describing a custom agent profile (tool names, path/content keys, glob patterns).")
    parser.add_argument("--list-agents", action="store_true", help="List built-in agent profiles and exit")
    parser.add_argument("--json", action="store_true", help="Print findings in machine-readable JSON format")
    parser.add_argument("--exhume", action="store_true", help="Copy recovered ghost files into primary workspace")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-confirm overwrite prompts during --exhume")
    parser.add_argument("--watch", action="store_true", help="Live monitor session history and alert on missing writes")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds for --watch")

    args = parser.parse_args()

    if args.list_agents:
        print(bold("Built-in agent profiles:\n"))
        for name, p in BUILTIN_PROFILES.items():
            if name == "generic":
                continue
            print(f"  {cyan(name):<20} {p.get('label', name)}")
        print(f"\n{dim('Use --agent <name> to restrict, or --history-dir + --profile-json for anything else.')}")
        return

    workspace_root = os.path.abspath(args.workspace)

    agent_name = args.agent
    if args.profile_json and not agent_name:
        # loading a raw custom profile with no builtin agent selected
        agent_name = None

    profiles = load_profiles(selected_agent=agent_name, profile_json_path=args.profile_json)

    if args.profile_json and args.agent is None:
        # If only --profile-json was given (no --agent), scan just the custom profile
        try:
            with open(args.profile_json, "r", encoding="utf-8") as f:
                custom_name = json.load(f).get("name", "custom")
        except Exception:
            custom_name = "custom"
        profiles = [p for p in profiles if p["name"] == custom_name] or profiles

    profile_dir_pairs = []
    for profile in profiles:
        dirs = resolve_profile_dirs(profile, override_dir=args.history_dir if (args.agent or args.profile_json) else None)
        if dirs:
            profile_dir_pairs.append((profile, dirs))

    if not profile_dir_pairs:
        if args.json:
            print(json.dumps({"error": "No agent session/history directory found", "records": []}))
        else:
            print(red("[!] Error: No agent CLI session directory found."))
            if args.agent:
                print(dim(f"    Checked profile '{args.agent}' default locations."))
            else:
                print(dim("    Checked all built-in agent profiles (see --list-agents)."))
            print(dim("    Use --history-dir (with --agent or --profile-json) to specify a custom location."))
        sys.exit(1)

    all_base_dirs = [d for _, dirs in profile_dir_pairs for d in dirs]
    profiles_by_name = {p["name"]: p for p, _ in profile_dir_pairs}

    if args.watch:
        run_watch_mode(profile_dir_pairs, workspace_root, all_base_dirs, profiles_by_name, interval=args.interval)
        return

    claimed_records = parse_history(profile_dir_pairs, workspace_root)
    reconciled = reconcile_claims(claimed_records)

    for item in reconciled:
        item["graves"] = search_graves(item, workspace_root, all_base_dirs, profiles_by_name)

    if args.json:
        serializable = []
        for r in reconciled:
            entry = {
                "claimed_path": r["claimed_path"],
                "resolved_path": r["resolved_path"],
                "status": r["status"],
                "tool": r["tool"],
                "agent": r.get("agent"),
                "timestamp": r["timestamp"].isoformat(),
                "file_mtime": r.get("file_mtime"),
                "graves": [
                    {
                        "type": g.get("type"),
                        "path": g.get("path"),
                        "description": g.get("description"),
                        "source": g.get("source"),
                        "has_content": bool(g.get("content"))
                    }
                    for g in r.get("graves", [])
                ]
            }
            serializable.append(entry)
        print(json.dumps(serializable, indent=2))
        return

    if args.exhume:
        perform_exhume(reconciled, workspace_root, auto_confirm=args.yes)
        return

    print_report_table(reconciled, workspace_root)

    ghosts = [r for r in reconciled if r["status"] in ("MISSING", "STALE")]
    if ghosts:
        recoverable = [r for r in ghosts if r.get("graves")]
        print(yellow(f"Found {len(ghosts)} ghost file(s) ({len(recoverable)} recoverable in graves)."))
        print(cyan("To restore them to your workspace, run:") + bold(' ./dms.py --exhume\n'))


if __name__ == "__main__":
    main()
