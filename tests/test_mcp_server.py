"""Lightweight tests for the MCP server entry point.

We can't run the full FastMCP server in tests (it'd require stdin/stdout
wiring), so we exercise the import surface and the helper that the
server's tool handlers call into.
"""

from __future__ import annotations

import pytest

from dlms_cli import mcp_server


def test_build_server_requires_fastmcp_or_raises():
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="fastmcp"):
            mcp_server.build_server()
    else:
        srv = mcp_server.build_server()
        assert srv is not None


def test_supported_transports_are_agent_agnostic():
    # stdio for local clients; http/sse for remote/web agents.
    assert {"stdio", "http", "sse"} <= mcp_server.SUPPORTED_TRANSPORTS


def test_run_rejects_unknown_transport():
    # Transport is validated before FastMCP is imported, so this fails fast
    # with exit code 2 even when the `mcp` extra isn't installed.
    with pytest.raises(SystemExit) as exc:
        mcp_server.run("bogus")
    assert exc.value.code == 2


def test_atom_to_dict_minimal():
    from dlms_cli import atoms as atoms_mod

    class FakeAtom:
        id = "x_1"
        type = "invariant"
        topic_key = "t"
        decision_status = None
        source_kind = "manual"
        source_ref = None
        valid_from = 0
        confidence = 1.0
        repo_id = None

    view = atoms_mod.AtomView(atom=FakeAtom(), summaries={50: "hello"})
    out = mcp_server._atom_to_dict(view)
    assert out["id"] == "x_1"
    assert out["summary_50w"] == "hello"
