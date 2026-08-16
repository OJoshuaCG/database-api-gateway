"""
Tests del writer del artefacto ``sql`` (módulo 10, fase F3).

Cubre los casos 3, 4 y 5 del plan de pruebas del §13 —saneamiento, determinismo y valores
límite— más el caso de **escala**: consumo de memoria PLANO, que es criterio de aceptación
del §8.1 y no un adorno.

Todo corre **sin motor**: el writer recibe las filas por un ``RowSource`` y el DDL sale del
adapter, que a estas alturas solo renderiza texto. Esa costura es justamente lo que permite
verificar el saneamiento y el determinismo en un entorno sin Docker.

**Lo que estos tests NO pueden afirmar** (política de honestidad del proyecto): que el SQL
generado sea VÁLIDO en MySQL 8, MariaDB 11 o PostgreSQL 16. Eso lo decide
``scripts/verify_export_e2e.py`` (F6) ejecutando el artefacto contra una instancia limpia y
comparando el esquema resultante con ``diff_snapshots``. Hasta que esa prueba corra, todo lo
cross-engine de acá es **no verificado**.
"""

from __future__ import annotations

import dataclasses
import gzip
import io
import json
import tracemalloc
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.remote_engine import ServerTarget
from app.services import export_package as epkg
from app.services.db_admin import export_spec as espec
from app.services.db_admin import export_writer as ew
from app.services.db_admin.dtos import (
    ColumnInfo,
    ComputedInfo,
    ForeignKeyInfo,
    IndexInfo,
    RoutineInfo,
    SchemaSnapshot,
    TableSchema,
    ViewInfo,
)
from app.services.db_admin.mysql_adapter import MySQLAdapter
from app.services.db_admin.postgres_adapter import PostgresAdapter

# --------------------------------------------------------------------------- #
# Andamiaje                                                                    #
# --------------------------------------------------------------------------- #


def adapter_for(engine: str):
    cls = PostgresAdapter if engine == "postgresql" else MySQLAdapter
    return cls(ServerTarget(1, engine, "host", 3306, "u", "p"))


class FakeRows:
    """
    Fuente de filas en memoria. Registra las consultas para poder afirmar sobre el
    ``ORDER BY``, el ``WHERE`` y el ``LIMIT`` que el writer construyó.
    """

    def __init__(self, by_table: dict | None = None, counters: dict | None = None):
        self.by_table = by_table or {}
        self.counters = counters or {}
        self.queries: list[str] = []

    def iter_rows(self, select_sql: str, *, batch_rows: int = 1000):
        self.queries.append(select_sql)
        table = select_sql.split(" FROM ")[1].split()[0].strip('`"')
        data = self.by_table.get(table, [])
        yield from (data() if callable(data) else iter(data))

    def counter_value(self, table: str, column: str):
        return self.counters.get(table)


def demo_snapshot(engine: str = "mysql") -> SchemaSnapshot:
    users = TableSchema(
        database="tienda",
        table="users",
        columns=[
            ColumnInfo(
                name="id", type="bigint(20)", nullable=False,
                primary_key=True, autoincrement=True,
            ),
            ColumnInfo(
                name="email", type="varchar(255)", nullable=False, comment="correo"
            ),
            ColumnInfo(name="saldo", type="decimal(30,10)", nullable=True),
            ColumnInfo(
                name="dominio", type="varchar(64)", nullable=True,
                computed=ComputedInfo(
                    sqltext="substring_index(email,'@',-1)", persisted=True
                ),
            ),
        ],
        primary_key=["id"],
        foreign_keys=[],
        indexes=[IndexInfo(name="ix_email", columns=["email"], unique=False)],
        comment="tabla de usuarios",
        storage_options={
            "engine": "InnoDB",
            "charset": "utf8mb4",
            "collation": "utf8mb4_general_ci",
        },
    )
    orders = TableSchema(
        database="tienda",
        table="orders",
        columns=[
            ColumnInfo(
                name="id", type="bigint(20)", nullable=False,
                primary_key=True, autoincrement=True,
            ),
            ColumnInfo(name="user_id", type="bigint(20)", nullable=False),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeyInfo(
                name="fk_o_u", columns=["user_id"],
                referred_table="users", referred_columns=["id"],
            )
        ],
        indexes=[],
        storage_options={"engine": "InnoDB"},
    )
    return SchemaSnapshot(
        database="tienda",
        source_engine=engine,
        db_charset="utf8mb4",
        db_collation="utf8mb4_general_ci",
        tables=[users, orders],
        views=[ViewInfo(name="v_users", definition="select `id` from `users`")],
        routines=[
            RoutineInfo(
                name="sp_x", kind="PROCEDURE",
                body="CREATE PROCEDURE `sp_x`() BEGIN SELECT 1; END",
            )
        ],
    )


DEMO_OBJECTS = (
    espec.CatalogObject("table", "users"),
    espec.CatalogObject("table", "orders"),
    espec.CatalogObject("routine", "sp_x"),
    espec.CatalogObject("view", "v_users"),
)


def render(
    spec: espec.ExportSpec,
    *,
    engine: str = "mysql",
    snapshot: SchemaSnapshot | None = None,
    objects=DEMO_OBJECTS,
    data_tables=(),
    source: FakeRows | None = None,
    **target_kwargs,
) -> tuple[str, ew.ExportStats]:
    """Renderiza el artefacto completo y devuelve ``(texto, estadísticas)``."""
    target = ew.ExportTarget(
        database="tienda",
        engine=engine,
        objects=objects,
        data_tables=data_tables,
        **target_kwargs,
    )
    chunks: list[str] = []
    stats = ew.ExportStats()
    for chunk in ew.iter_sql(
        spec,
        target,
        snapshot or demo_snapshot(engine),
        adapter_for(engine),
        source or FakeRows(),
        stats,
    ):
        chunks.append(chunk)
    return "".join(chunks), stats


def data_item(stats: ew.ExportStats, table: str) -> ew.ExportItemStat:
    return next(
        i for i in stats.items if i.object_type == "table_data" and i.object_name == table
    )


# --------------------------------------------------------------------------- #
# Caso 3 del §13 — SANEAMIENTO                                                 #
# --------------------------------------------------------------------------- #


def test_por_defecto_emite_estructura_preambulo_y_epilogo():
    sql, stats = render(espec.ExportSpec())
    assert sql.startswith("-- =====")
    assert "CREATE TABLE `users`" in sql
    assert "SET FOREIGN_KEY_CHECKS = 0;" in sql
    # Un script que deja la sesión con FOREIGN_KEY_CHECKS=0 es un fallo grave (§8.4): el
    # epílogo no es opcional cuando hubo preámbulo.
    assert "SET FOREIGN_KEY_CHECKS = @_gw_fk_checks;" in sql
    assert stats.complete is True


def test_suprimir_comentarios_no_deja_ninguno():
    """El caso 3 literal: ``script_comments: false`` ⇒ ni un ``--`` en todo el artefacto."""
    sql, _ = render(
        espec.ExportSpec(sanitize=espec.SanitizeOptions(script_comments=False))
    )
    assert "--" not in sql
    assert "CREATE TABLE `users`" in sql  # y sigue siendo un artefacto útil


def test_comentarios_de_script_y_de_objeto_son_opciones_separadas():
    """
    Son cosas distintas y perderlas juntas sería una pérdida de información real: el
    ``COMMENT`` del esquema es parte de la DEFINICIÓN, el encabezado del script no.
    """
    con_todo, _ = render(espec.ExportSpec())
    assert "COMMENT 'correo'" in con_todo

    sin_objeto, _ = render(
        espec.ExportSpec(sanitize=espec.SanitizeOptions(object_comments=False))
    )
    assert "COMMENT 'correo'" not in sin_objeto
    assert sin_objeto.startswith("-- =====")  # el encabezado del script sigue

    sin_script, _ = render(
        espec.ExportSpec(sanitize=espec.SanitizeOptions(script_comments=False))
    )
    assert "COMMENT 'correo'" in sin_script


def test_opciones_de_motor_desaparecen_por_defecto_y_vuelven_si_se_piden():
    sin_opts, _ = render(espec.ExportSpec())
    assert "ENGINE=" not in sin_opts
    # El charset NO es una "opción de motor": lo gobierna charset_override.
    assert "DEFAULT CHARSET=utf8mb4" in sin_opts

    con_opts, _ = render(
        espec.ExportSpec(sanitize=espec.SanitizeOptions(engine_specific_options=True))
    )
    assert "ENGINE=InnoDB" in con_opts


@pytest.mark.parametrize(
    ("modo", "esperado"),
    [
        (espec.DefinerMode.omit, False),
        (espec.DefinerMode.auto, False),  # en MySQL, ``auto`` resuelve a ``omit``
    ],
)
def test_definer_omitido(modo, esperado):
    sql, _ = render(espec.ExportSpec(sanitize=espec.SanitizeOptions(definer=modo)))
    assert ("DEFINER=" in sql) is esperado


def test_definer_replace_inyecta_el_valor_validado():
    sql, _ = render(
        espec.ExportSpec(
            sanitize=espec.SanitizeOptions(
                definer=espec.DefinerMode.replace, definer_value="'app'@'localhost'"
            )
        )
    )
    assert "CREATE DEFINER=`app`@`localhost` PROCEDURE `sp_x`" in sql


def test_definer_replace_rechaza_un_valor_que_no_es_una_identidad():
    """
    ``definer_value`` es entrada del cliente que termina DENTRO del DDL: no se interpola, se
    parte en usuario/host y se valida cada parte.
    """
    from app.exceptions import AppHttpException

    with pytest.raises(AppHttpException):
        render(
            espec.ExportSpec(
                sanitize=espec.SanitizeOptions(
                    definer=espec.DefinerMode.replace,
                    definer_value="app`@`localhost`; DROP DATABASE x; -- ",
                )
            )
        )


def test_autoincrement_omit_no_deja_contador_ni_setval():
    fuente = FakeRows({"users": [(1, "a@x", Decimal("1.5"))]}, counters={"users": 500})
    sql, _ = render(
        espec.ExportSpec(
            data=espec.DataOptions(
                mode=espec.DataSelectionMode.include, names=("users",)
            ),
            sanitize=espec.SanitizeOptions(autoincrement=espec.AutoincrementMode.omit),
        ),
        data_tables=("users",),
        source=fuente,
    )
    assert "AUTO_INCREMENT =" not in sql
    assert "setval" not in sql


def test_autoincrement_keep_emite_el_contador():
    fuente = FakeRows({"users": [(1, "a@x", Decimal("1.5"))]}, counters={"users": 500})
    sql, _ = render(
        espec.ExportSpec(
            data=espec.DataOptions(
                mode=espec.DataSelectionMode.include, names=("users",)
            ),
            sanitize=espec.SanitizeOptions(autoincrement=espec.AutoincrementMode.keep),
        ),
        data_tables=("users",),
        source=fuente,
    )
    assert "ALTER TABLE `users` AUTO_INCREMENT = 500;" in sql


def test_autoincrement_auto_solo_para_tablas_con_datos():
    """Un ``AUTO_INCREMENT=5000`` en una tabla que sale vacía es basura que confunde."""
    fuente = FakeRows(counters={"users": 500, "orders": 7})
    sql, _ = render(
        espec.ExportSpec(
            sanitize=espec.SanitizeOptions(autoincrement=espec.AutoincrementMode.auto)
        ),
        data_tables=(),
        source=fuente,
    )
    assert "AUTO_INCREMENT =" not in sql


def test_constraints_deferred_pone_indices_y_fks_despues_de_los_datos():
    fuente = FakeRows({"users": [(1, "a@x", None)]})
    sql, _ = render(
        espec.ExportSpec(
            data=espec.DataOptions(mode=espec.DataSelectionMode.include, names=("users",))
        ),
        data_tables=("users",),
        source=fuente,
    )
    assert sql.index("INSERT INTO `users`") < sql.index("ADD CONSTRAINT `fk_o_u`")
    assert sql.index("INSERT INTO `users`") < sql.index("CREATE INDEX `ix_email`")


def test_constraints_inline_los_pega_a_su_tabla():
    sql, _ = render(
        espec.ExportSpec(
            sanitize=espec.SanitizeOptions(
                constraints_placement=espec.ConstraintsPlacement.inline
            )
        )
    )
    assert "-- Índices y claves foráneas" not in sql
    assert sql.index("CREATE INDEX `ix_email`") < sql.index("CREATE TABLE `orders`")


def test_envoltura_transaccional_va_despues_del_contenedor():
    """En PostgreSQL un CREATE/DROP DATABASE dentro de una transacción falla (§7.1)."""
    sql, _ = render(
        espec.ExportSpec(sanitize=espec.SanitizeOptions(transaction_wrap=True))
    )
    assert sql.index("USE `tienda`") < sql.index("START TRANSACTION;")
    assert sql.index("START TRANSACTION;") < sql.index("COMMIT;")


def test_charset_override_alcanza_al_script_y_a_las_tablas():
    sql, _ = render(
        espec.ExportSpec(
            sanitize=espec.SanitizeOptions(
                charset_override=espec.CharsetOverride(
                    mode=espec.CharsetOverrideMode.override,
                    charset="latin1",
                    collation="latin1_swedish_ci",
                )
            )
        )
    )
    assert "SET NAMES latin1;" in sql
    assert "DEFAULT CHARSET=latin1" in sql
    assert "utf8mb4" not in sql


def test_scope_y_entity_drop_create():
    sql, _ = render(
        espec.ExportSpec(
            structure=espec.StructureOptions(
                scope_ddl=espec.ScopeDdl.DROP_CREATE,
                entity_ddl=espec.EntityDdl.DROP_CREATE,
                confirm_scope_drop="tienda",
            )
        )
    )
    assert "DROP DATABASE IF EXISTS `tienda`;" in sql
    assert "CREATE DATABASE `tienda`" in sql
    assert "DROP TABLE IF EXISTS `users`;" in sql
    # El DROP de una rutina necesita su TIPO, que sale del payload del snapshot.
    assert "DROP PROCEDURE IF EXISTS `sp_x`;" in sql


def test_create_if_not_exists_avisa_de_los_tipos_que_no_lo_admiten():
    """
    Fail-closed: el ``IF NOT EXISTS`` de las rutinas depende de la VERSIÓN del motor destino,
    que el gateway no conoce. Se emite el CREATE normal y se declara.
    """
    sql, stats = render(
        espec.ExportSpec(
            structure=espec.StructureOptions(entity_ddl=espec.EntityDdl.CREATE_IF_NOT_EXISTS)
        )
    )
    assert "CREATE TABLE IF NOT EXISTS `users`" in sql
    assert any("idempotente" in w for w in stats.warnings)


def test_cuerpos_procedurales_van_envueltos_en_delimiter_y_las_vistas_no():
    sql, _ = render(espec.ExportSpec())
    assert "DELIMITER $$" in sql and "DELIMITER ;" in sql
    assert sql.count("DELIMITER $$") == 1  # solo la rutina, no la vista


def test_postgresql_no_lleva_delimiter_ni_use():
    sql, _ = render(espec.ExportSpec(), engine="postgresql")
    assert "DELIMITER" not in sql
    assert 'SET search_path TO "public";' in sql
    assert "RESET search_path;" in sql


def test_las_particiones_se_declaran_como_no_reproducidas():
    """Una tabla particionada restaurada sin particiones "funciona" y se degrada callada."""
    _, stats = render(espec.ExportSpec())
    assert any("particiones" in w.lower() for w in stats.warnings)


# --------------------------------------------------------------------------- #
# Caso 4 del §13 — DETERMINISMO                                                #
# --------------------------------------------------------------------------- #


def _tabla_simple(name: str, columnas, pk) -> SchemaSnapshot:
    return SchemaSnapshot(
        database="tienda",
        source_engine="mysql",
        tables=[
            TableSchema(
                database="tienda", table=name, columns=columnas,
                primary_key=pk, foreign_keys=[], indexes=[],
            )
        ],
    )


COLS_PK = [
    ColumnInfo(name="id", type="int", nullable=False, primary_key=True),
    ColumnInfo(name="v", type="varchar(10)", nullable=True),
]


def test_dos_corridas_identicas_dan_el_mismo_artefacto_byte_a_byte():
    snap = _tabla_simple("t", COLS_PK, ["id"])
    objetos = (espec.CatalogObject("table", "t"),)
    spec = espec.ExportSpec(data=espec.DataOptions(mode=espec.DataSelectionMode.all))
    filas = [(1, "a"), (2, "b")]

    primera, _ = render(
        spec, snapshot=snap, objects=objetos, data_tables=("t",),
        source=FakeRows({"t": filas}),
    )
    segunda, _ = render(
        spec, snapshot=snap, objects=objetos, data_tables=("t",),
        source=FakeRows({"t": filas}),
    )
    assert primera == segunda


def test_los_metadatos_volatiles_no_estan_en_el_script_salvo_que_se_pasen():
    """
    Fecha e id de job viven en el MANIFIESTO: si estuvieran siempre en el script, dos
    volcados del mismo esquema no se podrían diffear sin recortarles el encabezado (§8.3).
    """
    snap = _tabla_simple("t", COLS_PK, ["id"])
    objetos = (espec.CatalogObject("table", "t"),)

    limpio, _ = render(espec.ExportSpec(), snapshot=snap, objects=objetos)
    assert "Fecha:" not in limpio and "Job:" not in limpio

    con_meta, _ = render(
        espec.ExportSpec(), snapshot=snap, objects=objetos,
        generated_at="2026-08-16T10:00:00", job_id=7,
    )
    assert "Fecha: 2026-08-16T10:00:00" in con_meta and "Job: 7" in con_meta


def test_las_filas_se_ordenan_por_la_clave_primaria():
    snap = _tabla_simple("t", COLS_PK, ["id"])
    fuente = FakeRows({"t": [(1, "a")]})
    render(
        espec.ExportSpec(data=espec.DataOptions(mode=espec.DataSelectionMode.all)),
        snapshot=snap, objects=(espec.CatalogObject("table", "t"),),
        data_tables=("t",), source=fuente,
    )
    assert fuente.queries[0].endswith("ORDER BY `id`")


def test_sin_pk_pero_con_columnas_ordenables_se_ordena_por_la_tupla_completa():
    columnas = [
        ColumnInfo(name="a", type="int", nullable=True),
        ColumnInfo(name="b", type="varchar(5)", nullable=True),
    ]
    snap = _tabla_simple("np", columnas, [])
    fuente = FakeRows({"np": [(1, "x")]})
    _, stats = render(
        espec.ExportSpec(data=espec.DataOptions(mode=espec.DataSelectionMode.all)),
        snapshot=snap, objects=(espec.CatalogObject("table", "np"),),
        data_tables=("np",), source=fuente,
    )
    assert fuente.queries[0].endswith("ORDER BY `a`, `b`")
    assert data_item(stats, "np").deterministic is True


def test_sin_pk_y_con_una_columna_no_ordenable_se_marca_no_determinista():
    """
    La degradación HONESTA del §8.3. No es solo que el motor pueda rechazar el ORDER BY: en
    MySQL ordenar por un BLOB/TEXT trunca a ``max_sort_length`` y el empate vuelve a dejar el
    orden al azar. Fingir determinismo ahí sería mentir sobre la comparabilidad.
    """
    columnas = [
        ColumnInfo(name="a", type="int", nullable=True),
        ColumnInfo(name="b", type="longblob", nullable=True),
    ]
    snap = _tabla_simple("nb", columnas, [])
    fuente = FakeRows({"nb": [(1, b"\x00\x01")]})
    _, stats = render(
        espec.ExportSpec(data=espec.DataOptions(mode=espec.DataSelectionMode.all)),
        snapshot=snap, objects=(espec.CatalogObject("table", "nb"),),
        data_tables=("nb",), source=fuente,
    )
    assert "ORDER BY" not in fuente.queries[0]
    assert data_item(stats, "nb").deterministic is False
    assert any("orden garantizado" in w for w in stats.warnings)


def test_el_orden_de_emision_es_el_congelado_por_el_preview():
    """El writer NO reordena: ese orden es el que hasheó el ``confirm_token``."""
    sql, _ = render(espec.ExportSpec())
    assert sql.index("CREATE TABLE `users`") < sql.index("CREATE TABLE `orders`")
    # Rutinas ANTES que vistas (§8.4 corregido): una vista puede llamar a una función y
    # PostgreSQL la valida al crearla.
    assert sql.index("PROCEDURE `sp_x`") < sql.index("VIEW `v_users`")


# --------------------------------------------------------------------------- #
# Caso 5 del §13 — VALORES LÍMITE                                              #
# --------------------------------------------------------------------------- #

COLS_LIMITE = [
    ColumnInfo(name="id", type="int", nullable=False, primary_key=True),
    ColumnInfo(name="nulo", type="varchar(10)", nullable=True),
    ColumnInfo(name="vacio", type="varchar(10)", nullable=True),
    ColumnInfo(name="comillas", type="varchar(50)", nullable=True),
    ColumnInfo(name="salto", type="varchar(50)", nullable=True),
    ColumnInfo(name="multi", type="varchar(50)", nullable=True),
    ColumnInfo(name="bin", type="varbinary(10)", nullable=True),
    ColumnInfo(name="dec", type="decimal(40,20)", nullable=True),
    ColumnInfo(name="td", type="time", nullable=True),
    ColumnInfo(name="fecha", type="datetime", nullable=True),
    ColumnInfo(
        name="calc", type="int", nullable=True,
        computed=ComputedInfo(sqltext="id*2", persisted=True),
    ),
]

FILA_LIMITE = (
    1,
    None,
    "",
    "o'brien \"x\" \\z",
    "a\nb",
    "日本語 🎉",
    b"\x00\xff",
    Decimal("12345678901234567890.12345678901234567890"),
    timedelta(hours=100, minutes=2, seconds=3),
    datetime(1000, 1, 1, 0, 0, 0),
)


def _render_limite(spec=None, fila=FILA_LIMITE, filas=None):
    snap = _tabla_simple("lim", COLS_LIMITE, ["id"])
    fuente = FakeRows({"lim": filas if filas is not None else [fila]})
    sql, stats = render(
        spec or espec.ExportSpec(data=espec.DataOptions(mode=espec.DataSelectionMode.all)),
        snapshot=snap,
        objects=(espec.CatalogObject("table", "lim"),),
        data_tables=("lim",),
        source=fuente,
    )
    return sql, stats, fuente


def test_valores_limite_se_renderizan_como_literales_seguros():
    sql, _, _ = _render_limite()
    insert = sql[sql.index("INSERT INTO `lim`") :]
    assert ", NULL, ''," in insert            # NULL y cadena vacía son distinguibles
    assert "o''brien" in insert               # comilla simple escapada
    assert "日本語 🎉" in insert                # multibyte intacto
    assert "x'00ff'" in insert                # binario en hexadecimal
    # Decimal por ``str``, jamás por punto flotante: un DECIMAL(40,20) pasado por float
    # pierde dígitos en silencio y el artefacto deja de reproducir el origen.
    assert "12345678901234567890.12345678901234567890" in insert
    assert "100:02:03" in insert              # timedelta = el TIME de MySQL (admite >24 h)
    assert "1000-01-01 00:00:00" in insert    # fecha extrema


def test_las_columnas_generadas_quedan_fuera_del_insert_y_del_select():
    """Incluirlas produce un script que el motor rechaza en su PRIMERA fila."""
    sql, _, fuente = _render_limite()
    prefijo = sql[sql.index("INSERT INTO `lim`") :].splitlines()[0]
    assert "`calc`" not in prefijo
    assert "`calc`" not in fuente.queries[0]
    # …pero sí están en la definición: la columna existe, solo no se le asignan valores.
    assert "GENERATED ALWAYS AS" in sql


def test_un_tipo_no_soportado_corta_la_tabla_y_no_filtra_el_error_del_motor():
    class Raro:
        pass

    fila = (1, Raro(), "", "", "", "", None, None, None, None)
    sql, stats, _ = _render_limite(fila=fila)
    item = data_item(stats, "lim")
    assert item.status == "error"
    # Motivo de vocabulario CERRADO. Jamás ``str(exc)`` del driver: puede incrustar VALORES
    # de filas (criterio R4, §9.5) y convertir el reporte del job en una fuga.
    assert item.reason.startswith("unsupported_type:")
    assert stats.complete is False
    assert "EXPORTACIÓN INCOMPLETA" in sql


def test_el_corte_de_sentencia_lo_manda_el_tamano_en_bytes():
    """
    Con ``rows_per_statement`` alto y filas grandes, el corte tiene que venir igual: una
    tabla con LONGTEXT revienta cualquier límite basado en conteo, y un INSERT que supera el
    ``max_allowed_packet`` del destino produce un artefacto que no se puede ejecutar.
    """
    grande = "x" * 5000
    filas = [(i, grande, "", "", "", "", None, None, None, None) for i in range(20)]
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all,
            rows_per_statement=1000,
            max_statement_bytes=20000,
        )
    )
    sql, _, _ = _render_limite(spec=spec, filas=filas)
    assert sql.count("INSERT INTO `lim`") >= 5
    assert sql.count("\n  (") == 20  # ninguna fila se perdió en el camino


def test_rows_per_statement_es_un_techo_superior():
    filas = [(i, "a", "", "", "", "", None, None, None, None) for i in range(10)]
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all,
            rows_per_statement=3,
            max_statement_bytes=1048576,
        )
    )
    sql, _, _ = _render_limite(spec=spec, filas=filas)
    assert sql.count("INSERT INTO `lim`") == 4  # 3 + 3 + 3 + 1


def test_where_y_limit_por_objeto_llegan_al_select():
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all,
            per_object={"lim": espec.RowFilter(where="id > 5", limit=3)},
        )
    )
    _, _, fuente = _render_limite(spec=spec, filas=[])
    # El filtro va entre PARÉNTESIS: además de acotar su precedencia, es la mitad del par
    # de defensas que impide que un ``where`` terminado en comentario se coma el ORDER BY
    # y el LIMIT que vienen detrás (la otra mitad la pone ``validate_row_filter``).
    assert "WHERE (id > 5)" in fuente.queries[0]
    assert fuente.queries[0].endswith("LIMIT 3")


def test_sin_lista_de_columnas_pero_con_generadas_se_fuerza_la_lista():
    """
    Sin lista, el número de valores tiene que coincidir con el de columnas de la tabla — y
    no coincide, porque las generadas se excluyeron. Se fuerza en vez de emitir un INSERT
    que el motor rechaza.
    """
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all, include_column_list=False
        )
    )
    sql, stats, _ = _render_limite(spec=spec)
    assert "INSERT INTO `lim` (`id`," in sql
    assert any("columnas generadas" in w for w in stats.warnings)


@pytest.mark.parametrize(
    ("engine", "variante", "esperado"),
    [
        ("mysql", espec.InsertVariant.insert_ignore, "INSERT IGNORE INTO `lim`"),
        ("mysql", espec.InsertVariant.replace, "REPLACE INTO `lim`"),
        ("mysql", espec.InsertVariant.upsert, "ON DUPLICATE KEY UPDATE"),
    ],
)
def test_variantes_de_insert_por_motor(engine, variante, esperado):
    spec = espec.ExportSpec(
        data=espec.DataOptions(mode=espec.DataSelectionMode.all, insert_variant=variante)
    )
    sql, _, _ = _render_limite(spec=spec)
    assert esperado in sql


def test_insert_variant_none_no_emite_ninguna_fila():
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all, insert_variant=espec.InsertVariant.none
        )
    )
    sql, stats, _ = _render_limite(spec=spec)
    assert "INSERT" not in sql
    assert any("ninguna fila" in w for w in stats.warnings)


# --------------------------------------------------------------------------- #
# Escala (§13) — CONSUMO DE MEMORIA PLANO                                      #
# --------------------------------------------------------------------------- #


def test_el_consumo_de_memoria_es_plano_e_independiente_del_tamano_de_la_tabla():
    """
    Criterio de aceptación del §8.1, medido: se genera un artefacto de varios MB desde
    200 000 filas y el pico de memoria del generador se mantiene en el orden del tamaño de
    UNA sentencia, no del artefacto.

    El test consume ``iter_sql`` trozo a trozo (que es como lo va a consumir el
    almacenamiento de F4) y descarta cada trozo: si el writer bufferizara la tabla o el
    artefacto, el pico crecería con ``N`` y esta aserción fallaría.
    """
    n = 200_000

    def filas():
        for i in range(n):
            yield (i, "valor-%d" % i, "", "", "", "", None, None, None, None)

    snap = _tabla_simple("lim", COLS_LIMITE, ["id"])
    spec = espec.ExportSpec(
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all,
            rows_per_statement=200,
            max_statement_bytes=65536,
        )
    )
    target = ew.ExportTarget(
        database="tienda",
        engine="mysql",
        objects=(espec.CatalogObject("table", "lim"),),
        data_tables=("lim",),
    )
    stats = ew.ExportStats()
    generador = ew.iter_sql(
        spec, target, snap, adapter_for("mysql"), FakeRows({"lim": filas}), stats
    )

    total = 0
    pico = 0
    tracemalloc.start()
    try:
        for indice, trozo in enumerate(generador):
            total += len(trozo)
            if indice == 50:  # descartar el calentamiento (render del DDL)
                tracemalloc.reset_peak()
            elif indice > 50 and indice % 500 == 0:
                pico = max(pico, tracemalloc.get_traced_memory()[1])
    finally:
        tracemalloc.stop()

    assert stats.rows_exported == n
    assert total > 4_000_000, "el artefacto tiene que ser grande para que la prueba valga"
    assert pico < 4_000_000, f"memoria no plana: pico de {pico} bytes"


# --------------------------------------------------------------------------- #
# Saneamiento del snapshot (función pura)                                      #
# --------------------------------------------------------------------------- #


def test_sanitize_snapshot_filtra_por_seleccion_y_es_puro():
    snap = demo_snapshot()
    keys = frozenset({("table", "users"), ("routine", "sp_x")})
    limpio = ew.sanitize_snapshot(snap, espec.ExportSpec(), keys=keys)
    assert [t.table for t in limpio.tables] == ["users"]
    assert [r.name for r in limpio.routines] == ["sp_x"]
    assert limpio.views == []
    # El original no se toca: es una transformación, no una mutación.
    assert [t.table for t in snap.tables] == ["users", "orders"]


# =========================================================================== #
# F5 — formatos de datos, organización, fragmentación y compresión            #
# =========================================================================== #
# Los tres formatos de datos (csv/json/ndjson) y el empaquetado se prueban juntos porque
# son la misma pregunta vista dos veces: qué TEXTO produce cada formato y en qué ARCHIVOS
# termina ese texto. Todo sin motor y sin HTTP; lo que estos tests no pueden afirmar sigue
# siendo lo mismo que en el resto del archivo: que el artefacto sirva contra un motor real.

FILAS_CSV = {
    "users": [
        (1, "ana", None),                    # NULL en la última columna
        (2, "", Decimal("3.50")),            # cadena VACÍA: no es lo mismo que NULL
        (3, 'con "comillas", coma\ny salto', Decimal(-1)),
    ]
}


def solo_datos(fmt, **output_kwargs) -> espec.ExportSpec:
    """Un spec válido para un formato de datos (la matriz exige todo esto apagado)."""
    return espec.ExportSpec(
        format=fmt,
        structure=espec.StructureOptions(
            scope_ddl=espec.ScopeDdl.NONE, entity_ddl=espec.EntityDdl.NONE
        ),
        data=espec.DataOptions(
            mode=espec.DataSelectionMode.all, insert_variant=espec.InsertVariant.none
        ),
        sanitize=espec.SanitizeOptions(session_preamble=False),
        output=espec.OutputOptions(**output_kwargs),
    )


def render_datos(spec, *, filas=None, engine="mysql", objects=DEMO_OBJECTS,
                 data_tables=("users",), **target_kwargs):
    """Corre ``iter_artifact`` y devuelve ``(lista de Chunk, estadísticas)``."""
    target = ew.ExportTarget(
        database="tienda", engine=engine, objects=objects,
        data_tables=data_tables, **target_kwargs,
    )
    stats = ew.ExportStats()
    chunks = list(
        ew.iter_artifact(
            spec, target, demo_snapshot(engine), adapter_for(engine),
            FakeRows(by_table=filas if filas is not None else FILAS_CSV), stats,
        )
    )
    return chunks, stats


def texto_de(chunks, entry=None) -> str:
    return "".join(c.text for c in chunks if entry is None or c.entry == entry)


# --------------------------------------------------------------------------- #
# csv                                                                          #
# --------------------------------------------------------------------------- #


def test_csv_lleva_encabezado_filas_y_un_archivo_por_tabla():
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    chunks, stats = render_datos(spec)
    assert {c.entry for c in chunks} == {"users"}
    lineas = texto_de(chunks).splitlines()
    # La columna generada (``dominio``) no viaja: el motor la calcula y la rechazaría.
    assert lineas[0] == "id,email,saldo"
    assert lineas[1] == "1,ana,"
    assert data_item(stats, "users").rows_exported == 3


def test_csv_distingue_null_de_cadena_vacia():
    """
    El caso que ``render_value_text`` deja explícitamente al llamador: devuelve ``""`` para
    los dos. En un CSV la diferencia es real y se pierde para siempre al reimportar.
    """
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    chunks, _ = render_datos(spec)
    lineas = texto_de(chunks).splitlines()
    assert lineas[1].endswith(",")           # NULL: campo vacío SIN comillas
    assert lineas[2].startswith('2,"",')     # cadena vacía: SIEMPRE cuoteada


def test_csv_con_centinela_de_nulos_sigue_distinguiendo_la_cadena_literal():
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    spec = dataclasses.replace(spec, csv=espec.CsvOptions(null_representation="\\N"))
    filas = {"users": [(1, None, None), (2, "\\N", None)]}
    chunks, _ = render_datos(spec, filas=filas)
    lineas = texto_de(chunks).splitlines()
    assert lineas[1] == "1,\\N,\\N"      # el NULL, sin comillas
    assert lineas[2] == '2,"\\N",\\N'    # el texto que se le parece, cuoteado


def test_csv_respeta_el_dialecto_configurado():
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    spec = dataclasses.replace(
        spec,
        csv=espec.CsvOptions(
            delimiter=";",
            quote_char="'",
            escape_char="\\",
            line_terminator=espec.LineTerminator.crlf,
            header=False,
            bom=True,
        ),
    )
    chunks, _ = render_datos(spec, filas={"users": [(1, "a'b;c", None)]})
    texto = texto_de(chunks)
    assert texto.startswith("﻿")            # marca de orden de bytes
    assert "id;email;saldo" not in texto         # sin encabezado
    assert texto.endswith("\r\n")
    assert "'a\\'b;c'" in texto                  # cuoteado y escapado con la barra


@pytest.mark.parametrize(
    "codificacion,esperado", [("hex", "0a10"), ("base64", "ChA=")]
)
def test_csv_codifica_los_binarios_segun_binary_encoding(codificacion, esperado):
    spec = solo_datos(
        espec.Format.csv,
        organization=espec.Organization.per_object,
        binary_encoding=espec.BinaryEncoding(codificacion),
    )
    chunks, _ = render_datos(spec, filas={"users": [(1, b"\x0a\x10", None)]})
    assert esperado in texto_de(chunks)


@pytest.mark.parametrize(
    "valor,esperado",
    [
        (None, ""),                      # NULL
        ("", '""'),                      # cadena vacía: cuoteada para no ser un NULL
        ("simple", "simple"),
        ("con,coma", '"con,coma"'),
        ('con"comilla', '"con""comilla"'),
        ("con\nsalto", '"con\nsalto"'),
    ],
)
def test_csv_field_cuotea_solo_lo_que_hace_falta(valor, esperado):
    assert ew.csv_field(valor, espec.CsvOptions()) == esperado


def test_csv_no_emite_estructura_pero_la_reporta_como_omitida():
    """§14: lo que no viaja tiene que dejar huella, o nadie sabe que existía."""
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    _chunks, stats = render_datos(spec)
    omitidos = {i.object_name: i.reason for i in stats.items if i.status == "skipped"}
    assert omitidos["v_users"] == "format_data_only"
    assert omitidos["sp_x"] == "format_data_only"


# --------------------------------------------------------------------------- #
# json / ndjson                                                                #
# --------------------------------------------------------------------------- #


def test_json_en_un_solo_archivo_es_un_documento_valido_con_las_tablas_adentro():
    spec = solo_datos(espec.Format.json)
    chunks, _ = render_datos(spec)
    doc = json.loads(texto_de(chunks))
    assert doc["database"] == "tienda"
    assert doc["complete"] is True
    assert [f["id"] for f in doc["tables"]["users"]] == [1, 2, 3]


def test_json_por_objeto_es_un_arreglo_puro_por_tabla():
    spec = solo_datos(espec.Format.json, organization=espec.Organization.per_object)
    chunks, _ = render_datos(spec)
    filas = json.loads(texto_de(chunks, "users"))
    assert isinstance(filas, list)
    assert filas[0] == {"id": 1, "email": "ana", "saldo": None}


def test_json_usa_tipos_nativos_y_decimal_como_cadena():
    """
    Los enteros y los nulos salen nativos (si no, el formato no sirve para integrar); el
    ``Decimal`` sale como cadena porque JSON no tiene decimal exacto y pasarlo por punto
    flotante perdería dígitos en silencio.
    """
    spec = solo_datos(espec.Format.json)
    chunks, _ = render_datos(spec, filas={"users": [(7, "a", Decimal("12345678901.0000000001"))]})
    fila = json.loads(texto_de(chunks))["tables"]["users"][0]
    assert fila["id"] == 7 and fila["email"] == "a"
    assert fila["saldo"] == "12345678901.0000000001"


def test_json_value_mantiene_los_tipos_que_json_sabe_representar():
    assert ew.json_value(None, binary_encoding="hex") is None
    assert ew.json_value(True, binary_encoding="hex") is True
    assert ew.json_value(5, binary_encoding="hex") == 5
    assert ew.json_value(1.5, binary_encoding="hex") == 1.5
    assert ew.json_value(Decimal("1.10"), binary_encoding="hex") == "1.10"
    assert ew.json_value(timedelta(hours=26), binary_encoding="hex") == "26:00:00"


def test_ndjson_por_objeto_es_una_fila_pelada_por_linea():
    """Es la variante que se procesa en flujo: cada línea, un registro completo."""
    spec = solo_datos(espec.Format.ndjson, organization=espec.Organization.per_object)
    chunks, _ = render_datos(spec)
    lineas = [json.loads(x) for x in texto_de(chunks, "users").splitlines()]
    assert len(lineas) == 3
    assert lineas[0] == {"id": 1, "email": "ana", "saldo": None}


def test_ndjson_en_un_solo_archivo_envuelve_cada_linea_con_su_tabla():
    spec = solo_datos(espec.Format.ndjson)
    chunks, _ = render_datos(spec)
    lineas = [json.loads(x) for x in texto_de(chunks).splitlines()]
    assert all(set(l) == {"table", "row"} for l in lineas)
    assert lineas[0]["table"] == "users"


def test_el_manifiesto_es_descriptivo_y_dice_que_no_es_ejecutable():
    spec = solo_datos(espec.Format.json, schema_manifest=True)
    chunks, _ = render_datos(spec)
    manifiesto = json.loads(texto_de(chunks))["manifest"]
    assert manifiesto["executable"] is False
    assert "no es un script" in manifiesto["note"].lower()
    users = next(t for t in manifiesto["tables"] if t["name"] == "users")
    assert users["primary_key"] == ["id"]
    assert [c["name"] for c in users["columns"]][:2] == ["id", "email"]
    assert users["indexes"][0]["name"] == "ix_email"
    orders = next(t for t in manifiesto["tables"] if t["name"] == "orders")
    assert orders["foreign_keys"][0]["referred_table"] == "users"
    # No es un script: no hay ni una sentencia en todo el artefacto.
    assert "CREATE TABLE" not in texto_de(chunks)


def test_el_manifiesto_por_objeto_va_en_su_propio_archivo():
    spec = solo_datos(
        espec.Format.ndjson,
        organization=espec.Organization.per_object,
        schema_manifest=True,
    )
    chunks, _ = render_datos(spec)
    assert "_esquema" in {c.entry for c in chunks}
    assert json.loads(texto_de(chunks, "_esquema"))["executable"] is False


def test_un_tipo_no_soportado_corta_la_tabla_pero_deja_el_json_bien_formado():
    class Raro:
        pass

    spec = solo_datos(espec.Format.json)
    chunks, stats = render_datos(spec, filas={"users": [(1, "a", None), (2, Raro(), None)]})
    doc = json.loads(texto_de(chunks))  # tiene que parsear igual
    assert len(doc["tables"]["users"]) == 1
    assert doc["complete"] is False
    item = data_item(stats, "users")
    assert item.status == "error"
    assert item.reason == "unsupported_type:Raro"


def test_ndjson_incompleto_lo_dice_en_una_linea_final():
    class Raro:
        pass

    spec = solo_datos(espec.Format.ndjson)
    chunks, _ = render_datos(spec, filas={"users": [(1, Raro(), None)]})
    assert json.loads(texto_de(chunks).splitlines()[-1]) == {"incomplete": True}


@pytest.mark.parametrize("fmt", [espec.Format.csv, espec.Format.json, espec.Format.ndjson])
def test_dos_corridas_del_mismo_formato_de_datos_dan_el_mismo_texto(fmt):
    spec = solo_datos(fmt, organization=espec.Organization.per_object)
    primera, _ = render_datos(spec)
    segunda, _ = render_datos(spec)
    assert texto_de(primera) == texto_de(segunda)


# --------------------------------------------------------------------------- #
# Organización por objeto del script sql                                       #
# --------------------------------------------------------------------------- #


def test_sql_por_objeto_parte_el_script_con_el_orden_de_ejecucion_en_el_nombre():
    """
    §15.3: un archivo por objeto es lo que permite versionar el esquema. El prefijo numérico
    ES el orden de ejecución, así que el orden alfabético del directorio y el orden en que
    hay que correr los archivos tienen que coincidir.
    """
    spec = espec.ExportSpec(
        output=espec.OutputOptions(organization=espec.Organization.per_object)
    )
    chunks, _ = render_datos(spec, data_tables=("users",))
    nombres = list(dict.fromkeys(c.entry for c in chunks))
    assert nombres == sorted(nombres)
    assert nombres[0] == "00000-prologo"
    assert "10001-table-users" in nombres
    assert nombres[-1] == "90000-epilogo"
    # Cada archivo lleva lo suyo y nada más.
    assert "CREATE TABLE" in texto_de(chunks, "10001-table-users")
    assert "INSERT INTO" in texto_de(chunks, "50001-datos-users")


def test_sql_en_un_solo_archivo_no_cambio_con_f5():
    """El artefacto de F4 tiene que salir idéntico: ``iter_sql`` es la firma histórica."""
    spec = espec.ExportSpec()
    por_secciones, _ = render_datos(spec, data_tables=("users",))
    target = ew.ExportTarget(
        database="tienda", engine="mysql", objects=DEMO_OBJECTS, data_tables=("users",)
    )
    stats = ew.ExportStats()
    texto = "".join(
        ew.iter_sql(
            spec, target, demo_snapshot("mysql"), adapter_for("mysql"),
            FakeRows(by_table=FILAS_CSV), stats,
        )
    )
    assert texto_de(por_secciones) == texto
    assert all(c.entry is None for c in por_secciones)


# --------------------------------------------------------------------------- #
# Empaquetado: fragmentación y compresión (§10.3)                              #
# --------------------------------------------------------------------------- #
# Se prueba junto al writer porque es su otra mitad: el writer decide QUÉ texto va en cada
# archivo lógico y el empaquetador en qué archivo FÍSICO termina. Probar uno sin el otro
# deja fuera justo lo que puede salir mal (un fragmento sin encabezado, un zip sin cerrar).


def empaquetar(spec, chunks) -> tuple[bytes, epkg.ArtifactPackager]:
    salida = io.BytesIO()
    with epkg.packager(spec, salida.write, base_name="tienda-export") as pack:
        for chunk in chunks:
            pack.write(chunk)
        pack.finish(complete=True, job_id=7)
    return salida.getvalue(), pack


def generar(spec, **kwargs) -> tuple[bytes, epkg.ArtifactPackager, ew.ExportStats]:
    chunks, stats = render_datos(spec, **kwargs)
    blob, pack = empaquetar(spec, chunks)
    return blob, pack, stats


def test_sin_compresion_ni_troceado_el_artefacto_son_los_bytes_del_writer():
    spec = espec.ExportSpec()
    chunks, _ = render_datos(spec, data_tables=("users",))
    blob, pack = empaquetar(spec, chunks)
    assert blob.decode("utf-8") == texto_de(chunks)
    assert pack.part_count == 1
    assert epkg.artifact_extension(spec) == ".sql"
    assert epkg.content_type(spec) == "application/sql"


def test_gzip_comprime_el_mismo_flujo_y_es_reproducible():
    plano = espec.ExportSpec()
    comprimido = espec.ExportSpec(
        output=espec.OutputOptions(compression=espec.Compression.gzip)
    )
    esperado = texto_de(render_datos(plano, data_tables=("users",))[0])
    blob, _, _ = generar(comprimido, data_tables=("users",))
    assert gzip.decompress(blob).decode("utf-8") == esperado
    # Sin ``mtime=0`` dos corridas idénticas darían archivos distintos (§8.3).
    otra, _, _ = generar(comprimido, data_tables=("users",))
    assert blob == otra
    assert epkg.artifact_extension(comprimido) == ".sql.gz"
    assert epkg.content_type(comprimido) == "application/gzip"


def test_un_archivo_por_objeto_se_entrega_en_un_contenedor_zip():
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    blob, pack, _ = generar(spec)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        nombres = zf.namelist()
        assert "users.csv" in nombres
        assert zf.read("users.csv").decode("utf-8").startswith("id,email,saldo")
    # El contenedor NO se pidió: se eleva porque un multiarchivo no se entrega suelto.
    assert spec.output.compression == espec.Compression.none
    assert espec.effective_compression(spec) == espec.Compression.zip
    assert epkg.artifact_extension(spec) == ".zip"
    assert pack.part_count == len(nombres)


def test_el_contenedor_lleva_un_indice_con_el_orden_de_ejecucion():
    spec = espec.ExportSpec(
        output=espec.OutputOptions(organization=espec.Organization.per_object)
    )
    blob, _, _ = generar(spec, data_tables=("users",))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        indice = zf.read("000-INDICE.txt").decode("utf-8")
        entradas = [n for n in zf.namelist() if n.endswith(".sql")]
    assert "Orden de EJECUCIÓN" in indice
    for nombre in entradas:
        assert nombre in indice
    # El índice se escribe al final pero se lee primero: su nombre lo pone antes que todo.
    assert min([*entradas, "000-INDICE.txt"]) == "000-INDICE.txt"


def test_el_troceado_nombra_los_fragmentos_y_repite_el_encabezado():
    """
    ``{base}.part{NN}.{ext}``, y **cada fragmento con su fila de encabezado**: un ``part02``
    sin encabezado no lo lee igual ningún importador que el ``part01``.
    """
    spec = solo_datos(
        espec.Format.csv,
        organization=espec.Organization.per_object,
        split_max_bytes=1024,
    )
    filas = {"users": [(i, f"correo{i}@x.com", None) for i in range(200)]}
    blob, pack, _ = generar(spec, filas=filas)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        partes = [n for n in zf.namelist() if n.endswith(".csv")]
        cuerpos = {n: zf.read(n).decode("utf-8") for n in partes}
    assert partes == sorted(partes) and len(partes) > 2
    assert partes[0] == "users.part01.csv" and partes[1] == "users.part02.csv"
    assert all(c.startswith("id,email,saldo\n") for c in cuerpos.values())
    # Ninguna fila se parte por la mitad y no se pierde ninguna.
    total = sum(len(c.splitlines()) - 1 for c in cuerpos.values())
    assert total == 200
    assert pack.part_count == len(partes) + 1  # + el índice


def test_el_troceado_no_parte_una_fila_ni_deja_un_fragmento_solo_con_encabezado():
    spec = solo_datos(
        espec.Format.csv,
        organization=espec.Organization.per_object,
        split_max_bytes=1024,
    )
    # Una fila sola más grande que el tope: el fragmento se pasa, pero la fila viaja entera.
    filas = {"users": [(1, "x" * 4000, None), (2, "y" * 4000, None)]}
    blob, _, _ = generar(spec, filas=filas)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        cuerpos = [
            zf.read(n).decode("utf-8") for n in zf.namelist() if n.endswith(".csv")
        ]
    assert len(cuerpos) == 2
    for cuerpo in cuerpos:
        assert len(cuerpo.splitlines()) == 2  # encabezado + su fila, entera
    assert "x" * 4000 in cuerpos[0] and "y" * 4000 in cuerpos[1]


def test_ndjson_se_puede_trocear_porque_cada_linea_es_un_documento():
    spec = solo_datos(
        espec.Format.ndjson,
        organization=espec.Organization.per_object,
        split_max_bytes=1024,
    )
    filas = {"users": [(i, f"u{i}", None) for i in range(300)]}
    blob, _, _ = generar(spec, filas=filas)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        partes = [n for n in zf.namelist() if n.endswith(".ndjson")]
        lineas = [
            json.loads(linea)
            for n in partes
            for linea in zf.read(n).decode("utf-8").splitlines()
        ]
    assert len(partes) > 1
    assert [f["id"] for f in lineas] == list(range(300))


def test_un_artefacto_multiarchivo_sin_contenedor_es_un_fallo_del_empaquetado():
    """
    Fail-closed: concatenar dos archivos lógicos en un flujo suelto produciría un artefacto
    sin sentido (dos CSV pegados). Es una incoherencia interna, no un caso de uso.
    """
    spec = espec.ExportSpec()  # single, sin comprimir
    salida = io.BytesIO()
    with (
        pytest.raises(epkg.PackagingError),
        epkg.packager(spec, salida.write, base_name="x") as pack,
    ):
        pack.write(ew.Chunk("uno", "a"))
        pack.write(ew.Chunk("dos", "b"))


def test_el_tope_de_fragmentos_corta_antes_de_generar_miles_de_entradas():
    spec = solo_datos(
        espec.Format.csv,
        organization=espec.Organization.per_object,
        split_max_bytes=1024,
    )
    chunks, _ = render_datos(
        spec, filas={"users": [(i, f"u{i}", None) for i in range(500)]}
    )
    salida = io.BytesIO()
    pack = epkg.ArtifactPackager(spec, salida.write, base_name="x", max_parts=3)
    try:
        with pytest.raises(epkg.PackagingError):
            for chunk in chunks:
                pack.write(chunk)
    finally:
        pack.close()


def test_un_artefacto_incompleto_lleva_su_aviso_dentro_del_contenedor():
    spec = solo_datos(espec.Format.csv, organization=espec.Organization.per_object)
    chunks, _ = render_datos(spec)
    salida = io.BytesIO()
    with epkg.packager(spec, salida.write, base_name="x") as pack:
        for chunk in chunks:
            pack.write(chunk)
        pack.finish(complete=False, job_id=42)
    with zipfile.ZipFile(io.BytesIO(salida.getvalue())) as zf:
        aviso = zf.read("000-EXPORTACION-INCOMPLETA.txt").decode("utf-8")
    assert "INCOMPLETA" in aviso and "42" in aviso


def test_json_con_una_tabla_saltada_sigue_siendo_un_documento_valido():
    """
    Regresión: la coma que separa las tablas del envoltorio depende de lo YA EMITIDO, no de
    la posición en la lista. Con la primera tabla saltada (todas sus columnas generadas) el
    documento salía como ``{"tables":{,"orders":[…]}}`` y no parseaba.
    """
    snap = demo_snapshot()
    generadas = TableSchema(
        database="tienda",
        table="calculada",
        columns=[
            ColumnInfo(
                name="x", type="int", nullable=True,
                computed=ComputedInfo(sqltext="1", persisted=True),
            )
        ],
        primary_key=[],
        foreign_keys=[],
        indexes=[],
    )
    snap = SchemaSnapshot(
        database=snap.database, source_engine=snap.source_engine,
        tables=[generadas, *snap.tables],
    )
    spec = solo_datos(espec.Format.json)
    target = ew.ExportTarget(
        database="tienda", engine="mysql",
        objects=(espec.CatalogObject("table", "calculada"), *DEMO_OBJECTS),
        data_tables=("calculada", "users"),
    )
    stats = ew.ExportStats()
    texto = "".join(
        c.text
        for c in ew.iter_artifact(
            spec, target, snap, adapter_for("mysql"), FakeRows(by_table=FILAS_CSV), stats
        )
    )
    doc = json.loads(texto)
    assert list(doc["tables"]) == ["users"]
    assert next(i for i in stats.items if i.object_name == "calculada").reason == (
        "all_columns_generated"
    )


# --------------------------------------------------------------------------- #
# F6 — el artefacto no puede llevar el nombre de la base de ORIGEN adentro     #
# --------------------------------------------------------------------------- #
# MySQL/MariaDB guardan el cuerpo con el esquema CALIFICADO
# (``information_schema.VIEWS.VIEW_DEFINITION`` devuelve ``select `origen`.`t`…``).
# Emitirlo tal cual convierte el artefacto en una FUGA: restaurado en una base con OTRO
# nombre, la vista sigue leyendo de la base de origen, en silencio.


def _snapshot_con_cuerpos_calificados(engine="mysql") -> SchemaSnapshot:
    snap = demo_snapshot(engine)
    return SchemaSnapshot(
        database=snap.database,
        source_engine=snap.source_engine,
        db_charset=snap.db_charset,
        db_collation=snap.db_collation,
        tables=snap.tables,
        views=[
            ViewInfo(
                name="v_users",
                definition="select `tienda`.`users`.`id` from `tienda`.`users`",
            )
        ],
        routines=[
            RoutineInfo(
                name="sp_x",
                kind="PROCEDURE",
                body=(
                    "CREATE PROCEDURE `sp_x`() BEGIN "
                    "SELECT COUNT(*) FROM `tienda`.`orders`; END"
                ),
            )
        ],
    )


def test_los_cuerpos_no_llevan_el_calificador_de_la_base_de_origen():
    sql, _ = render(espec.ExportSpec(), snapshot=_snapshot_con_cuerpos_calificados())
    assert "`tienda`." not in sql
    # Y siguen siendo objetos completos, no cuerpos mutilados.
    assert "`users`.`id`" in sql
    assert "FROM `orders`" in sql


def test_una_referencia_a_otra_base_se_conserva():
    """
    Solo se quita el calificador PROPIO. Si el objeto de verdad cruza de base, eso es
    parte de su definición y borrarlo la cambiaría.
    """
    snap = _snapshot_con_cuerpos_calificados()
    snap = SchemaSnapshot(
        database=snap.database,
        source_engine=snap.source_engine,
        tables=snap.tables,
        views=[
            ViewInfo(
                name="v_users",
                definition="select `tienda`.`users`.`id` from `otra_db`.`espejo`",
            )
        ],
        routines=snap.routines,
    )
    sql, _ = render(espec.ExportSpec(), snapshot=snap)
    assert "`otra_db`.`espejo`" in sql
    assert "`tienda`." not in sql


def test_postgresql_no_se_toca():
    """
    ``strip_self_schema_qualifier`` es de la familia MySQL: PostgreSQL no incrusta el
    nombre de la BASE en el cuerpo (a lo sumo el ESQUEMA, que sí es parte de la
    definición). Aplicarlo ahí sería mutilar SQL válido.
    """
    snap = demo_snapshot("postgresql")
    snap = SchemaSnapshot(
        database=snap.database,
        source_engine="postgresql",
        tables=snap.tables,
        views=[ViewInfo(name="v_users", definition='select id from public."users"')],
    )
    sql, _ = render(
        espec.ExportSpec(),
        engine="postgresql",
        snapshot=snap,
        objects=(
            espec.CatalogObject("table", "users"),
            espec.CatalogObject("table", "orders"),
            espec.CatalogObject("view", "v_users"),
        ),
    )
    assert 'public."users"' in sql
