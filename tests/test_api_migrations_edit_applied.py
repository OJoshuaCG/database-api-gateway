"""
Edición del SQL de una versión de blueprint que YA está aplicada en alguna BD.

El freeze (`model_migration.sql_frozen`) sigue siendo el default: lo que se prueba acá es la
ÚNICA vía para atravesarlo —doble factor + rastro permanente— y, sobre todo, que no se pueda
atravesar por accidente.

Por qué la vía existe: hay correcciones cuyo valor está en que las BDs NUEVAS no repitan el
defecto. El caso testigo es un `COLLATE` hardcodeado en el DDL de las primeras versiones:
describirlo con una versión correctiva al final de la cadena obliga a toda base nueva a
crearse mal y convertirse después.

Lo que la vía NO hace —y por eso cada test que la ejercita comprueba también el rastro—:
editar `up_sql` no re-ejecuta nada. Las BDs que ya aplicaron la versión conservan
FÍSICAMENTE lo que corrió, así que quedan divergentes. La divergencia es inevitable; que sea
SILENCIOSA no.
"""

from datetime import UTC, datetime

import pytest

from app.core.database import Database
from app.models.audit_log import AuditLog
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.enums import MigrationStatus
from app.models.managed_database import ManagedDatabase

ORIG = "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100)) COLLATE utf8mb4_general_ci"
NUEVO = "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100)) COLLATE utf8mb4_unicode_ci"


def _public_context(resp) -> dict:
    """`public_context` viaja dentro de `detail` (ver HandlerExceptions)."""
    return (resp.json().get("detail") or {}).get("public_context") or {}


@pytest.fixture()
def applied_version(admin_client, monkeypatch):
    """Blueprint con la versión 0001 aplicada en una BD que el motor reporta en 0001.

    La versión del motor se fija con un doble: `_still_applied_live` es AUTORITATIVO a
    propósito (lee el motor, no la caché del inventario), así que sin el doble el test no
    podría ejercitar la rama que importa.
    """
    import app.controllers.model_migration_controller as ctrl_mod
    from app.services.db_admin.migrations import MigrationRunner

    engine_version = {"v": "0001"}
    monkeypatch.setattr(ctrl_mod, "get_server_or_404", lambda s, sid: object())
    monkeypatch.setattr(ctrl_mod, "build_target", lambda srv: object())
    monkeypatch.setattr(
        MigrationRunner, "get_current_version", lambda self, t, d, s: engine_version["v"]
    )

    model_id = admin_client.post(
        "/api/v1/database-models", json={"name": "WA", "slug": "wa"}
    ).json()["data"]["id"]
    r = admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations",
        json={"version": "0001", "name": "inicial", "up_sql": ORIG},
    )
    assert r.status_code == 201, r.text
    migration_id = r.json()["data"]["id"]

    session = Database().get_declarative_base_session()
    session.add(
        ManagedDatabase(
            name="wa_cliente1", server_id=1, owner_id=1,
            model_id=model_id, model_version="0001",
        )
    )
    session.flush()
    db_id = session.query(ManagedDatabase).first().id
    session.add(
        DatabaseMigrationHistory(
            managed_database_id=db_id,
            model_migration_id=migration_id,
            applied_at=datetime.now(UTC),
            status=MigrationStatus.applied,
        )
    )
    session.commit()
    session.close()
    return {
        "model_id": model_id,
        "migration_id": migration_id,
        "db_id": db_id,
        "engine_version": engine_version,
    }


def _preview(admin_client, model_id, version, **sql):
    return admin_client.post(
        f"/api/v1/database-models/{model_id}/migrations/{version}/edit-preview", json=sql
    )


# --------------------------------------------------------------------------- #
# El freeze sigue siendo el default                                            #
# --------------------------------------------------------------------------- #
def test_patch_sin_confirmacion_sigue_dando_409(admin_client, applied_version):
    """La vía no se abre sola: sin los dos factores, la respuesta es la de siempre."""
    mid = applied_version["model_id"]
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001", json={"up_sql": NUEVO}
    )
    assert r.status_code == 409, r.text
    pc = _public_context(r)
    assert pc["code"] == "model_migration.sql_frozen"
    # La UI necesita distinguir "no se puede" de "se puede confirmando": un 409 que oculta
    # la única salida obliga a leer el código para encontrarla.
    assert pc["override_available"] is True
    assert pc["blocking_databases"][0]["managed_database_id"] == applied_version["db_id"]


def test_el_sql_no_se_toco_tras_el_409(admin_client, applied_version):
    mid = applied_version["model_id"]
    admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001", json={"up_sql": NUEVO}
    )
    d = admin_client.get(f"/api/v1/database-models/{mid}/migrations/0001").json()["data"]
    assert d["up_sql"] == ORIG
    assert d["sql_diverged"] is False


# --------------------------------------------------------------------------- #
# Preview                                                                      #
# --------------------------------------------------------------------------- #
def test_preview_nombra_las_bds_que_quedaran_divergentes(admin_client, applied_version):
    mid = applied_version["model_id"]
    r = _preview(admin_client, mid, "0001", up_sql=NUEVO)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["requires_confirmation"] is True
    assert d["confirm_token"]
    assert d["blocking_databases"] == [
        {
            "managed_database_id": applied_version["db_id"],
            "reason": "still_applied",
            "current_version": "0001",
        }
    ]


def test_preview_de_una_version_no_vigente_no_emite_token(admin_client, applied_version):
    """Emitir un token que no hace falta entrena a mandarlo siempre."""
    mid = applied_version["model_id"]
    applied_version["engine_version"]["v"] = None  # la BD ya no alcanza la versión
    r = _preview(admin_client, mid, "0001", up_sql=NUEVO)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["requires_confirmation"] is False
    assert d["confirm_token"] is None


# --------------------------------------------------------------------------- #
# Los dos factores                                                             #
# --------------------------------------------------------------------------- #
def test_confirm_version_equivocado_es_422(admin_client, applied_version):
    mid = applied_version["model_id"]
    token = _preview(admin_client, mid, "0001", up_sql=NUEVO).json()["data"]["confirm_token"]
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={"up_sql": NUEVO, "confirm_version": "0002", "confirm_token": token},
    )
    assert r.status_code == 422, r.text
    assert _public_context(r)["code"] == "model_migration.edit_confirm_mismatch"


def test_token_no_sirve_para_un_sql_distinto_del_previsualizado(
    admin_client, applied_version
):
    """
    Es la razón de ser del `subject` del token.

    Sin atarlo al SQL, `(operación, model_id, version)` es igual para CUALQUIER edición de
    esa versión: se podría pedir el preview de una corrección inocua —viendo un
    `blocking_databases` tranquilizador— y mandar otra cosa en el PATCH.
    """
    mid = applied_version["model_id"]
    token = _preview(admin_client, mid, "0001", up_sql=NUEVO).json()["data"]["confirm_token"]
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={
            "up_sql": NUEVO.replace("unicode_ci", "bin"),
            "confirm_version": "0001",
            "confirm_token": token,
        },
    )
    assert r.status_code == 422, r.text


def test_token_de_otra_version_no_sirve(admin_client, applied_version):
    """El token está atado a la VERSIÓN, no solo al blueprint.

    Se monta una 0002 también vigente para que su preview emita token de verdad: con una
    versión no aplicada el preview no emite nada y el test no probaría lo que dice.
    """
    mid = applied_version["model_id"]
    r = admin_client.post(
        f"/api/v1/database-models/{mid}/migrations",
        json={"version": "0002", "name": "otra", "up_sql": "CREATE TABLE t2 (id INT)"},
    )
    assert r.status_code == 201, r.text
    session = Database().get_declarative_base_session()
    session.add(
        DatabaseMigrationHistory(
            managed_database_id=applied_version["db_id"],
            model_migration_id=r.json()["data"]["id"],
            applied_at=datetime.now(UTC),
            status=MigrationStatus.applied,
        )
    )
    session.commit()
    session.close()
    applied_version["engine_version"]["v"] = "0002"  # la BD alcanza ambas

    ajeno = _preview(admin_client, mid, "0002", up_sql="CREATE TABLE t3 (id INT)").json()
    assert ajeno["data"]["confirm_token"], "el preview de 0002 debía emitir token"
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={
            "up_sql": NUEVO,
            "confirm_version": "0001",
            "confirm_token": ajeno["data"]["confirm_token"],
        },
    )
    assert r.status_code == 422, r.text


def test_token_forjado_no_sirve(admin_client, applied_version):
    """Sin SECRET_KEY no se puede fabricar: un HMAC inventado con vencimiento futuro cae."""
    import time

    mid = applied_version["model_id"]
    forjado = f"{int(time.time()) + 600}.{'a' * 64}"
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={"up_sql": NUEVO, "confirm_version": "0001", "confirm_token": forjado},
    )
    assert r.status_code == 422, r.text
    assert (
        admin_client.get(f"/api/v1/database-models/{mid}/migrations/0001")
        .json()["data"]["up_sql"]
        == ORIG
    )


# --------------------------------------------------------------------------- #
# La vía completa, y su rastro                                                 #
# --------------------------------------------------------------------------- #
def test_con_los_dos_factores_edita_y_marca_divergencia(admin_client, applied_version):
    mid = applied_version["model_id"]
    token = _preview(admin_client, mid, "0001", up_sql=NUEVO).json()["data"]["confirm_token"]
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={"up_sql": NUEVO, "confirm_version": "0001", "confirm_token": token},
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["up_sql"] == NUEVO
    assert d["sql_diverged"] is True

    # La bandera es un HECHO persistido (derivado de auditoría), no un dato de la respuesta.
    assert admin_client.get(
        f"/api/v1/database-models/{mid}/migrations/0001"
    ).json()["data"]["sql_diverged"] is True
    listado = admin_client.get(f"/api/v1/database-models/{mid}/migrations").json()["data"]
    assert listado[0]["sql_diverged"] is True


def test_la_divergencia_queda_auditada_con_par_attempt_success(
    admin_client, applied_version
):
    mid = applied_version["model_id"]
    token = _preview(admin_client, mid, "0001", up_sql=NUEVO).json()["data"]["confirm_token"]
    admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001",
        json={"up_sql": NUEVO, "confirm_version": "0001", "confirm_token": token},
    )
    session = Database().get_declarative_base_session()
    rows = (
        session.query(AuditLog)
        .filter(AuditLog.action == "migration.sql_edited_after_apply")
        .all()
    )
    assert sorted(r.status for r in rows) == ["attempt", "success"]
    # Anclada al id de la MIGRACIÓN (no del blueprint): es lo que permite derivar la
    # bandera por versión con una sola query en lote.
    assert {r.target_id for r in rows} == {applied_version["migration_id"]}
    assert {r.target_type for r in rows} == {"model_migration"}
    assert str(applied_version["db_id"]) in (rows[0].detail or "")
    session.close()


def test_sin_bd_vigente_el_patch_comun_no_pide_nada(admin_client, applied_version):
    """No regresión: la vía nueva no le agrega fricción al caso que ya funcionaba."""
    mid = applied_version["model_id"]
    applied_version["engine_version"]["v"] = None
    r = admin_client.patch(
        f"/api/v1/database-models/{mid}/migrations/0001", json={"up_sql": NUEVO}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sql_diverged"] is False
