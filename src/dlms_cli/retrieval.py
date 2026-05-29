"""Personalized PageRank retrieval over the typed atom graph (SPEC §4).

The retrieval contract is HippoRAG-style: a query produces a *seed set*
(top-K embedding hits plus structural hits like atoms LOCATED_IN the
current file). We then run Personalized PageRank with α=0.15, 20
iterations, using a *kind-weighted* transition matrix so high-signal
edges (MIRRORS, IMPLEMENTS) carry more probability mass than weak ones
(REFERENCES, CO_CHANGED). Results are ranked by graph-flow, surfacing
BLOCKS and MIRRORS partners that pure k-NN misses.

Each result carries a provenance trail (depth + via_kind + via_edge)
so the caller can render the "Surfaced because 2 hops from
auth_service.py via MIRRORS" citation line from SPEC §4.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import embeddings, liveness

# Kind-weighted transition mass (SPEC §4). Higher = more probability flows
# along this edge kind during PPR.
EDGE_WEIGHTS: dict[str, float] = {
    "MIRRORS":     1.0,
    "IMPLEMENTS":  0.9,
    "SUPERSEDES":  0.7,
    "CONTRADICTS": 0.8,
    "BLOCKS":      0.9,
    "DERIVED_FROM": 0.6,
    "ROLLS_UP":    0.6,   # SPEC §14.1 — module↔child hierarchy mass
    "REFERENCES":  0.5,
    "LOCATED_IN":  0.6,
    "DEPENDS_ON":  0.5,
    "OWNS":        0.4,
    "CO_CHANGED":  0.4,
}

ALPHA = 0.15  # teleportation probability — SPEC §4
ITERATIONS = 20
DRILL_MODULES = 3  # SPEC §14.1 — how many hot modules to drill into
MAX_HOPS = 3       # SPEC §14.4 — cap BFS depth so a hub can't pull in the graph
EPSILON = 1e-6     # SPEC §14.4 — PPR early-exit when the max rank delta < ε
DECAY_HALF_LIFE_DAYS = 90.0  # SPEC §14.5 — score halves every half-life of staleness
VERIFY_FRESHNESS_SECONDS = 3600  # SPEC §14.5 — trust a liveness check newer than this


@dataclass
class Retrieved:
    atom_id: str
    score: float
    depth: int
    via_kind: str | None
    via_edge_id: int | None
    seed: bool  # was this atom in the seed set?

    def trail(self) -> str:
        if self.seed:
            return "seed"
        if self.via_kind:
            return f"{self.depth}-hop via {self.via_kind}"
        return f"{self.depth}-hop"


def _seed_set(
    conn: sqlite3.Connection,
    *,
    query: str,
    file_context: list[str] | None,
    k: int,
    embedder: embeddings.Embedder | None,
) -> dict[str, float]:
    """Build the personalization vector — atom_id → probability mass."""
    seeds: dict[str, float] = {}

    # 1) Embedding hits — top-K by cosine, weighted by similarity.
    if embedder is not None:
        qvec = embedder.embed(query)
        for atom_id, sim in embeddings.top_k(conn, qvec, k=k):
            if sim > 0:
                seeds[atom_id] = max(seeds.get(atom_id, 0.0), sim)

    # 2) Structural hits — atoms LOCATED_IN any provided file path. We use
    # the atom's `source_ref` as a coarse proxy since LOCATED_IN edges are
    # populated by the post-ingest edge-discovery pass (not yet wired).
    if file_context:
        placeholders = ",".join("?" for _ in file_context)
        rows = conn.execute(
            f"SELECT id FROM live_atoms WHERE source_ref IN ({placeholders})",
            file_context,
        ).fetchall()
        for r in rows:
            seeds[r["id"]] = max(seeds.get(r["id"], 0.0), 1.0)

    if not seeds:
        return seeds
    total = sum(seeds.values()) or 1.0
    return {k_: v / total for k_, v in seeds.items()}


def _load_edges(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str, int, float]]]:
    """Adjacency list: src → [(dst, kind, edge_id, weight*edge_kind_weight)].

    Symmetric edges add both directions. Edge ``weight`` is multiplied by
    the kind weight so a low-confidence MIRRORS edge still beats a high-
    confidence CO_CHANGED edge.
    """
    out: dict[str, list[tuple[str, str, int, float]]] = {}
    rows = conn.execute(
        """SELECT id, src_id, dst_id, kind, directed, weight
             FROM edges
            WHERE valid_to IS NULL AND status = 'live'"""
    ).fetchall()
    for r in rows:
        kind_w = EDGE_WEIGHTS.get(r["kind"], 0.3)
        w = r["weight"] * kind_w
        out.setdefault(r["src_id"], []).append((r["dst_id"], r["kind"], r["id"], w))
        if not r["directed"]:
            out.setdefault(r["dst_id"], []).append((r["src_id"], r["kind"], r["id"], w))
    # Row-normalize so each row sums to 1.
    for src, neighbors in out.items():
        total = sum(w for _, _, _, w in neighbors) or 1.0
        out[src] = [(d, k, eid, w / total) for d, k, eid, w in neighbors]
    return out


def _forward_reachable(
    seeds: dict[str, float],
    adj: dict[str, list[tuple[str, str, int, float]]],
    *,
    max_hops: int = MAX_HOPS,
) -> set[str]:
    """Universe = seeds ∪ everything within `max_hops` live-edge hops of them.

    The hop cap (SPEC §14.4) keeps the candidate universe bounded regardless of
    repo size — a high-degree hub can't drag the whole graph into the PPR.
    """
    universe: set[str] = set(seeds)
    frontier = list(seeds)
    hops = 0
    while frontier and hops < max_hops:
        hops += 1
        nxt: list[str] = []
        for node in frontier:
            for dst, *_ in adj.get(node, ()):
                if dst not in universe:
                    universe.add(dst)
                    nxt.append(dst)
        frontier = nxt
    return universe


def _run_ppr(
    universe: set[str],
    seeds: dict[str, float],
    adj: dict[str, list[tuple[str, str, int, float]]],
    *,
    alpha: float,
    iterations: int,
    epsilon: float = EPSILON,
) -> dict[str, float]:
    """Personalized PageRank power iteration over `universe`. Dangling mass
    redistributes to the personalization (seed) vector. Stops early once the
    largest per-node rank delta falls below `epsilon` (SPEC §14.4) — typically
    well before `iterations`, since PPR converges geometrically."""
    rank: dict[str, float] = {a: seeds.get(a, 0.0) for a in universe}
    for _ in range(iterations):
        nxt: dict[str, float] = {a: alpha * seeds.get(a, 0.0) for a in universe}
        leak = 0.0
        for node in universe:
            neighbors = adj.get(node, ())
            mass = rank[node] * (1 - alpha)
            if not neighbors:
                leak += mass
                continue
            for dst, _kind, _eid, w in neighbors:
                if dst in nxt:
                    nxt[dst] += mass * w
        if leak:
            for s, p in seeds.items():
                nxt[s] = nxt.get(s, 0.0) + leak * p
        delta = max((abs(nxt[a] - rank[a]) for a in universe), default=0.0)
        rank = nxt
        if delta < epsilon:
            break
    return rank


def _hot_modules(
    conn: sqlite3.Connection, universe: set[str], rank: dict[str, float], k: int
) -> list[str]:
    """The top-`k` module-tier atoms in `universe` by PPR score (score > 0).

    These are the subsystems the query's mass concentrated on — the only ones
    worth drilling into (SPEC §14.1). Empty when the store has no module atoms,
    which makes drill-down a no-op (full backward compatibility).
    """
    if not universe:
        return []
    ph = ",".join("?" for _ in universe)
    module_ids = {
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM atoms WHERE tier = 'module' AND id IN ({ph})",
            list(universe),
        )
    }
    ranked = sorted(
        ((aid, rank.get(aid, 0.0)) for aid in module_ids if rank.get(aid, 0.0) > 0),
        # Secondary key (atom id) breaks score ties deterministically — set
        # iteration order is hash-randomized, so score-only sort would be flaky.
        key=lambda t: (t[1], t[0]),
        reverse=True,
    )
    return [aid for aid, _ in ranked[:k]]


def _module_children(
    conn: sqlite3.Connection, module_ids: list[str]
) -> list[tuple[str, str, int]]:
    """(module_id, child_id, edge_id) for live ROLLS_UP edges into `module_ids`.

    ROLLS_UP is directed child→module, so children are the *sources* of edges
    whose destination is a hot module — the reverse lookup forward BFS misses.
    """
    if not module_ids:
        return []
    ph = ",".join("?" for _ in module_ids)
    rows = conn.execute(
        f"""SELECT id, src_id, dst_id FROM edges
             WHERE dst_id IN ({ph}) AND kind = 'ROLLS_UP'
               AND valid_to IS NULL AND status = 'live'""",
        module_ids,
    ).fetchall()
    return [(r["dst_id"], r["src_id"], r["id"]) for r in rows]


def _augment_downward(
    adj: dict[str, list[tuple[str, str, int, float]]],
    triples: list[tuple[str, str, int]],
) -> dict[str, list[tuple[str, str, int, float]]]:
    """Return a copy of `adj` with downward module→child ROLLS_UP edges added.

    Base ROLLS_UP is directed child→module (mass flows up). To let a *hot*
    module's mass reach its children during drill-down (SPEC §14.1), we add the
    reverse edge for those modules only and re-normalize the affected rows so
    the row stays a probability distribution.
    """
    rolls_w = EDGE_WEIGHTS["ROLLS_UP"]
    by_module: dict[str, list[tuple[str, int]]] = {}
    for module, child, eid in triples:
        by_module.setdefault(module, []).append((child, eid))

    out = dict(adj)
    for module, kids in by_module.items():
        combined = list(adj.get(module, ())) + [
            (child, "ROLLS_UP", eid, rolls_w) for child, eid in kids
        ]
        total = sum(w for *_, w in combined) or 1.0
        out[module] = [(d, k, e, w / total) for d, k, e, w in combined]
    return out


def _liveness_meta(
    conn: sqlite3.Connection, ids: list[str]
) -> dict[str, tuple[str | None, int | None]]:
    """(liveness_kind, liveness_last_ok) for each id — one query, no I/O."""
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    return {
        r["id"]: (r["liveness_kind"], r["liveness_last_ok"])
        for r in conn.execute(
            f"SELECT id, liveness_kind, liveness_last_ok FROM atoms WHERE id IN ({ph})",
            ids,
        )
    }


def _is_live_now(
    conn: sqlite3.Connection,
    atom_id: str,
    meta: tuple[str | None, int | None] | None,
    *,
    repo_root: Path,
    now: int,
    freshness: int,
) -> bool:
    """Lazy read-time liveness (SPEC §14.5). Returns True if the atom may be
    surfaced. No predicate → live. A check newer than `freshness` is trusted
    without re-running. Otherwise re-run the predicate and bump `last_ok` on
    pass. An un-runnable predicate (ast/sql not yet implemented) is *kept* —
    "can't verify" is not "verified dead"; only a predicate that actually fails
    drops the atom."""
    kind, last_ok = meta if meta else (None, None)
    if kind is None or kind == "none":
        return True
    if last_ok is not None and (now - last_ok) < freshness:
        return True
    try:
        return liveness.check_atom(conn, atom_id, repo_root=repo_root).ok
    except (NotImplementedError, ValueError, KeyError):
        return True


def _apply_decay(
    conn: sqlite3.Connection,
    rank: dict[str, float],
    *,
    half_life_days: float,
    now: int,
) -> dict[str, float]:
    """Down-rank stale / low-confidence atoms (SPEC §14.5).

    Each score is multiplied by ``confidence × 0.5^(age / half_life)`` where age
    is seconds since the atom was last reconfirmed (liveness_last_ok, else
    updated_at, else asserted_at). Time-weighted, never zeroed by age alone — a
    fact just sinks as it goes unconfirmed; only confidence=0 removes it.
    """
    if not rank or half_life_days <= 0:
        return rank
    ids = list(rank)
    ph = ",".join("?" for _ in ids)
    meta = {
        r["id"]: (r["confidence"], r["ts"])
        for r in conn.execute(
            f"""SELECT id, confidence,
                       COALESCE(liveness_last_ok, updated_at, asserted_at) AS ts
                  FROM atoms WHERE id IN ({ph})""",
            ids,
        )
    }
    hl_secs = half_life_days * 86_400
    adjusted: dict[str, float] = {}
    for aid, score in rank.items():
        conf, ts = meta.get(aid, (1.0, now))
        age = max(0, now - (ts if ts is not None else now))
        adjusted[aid] = score * conf * (0.5 ** (age / hl_secs))
    return adjusted


def retrieve(
    conn: sqlite3.Connection,
    *,
    query: str,
    file_context: list[str] | None = None,
    embedder: embeddings.Embedder | None = None,
    seed_k: int = 10,
    top_n: int = 20,
    alpha: float = ALPHA,
    iterations: int = ITERATIONS,
    drill_down: bool = True,
    drill_modules: int = DRILL_MODULES,
    max_hops: int = MAX_HOPS,
    decay: bool = True,
    decay_half_life_days: float = DECAY_HALF_LIFE_DAYS,
    repo_root: Path | None = None,
    verify_live: bool = True,
    verify_freshness_seconds: int = VERIFY_FRESHNESS_SECONDS,
) -> list[Retrieved]:
    """Run PPR seeded on query + file context. Return top_n atoms.

    Retrieval is tiered (SPEC §14.1): a coarse PPR pass ranks the seeds'
    reachable component (including parent modules); then, when `drill_down`
    is set, the top `drill_modules` modules by mass are expanded into their
    children and the rank is recomputed over the larger universe. With no
    module atoms in the store this is a no-op and behaves as a single pass.

    If no seeds emerge (empty store, no embeddings yet), returns the
    most-recently-asserted live atoms as a degenerate fallback.

    Lazy liveness (SPEC §14.5): when `verify_live` and a `repo_root` is given,
    each atom is verified *as it is emitted* — predicates whose last check is
    stale are re-run, and an atom whose predicate now fails is dropped (we
    never return a fact we can't verify live). This is bounded: verification
    stops once `top_n` live atoms are collected, never scanning the whole store.
    With no `repo_root` (e.g. unit tests, ranking-only callers) verification is
    skipped and the result is identical to the pre-§14.5 behaviour.
    """
    embedder = embedder or embeddings.default_embedder()
    seeds = _seed_set(
        conn, query=query, file_context=file_context, k=seed_k, embedder=embedder
    )
    if not seeds:
        rows = conn.execute(
            "SELECT id FROM live_atoms ORDER BY asserted_at DESC LIMIT ?",
            (top_n,),
        ).fetchall()
        return [
            Retrieved(atom_id=r["id"], score=0.0, depth=0, via_kind=None,
                      via_edge_id=None, seed=False)
            for r in rows
        ]

    adj = _load_edges(conn)

    # Pass 1 (coarse): PPR over the seeds' forward-reachable component. Because
    # ROLLS_UP is directed child→module, this reaches parent modules but not a
    # module's *other* children — keeping the coarse universe bounded.
    universe = _forward_reachable(seeds, adj, max_hops=max_hops)
    rank = _run_ppr(universe, seeds, adj, alpha=alpha, iterations=iterations)

    # Pass 2 (drill-down, SPEC §14.1): for the modules the query's mass
    # concentrated on, pull their children into the universe AND flow mass
    # downward into them (base ROLLS_UP only points up), then re-rank.
    if drill_down:
        hot = _hot_modules(conn, universe, rank, drill_modules)
        if hot:
            triples = _module_children(conn, hot)
            child_ids = {child for _, child, _ in triples}
            if child_ids - universe:
                universe |= child_ids
                adj = _augment_downward(adj, triples)
                rank = _run_ppr(universe, seeds, adj, alpha=alpha, iterations=iterations)

    # Provenance: for each atom, pick the best (highest-flow) incoming edge.
    best_in: dict[str, tuple[str, int, int]] = {}  # atom_id -> (kind, edge_id, depth)
    # BFS from seeds to determine depth + best edge.
    visited = {s: (0, None, None) for s in seeds}
    queue: list[str] = list(seeds)
    while queue:
        node = queue.pop(0)
        depth, _, _ = visited[node]
        if depth >= max_hops:  # provenance is only needed for in-universe atoms
            continue
        for dst, kind, eid, _ in adj.get(node, ()):
            if dst not in visited:
                visited[dst] = (depth + 1, kind, eid)
                queue.append(dst)
                best_in[dst] = (kind, eid, depth + 1)

    # Confidence/staleness decay (SPEC §14.5) — applied to final scores before
    # the top-n cut so unconfirmed or low-confidence atoms sink in ranking.
    if decay:
        rank = _apply_decay(
            conn, rank, half_life_days=decay_half_life_days, now=int(time.time())
        )

    out = sorted(rank.items(), key=lambda t: t[1], reverse=True)
    # Lazy read-time liveness (SPEC §14.5). Only active when a repo_root is
    # supplied; metadata is fetched once and predicates re-run lazily as atoms
    # are emitted. We iterate the full ranked list but stop at top_n live
    # results, so a dead atom is replaced by the next-best live one without an
    # eager global scan.
    verify = verify_live and repo_root is not None
    live_meta = _liveness_meta(conn, [aid for aid, _ in out]) if verify else {}
    now_ts = int(time.time())
    results: list[Retrieved] = []
    for atom_id, score in out:
        if score <= 0:
            continue
        if len(results) >= top_n:
            break
        if verify and not _is_live_now(
            conn, atom_id, live_meta.get(atom_id),
            repo_root=repo_root, now=now_ts, freshness=verify_freshness_seconds,
        ):
            continue
        if atom_id in seeds:
            results.append(
                Retrieved(atom_id=atom_id, score=score, depth=0, via_kind=None,
                          via_edge_id=None, seed=True)
            )
        else:
            kind, eid, depth = best_in.get(atom_id, (None, None, 0))
            results.append(
                Retrieved(atom_id=atom_id, score=score, depth=depth,
                          via_kind=kind, via_edge_id=eid, seed=False)
            )
    return results


__all__ = ["EDGE_WEIGHTS", "Retrieved", "retrieve"]
