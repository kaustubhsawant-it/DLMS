# DLMS — Dynamic Living Memory Substrate

A project-agnostic, bi-temporal knowledge substrate for Claude Code. Designed for
maintenance-phase development of multi-repo systems. Drop-in portable.

This document is the single source of truth for design decisions. It is updated
in place as decisions firm up. Older revisions live in git history.

Status: design locked, build in progress (2026-05-22).

---

## 1. Philosophy

Maintenance is a control loop, not a project. The user is past the build phase
on a working multi-app system; the bottleneck is **orientation** (where to
change), not **specification** (what to build). DLMS exists to make Claude an
oriented collaborator from token zero.

Five sentences that define the system:

1. **Commits are atoms** — ingestion unit, not files.
2. **BM25 is bread, embeddings are luxuries** — embed only summaries.
3. **Tree-sitter is the universal parser** — 200+ languages, no per-project setup.
4. **Memory is JSONL on git** — teammates pull memory like code.
5. **Atoms come in three sizes** — hook picks the size that fits the budget.

## 2. The OODAR loop

One *tick* = one OODAR cycle = one commit, target <30 minutes.

| Phase | Time | Action |
|---|---|---|
| Observe | ~2m | git state, recent atoms, drift detector |
| Orient | ~5-10m | KB query — invariants, decisions, owners (load-bearing) |
| Decide | ~2m | smallest change that achieves intent |
| Act | ~10-15m | one atomic commit |
| Verify | ~3-15m | run the app, not just tests (UI ticks are longer) |
| **Remember** | ~2m | **write atom back to KB — the engine that makes the system compound** |

Skip Orient → break invariants. Skip Remember → KB rots. Both are hard gates.

## 3. Atom taxonomy (closed, 9 types)

`invariant`, `schema_fact`, `decision` (OPEN/CLOSED/DEFERRED),
`convention`, `dependency`, `owner`, `runtime`, `build_recipe`, `glossary`.

Each atom has: provenance (source kind + ref), bi-temporal validity
(`valid_from`/`valid_to`), liveness predicate (grep/AST/SQL pattern that
re-validates the atom), multi-resolution summaries (10w/50w/250w), embedding.

## 4. Edge types (closed, 11 kinds)

```
MIRRORS       — service A writes col X ↔ service B reads col X (cross-repo schema coupling)
IMPLEMENTS    — code symbol fulfills invariant/decision
SUPERSEDES    — newer decision overrides older (directed DAG)
CONTRADICTS   — atoms disagree (symmetric, flagged for human)
LOCATED_IN    — atom pertains to file/dir path
REFERENCES    — atom mentions symbol/path (weaker than LOCATED_IN)
DEPENDS_ON    — runtime/build dependency
OWNS          — person/role → area/file (directed)
BLOCKS        — decision forbids change to target (directed)
CO_CHANGED    — empirical: edited in same commit ≥3 times
DERIVED_FROM  — summary 50w ← raw 250w; atom ← source doc (directed)
```

**Auto-discovery rules** (no manual wikilinks):
1. Shared file refs → `REFERENCES` (weight 0.6)
2. Same symbol cross-app → `MIRRORS` (weight 0.8)
3. Commit-couple ≥3 → `CO_CHANGED` (weight = min(1, count/10))
4. Embedding cosine ≥ 0.88 → **suggested** edge, requires LLM/human accept
5. LLM extraction during ingestion → typed edges with quoted evidence
6. `git blame` → `OWNS` edge from person to file

**Retrieval algorithm — Personalized PageRank (HippoRAG-style):**
Seed with top-K embedding hits + structural hits (atoms `LOCATED_IN` current
file). Run PPR with α=0.15, 20 iterations, kind-weighted transition matrix
(`MIRRORS`=1.0, `IMPLEMENTS`=0.9, `REFERENCES`=0.5, `CO_CHANGED`=0.4).
Returns atoms ranked by graph-flow, not raw similarity — surfaces `BLOCKS`
decisions and `MIRRORS` partners that pure k-NN misses.

**"Show me why" output**: every retrieved atom comes with its provenance trail
(hops + edge kinds + evidence). Claude cites verbatim:
*"Surfaced because 2 hops from auth_service.py via MIRRORS to the
session-token-rotation invariant (commit a33e92c)."*

**Visualization**: `dlms graph export --seed file.py --k 2 --format mermaid`
regenerates the Obsidian-feel view on demand, auto-built, typed edges colored
by kind.

## 5. Skills

| Skill | OODAR phase | Use when |
|---|---|---|
| `/dlms:tweak` | compressed cycle | ≤2 files, no schema/auth — typos, colors, copy |
| `/dlms:patch` | Orient+Decide+Act | multi-file, needs invariants. Spawns up to 3 sub-agents when complex |
| `/dlms:plan` | Orient+Decide | constrain plan before edits using invariants & decisions |
| `/dlms:check` | Verify+Remember | runs against staged diff. 2 sub-agents: invariant audit + test impact |
| `/dlms:trace` | Observe | KB introspection: "where does X live, who writes Y" |

Each skill ends with `Next:` line + edge cases enumerated (severity-tagged).

## 6. Hooks (Claude Code surfaces)

- `SessionStart` — digest: branch + dirty + recent + top-5 atoms + halt contract
- `UserPromptSubmit` — classify → route → inject (≤3K tokens per turn)
- `PostToolUse(Edit|Write)` — invariant-watcher (zero-cost happy path)
- `PreCompact` — generate handoff (per Team Q spec)

## 7. Token budget (Sonnet 200K, target 10% bootstrap = 20K)

| Layer | Tokens |
|---|---|
| Claude Code overhead | ~6K |
| SessionStart digest (skeleton + top symbols + invariants + decisions) | ~4K |
| Per-turn injection (capped) | ≤3K |
| **Bootstrap total** | **~13K (6.5% of Sonnet)** |

Scales free on Opus 1M (~1%). Holds at 1M LOC because top-200 PageRank
symbols capture ~80% of inbound-edge mass.

## 8. Branching + backup

- `main` always deployable. Short-lived branches off main, squash-merge.
- **Tags ARE stable** — no long-lived stable/develop branches.
- Submodules: pinned exact SHAs; never `branch=main` tracking.
- Per-app tags (`app-v1.2.3`), parent deploy-date tags (`deploy-2026-05-22`).

Backup layers (revised 2026-05-22, Supabase Pro deferred):
1. Git push every session (non-negotiable)
2. **External SSD — full disk image + project backups** (user's chosen layer)
3. `pg_dump` of Supabase to SSD weekly via cron — under Free-tier constraints,
   self-managed backups are the only DB recovery path
4. 1Password vault for secrets, printed recovery codes off-site

**Important caveat**: without Supabase Pro, there is no point-in-time recovery
and no Supabase-side daily snapshots beyond 7 days. User accepts this risk and
mitigates with weekly `pg_dump` + SSD storage. Document a *tested* restore
procedure (spin up scratch Supabase, restore dump, verify load) — untested
backups are not backups.

## 9. Handoff protocol (anti-hallucination)

**Triggers**: any of —
- `PreCompact` hook fires (harness reports ≥80% context)
- Skill exits cleanly (`tick_complete`)
- User invokes `/dlms:handoff`
- Halt-safety triggers (≥85% capacity)
- `Stop` hook with dirty files and no handoff this session (auto-stub)

**Storage**: `.dlms/handoffs/<UTC-ts>-<session_id>.md` + symlink `LATEST.md`.
Committed to git by default (tribal knowledge). `LATEST.md` is gitignored
(machine-local pointer).

**Schema** (YAML frontmatter + capped narrative):
```yaml
session_id, started_at, ended_at, ended_reason, context_pct_at_end
branch, last_commit_sha, dirty_files
current_tick: {id, title, status, phase}
atoms_consulted: [ids only — bodies fetched on demand]
atoms_drafted: [pending Remember-phase write]
open_questions: [things user hasn't answered]
next_action: [literal verb+target, one sentence]
failed_assumptions: [things that turned out wrong]
do_not_redo: [tried, didn't work — prevent loops]
edge_cases_pending: [from last patch's enumerator, unverified]
```

The narrative section is hard-capped at 400 tokens and lints out any sentence
recoverable from `git log -p` or an atom ID. If regenerable, deleted.

**Resumption (SessionStart logic)**:
```
if LATEST.md exists and age < 24h:
    inject "RESUMPTION BLOCK" (cap 2K tokens):
      current_tick, next_action, failed_assumptions, do_not_redo,
      atom IDs (not bodies), live `git diff --stat`
    marker <!-- DLMS-RESUME --> so Claude treats as authoritative
else:
    inject normal digest
```

**Halt-before-hallucination (≥85% capacity)**:
Agentic skills enter safe-halt: tool calls blocked except write-handoff +
git status + atom-draft. Skill emits:
`HALT: context 86%. Wrote handoff <path>. Resume with /dlms:resume in fresh session.`
Read-only skills may continue but refuse new atom drafts. Override:
`--force-continue` (logged; user owns risk).

**StatusLine** shows `ctx 72% | tick TICK-114 | atoms 4` so pressure is visible.

## 10. Sub-agent teams within skills

| Skill | Default | Spawn when… | Cap |
|---|---|---|---|
| `:tweak` | 0 | NEVER (single-context only) | 0 |
| `:patch` | 0 | repos_touched>1 OR schema_changed OR diff>100 LOC OR cross_cutting_hits≥3 | 3 |
| `:plan` | 0 | NEVER (planning only) | 0 |
| `:check` | 2 | ALWAYS (invariant audit + test impact) | 2 |
| `:trace` | N | one per workspace root matching query | 5 |
| any | +1 | `--deep` flag | — |

**Team shapes:**

`:patch` complex:
- **A1 Implementer** — primary repo only, writes diff
- **A2 Cross-repo verifier** — partitioned to OTHER repos, checks MIRRORS-edges
- **A3 Edge-case enumerator** — runs LAST, sees staged diff + 2-hop neighborhood

Spawn order: A1 → (A2 ‖ A3 in parallel after staging). Partition by repo to
prevent file-collision.

`:check`:
- **B1 Invariant auditor** — verifies each invariant on touched atoms post-diff
- **B2 Test impact analyzer** — maps touched lines → covering tests, flags gaps

`:trace`:
- **C1..Cn** — one per workspace root, each searches independently, parent
merges by symbol

**Sub-agent JSON contract** (parent enforces; reject finding if violated):
```json
{
  "agent": "edge_enumerator",
  "scope": "<path or repo>",
  "findings": [{
    "kind": "boundary_input|race|cross_repo_impact|schema_migration|auth_permission|error_path|backwards_compat|perf_hotpath|invariant_risk|test_gap",
    "severity": "high|med|low",
    "claim": "<one sentence>",
    "evidence": ["file:line", "atom_id"],
    "suggested_action": "<one sentence>"
  }]
}
```

**Validation gates**: every `evidence[]` resolves (file exists, line in range,
atom_id in DLMS) — fail = reject, don't demote. Dedupe by (kind, file,
line_range±5). If 0 findings survive validation, surface honestly: *"no edge
cases detected (N candidates rejected as unverifiable)."*

## 11. Edge-case enumeration (every change)

**Algorithm** (deterministic, runs as skill's penultimate step):
1. **Symbol diff** — parse patch, extract changed symbol names
2. **Caller scan** — ripgrep across project + sibling repos (per `.dlms/mirrors.yml`)
3. **Invariant proximity** — grep `// invariant:`, `assert`, schema constraints ±20 lines
4. **Test coverage gap** — per touched line, check if any test file references symbol
5. **Cross-repo mirrors** — consult atoms tagged `MIRRORS:<symbol>`
6. Rank by (callers × invariant proximity), keep top 5

**Pre-filter by file fingerprint** — CSS change skips auth/race/schema entirely.
Schema change mandates schema_migration + cross_repo_impact + backwards_compat.

**Output format** (each line ≤80 chars, severity-prefixed, max 5; rest collapsed
to `+N more, run :check --deep`):
```
✓ Patched: 3 files, +47 -12 across api, web
  Atoms touched: session.token_ttl, SessionService.refresh
  Agents: implementer, cross-repo (1 finding), edge-enum (3)

Edge cases to consider:
  • [high] web client caches refresh token in localStorage — verify rotation
    → web/src/services/session_service.ts:142
  • [med]  session_revocations RLS depends on user_id shape
    → check policy on table after migration
  • [low]  No test covers token_expired=true branch — smoke manually

Next: review staged diff, run /dlms:check before commit
```

Edge-case surfacing is **mandatory output**, not optional. Empty list says so
explicitly.

---

## 12. Runtime: Python 3.11+

**Decision (2026-05-22)**: the entire DLMS toolchain is Python. Rationale:

| Component | Why Python wins |
|---|---|
| Tree-sitter (200+ grammars) | `py-tree-sitter` + `tree_sitter_languages` — pre-built |
| Embedder (`bge-small-en-v1.5`) | `sentence-transformers` / `fastembed` first-class |
| Cross-encoder reranker | `sentence-transformers` cross-encoders native |
| sqlite-vss vector search | Python bindings stable |
| MCP server | `FastMCP` framework — clean stdio server in ~150 LOC |
| Reference impls (GraphRAG, HippoRAG, Letta, Mem0, Graphiti) | all Python |

Node was the only serious alternative (MCP servers there are slightly more
mature historically), but the Python ML stack asymmetry decides it. One
language, one codebase, one `uv` venv.

**Stack lock-in**:
- Python 3.11+ (match statements, native exception groups)
- `uv` for dependency management (10x faster than pip)
- `FastMCP` for the MCP server
- `sqlite-utils` + `sqlite-vss` for the store
- `sentence-transformers` for embed + rerank
- `tree-sitter` + `tree_sitter_languages` for AST
- `typer` for the CLI
- `pytest` for tests
- `ruff` for lint/format

## 13. Memory Pulse — the status indicator

A sibling status line beneath Claude Code's existing context widget. Five
glyphs, one line, ambient feedback:

```
DLMS │ 🧠 142 · ⏵Orient · 🟢→3 · ⚠ 1 stale · ✦ tick 3
```

| Glyph | Meaning | Source |
|---|---|---|
| `🧠 N`        | live atom count | `SELECT count(*) FROM live_atoms` |
| `⏵<phase>`   | current OODAR phase | tool pattern heuristic (see below) |
| `<color>→N` | atoms injected this turn | retrieval log; color = class |
| `⚠ N stale`  | atoms with failed liveness today | `liveness_last_ok < today` |
| `✦ tick N`   | OODAR ticks completed today | `git log --since=midnight --oneline | wc -l` |

**OODAR auto-detect heuristic** (uses last 5 tool calls in session):
- `Read|Grep|Glob` dominant → **Observe**
- MCP `query_facts|graph_neighbors` recent → **Orient**
- No tools, only assistant text → **Decide**
- `Edit|Write` recent → **Act**
- `Bash` recent (test/run pattern) → **Verify**
- `assert_fact|supersede` recent → **Remember**

**Color codes for injected-atoms class:**
- 🔵 navigational (`where is X`, `show me Y`)
- 🟢 semantic (`why does X`, `is it safe`)
- 🔴 contradictory (`but we said`, `didn't we decide`)
- 🟣 implementation (`add`, `fix`, `refactor`)

**Killer feature: `⚠ N stale`** — when an atom's liveness predicate fails
(column dropped, file renamed, function signature changed), the count flips
visibly. The user sees schema drift the moment it happens, not after Claude
hallucinates from a stale fact. This is what makes the substrate *visibly
alive*, not just "Claude has memory."

**Implementation**: `dlms statusline` command reads `.dlms/atoms.sqlite`
(read-only, sub-50ms), prints the line. Wired via Claude Code's `statusLine`
setting in `.claude/settings.json`:
```json
{ "statusLine": "dlms statusline --compact" }
```

**Refresh cadence**: on every assistant turn (cheap query). The `⚠` count
recomputes nightly during consolidation, not on every turn.

---

## 14. Scaling architecture (target: 1M LOC, no degradation)

**Core invariant: retrieval cost and per-turn context cost MUST be sublinear
in codebase size.** A 1M-LOC repo and a 10K-LOC repo should both return ~5
atoms in comparable time and token budget. We scale by bounding the working
set, not by growing it. Five pillars, in dependency order:

### 14.1 Hierarchical atoms (foundational — everything depends on this)
Atoms gain a `tier` dimension: `module` → `file` → `symbol`. A `module`-tier
atom summarizes an entire subsystem and holds `ROLLS_UP` edges to its children.
Retrieval starts at the coarsest tier whose atoms match the seed set, then
drills into a subsystem's `file`/`symbol` atoms only when the query's PPR mass
concentrates there. This bounds the candidate universe regardless of repo size.
- New edge kind: `ROLLS_UP` (child → parent tier). Weight 0.6.
- Ingestion emits module atoms from directory/package structure; symbol atoms
  remain lazy (materialized on first drill-down).

**Status: IMPLEMENTED (2026-05-27).** `tier` column + `ROLLS_UP` edge in
schema; `modules` ingester emits module atoms + roll-up edges; `retrieve()`
runs a coarse PPR pass, picks the top `drill_modules` hot modules, then flows
mass downward into their children and re-ranks (`_augment_downward`). No module
atoms → single-pass, fully backward compatible. Covered by `test_modules.py`
and `test_retrieval.py::test_drill_down_*`.

### 14.2 Incremental everything, keyed on git diffs
Never re-scan the world. The stored `last_indexed_sha` drives all maintenance:
- **Ingest**: only files in `git diff <last_sha>..HEAD` are re-parsed.
- **Embeddings**: only atoms whose `source_ref` changed are re-embedded
  (the `embedding_ledger.json` already tracks this).
- **Liveness**: PostToolUse watcher already scopes to touched files; extend the
  same diff-keying to the background sweep.
First ingest of a huge repo is slow *once*; steady state is O(diff), not O(repo).

**Status: Ingest IMPLEMENTED (2026-05-27).** `git_utils.changed_files_since`
unions committed diff + dirty working tree; `IngestContext.changed_files` /
`.is_changed` thread it through; `iter_files` iterates the changed set directly
(O(diff)); readme/manifest/modules gate on it; first run (no prior sha) → full
scan. Embeddings already skip-by-hash. Deletions (superseding atoms for removed
files) still rely on liveness — a dedicated reaper is a follow-up.

### 14.3 ANN vector index (replaces linear blob scan)
JSON-blob vectors with `top_k` linear cosine scan dies at ~10⁴ atoms. Swap in
`sqlite-vec` (stays inside the existing SQLite file — zero new infra; preserves
the "local-only, no network" property). hnswlib/faiss only if we outgrow it.
The `embeddings.top_k` contract stays; only its backend changes.

**Status: IMPLEMENTED (2026-05-27).** Optional `dlms[vec]` extra. Vectors are
mirrored into a `vec0` virtual table keyed by `vss_rowid`; `top_k` runs KNN
(L2→cosine on normalized vecs) only when the index is loaded AND fully synced
with the JSON sidecar, else falls back to the linear scan — so retrieval is
correct with or without the extension. `embed`/`embed_pending` keep the index
synced (`sync_vec_index` backfills JSON-only vectors). Covered by
`test_vec_index.py` (skipped without the extra) + a forced-fallback test.

### 14.4 Seed-localized, capped PPR
The current `retrieve()` already restricts the universe to the seeds' connected
component — formalize and harden this for scale:
- Cap hop depth (default 3) so a pathological hub can't pull the whole graph in.
- Precompute and cache PPR vectors for "hot" nodes (auth, schema, payment) and
  reuse them as priors.
- Keep the hand-rolled power iteration (no graph-lib dependency); add an early
  exit when rank deltas fall below ε.

**Status: hop-cap + ε early-exit IMPLEMENTED (2026-05-27).** `_forward_reachable`
caps BFS at `max_hops` (default 3); the provenance BFS is capped too; `_run_ppr`
breaks once the max per-node rank delta < `EPSILON`. Covered by hop-boundary
(parametrized 1/2/3 + default), epsilon-fires, and provenance-at-boundary tests.
Hot-node PPR-vector prior caching is a deferred optimization (needs a cache +
graph-change invalidation) — not required for the bounding guarantee.

**Documented bounds (audit, 2026-05-27):** (1) Retrieval recall is bounded to
atoms within `max_hops` of a seed — atoms farther out are intentionally dropped
(their PPR mass after ≥(1-α)^4 decay is rarely top-n anyway). `max_hops` is a
tunable `retrieve()` param for deep-graph callers. (2) ε early-exit guarantees
ranking stable up to O(ε/α) ≈ 1e-5 score ties, not bit-identical ordering —
negligible for `top_n`.

### 14.5 Confidence decay + conflict detection + lazy liveness
At scale, contradictory and stale atoms accumulate:
- **Decay**: down-rank atoms whose `confidence` hasn't been reconfirmed within a
  half-life window (time-weighted, not deleted).
- **Conflict**: when a new atom shares `topic_key` with a live atom but differs,
  raise a `CONTRADICTS` edge and surface it rather than silently superseding.
- **Lazy liveness**: verify an atom is live *at read time*, plus a background
  incremental revalidation sweep — never an eager global scan over millions.

**Status: Decay IMPLEMENTED (2026-05-27).** `retrieve()` multiplies each final
score by `confidence × 0.5^(age/half_life)` (`_apply_decay`, default half-life
90d) before the top-n cut, where age is since last reconfirmation
(liveness_last_ok → updated_at → asserted_at). Time-weighted, never zeroed by
age; opt-out via `decay=False`. Covered by stale-downrank, decay-off, and
low-confidence tests.

**Status: Conflict detection IMPLEMENTED (2026-05-27).** When `assert_fact`
auto-supersedes a live atom with a *divergent* claim on the same
`(type, topic_key, workspace_id)`, it raises a symmetric `CONTRADICTS` edge
(old↔new) and reports the closed ids on the returned atom's runtime-only
`conflicts` field — the supersede is surfaced, not silent. The single-live-atom
invariant is unchanged (the old atom is still closed); edges are created after
the atom txn commits (advisory, never fail the assert). Skipped for
`NATURAL_EVOLUTION_SOURCES` (commit/schema_snapshot/manifest), where supersede
is normal re-ingestion, and via `detect_conflict=False`. Covered by
divergent-supersede, natural-evolution-skip, opt-out, idempotent, and fresh-topic
tests.

**Status: Lazy liveness IMPLEMENTED (2026-05-27) — §14.5 COMPLETE.** `retrieve()`
verifies atoms *as it emits them* when given a `repo_root`: a predicate whose
`liveness_last_ok` is older than `verify_freshness_seconds` (default 1h) is
re-run, and an atom whose predicate now fails is dropped — we never return a
fact we can't verify live. Bounded: emission stops at `top_n` live atoms, so a
dead atom is replaced by the next-best live one, never an eager global scan.
Recently-checked and predicate-less atoms pass without I/O; un-runnable
predicates (ast/sql) are kept (can't-verify ≠ verified-dead). With no `repo_root`
(unit tests, ranking-only callers) verification is skipped — identical to prior
behaviour. The CLI `query` and MCP `query_facts` pass `repo_root=layout.root`.
Background `liveness.revalidate_sweep(repo_root, limit=50)` re-checks only the
`limit` stalest atoms (oldest `liveness_last_ok` first), walking the staleness
frontier forward across sweeps — never a global scan. Covered by drop-dead,
skip-without-root, trust-fresh, and sweep bounded/recheck/unrunnable/empty tests.

**Reliability principle:** never return a fact that cannot be verified live.
"I have no live fact for that, here's what I'd read" beats a hallucinated
invariant. This is the credibility moat — a memory layer that is ever
confidently wrong gets abandoned. Capacity is the symptom; trust is the product.

## 15. Planning mode — `/dlms:plan` (new skill)

DLMS lacks a planning surface; this adds one. `/dlms:plan <goal>` uses the
memory graph to constrain a plan *before* any edit:
1. Retrieve invariants, closed decisions, owners, and affected module-tier atoms
   for the goal.
2. Emit a **dependency-ordered task plan** where each task carries the
   constraints (invariants, schema facts) it must not violate.
3. Flag any naive approach that hits a closed decision (`BLOCKS`/`CONTRADICTS`)
   up front, instead of discovering it mid-patch.
4. Persist the plan itself as a `decision`-tier atom set, so planning history is
   queryable ("why did we choose this approach?").

Plans feed existing phase/execution workflows (e.g. GSD) as constraint context —
DLMS supplies the facts, it does not own execution.

## 16. Adoption pillars (path to industry standard)

Sections 14–15 make DLMS *reliable*; these make it *adopted*:
- **Agent-agnostic MCP.** The MCP server is the wedge — make `query_facts` /
  `assert_fact` consumable by Cursor, Windsurf, Cline, Copilot, not just Claude
  Code. The memory layer that works everywhere wins.
- **Tree-sitter symbol graphs.** Replace regex ingesters with tree-sitter for
  accurate, language-agnostic symbol atoms and edges on large polyglot repos.
- **Team sync protocol.** The shared `.dlms/atoms.sqlite` is committed (§8);
  formalize a merge/conflict resolution so two devs' memories combine cleanly.
- **Eval harness (non-negotiable for credibility).** A small multi-hop benchmark
  measuring retrieval quality + token cost vs naive grep vs full-file-read.
  Turns "comparable to HippoRAG" into a defensible chart, not a vibe.
- **Observability.** `dlms why <atom>` prints the full provenance trail — makes
  the no-hallucination claim demonstrable; the best demo surface.

Highest leverage if forced to pick two: **§14.1 hierarchical atoms** (or it
won't scale) and **§16 eval harness** (or no one will believe it scales).

## 17. Prior art & licensing (HippoRAG)

DLMS's retrieval core (§4) is an **independent implementation of the
HippoRAG idea** (Gutiérrez et al., *HippoRAG: Neurobiologically Inspired
Long-Term Memory for LLMs*, NeurIPS 2024, arXiv:2405.14831): seed a query into a
knowledge graph and rank by Personalized PageRank. It is **not a fork or
derivative of the HippoRAG codebase.** Verified divergence (checked 2026-05-27):

| | HippoRAG (`OSU-NLP-Group/HippoRAG`) | DLMS (`retrieval.py`) |
|---|---|---|
| PPR engine | `igraph.personalized_pagerank(..., implementation='prpack')` | hand-rolled power iteration, no graph lib |
| Graph store | in-memory `igraph.Graph` | typed atoms + edges in SQLite |
| Nodes | LLM-extracted entities / passages from documents | typed atoms (invariant, decision, …) |
| Edges | synonymy / fact / passage edges | 11 closed kind-weighted edge types (§4) |
| Domain | document QA / multi-hop retrieval | live code memory with liveness invalidation |
| Key symbols | `HippoRAG.run_ppr`, `graph_search_with_fact_entities` | `retrieve`, `_seed_set`, `_load_edges` |

**Licensing posture (both MIT):** HippoRAG is MIT (© 2025 OSU Natural Language
Processing); DLMS is MIT. Reimplementing a *published algorithm* in original
code is standard, legal practice — copyright protects expression (their source,
their text), not the idea. PageRank's original Stanford patent (US 6,285,999)
expired in 2019; Personalized PageRank is a long-standing academic variant. No
HippoRAG-specific patent surfaced in search (NeurIPS publication + MIT code
release signal no enforcement intent). Because no HippoRAG source was copied,
even the MIT attribution clause is not triggered — but we attribute the idea
anyway in good faith (README + this section). *Not legal advice; a patent
sanity-check and, if commercializing, a brief IP-attorney consult remain prudent.*

---

## Design status

All open questions resolved (2026-05-22). Spec is build-ready. Implementation
progresses task-by-task per the task list (`TaskList` in this session, or
`.dlms/state/tasks.json` in production). Sections 14–17 (scaling, planning,
adoption, prior art) added 2026-05-27 as the post-v1 roadmap — not yet
implemented; see §14.1 / §16 eval harness for the recommended starting points.
