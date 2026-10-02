"""
``app/core/assignment_policy.split``: qué parte de un cambio de acceso se aplica ya y qué parte
pide un segundo aprobador (C3). Puro: sin BD ni ``TestClient``.
"""

from app.core.assignment_policy import AccessState, split, state_hash


def _s(base="viewer", globals_=(), grants=()):
    return AccessState.of(base, globals_, grants)


def test_owner_base_is_an_elevation_and_stays_at_the_current_role():
    inm, elev = split(_s("operator"), _s("owner"))
    assert inm.base_role == "operator"
    assert elev == [{"kind": "base_role", "role": "owner"}]


def test_operator_is_not_an_elevation():
    inm, elev = split(_s("viewer"), _s("operator"))
    assert (inm.base_role, elev) == ("operator", [])


def test_demotions_are_immediate():
    actual = _s("owner", ["access_admin", "security_officer"],
                [("environment", 1, "owner"), ("server", 2, "operator")])
    deseado = _s("viewer", [], [])
    inm, elev = split(actual, deseado)
    assert elev == []
    assert inm == deseado


def test_every_added_global_is_an_elevation_and_removed_ones_go_now():
    inm, elev = split(_s(globals_=["security_officer"]), _s(globals_=["access_admin"]))
    assert inm.globals_ == frozenset()
    assert elev == [{"kind": "global_capability", "global_capability": "access_admin"}]


def test_an_owner_scope_keeps_the_previous_role_at_that_scope_until_approved():
    actual = _s(grants=[("environment", 1, "operator")])
    deseado = _s(grants=[("environment", 1, "owner"), ("environment", 2, "owner"),
                         ("server", 3, "viewer")])
    inm, elev = split(actual, deseado)
    assert inm.grants == frozenset({("environment", 1, "operator"), ("server", 3, "viewer")})
    assert [(e["scope_type"], e["scope_id"]) for e in elev] == [("environment", 1), ("environment", 2)]


def test_keeping_an_existing_owner_is_not_an_elevation():
    actual = _s("owner", ["access_admin"], [("environment", 1, "owner")])
    inm, elev = split(actual, actual)
    assert (inm, elev) == (actual, [])


def test_the_hash_is_order_independent_and_changes_with_the_access():
    a = _s("viewer", ["b", "a"], [("server", 2, "viewer"), ("environment", 1, "owner")])
    b = _s("viewer", ["a", "b"], [("environment", 1, "owner"), ("server", 2, "viewer")])
    assert state_hash(a) == state_hash(b)
    assert state_hash(a) != state_hash(_s("operator", ["a", "b"], a.grants))


def test_the_state_round_trips_through_its_public_dict():
    a = _s("owner", ["access_admin"], [("environment", 1, "owner")])
    assert AccessState.from_dict(a.as_dict()) == a
