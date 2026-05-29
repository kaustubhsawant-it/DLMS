# Installing DLMS on a new machine

DLMS = a `dlms` Python CLI (the engine) **+** a set of Claude Code skills
(`/dlms:*`). You need both. This bundle contains everything except the Python
dependencies, which install automatically from the internet.

## 0. Prerequisites

- **Python ≥ 3.11**
- **git** (DLMS ingests git history)
- **Claude Code** (the skills run inside it)
- One of: [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`

## 1. Install the `dlms` CLI

From inside this unzipped folder:

```bash
# Recommended — uv installs it as an isolated tool on your PATH:
uv tool install .                 # add  ".[mcp,embed]"  for the MCP server + embeddings

# …or with pip:
pip install .                     # add  ".[mcp,embed]"  for extras
```

Verify:

```bash
dlms --help        # should print the command list
```

> Optional extras:
> - `[mcp]`   → the FastMCP server (`dlms mcp`) so Claude can call `query_facts`, `assert_fact`, …
> - `[embed]` → sentence-transformers, for semantic embedding of atom summaries

## 2. Install the Claude Code skills

Copy the eight skill folders into your Claude skills directory:

```bash
mkdir -p ~/.claude/skills
cp -R skills/dlms skills/dlms-* ~/.claude/skills/
```

Restart Claude Code (or start a new session). `/dlms`, `/dlms:plan`,
`/dlms:patch`, `/dlms:check`, `/dlms:trace`, `/dlms:tweak`, `/dlms:handoff`,
and `/dlms:resume` should now be available.

## 3. Initialise DLMS in a repo

```bash
cd /path/to/your-repo
dlms init        # writes dlms.toml + creates .dlms/
dlms ingest      # first scan of git history + code (~60s default budget)
dlms embed       # optional — only if you installed the [embed] extra
dlms status      # confirms atom count + last indexed SHA
```

## 4. Wire the Claude Code hooks (recommended)

Add to the repo's `.claude/settings.json` so Claude stays oriented automatically:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "dlms digest"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "dlms route \"$CLAUDE_USER_PROMPT\""}]}],
    "PostToolUse":      [{"matcher": "Edit|Write|MultiEdit",
                          "hooks": [{"type": "command", "command": "dlms watch \"$CLAUDE_TOOL_FILES\""}]}],
    "PreCompact":       [{"hooks": [{"type": "command", "command": "dlms handoff --reason capacity_pre_compact"}]}]
  },
  "statusLine": "dlms statusline --compact"
}
```

For the MCP server, see `examples/mcp/stdio.mcp.json` and `examples/mcp/README.md`.

## What's in this bundle

| Path | What |
|---|---|
| `src/dlms_cli/` | The full Python engine (atom store, PPR retrieval, ingesters, MCP server, hooks) |
| `schema.sql` | SQLite schema for the atom graph |
| `skills/` | All 8 Claude Code skills (the `/dlms:*` commands) |
| `tests/` | Test suite — run `pytest` after a `[dev]` install to verify |
| `examples/mcp/` | Example MCP server wiring |
| `SPEC.md` | Full design: atom taxonomy, OODAR loop, liveness predicates |
| `README.md` | Overview + rationale |
| `dlms.toml.example` | Annotated config |
| `LICENSE` | MIT |

**Not included:** Python dependencies (installed automatically by `uv`/`pip`),
build caches, and any local `.dlms/` runtime state.

## Verify the install (optional)

```bash
uv tool install ".[dev]"     # or: pip install ".[dev]"
pytest                        # runs the bundled test suite
```
