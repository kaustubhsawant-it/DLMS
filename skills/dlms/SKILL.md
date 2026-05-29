---
name: dlms
description: Living Memory Substrate for Claude Code — typed atom store, PPR retrieval, OODAR loop, halt-before-hallucination handoff. Use when the user asks about DLMS, mentions atoms/invariants/decisions/MIRRORS, or wants memory persisted across sessions. Sub-skills: dlms-tweak, dlms-patch, dlms-check, dlms-trace, dlms-handoff, dlms-resume.
---

# DLMS — Living Memory Substrate

DLMS is a project-agnostic memory substrate that makes Claude Code an
*oriented* collaborator from token zero. The full design is in
`<workspace>/dlms/SPEC.md` once installed. This skill is the umbrella
entry — sub-skills handle specific phases of the OODAR loop.

## Mental model

Five sentences:

1. **Commits are atoms** — ingestion unit, not files.
2. **BM25 is bread, embeddings are luxuries** — embed only summaries.
3. **Tree-sitter is the universal parser** — 200+ languages.
4. **Memory is JSONL on git** — teammates pull memory like code.
5. **Atoms come in three sizes** — hook picks the size that fits the budget.

## The skills

| Skill | When to use |
|---|---|
| `/dlms:tweak`   | Tiny change, ≤2 files, no schema/auth |
| `/dlms:patch`   | Multi-file change needing invariant context |
| `/dlms:check`   | Audit staged diff before commit |
| `/dlms:trace`   | KB introspection — "where does X live" |
| `/dlms:handoff` | Write resumption doc at session end |
| `/dlms:resume`  | Restore tick from LATEST.md, continue |

## CLI surface

```bash
dlms init              # install in workspace
dlms ingest            # scan with all adapters
dlms status            # workspace state
dlms embed             # (re-)embed live atom summaries
dlms query "<text>"    # PPR retrieval
dlms route "<prompt>"  # classify + retrieve (UserPromptSubmit hook)
dlms digest            # SessionStart bootstrap JSON
dlms watch <file>...   # PostToolUse invariant check
dlms handoff --next X  # PreCompact resumption doc
dlms statusline        # Memory Pulse glyph line
dlms enumerate         # edge-case enumerator (SPEC §11)
dlms mcp               # FastMCP stdio server (needs dlms[mcp])
```

## Quickstart for a new repo

```bash
cd <your-repo>
pip install /path/to/dlms          # or `pip install dlms[mcp,embed]`
dlms init
dlms ingest
dlms embed
dlms status   # confirms atoms + repos
```

Then wire the hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart":      [{"hooks": [{"type": "command", "command": "dlms digest"}]}],
    "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": "dlms route \"$CLAUDE_USER_PROMPT\""}]}],
    "PostToolUse":       [{"matcher": "Edit|Write|MultiEdit",
                           "hooks": [{"type": "command", "command": "dlms watch \"$CLAUDE_TOOL_FILES\""}]}],
    "PreCompact":        [{"hooks": [{"type": "command", "command": "dlms handoff --reason capacity_pre_compact"}]}]
  },
  "statusLine": "dlms statusline --compact"
}
```

## Halt-before-hallucination

At ≥85% context capacity, agentic skills (`patch`, `check`, `trace` with
sub-agents) enter safe-halt:
1. Write a handoff (`/dlms:handoff` or `dlms handoff --reason halt_safety`).
2. Stop. Refuse new tool calls except git status + atom-draft.
3. Resume in a fresh session via `/dlms:resume`.

Read-only operations may continue past the threshold but cannot draft
new atoms. Override with `--force-continue` (logged; user owns the risk).
