# DLMS — Dynamic Living Memory Substrate

A project-agnostic memory layer for coding agents. Deepest integration with
Claude Code (hooks + skills); the MCP tools work with any MCP-capable agent
(Cursor, Windsurf, Cline, Continue, Claude Desktop) — see
[Connecting other agents](#connecting-other-agents).

Drop into any repo. Claude Code learns the project's invariants, decisions, and
connections — and stays oriented across sessions without re-reading the codebase
or bloating context.

## Why

Coding agents forget. Every session starts cold, so they re-read files to rebuild
context — which burns tokens and, on large or mature codebases, hits the context
ceiling before any real work happens. DLMS gives the agent a persistent, typed,
self-invalidating memory so it can answer "what's true about this project" from a
graph of facts instead of re-reading source every time.

## How it works

DLMS stores knowledge as **typed atoms** (invariants, schema facts, decisions,
owners, …) in a local SQLite graph, with **bi-temporal validity** and **liveness
predicates** that auto-expire facts when the code they describe changes. Retrieval
walks that graph with **Personalized PageRank** to surface the most relevant
connected facts for the current query — no LLM call in the hot path.

The PageRank-over-knowledge-graph retrieval core is the same insight behind
[HippoRAG](https://arxiv.org/abs/2405.14831) (NeurIPS 2024), which showed single-step
PPR retrieval can match iterative RAG while being far cheaper and faster. DLMS is an
independent, from-scratch implementation of that idea, re-engineered as a *live*
memory layer for a coding agent: typed atoms, liveness-based invalidation, and tight
Claude Code hook integration — none of which HippoRAG (a static document index) has.
It is not a fork or derivative of the HippoRAG codebase.

## Quick start

```bash
cd your-repo
dlms init --all            # writes dlms.toml, creates .dlms/, then ingests + embeds
dlms status                # show atom count, last indexed SHA
```

`dlms init --all` (short flag `-a`) is the one-shot setup — it chains
`init` → `ingest` → `embed` so the substrate is ready immediately. Prefer the
step-by-step form if you want to inspect each stage:

```bash
dlms init                  # writes dlms.toml + creates .dlms/ (schema applied)
dlms ingest                # first-time scan (~60s default budget; raise for big repos)
dlms embed                 # build local embedding vectors
dlms status                # show atom count, last indexed SHA
```

Claude Code picks up the substrate automatically via:
- SessionStart hook (digest injection)
- UserPromptSubmit hook (per-turn relevant atoms)
- PostToolUse hook (invariant-watcher)
- PreCompact hook (handoff generation)
- The `dlms` MCP server (`query_facts`, `assert_fact`, etc.)

## Skills

- `/dlms:tweak` — tiny changes (≤2 files, no schema/auth) with KB-guided edits
- `/dlms:patch` — multi-file changes with sub-agent verification
- `/dlms:check` — invariant + test impact audit against staged diff
- `/dlms:trace` — "where does X live, who writes Y, what closed decisions block Z"
- `/dlms:handoff` — write a resumption handoff before context runs out
- `/dlms:resume` — restore the last handoff in a fresh session

## Connecting other agents

The `dlms` MCP server speaks standard [Model Context Protocol](https://modelcontextprotocol.io),
so any MCP-capable agent can use its tools — `query_facts`, `assert_fact`,
`supersede`, `graph_neighbors`, `embed_status`, `ping`. Install the extra
(`pip install 'dlms[mcp]'`) and point your client at the `dlms mcp` command.

What's portable vs. Claude-Code-specific:
- **Portable (any MCP client):** the substrate tools above — query the graph,
  write facts back, walk edges.
- **Claude Code only (for now):** the *ambient* layer — SessionStart digest,
  per-turn injection, PostToolUse liveness watcher, PreCompact handoff, the
  Memory Pulse statusline, and the `/dlms:*` skills. Other agents get the
  tools; they call them explicitly rather than getting auto-injected context.

**Local clients (stdio)** — they spawn the server as a subprocess. Example
`mcpServers` entry (Claude Desktop, Cursor, Windsurf, Cline, Continue all use
this shape):

```jsonc
{
  "mcpServers": {
    "dlms": {
      "command": "dlms",
      "args": ["mcp"],
      // run from your project root so it finds that repo's .dlms/
      "cwd": "/path/to/your-repo"
    }
  }
}
```

See [`examples/mcp/`](examples/mcp/) for ready-to-edit config files per client.

**Remote / web agents (http or sse)** — serve over a port instead:

```bash
dlms mcp --transport http --host 127.0.0.1 --port 8765
```

then point the client's MCP URL at `http://127.0.0.1:8765`.

## Maintenance

```bash
dlms doctor                # health report: schema, liveness, embeddings, edges, drift
dlms migrate               # apply pending schema migrations (idempotent)
```

`dlms doctor` surfaces silent rot (schema drift, stale liveness predicates,
mixed embedding models) before it turns into a wrong answer. If it reports a
schema-version gap, `dlms migrate` brings the DB up to date. Schema migrations
also run automatically whenever the store is opened, so read commands never
trip over a column added by a newer version.

## See

- [SPEC.md](SPEC.md) — design decisions, atom taxonomy, OODAR loop
- `dlms.toml.example` — config schema with comments

## License

MIT — see [LICENSE](LICENSE).
