from dlms_cli.ids import atom_id, normalize


def test_normalize_collapses_whitespace_and_lowercases():
    assert normalize("  Hello   World  ") == "hello world"


def test_id_is_stable_for_same_inputs():
    a = atom_id("invariant", "auth.token.rotation", "API rotates session token; web re-reads it.")
    b = atom_id("invariant", "auth.token.rotation", "API rotates session token; web re-reads it.")
    assert a == b


def test_id_is_stable_under_whitespace_changes():
    a = atom_id("decision", "branching", "main + tags only")
    b = atom_id("decision", "branching", "Main + Tags  Only")
    assert a == b


def test_id_changes_with_claim():
    a = atom_id("decision", "branching", "main + tags only")
    b = atom_id("decision", "branching", "main + develop + tags")
    assert a != b


def test_id_is_prefixed_by_type():
    assert atom_id("invariant", "k", "v").startswith("invariant_")
    assert atom_id("decision", "k", "v").startswith("decision_")
