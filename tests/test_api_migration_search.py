"""
Búsqueda de texto en el ``up_sql`` de las versiones de un blueprint
(``GET /database-models/{id}/migrations/search``).

Todo corre contra la BD del gateway en SQLite: el endpoint no abre conexión a ningún motor.
Lo que SQLite NO prueba es el prefiltro ``icontains`` bajo la collation real de MySQL
(``_ci``/``_ai``) ni el ``LIKE`` sensible a mayúsculas de PostgreSQL; el veredicto final es el
conteo en Python, que es el mismo en los tres motores, pero el superconjunto del prefiltro
queda sin verificar contra motor real.
"""

from datetime import datetime

from app.services import migration_freeze_catalog as freeze_codes


def _new_model(admin_client, slug="buscador", name="Buscador") -> int:
    r = admin_client.post("/api/v1/database-models", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_migration(admin_client, model_id, version, up_sql, name=None):
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": version, "name": name or f"v{version}", "up_sql": up_sql},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _search(admin_client, model_id, **params):
    return admin_client.get(f"/api/v1/database-models/{model_id}/migrations/search", params=params)


def _versions(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [h["version"] for h in resp.json()["data"]]


def _pc(resp) -> dict:
    return (resp.json().get("detail") or {}).get("public_context") or {}


def _set_created_at(migration_id: int, when: datetime) -> None:
    """``created_at`` lo pone el servidor; para probar el filtro por fecha se fija a mano."""
    from app.core.database import Database
    from app.models.model_migration import ModelMigration

    s = Database().get_declarative_base_session()
    try:
        s.get(ModelMigration, migration_id).created_at = when
        s.commit()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
def test_requires_auth(client):
    r = client.get("/api/v1/database-models/1/migrations/search", params={"q": "users"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Camino feliz                                                                 #
# --------------------------------------------------------------------------- #
def test_basic_hit_reports_line_and_highlight_offsets(admin_client):
    model_id = _new_model(admin_client)
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE users (id INT)")
    _create_migration(
        admin_client,
        model_id,
        "0002",
        "CREATE TABLE orders (id INT);\nALTER TABLE orders ADD COLUMN invoice_ref INT;",
    )

    r = _search(admin_client, model_id, q="invoice_ref")
    assert _versions(r) == ["0002"]
    hit = r.json()["data"][0]
    assert hit["model_id"] == model_id
    assert hit["name"] == "v0002"
    assert hit["is_latest"] is True
    assert hit["match_count"] == 1
    assert hit["lines_matched"] == 1
    assert "up_sql" not in hit, "la búsqueda nunca devuelve el SQL completo"

    (snippet,) = hit["snippets"]
    assert snippet["line"] == 2
    assert snippet["text"][snippet["match_start"] : snippet["match_end"]] == "invoice_ref"


def test_snippets_capped_but_counts_are_total(admin_client):
    model_id = _new_model(admin_client, slug="muchas", name="Muchas")
    sql = "\n".join(f"ALTER TABLE t{i} ADD COLUMN audit_col INT;" for i in range(5))
    _create_migration(admin_client, model_id, "0001", sql)

    hit = _search(admin_client, model_id, q="audit_col").json()["data"][0]
    assert hit["match_count"] == 5
    assert hit["lines_matched"] == 5
    assert [s["line"] for s in hit["snippets"]] == [1, 2, 3]


def test_long_line_is_trimmed_around_the_match(admin_client):
    model_id = _new_model(admin_client, slug="larga", name="Larga")
    cols = ", ".join(f"c{i} INT" for i in range(200))
    _create_migration(
        admin_client, model_id, "0001", f"CREATE TABLE t ({cols}, needle_col INT, {cols})"
    )

    (snippet,) = _search(admin_client, model_id, q="needle_col").json()["data"][0]["snippets"]
    assert snippet["text"].startswith("…") and snippet["text"].endswith("…")
    assert len(snippet["text"]) <= 170
    assert snippet["text"][snippet["match_start"] : snippet["match_end"]] == "needle_col"


def test_query_is_stripped_before_searching(admin_client):
    model_id = _new_model(admin_client, slug="strip", name="Strip")
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE users (id INT)")
    assert _versions(_search(admin_client, model_id, q="  users  ")) == ["0001"]


# --------------------------------------------------------------------------- #
# Validación de entrada                                                        #
# --------------------------------------------------------------------------- #
def test_query_shorter_than_minimum_is_422_with_code(admin_client):
    model_id = _new_model(admin_client, slug="corto", name="Corto")
    for q in ("abc", "  ab  "):
        r = _search(admin_client, model_id, q=q)
        assert r.status_code == 422, r.text
        pc = _pc(r)
        assert pc["code"] == freeze_codes.CODE_SEARCH_QUERY_TOO_SHORT
        assert pc["min_length"] == 4


def test_invalid_date_range_is_422_with_code(admin_client):
    model_id = _new_model(admin_client, slug="rango", name="Rango")
    r = _search(admin_client, model_id, q="users", date_from="2026-09-10", date_to="2026-09-01")
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == freeze_codes.CODE_SEARCH_INVALID_DATE_RANGE


def test_unknown_blueprint_is_404(admin_client):
    r = admin_client.get("/api/v1/database-models/9999/migrations/search", params={"q": "users"})
    assert r.status_code == 404


def test_search_codes_are_in_the_closed_vocabulary():
    assert freeze_codes.CODE_SEARCH_QUERY_TOO_SHORT in freeze_codes.ERROR_CODES
    assert freeze_codes.CODE_SEARCH_INVALID_DATE_RANGE in freeze_codes.ERROR_CODES


# --------------------------------------------------------------------------- #
# Semántica de la coincidencia                                                 #
# --------------------------------------------------------------------------- #
def test_like_wildcards_are_literal(admin_client):
    """``%`` y ``_`` no son comodines: sin autoescape, 'x_pct' casaría con 'xApct'."""
    model_id = _new_model(admin_client, slug="comodin", name="Comodin")
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE a (x_pct INT)")
    _create_migration(admin_client, model_id, "0002", "CREATE TABLE b (xApct INT)")
    _create_migration(
        admin_client, model_id, "0003", "CREATE TABLE c (d VARCHAR(10) DEFAULT '50%off')"
    )
    _create_migration(
        admin_client, model_id, "0004", "CREATE TABLE e (d VARCHAR(10) DEFAULT '500off')"
    )

    assert _versions(_search(admin_client, model_id, q="x_pct")) == ["0001"]
    assert _versions(_search(admin_client, model_id, q="0%off")) == ["0003"]


def test_case_sensitive_vs_insensitive(admin_client):
    model_id = _new_model(admin_client, slug="mayus", name="Mayus")
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE Customers (id INT)")
    _create_migration(admin_client, model_id, "0002", "CREATE TABLE customers_log (id INT)")

    assert _versions(_search(admin_client, model_id, q="customers")) == ["0002", "0001"]
    assert _versions(_search(admin_client, model_id, q="customers", case_sensitive=True)) == [
        "0002"
    ]
    assert _versions(_search(admin_client, model_id, q="Customers", case_sensitive=True)) == [
        "0001"
    ]


def test_order_asc_and_desc(admin_client):
    model_id = _new_model(admin_client, slug="orden", name="Orden")
    for v in ("0001", "0002", "0010"):
        _create_migration(admin_client, model_id, v, f"CREATE TABLE shared_{v} (id INT)")

    assert _versions(_search(admin_client, model_id, q="shared_")) == ["0010", "0002", "0001"]
    assert _versions(_search(admin_client, model_id, q="shared_", order="asc")) == [
        "0001",
        "0002",
        "0010",
    ]


def test_is_latest_only_on_blueprint_tip(admin_client):
    """La punta es la del blueprint, aunque la última versión no case con la búsqueda."""
    model_id = _new_model(admin_client, slug="punta", name="Punta")
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE target_tbl (id INT)")
    _create_migration(admin_client, model_id, "0002", "CREATE TABLE other (id INT)")

    (hit,) = _search(admin_client, model_id, q="target_tbl").json()["data"]
    assert hit["is_latest"] is False


# --------------------------------------------------------------------------- #
# Ventana: last y fechas                                                       #
# --------------------------------------------------------------------------- #
def test_last_restricts_the_window_to_the_latest_versions(admin_client):
    model_id = _new_model(admin_client, slug="ultimas", name="Ultimas")
    for v in ("0001", "0002", "0003", "0004"):
        _create_migration(admin_client, model_id, v, f"CREATE TABLE common_{v} (id INT)")
    # Versión alta que NO casa: consume un lugar de la ventana igual.
    _create_migration(admin_client, model_id, "0005", "CREATE TABLE unrelated (id INT)")

    assert _versions(_search(admin_client, model_id, q="common_", last=3)) == ["0004", "0003"]
    assert _versions(_search(admin_client, model_id, q="common_")) == [
        "0004",
        "0003",
        "0002",
        "0001",
    ]


def test_last_uses_numeric_order_not_lexicographic(admin_client):
    model_id = _new_model(admin_client, slug="numerico", name="Numerico")
    _create_migration(admin_client, model_id, "0009", "CREATE TABLE numtest_a (id INT)")
    _create_migration(admin_client, model_id, "10000", "CREATE TABLE numtest_b (id INT)")
    assert _versions(_search(admin_client, model_id, q="numtest", last=1)) == ["10000"]


def test_date_filters_with_inclusive_date_to(admin_client):
    model_id = _new_model(admin_client, slug="fechas", name="Fechas")
    m1 = _create_migration(admin_client, model_id, "0001", "CREATE TABLE dated_a (id INT)")
    m2 = _create_migration(admin_client, model_id, "0002", "CREATE TABLE dated_b (id INT)")
    m3 = _create_migration(admin_client, model_id, "0003", "CREATE TABLE dated_c (id INT)")
    _set_created_at(m1["id"], datetime(2026, 8, 1, 12, 0, 0))
    _set_created_at(m2["id"], datetime(2026, 9, 1, 23, 59, 59, 500000))
    _set_created_at(m3["id"], datetime(2026, 9, 20, 8, 30, 0))

    # date_to inclusivo: la de las 23:59:59.5 del 1 de septiembre entra.
    r = _search(admin_client, model_id, q="dated_", date_to="2026-09-01")
    assert _versions(r) == ["0002", "0001"]
    # Solo date_from: "desde esa fecha hasta hoy".
    r = _search(admin_client, model_id, q="dated_", date_from="2026-09-02")
    assert _versions(r) == ["0003"]
    # Ambos límites.
    r = _search(admin_client, model_id, q="dated_", date_from="2026-09-01", date_to="2026-09-01")
    assert _versions(r) == ["0002"]


def test_last_and_dates_combine_as_intersection(admin_client):
    model_id = _new_model(admin_client, slug="interseccion", name="Interseccion")
    m1 = _create_migration(admin_client, model_id, "0001", "CREATE TABLE inter_a (id INT)")
    m2 = _create_migration(admin_client, model_id, "0002", "CREATE TABLE inter_b (id INT)")
    m3 = _create_migration(admin_client, model_id, "0003", "CREATE TABLE inter_c (id INT)")
    _set_created_at(m1["id"], datetime(2026, 9, 5, 10, 0, 0))
    _set_created_at(m2["id"], datetime(2026, 8, 5, 10, 0, 0))
    _set_created_at(m3["id"], datetime(2026, 9, 6, 10, 0, 0))

    # La ventana son 0003 y 0002; de esas, solo 0003 es de septiembre. 0001 es de
    # septiembre pero está fuera de la ventana.
    r = _search(admin_client, model_id, q="inter_", last=2, date_from="2026-09-01")
    assert _versions(r) == ["0003"]


# --------------------------------------------------------------------------- #
# Paginación y aislamiento                                                     #
# --------------------------------------------------------------------------- #
def test_pagination_total_counts_matching_versions(admin_client):
    model_id = _new_model(admin_client, slug="paginas", name="Paginas")
    for i in range(1, 6):
        _create_migration(admin_client, model_id, f"{i:04d}", f"CREATE TABLE paged_{i} (id INT)")
    _create_migration(admin_client, model_id, "0006", "CREATE TABLE nomatch (id INT)")

    r = _search(admin_client, model_id, q="paged_", size=2, page=2)
    assert _versions(r) == ["0003", "0002"]
    meta = r.json()["pagination"]
    assert meta["total"] == 5
    assert meta["pages"] == 3


def test_case_sensitive_total_excludes_prefilter_false_positives(admin_client):
    """El prefiltro SQL es insensible; el total tiene que reflejar el veredicto exacto."""
    model_id = _new_model(admin_client, slug="exacto", name="Exacto")
    _create_migration(admin_client, model_id, "0001", "CREATE TABLE Widget (id INT)")
    _create_migration(admin_client, model_id, "0002", "CREATE TABLE widget_x (id INT)")

    r = _search(admin_client, model_id, q="Widget", case_sensitive=True)
    assert _versions(r) == ["0001"]
    assert r.json()["pagination"]["total"] == 1


def test_other_blueprints_are_not_leaked(admin_client):
    mine = _new_model(admin_client, slug="propio", name="Propio")
    other = _new_model(admin_client, slug="ajeno", name="Ajeno")
    _create_migration(admin_client, mine, "0001", "CREATE TABLE mine_only (id INT)")
    _create_migration(admin_client, other, "0001", "CREATE TABLE secret_tbl (id INT)")
    _create_migration(admin_client, other, "0002", "CREATE TABLE mine_only_copy (id INT)")

    assert _versions(_search(admin_client, mine, q="secret_tbl")) == []
    r = _search(admin_client, mine, q="mine_only")
    assert _versions(r) == ["0001"]
    assert all(h["model_id"] == mine for h in r.json()["data"])
