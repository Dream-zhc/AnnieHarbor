"""Phase 6 namespace contract: no implicit person identity or robot sharing."""

import pytest
from annie_brain.memory_scope import memory_namespace


def test_memory_namespace_fixed_scopes():
    assert memory_namespace("annie-a", None, "semantic") == ("memory", "annie-a", "semantic")
    assert memory_namespace("annie-a", "warm", "persona") == (
        "memory", "annie-a", "persona", "warm",
    )
    assert memory_namespace("annie-a", "warm", "episodes") == (
        "memory", "annie-a", "persona", "warm", "episodes",
    )


def test_memory_namespace_preserves_exact_identity_segments():
    ids = ("a", "ab", "a/b", "A", "a:b", "小安妮", "a*", "a?", "a[0]")
    namespaces = {memory_namespace(robot, None, "semantic") for robot in ids}
    assert len(namespaces) == len(ids)
    assert memory_namespace("a/b", "c", "persona") != memory_namespace("a", "b/c", "persona")
    assert memory_namespace("a", "p", "episodes") != memory_namespace("a", "p", "persona")
    assert memory_namespace("a", "p", "persona") != memory_namespace("a", "P", "persona")


@pytest.mark.parametrize("robot,persona,kind", [
    ("", None, "semantic"), ("  ", None, "semantic"),
    ("a", None, "persona"), ("a", "", "episodes"),
    ("a", "p", "global"),
])
def test_invalid_namespace_rejected(robot, persona, kind):
    with pytest.raises(ValueError):
        memory_namespace(robot, persona, kind)
