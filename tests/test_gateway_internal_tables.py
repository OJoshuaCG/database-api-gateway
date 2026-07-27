"""
Tests del aislamiento de la contabilidad INTERNA del gateway (``_gw_v_*``/``_gw_stg_*``).

Fallo REAL que motivó este archivo (2026-07-27, producción). Al comparar una BD origen
contra una BD gestionada destino, el snapshot estructural incluía la tabla de versión de
Alembic del destino (``_gw_v_{slug}``). El diff la veía "en el target y no en el source" y
emitía ``DROP TABLE _gw_v_{slug}``. Adoptado como versión de blueprint y aplicado:

    Running upgrade 0006 -> 0007
    (1146, "Table 'test_cirox_central_05._gw_v_test_cirox_main_central' doesn't exist")
    [SQL: UPDATE _gw_v_test_cirox_main_central SET version_num='0007' WHERE ... = '0006']

Alembic leyó la versión actual (de ahí el ``WHERE ... = '0006'``), ejecutó TODO el DDL
—incluido el DROP de su propia tabla de contabilidad— y murió al registrar la versión
nueva. La BD quedó con los cambios aplicados pero SIN puntero de versión, y en cuarentena.

Peor: como el fallo ocurre en la contabilidad de Alembic y NO en una sentencia del
``up_sql``, el checkpoint por sentencia marcaba ``last == total`` → no hay "aplicación
parcial" → la auto-reconciliación (``on_failure=auto``) no se dispara.

El caso SIMÉTRICO es igual de grave: si el ORIGEN es una BD gestionada y el destino no, el
diff emitiría ``CREATE TABLE _gw_v_{slug_del_origen}`` sobre el destino, inyectándole una
tabla de versión ajena con la versión de otro blueprint.
"""

import pytest

from app.exceptions import AppHttpException
from app.services.db_admin.identifiers import (
    GATEWAY_TABLE_PREFIXES,
    exclude_gateway_internal_tables,
    is_gateway_internal_table,
    references_gateway_internal_table,
)
from app.services.db_admin.migrations import version_table_name


# --------------------------------------------------------------------------- #
# El nombre que se CREA y el que se EXCLUYE no pueden divergir                 #
# --------------------------------------------------------------------------- #
def test_version_table_name_is_recognized_as_gateway_internal():
    """
    Invariante central: la tabla que ``version_table_name`` CREA tiene que ser
    reconocida como interna por el filtro que los snapshots aplican. Si alguien cambia
    el prefijo en un lado y no en el otro, el gateway vuelve a generar DDL contra su
    propia contabilidad — que es exactamente el bug de producción.
    """
    for slug in ("test_cirox_main_central", "whatsapp", "mi-blueprint", "a", "X" * 80):
        table = version_table_name(slug)
        assert is_gateway_internal_table(table), table


def test_gateway_prefixes_cover_the_documented_internal_tables():
    assert "_gw_v_" in GATEWAY_TABLE_PREFIXES      # tabla de versión de Alembic
    assert "_gw_stg_" in GATEWAY_TABLE_PREFIXES    # staging de la copia de datos


# --------------------------------------------------------------------------- #
# Clasificación                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "_gw_v_test_cirox_main_central",   # el caso exacto del incidente
        "_gw_v_whatsapp",
        "_gw_stg_a1b2c3d4e5f6",
        "_GW_V_MAYUSCULAS",                # el motor puede devolver otra caja
    ],
)
def test_internal_tables_are_detected(name):
    assert is_gateway_internal_table(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "users", "invoices", "gw_v_users", "_gw", "_gwv_x", "",
        # NO se filtra: es una tabla del usuario que solo empieza parecido.
        "_gateway_config",
        # gw_ou_* (función/trigger que PostgreSQL usa para emular ON UPDATE) NO es
        # contabilidad: implementa el comportamiento de una COLUMNA del usuario, así que
        # tiene que seguir visible o el diff intentaría recrearlo en cada corrida.
        "gw_ou_a1b2c3d4e5f6a7b8",
    ],
)
def test_user_tables_are_not_filtered(name):
    assert is_gateway_internal_table(name) is False


def test_exclude_filters_only_internal_tables_preserving_order():
    tables = [
        "_gw_v_test_cirox_main_central",
        "invoices",
        "_gw_stg_deadbeef",
        "users",
        "gw_ou_hash",
    ]
    assert exclude_gateway_internal_tables(tables) == ["invoices", "users", "gw_ou_hash"]


def test_exclude_accepts_a_generator():
    """Los adapters se lo pasan como generador desde el cursor, no como lista."""
    assert exclude_gateway_internal_tables(
        t for t in ("_gw_v_x", "users")
    ) == ["users"]


# --------------------------------------------------------------------------- #
# El snapshot estructural no puede ver la contabilidad del gateway             #
# --------------------------------------------------------------------------- #
def test_structural_snapshot_excludes_the_version_table(monkeypatch):
    """
    Reproduce el escenario del incidente a nivel de snapshot: una BD que en el motor
    tiene la tabla de versión de Alembic no debe reportarla como parte de su esquema.
    Si esta tabla apareciera, el diff la clasificaría como new/dropped y generaría DDL
    contra ella.
    """
    from app.services.db_admin import base_adapter as ba

    class _FakeInspector:
        def get_table_names(self, schema=None):
            # Lo que devuelve el motor REAL de una BD gestionada: sus tablas + la
            # contabilidad del gateway.
            return ["invoices", "_gw_v_test_cirox_main_central", "users"]

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # Doble liviano: se invoca el método REAL (sin ligar) sobre un objeto que solo aporta
    # los hooks que ``structural_snapshot`` usa. Evita implementar los ~13 métodos
    # abstractos de ServerAdapter, que no participan de este caso.
    class _FakeAdapter:
        dialect = "mysql"
        target = None

        def _inspect_schema(self, database):
            return database

        def _build_table_schema(self, insp, conn, database, table, schema):
            from app.services.db_admin.dtos import ColumnInfo, TableSchema

            return TableSchema(
                database=database, table=table,
                columns=[ColumnInfo(name="id", type="int", nullable=False)],
                primary_key=["id"], foreign_keys=[], indexes=[],
            )

        def _snapshot_views(self, *a):
            return []

        _snapshot_routines = _snapshot_views
        _snapshot_triggers = _snapshot_views
        _snapshot_sequences = _snapshot_views
        _snapshot_enum_types = _snapshot_views
        _snapshot_extensions = _snapshot_views
        _snapshot_events = _snapshot_views

    monkeypatch.setattr(ba, "database_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(ba, "inspect", lambda conn: _FakeInspector())

    snap = ba.ServerAdapter.structural_snapshot(_FakeAdapter(), "test_cirox_central_05")
    names = [t.table for t in snap.tables]
    assert names == ["invoices", "users"], names
    assert not any(is_gateway_internal_table(n) for n in names)


# --------------------------------------------------------------------------- #
# Barreras: crear/editar una migración que toque la contabilidad del gateway   #
# --------------------------------------------------------------------------- #
def test_sql_referencing_the_version_table_is_detected():
    """El SQL exacto que se generó en el incidente."""
    sql = "DROP TABLE `_gw_v_test_cirox_main_central`"
    assert references_gateway_internal_table(sql) == ["_gw_v_"]


def test_detection_is_case_insensitive_and_covers_staging():
    assert references_gateway_internal_table("drop table _GW_V_x") == ["_gw_v_"]
    assert references_gateway_internal_table("INSERT INTO _gw_stg_ab CDE") == ["_gw_stg_"]


def test_clean_sql_is_not_flagged():
    assert references_gateway_internal_table(
        "ALTER TABLE `invoices` ADD COLUMN `org_group` INT NULL"
    ) == []
    assert references_gateway_internal_table("") == []
    assert references_gateway_internal_table(None or "") == []


def test_create_migration_rejects_sql_touching_the_version_table():
    """
    Barrera de CREACIÓN: ni una migración escrita a mano puede reintroducir el bug.
    Cubre las 4 variantes de SQL (base, overrides por motor y rollback).
    """
    from app.controllers.model_migration_controller import ModelMigrationController

    reject = ModelMigrationController._reject_gateway_internal_sql

    # No lanza con SQL limpio.
    reject("ALTER TABLE `invoices` ADD COLUMN `x` INT", None, None, None)

    for variant in range(4):
        args = [None, None, None, None]
        args[variant] = "DROP TABLE `_gw_v_whatsapp`"
        with pytest.raises(AppHttpException) as exc:
            reject(*args)
        assert exc.value.status_code == 422
        assert "_gw_v_" in str(exc.value.context.get("gateway_internal_prefixes"))


def test_apply_guard_blocks_versions_created_before_the_fix():
    """
    Red de seguridad para versiones YA persistidas (como la 0007 del incidente): aplicarlas
    a OTRA base de datos del mismo blueprint repetiría el fallo. El guard corta antes de
    tocar el motor y nombra las versiones ofensoras.
    """
    from app.controllers.managed_migration_controller import ManagedMigrationController
    from app.services.db_admin.migrations import MigrationSpec

    def spec(version, up):
        return MigrationSpec(
            id=int(version), version=version, name="v", up_sql=up, up_sql_mysql=None,
            up_sql_postgresql=None, down_sql=None, checksum="c",
        )

    good = [spec("0006", "ALTER TABLE `invoices` ADD COLUMN `x` INT")]
    ManagedMigrationController._guard_gateway_internal_sql(good)  # no lanza

    bad = good + [spec("0007", "DROP TABLE `_gw_v_test_cirox_main_central`")]
    with pytest.raises(AppHttpException) as exc:
        ManagedMigrationController._guard_gateway_internal_sql(bad)
    assert exc.value.status_code == 409
    offenders = exc.value.public_context["offending_versions"]
    assert "0007" in offenders and "0006" not in offenders


# --------------------------------------------------------------------------- #
# Comparaciones YA PERSISTIDAS antes del fix (Opción A y Opción B)             #
# --------------------------------------------------------------------------- #
def test_stale_comparison_items_are_rejected_before_executing():
    """
    La causa raíz está corregida en el snapshot, pero una comparación calculada ANTES del
    fix sigue viva hasta que expire su TTL con el SQL malo persistido en
    ``schema_comparison_items``. La **Opción B** (``/execute``) NO pasa por
    ``create_migration``: ejecuta ese SQL directo con ``execute_adhoc``, así que necesita
    su propio guard.
    """
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    assert_clean = SchemaComparisonController._assert_no_gateway_internal_sql

    # Un conjunto limpio no se bloquea.
    assert_clean(
        [{"id": 1, "sql": "ALTER TABLE `invoices` ADD COLUMN `x` INT", "down_sql": None}],
        operation="execute:all",
    )

    # El SQL exacto del incidente, en la dirección DROP (target tenía la tabla).
    with pytest.raises(AppHttpException) as exc:
        assert_clean(
            [
                {"id": 1, "sql": "ALTER TABLE `invoices` ADD COLUMN `x` INT", "down_sql": None},
                {"id": 2, "sql": "DROP TABLE `_gw_v_test_cirox_main_central`", "down_sql": None},
            ],
            operation="execute:all",
        )
    assert exc.value.status_code == 409
    assert exc.value.public_context["offending_item_ids"] == [2]
    assert exc.value.public_context["recalculate_required"] is True


def test_stale_comparison_detects_the_inverse_direction_too():
    """
    Dirección SIMÉTRICA e igual de peligrosa: si el ORIGEN era una BD gestionada y el
    destino no, el diff emitía ``CREATE TABLE _gw_v_{slug_del_origen}``, inyectándole al
    target una tabla de versión ajena.
    """
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    with pytest.raises(AppHttpException) as exc:
        SchemaComparisonController._assert_no_gateway_internal_sql(
            [{"id": 7,
              "sql": "CREATE TABLE `_gw_v_blueprint_origen` (`version_num` VARCHAR(32) NOT NULL)",
              "down_sql": None}],
            operation="execute:custom",
        )
    assert exc.value.status_code == 409


def test_stale_comparison_also_checks_the_rollback_sql():
    """El ``down_sql`` persistido también se ejecuta (al revertir): entra en el guard."""
    from app.controllers.schema_comparison_controller import SchemaComparisonController

    with pytest.raises(AppHttpException):
        SchemaComparisonController._assert_no_gateway_internal_sql(
            [{"id": 3, "sql": "ALTER TABLE `t` ADD COLUMN `c` INT",
              "down_sql": "CREATE TABLE `_gw_v_x` (`version_num` VARCHAR(32))"}],
            operation="adopt",
        )
