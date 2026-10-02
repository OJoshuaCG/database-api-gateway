"""
Clones, exportaciones, conversiones de collation y diff de esquemas con capa 2 (``require_at``).

POR QUÉ ESTE ARCHIVO EXISTE
---------------------------
``test_scope_registry`` ejerce estas rutas con ids inexistentes (todo resuelve al entorno más
protegido): prueba que el guard existe, no que apunte al EXTREMO correcto. Acá se siembran jobs
persistidos con origen y destino reales y se verifica que:

1. un clon exige ``clones.execute`` en el origen Y en el destino (prod -> dev es exfiltración);
2. ``execute`` relee el entorno de la FILA del job: reclasificar la base entre el plan y la
   ejecución cambia el veredicto;
3. cancelar un clon es una acción sobre el DESTINO;
4. exportar se evalúa en el origen, y retirar el artefacto exige ``exports.download`` allí;
5. el diff se evalúa en el DESTINO persistido de la comparación.

El actor es ``owner`` de base con un grant ``viewer`` sobre producción. Ninguna prueba conecta
a un motor: los casos "pasa" mandan un cuerpo incompleto, de modo que lo que sigue a la capa 2
es un 422 y nunca un 403.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import Database
from tests.scope_helpers import env_id, otorgar, sembrar_bd

_API = "/api/v1"


def _forbidden(r) -> bool:
    return (
        r.status_code == 403
        and r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    )


def _futuro() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


def _insertar(modelo, **campos) -> int:
    s = Database().get_declarative_base_session()
    try:
        fila = modelo(**campos)
        s.add(fila)
        s.commit()
        return fila.id
    finally:
        s.close()


def _clon(src_sid: int, src_name: str, tgt_sid: int, tgt_name: str) -> int:
    from app.models.clone_job import CloneJob

    return _insertar(
        CloneJob,
        source_server_id=src_sid,
        source_database_name=src_name,
        source_engine="mysql",
        target_server_id=tgt_sid,
        target_database_name=tgt_name,
        target_engine="mysql",
        target_mode="existing",
        source_fingerprint="f" * 64,
        expires_at=_futuro(),
    )


def _export(sid: int, name: str) -> int:
    from app.models.export_job import ExportJob

    return _insertar(
        ExportJob,
        server_id=sid,
        database_name=name,
        engine="mysql",
        spec="{}",
        source_fingerprint="f" * 64,
        expires_at=_futuro(),
    )


def _collation(sid: int, name: str) -> int:
    from app.models.collation_conversion_job import CollationConversionJob

    return _insertar(
        CollationConversionJob,
        server_id=sid,
        database_name=name,
        engine="mysql",
        target_collation="utf8mb4_unicode_ci",
        source_fingerprint="f" * 64,
        expires_at=_futuro(),
    )


def _comparacion(src: tuple[int, str], tgt: tuple[int, str]) -> int:
    from app.models.schema_comparison import SchemaComparison

    return _insertar(
        SchemaComparison,
        source_server_id=src[0],
        source_database_name=src[1],
        source_engine="mysql",
        source_fingerprint="f" * 64,
        target_server_id=tgt[0],
        target_database_name=tgt[1],
        target_engine="mysql",
        target_fingerprint="f" * 64,
        expires_at=_futuro(),
    )


@pytest.fixture()
def escenario(admin_client, server_payload):
    """Un servidor SOLO producción (``appprod``) y otro SOLO desarrollo (``appdev``)."""
    prod, dev = env_id("production"), env_id("development")
    sid_prod = admin_client.post(f"{_API}/servers", json=server_payload()).json()["data"]["id"]
    sid_dev = admin_client.post(
        f"{_API}/servers", json=server_payload(name="srv-dev", port=3400)
    ).json()["data"]["id"]
    esc = {
        "prod": prod,
        "dev": dev,
        "sid_prod": sid_prod,
        "sid_dev": sid_dev,
        "db_prod": sembrar_bd(server_id=sid_prod, environment_id=prod, name="appprod"),
        "db_dev": sembrar_bd(server_id=sid_dev, environment_id=dev, name="appdev"),
    }
    otorgar("environment", prod, "viewer")
    return esc


# --------------------------------------------------------------------------- #
# Clones: ambos extremos                                                        #
# --------------------------------------------------------------------------- #


def _crear_clon(client, esc, *, origen: str, destino: str, completo: bool = True):
    por_nombre = {
        "prod": (esc["sid_prod"], "appprod"),
        "dev": (esc["sid_dev"], "appdev"),
    }
    sid_o, nom_o = por_nombre[origen]
    sid_d, nom_d = por_nombre[destino]
    cuerpo = {
        "source_server_id": sid_o,
        "source_database_name": nom_o,
        "target_server_id": sid_d,
        "target_database_name": nom_d,
    }
    if completo:
        cuerpo["target_mode"] = "existing"
    return client.post(f"{_API}/database-clones", json=cuerpo)


def test_clone_from_prod_to_dev_is_denied(admin_client, escenario):
    """La exfiltración: leer producción y volcarla en un destino donde sí se puede escribir."""
    assert _forbidden(_crear_clon(admin_client, escenario, origen="prod", destino="dev"))


def test_clone_into_prod_is_denied(admin_client, escenario):
    assert _forbidden(_crear_clon(admin_client, escenario, origen="dev", destino="prod"))


def test_clone_by_source_database_id_is_checked_on_the_row(admin_client, escenario):
    r = admin_client.post(
        f"{_API}/database-clones",
        json={
            "source_database_id": escenario["db_prod"],
            "target_server_id": escenario["sid_dev"],
            "target_database_name": "appdev",
            "target_mode": "existing",
        },
    )
    assert _forbidden(r)


def test_clone_target_database_id_cannot_lower_the_target_environment(admin_client, escenario):
    """``target_database_id`` es informativo: declarar una BD de dev no rebaja un destino prod."""
    r = admin_client.post(
        f"{_API}/database-clones",
        json={
            "source_server_id": escenario["sid_dev"],
            "source_database_name": "appdev",
            "target_server_id": escenario["sid_prod"],
            "target_database_name": "appprod",
            "target_database_id": escenario["db_dev"],
            "target_mode": "existing",
        },
    )
    assert _forbidden(r)


def test_clone_dev_to_dev_passes_layer2(admin_client, escenario):
    r = _crear_clon(admin_client, escenario, origen="dev", destino="dev", completo=False)
    assert not _forbidden(r), r.text


@pytest.mark.parametrize("accion", ["preview", "execute"])
@pytest.mark.parametrize(
    "origen,destino", [("prod", "dev"), ("dev", "prod")], ids=["origen-prod", "destino-prod"]
)
def test_clone_job_preview_and_execute_check_both_ends(
    admin_client, escenario, accion, origen, destino
):
    mapa = {"prod": (escenario["sid_prod"], "appprod"), "dev": (escenario["sid_dev"], "appdev")}
    job = _clon(*mapa[origen], *mapa[destino])
    r = admin_client.post(f"{_API}/database-clones/{job}/{accion}", json={})
    assert _forbidden(r), r.text


@pytest.mark.parametrize("accion", ["preview", "execute"])
def test_clone_job_dev_to_dev_passes_layer2(admin_client, escenario, accion):
    job = _clon(escenario["sid_dev"], "appdev", escenario["sid_dev"], "appdev")
    r = admin_client.post(f"{_API}/database-clones/{job}/{accion}", json={})
    assert not _forbidden(r), r.text


def test_clone_execute_rereads_the_environment_from_the_job_row(admin_client, escenario):
    """Plan válido (dev -> dev); la base se reclasifica a producción; ejecutar debe dar 403."""
    job = _clon(escenario["sid_dev"], "appdev", escenario["sid_dev"], "appdev")
    antes = admin_client.post(f"{_API}/database-clones/{job}/execute", json={})
    assert not _forbidden(antes), antes.text
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE managed_databases SET environment_id = :e WHERE id = :i"),
            {"e": escenario["prod"], "i": escenario["db_dev"]},
        )
    despues = admin_client.post(f"{_API}/database-clones/{job}/execute", json={})
    assert _forbidden(despues), despues.text


def test_clone_cancel_is_an_action_on_the_target(admin_client, escenario):
    """Cancelar mira solo el destino: origen prod + destino dev se puede cancelar; al revés no."""
    cancelable = _clon(escenario["sid_prod"], "appprod", escenario["sid_dev"], "appdev")
    r = admin_client.post(f"{_API}/database-clones/{cancelable}/cancel")
    assert not _forbidden(r), r.text
    a_prod = _clon(escenario["sid_dev"], "appdev", escenario["sid_prod"], "appprod")
    r = admin_client.post(f"{_API}/database-clones/{a_prod}/cancel")
    assert _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Exportaciones: en el origen                                                   #
# --------------------------------------------------------------------------- #


def test_export_plan_from_prod_is_denied(admin_client, escenario):
    r = admin_client.post(
        f"{_API}/servers/{escenario['sid_prod']}/databases/appprod/database-exports", json={}
    )
    assert _forbidden(r), r.text


def test_export_plan_from_dev_passes_layer2(admin_client, escenario):
    r = admin_client.post(
        f"{_API}/servers/{escenario['sid_dev']}/databases/appdev/database-exports", json={}
    )
    assert not _forbidden(r), r.text


_EXPORT_RUTAS = [
    ("POST", "preview", {}),
    ("POST", "execute", {}),
    ("POST", "cancel", None),
    ("POST", "download-ticket", None),
    ("GET", "download", None),
    ("GET", "content", None),
]


@pytest.mark.parametrize("method,sufijo,body", _EXPORT_RUTAS)
def test_export_job_routes_deny_on_a_production_source(
    admin_client, escenario, method, sufijo, body
):
    job = _export(escenario["sid_prod"], "appprod")
    r = admin_client.request(method, f"{_API}/database-exports/{job}/{sufijo}", json=body)
    assert _forbidden(r), f"{method} {sufijo}: {r.status_code} {r.text}"


@pytest.mark.parametrize("method,sufijo,body", _EXPORT_RUTAS)
def test_export_job_routes_pass_layer2_on_a_development_source(
    admin_client, escenario, method, sufijo, body
):
    job = _export(escenario["sid_dev"], "appdev")
    r = admin_client.request(method, f"{_API}/database-exports/{job}/{sufijo}", json=body)
    assert not _forbidden(r), f"{method} {sufijo}: {r.status_code} {r.text}"


def test_export_download_needs_exports_download_at_the_source(admin_client, escenario):
    """Sin ``exports.download`` en el origen (producción) no hay ticket de entrega."""
    job = _export(escenario["sid_prod"], "appprod")
    r = admin_client.post(f"{_API}/database-exports/{job}/download-ticket")
    assert _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Conversión de collation: en la base del job                                   #
# --------------------------------------------------------------------------- #


def test_collation_create_on_prod_is_denied_and_on_dev_passes(admin_client, escenario):
    r = admin_client.post(
        f"{_API}/servers/{escenario['sid_prod']}/databases/appprod/collation-conversions",
        json={},
    )
    assert _forbidden(r), r.text
    r = admin_client.post(
        f"{_API}/servers/{escenario['sid_dev']}/databases/appdev/collation-conversions",
        json={},
    )
    assert not _forbidden(r), r.text


@pytest.mark.parametrize("sufijo", ["preview", "execute", "cancel"])
def test_collation_job_routes_check_the_job_row(admin_client, escenario, sufijo):
    en_prod = _collation(escenario["sid_prod"], "appprod")
    en_dev = _collation(escenario["sid_dev"], "appdev")
    r = admin_client.post(f"{_API}/collation-conversions/{en_prod}/{sufijo}", json={})
    assert _forbidden(r), r.text
    r = admin_client.post(f"{_API}/collation-conversions/{en_dev}/{sufijo}", json={})
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Diff de esquemas: en el destino persistido                                    #
# --------------------------------------------------------------------------- #

_DIFF_RUTAS = ["adopt", "execute-preview", "execute"]


@pytest.mark.parametrize("sufijo", _DIFF_RUTAS)
def test_schema_diff_routes_deny_when_the_target_is_prod(admin_client, escenario, sufijo):
    comp = _comparacion(
        (escenario["sid_dev"], "appdev"), (escenario["sid_prod"], "appprod")
    )
    r = admin_client.post(f"{_API}/schema-comparisons/{comp}/{sufijo}", json={})
    assert _forbidden(r), r.text


@pytest.mark.parametrize("sufijo", _DIFF_RUTAS)
def test_schema_diff_routes_pass_layer2_when_only_the_source_is_prod(
    admin_client, escenario, sufijo
):
    """Leer producción para aplicar el diff en desarrollo no escribe en producción."""
    comp = _comparacion(
        (escenario["sid_prod"], "appprod"), (escenario["sid_dev"], "appdev")
    )
    r = admin_client.post(f"{_API}/schema-comparisons/{comp}/{sufijo}", json={})
    assert not _forbidden(r), r.text


def test_schema_diff_read_routes_stay_at_the_viewer_floor(admin_client, escenario):
    """Crear/listar/exportar no tienen 403 observable: ``schema_diff.read`` es piso viewer."""
    comp = _comparacion(
        (escenario["sid_dev"], "appdev"), (escenario["sid_prod"], "appprod")
    )
    r = admin_client.get(f"{_API}/schema-comparisons/{comp}")
    assert not _forbidden(r), r.text
