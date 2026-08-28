"""
Eliminación de una versión INTERMEDIA de un blueprint, con renumerado y re-stamp.

Contrato que fijan estos tests:

- Se puede eliminar una versión **si y solo si ninguna BD está parada exactamente en
  ella**. Que haya BDs adelante o atrás no bloquea (antes el criterio era ``>=`` y solo
  se podía borrar la punta).
- Las versiones posteriores bajan **un** escalón. Las anteriores no se tocan.
- A las BDs que están adelante se les mueve el puntero a la etiqueta NUEVA de su MISMA
  migración. No se ejecuta ningún SQL del blueprint: no es un rollback.
- Los stamps van **antes** del renumerado, porque ``command.stamp`` necesita resolver el
  valor actual del puntero dentro de la cadena vigente.

El motor se mockea entero. Lo que NO se mockea es la BD de metadatos (SQLite), porque la
mitad de las invariantes que acá importan —el UNIQUE ``(model_id, version)`` durante el
renumerado y el ``checksum`` recalculado— viven ahí.
"""

from contextlib import contextmanager

from app.models.enums import EngineType
from app.services.db_admin.migration_integrity import compute_checksum

_UNREADABLE = object()


# --------------------------------------------------------------------------- #
# Dobles del motor                                                             #
# --------------------------------------------------------------------------- #
class _StampLog:
    """Registra los stamps para poder afirmar el ORDEN y los destinos."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []  # (db_name, version)
        self.fail_on: str | None = None
        self.fail_compensation = False

    def record(self, db_name, version):
        if self.fail_on is not None and db_name == self.fail_on:
            # El fallo es solo para el AVANCE; si no, la compensación de ese mismo
            # nombre también fallaría y no se podría probar el camino feliz de la
            # compensación por separado.
            if not any(c[0] == db_name for c in self.calls):
                raise RuntimeError("boom: el motor rechazó el stamp")
        if self.fail_compensation and self.calls and self.calls[-1][0] == db_name:
            raise RuntimeError("boom: la compensación también falló")
        self.calls.append((db_name, version))


@contextmanager
def _engine(versions: dict[str, object], log: _StampLog | None = None):
    """Doble del motor con una versión POR BD (``{db_name: version}``).

    ``_engine_version`` de ``test_api_model_migrations`` devuelve la misma versión para
    todas, y acá el punto entero es que cada BD esté en una distinta.

    Un valor ``_UNREADABLE`` simula un motor caído; ``None``, una BD que está en base.
    """
    import app.controllers.model_migration_controller as mod

    log = log or _StampLog()

    class _Runner:
        def get_current_version(self, target, db_name, slug):
            v = versions.get(db_name)
            if v is _UNREADABLE:
                raise RuntimeError("(2003, \"Can't connect (user=root password=rootpw)\")")
            return v

        def stamp(self, target, *, db_name, slug, engine, managed_db_id, specs, version,
                  purge=False):
            assert any(s.version == version for s in specs), (
                f"stamp a {version!r}, que no está en la cadena vigente: "
                f"{[s.version for s in specs]}"
            )
            log.record(db_name, version)
            versions[db_name] = version

    class _Server:
        """El controller le pide el motor con ``engine_value``, así que no basta un object()."""

        id = 1
        engine = EngineType.postgresql

    originales = (mod.MigrationRunner, mod.build_target, mod.get_server_or_404)
    mod.MigrationRunner = _Runner
    mod.build_target = lambda server: object()
    mod.get_server_or_404 = lambda session, server_id: _Server()
    try:
        yield log
    finally:
        mod.MigrationRunner, mod.build_target, mod.get_server_or_404 = originales


# --------------------------------------------------------------------------- #
# Fixtures de datos                                                            #
# --------------------------------------------------------------------------- #
def _blueprint(admin_client, slug, versions):
    r = admin_client.post("/api/v1/database-models", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    for v in versions:
        r = admin_client.post(
            f"/api/v1/database-models/{model_id}/migrations",
            json={
                "version": v,
                "name": f"m{v}",
                "up_sql": f"CREATE TABLE t{v} (id INT PRIMARY KEY)",
            },
        )
        assert r.status_code == 201, r.text
    return model_id


def _managed_db(admin_client, model_id, port, name, cached="0001"):
    """Registra una BD del blueprint y le fija la CACHÉ ``model_version``.

    Fijarla no es decorativo: el planificador saltea las BDs que nunca fueron posicionadas
    (``model_version`` nulo y sin historial exitoso), porque una BD en base no puede estar
    parada en la versión que se borra ni adelante de ella. Una BD sin caché quedaría
    invisible y el test probaría otra cosa.

    La caché puede diferir a propósito de lo que reporta el motor: el veredicto autoritativo
    es el del motor, y hay tests que dependen de esa divergencia.
    """
    sid = admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv{port}", "host": "10.0.0.9", "port": port,
            "engine": "postgresql", "root_username": "root", "root_password": "rootpw",
        },
    ).json()["data"]["id"]
    oid = admin_client.post(
        "/api/v1/server-users", json={"server_id": sid, "username": "owner1"}
    ).json()["data"]["id"]
    r = admin_client.post(
        "/api/v1/managed-databases",
        json={"server_id": sid, "owner_id": oid, "name": name, "model_id": model_id},
    )
    assert r.status_code == 201, r.text
    db_id = r.json()["data"]["id"]
    _set_cached_version(db_id, cached)
    return db_id


def _set_cached_version(db_id, version):
    from app.core.database import Database
    from app.models.managed_database import ManagedDatabase

    s = Database().get_declarative_base_session()
    try:
        s.get(ManagedDatabase, db_id).model_version = version
        s.commit()
    finally:
        s.close()


def _versions(admin_client, model_id):
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations?size=50")
    assert r.status_code == 200, r.text
    return [m["version"] for m in r.json()["data"]]


def _rows(model_id):
    """Filas crudas del blueprint, para verificar el checksum sin pasar por la API."""
    from app.core.database import Database
    from app.models.model_migration import ModelMigration

    s = Database().get_declarative_base_session()
    try:
        return {
            m.version: (m.id, m.checksum, m.up_sql)
            for m in s.query(ModelMigration)
            .filter(ModelMigration.model_id == model_id)
            .all()
        }
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Funciones puras del renumerado                                               #
# --------------------------------------------------------------------------- #
def test_shift_down_one_conserva_padding_de_cuatro():
    from app.controllers.model_migration_controller import ModelMigrationController as C

    assert C._shift_down_one("0016") == "0015"
    assert C._shift_down_one("0001") == "0000"
    # Cinco dígitos bajan a cuatro: el padding es un MÍNIMO, y el orden del módulo es
    # numérico (length, value), así que sigue ordenando bien.
    assert C._shift_down_one("10000") == "9999"


def test_renumber_map_solo_toca_las_posteriores_y_conserva_huecos_previos():
    """El renumerado baja UN escalón; no re-secuencia el blueprint.

    Importa cuando ya había huecos (``create_migration`` acepta ``version`` explícita):
    re-secuenciar cerraría todos, y eso cambiaría el número de versiones ANTERIORES a la
    borrada, dejando mintiendo el puntero de las BDs que están atrás.
    """
    from app.controllers.model_migration_controller import ModelMigrationController as C

    m = C._renumber_map(["0001", "0005", "0006", "0007", "0020"], "0006")
    assert m == {"0007": "0006", "0020": "0019"}
    assert "0001" not in m and "0005" not in m


# --------------------------------------------------------------------------- #
# Bloqueos                                                                     #
# --------------------------------------------------------------------------- #
def test_bd_parada_exactamente_en_la_version_bloquea(admin_client):
    """El único caso que el renumerado no puede resolver: no hay etiqueta a la que apuntar."""
    model_id = _blueprint(admin_client, "del-inuse", ["0001", "0002", "0003"])
    _managed_db(admin_client, model_id, 5601, "db_en_2", cached="0002")

    with _engine({"db_en_2": "0002"}):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["public_context"]["code"] == "model_migration.version_in_use"
    assert body["public_context"]["blocking_databases"][0]["reason"] == "in_use"
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003"]


def test_bd_ilegible_bloquea_fail_closed(admin_client):
    """Sin poder leerla no se puede descartar que esté parada acá, ni re-stampearla."""
    model_id = _blueprint(admin_client, "del-unread", ["0001", "0002", "0003"])
    _managed_db(admin_client, model_id, 5602, "db_muda", cached="0002")

    with _engine({"db_muda": _UNREADABLE}):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == "model_migration.unreadable_databases"
    # El mensaje del motor lleva credenciales: no puede filtrarse al 409 (criterio R4).
    assert "rootpw" not in r.text and "10.0.0.9" not in r.text
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003"]


def test_bd_adelante_exige_confirm_token(admin_client):
    """Mover un puntero es una escritura REMOTA: no puede pasar sin confirmación."""
    model_id = _blueprint(admin_client, "del-token", ["0001", "0002", "0003"])
    _managed_db(admin_client, model_id, 5603, "db_en_3", cached="0003")

    with _engine({"db_en_3": "0003"}):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 409, r.text
    assert (
        r.json()["detail"]["public_context"]["code"]
        == "model_migration.renumber_confirmation_required"
    )
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003"]


# --------------------------------------------------------------------------- #
# Camino feliz                                                                 #
# --------------------------------------------------------------------------- #
def test_borrar_la_punta_sin_bds_adelante_no_pide_token(admin_client):
    """Compatibilidad: el cliente viejo borra la punta sin token y sigue funcionando."""
    model_id = _blueprint(admin_client, "del-tip", ["0001", "0002"])
    _managed_db(admin_client, model_id, 5604, "db_en_1", cached="0001")

    with _engine({"db_en_1": "0001"}) as log:
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 200, r.text
    assert log.calls == []  # nada que stampear
    assert _versions(admin_client, model_id) == ["0001"]


def test_borrar_intermedia_renumera_y_mueve_punteros(admin_client):
    """El escenario completo: una BD atrás (intacta), una adelante (sigue el renombre)."""
    model_id = _blueprint(
        admin_client, "del-mid", ["0001", "0002", "0003", "0004", "0005"]
    )
    _managed_db(admin_client, model_id, 5605, "db_atras", cached="0001")
    _managed_db(admin_client, model_id, 5606, "db_adelante", cached="0005")

    estado = {"db_atras": "0001", "db_adelante": "0005"}
    with _engine(estado) as log:
        r = admin_client.get(
            f"/api/v1/database-models/{model_id}/migrations/0003/delete-plan"
        )
        assert r.status_code == 200, r.text
        plan = r.json()["data"]
        assert plan["deletable"] is True
        assert plan["renumber"] == [
            {"from_version": "0004", "to_version": "0003"},
            {"from_version": "0005", "to_version": "0004"},
        ]
        assert [s["database_name"] for s in plan["stamp_plan"]] == ["db_adelante"]
        assert plan["stamp_plan"][0]["from_version"] == "0005"
        assert plan["stamp_plan"][0]["to_version"] == "0004"
        assert plan["requires_confirmation"] is True
        assert plan["confirm_token"]

        r = admin_client.delete(
            f"/api/v1/database-models/{model_id}/migrations/0003",
            params={"confirm_token": plan["confirm_token"]},
        )
        assert r.status_code == 200, r.text

    # La cadena cerró el hueco; las anteriores no se movieron.
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003", "0004"]
    # El puntero de la BD adelantada siguió el renombre; la de atrás no se tocó.
    assert log.calls == [("db_adelante", "0004")]
    assert estado == {"db_atras": "0001", "db_adelante": "0004"}


def test_el_checksum_se_recalcula_en_cada_renumerada(admin_client):
    """Sin esto el blueprint queda MUERTO.

    ``compute_checksum`` incluye la ``version``, y ``_verify_integrity`` lo recomputa y
    compara en cada apply, rollback, stamp y apply-all. Una versión renumerada sin
    recalcular su checksum hace que TODO el blueprint responda 409 "fue alterada".
    """
    model_id = _blueprint(admin_client, "del-cksum", ["0001", "0002", "0003"])

    with _engine({}):
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 200, r.text

    rows = _rows(model_id)
    assert set(rows) == {"0001", "0002"}
    for version, (_id, checksum, up_sql) in rows.items():
        assert checksum == compute_checksum(up_sql, None, None, None, version), (
            f"la versión {version} quedó con un checksum que no corresponde a su versión"
        )
    # La 0002 de ahora es la vieja 0003: el SQL viajó con el renombre.
    assert "t0003" in rows["0002"][2]


def test_verify_integrity_pasa_despues_del_renumerado(admin_client):
    """La prueba de que el blueprint sigue vivo, con el guard REAL (no una reimplementación)."""
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.core.database import Database

    model_id = _blueprint(admin_client, "del-integ", ["0001", "0002", "0003", "0004"])
    _managed_db(admin_client, model_id, 5607, "db_adelante", cached="0004")

    with _engine({"db_adelante": "0004"}):
        plan = admin_client.get(
            f"/api/v1/database-models/{model_id}/migrations/0002/delete-plan"
        ).json()["data"]
        r = admin_client.delete(
            f"/api/v1/database-models/{model_id}/migrations/0002",
            params={"confirm_token": plan["confirm_token"]},
        )
        assert r.status_code == 200, r.text

    s = Database().get_declarative_base_session()
    try:
        specs = ManagedMigrationController._load_specs(s, model_id)
        ManagedMigrationController._verify_integrity(specs)  # no debe lanzar
    finally:
        s.close()
    assert [sp.version for sp in specs] == ["0001", "0002", "0003"]


# --------------------------------------------------------------------------- #
# Fallos y compensación                                                        #
# --------------------------------------------------------------------------- #
def test_fallo_de_stamp_compensa_y_no_toca_el_blueprint(admin_client):
    """Si un puntero no se puede mover, los ya movidos vuelven y el blueprint queda igual."""
    model_id = _blueprint(admin_client, "del-comp", ["0001", "0002", "0003", "0004"])
    _managed_db(admin_client, model_id, 5608, "db_a", cached="0003")
    _managed_db(admin_client, model_id, 5609, "db_b", cached="0004")

    estado = {"db_a": "0003", "db_b": "0004"}
    log = _StampLog()
    log.fail_on = "db_b"  # la segunda del plan (orden por id)
    with _engine(estado, log):
        plan = admin_client.get(
            f"/api/v1/database-models/{model_id}/migrations/0002/delete-plan"
        ).json()["data"]
        r = admin_client.delete(
            f"/api/v1/database-models/{model_id}/migrations/0002",
            params={"confirm_token": plan["confirm_token"]},
        )
    assert r.status_code == 409, r.text
    pc = r.json()["detail"]["public_context"]
    assert pc["code"] == "model_migration.renumber_stamp_failed"
    assert pc["compensated"] is True
    # db_a se movió y volvió; db_b nunca se movió. El blueprint intacto.
    assert estado == {"db_a": "0003", "db_b": "0004"}
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003", "0004"]


def test_token_deja_de_valer_si_el_parque_se_movio(admin_client):
    """Defensa TOCTOU: el token está atado a la huella del parque que congeló el preview."""
    model_id = _blueprint(admin_client, "del-toctou", ["0001", "0002", "0003", "0004"])
    _managed_db(admin_client, model_id, 5610, "db_x", cached="0003")

    estado = {"db_x": "0003"}
    with _engine(estado):
        plan = admin_client.get(
            f"/api/v1/database-models/{model_id}/migrations/0002/delete-plan"
        ).json()["data"]
        estado["db_x"] = "0004"  # alguien le aplicó otra versión entre el preview y el delete
        r = admin_client.delete(
            f"/api/v1/database-models/{model_id}/migrations/0002",
            params={"confirm_token": plan["confirm_token"]},
        )
    assert r.status_code == 422, r.text
    assert _versions(admin_client, model_id) == ["0001", "0002", "0003", "0004"]


# --------------------------------------------------------------------------- #
# Banderas de política                                                         #
# --------------------------------------------------------------------------- #
def test_flags_deletable_ya_no_exige_ser_la_punta(admin_client):
    """``not_tip`` desapareció: una intermedia libre es borrable."""
    model_id = _blueprint(admin_client, "del-flags", ["0001", "0002", "0003"])
    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations?size=50")
    flags = {m["version"]: m for m in r.json()["data"]}
    assert flags["0001"]["deletable"] is True
    assert flags["0001"]["block_reason"] is None
    assert all(m["block_reason"] != "not_tip" for m in flags.values())


def test_flags_marcan_in_use_y_requires_stamps(admin_client):
    """La caché alimenta la UI: qué versión está tomada y cuál pediría mover punteros."""
    from app.core.database import Database
    from app.models.managed_database import ManagedDatabase

    model_id = _blueprint(admin_client, "del-flags2", ["0001", "0002", "0003"])
    db_id = _managed_db(admin_client, model_id, 5611, "db_c", cached="0002")
    s = Database().get_declarative_base_session()
    try:
        s.get(ManagedDatabase, db_id).model_version = "0002"
        s.commit()
    finally:
        s.close()

    r = admin_client.get(f"/api/v1/database-models/{model_id}/migrations?size=50")
    flags = {m["version"]: m for m in r.json()["data"]}
    # La BD está parada en 0002 → esa no se borra…
    assert flags["0002"]["deletable"] is False
    assert flags["0002"]["block_reason"] == "in_use"
    # …pero la 0001 sí, y borrarla implicaría mover el puntero de esa BD.
    assert flags["0001"]["deletable"] is True
    assert flags["0001"]["delete_requires_stamps"] is True
    # La 0003 está por delante de todas: nadie que stampear.
    assert flags["0003"]["delete_requires_stamps"] is False


def test_una_bd_nunca_posicionada_no_obliga_a_leer_el_motor(admin_client):
    """Anti-regresión del PRE-FILTRO: borrar no puede depender de que todos los motores vivan.

    Una BD con ``model_version`` nulo y sin historial exitoso nunca fue posicionada por el
    gateway —los cuatro caminos que la mueven (apply, rollback, stamp, stamp-on-adopt)
    escriben esa caché—, así que está en base: no puede estar parada en la versión que se
    borra ni adelante de ella. Sin este filtro, un solo motor caído en el blueprint
    bloquearía el borrado de CUALQUIER versión, incluidas las que nadie aplicó jamás.

    El doble deja el motor mudo justamente para probar que no se lo consulta: si se
    consultara, el fail-closed respondería 409 'unreadable'.
    """
    model_id = _blueprint(admin_client, "del-prefilt", ["0001", "0002", "0003"])
    _managed_db(admin_client, model_id, 5612, "db_virgen", cached=None)

    with _engine({"db_virgen": _UNREADABLE}) as log:
        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0002")
    assert r.status_code == 200, r.text
    assert log.calls == []
    assert _versions(admin_client, model_id) == ["0001", "0002"]


def test_hueco_en_la_numeracion_bloquea_antes_de_tocar_nada(admin_client):
    """Borde real: bajar un escalón puede caer en un hueco que no existe en la cadena.

    Con versiones 0001/0005/0007 y una BD parada en 0007, el renumerado la dejaría en
    0006 — una etiqueta que hoy no existe. Y el stamp corre ANTES del renumerado
    (``command.stamp`` tiene que resolver ambos extremos contra los archivos de revisión,
    que se generan de las versiones de HOY), así que ese destino tiene que existir ya.

    Los huecos existen porque ``create_migration`` acepta una ``version`` explícita. Se
    rechaza en el preflight, sin mover un solo puntero.
    """
    model_id = _blueprint(admin_client, "del-hueco", ["0001", "0005", "0007"])
    _managed_db(admin_client, model_id, 5613, "db_en_7", cached="0007")

    with _engine({"db_en_7": "0007"}) as log:
        r = admin_client.get(
            f"/api/v1/database-models/{model_id}/migrations/0005/delete-plan"
        )
        assert r.status_code == 200, r.text
        plan = r.json()["data"]
        assert plan["deletable"] is False
        assert plan["confirm_token"] is None
        assert plan["unstampable"] == [
            {"managed_database_id": 1, "current_version": "0007", "missing_target": "0006"}
        ]

        r = admin_client.delete(f"/api/v1/database-models/{model_id}/migrations/0005")
    assert r.status_code == 409, r.text
    assert (
        r.json()["detail"]["public_context"]["code"]
        == "model_migration.renumber_target_missing"
    )
    assert log.calls == []
    assert _versions(admin_client, model_id) == ["0001", "0005", "0007"]
