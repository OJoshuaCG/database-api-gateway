"""
Tools que leen el CATÁLOGO del motor: ``list_objects``, ``get_schema`` y ``diff_schemas``.

Las tres pasan por ``ctx.open_readonly``: gate completo de la base, credencial de SOLO LECTURA del
servidor (nunca la pseudo-root) y sesión ``READ ONLY`` con timeouts propios. Ninguna acepta SQL
del agente, ni un nombre de base, ni un identificador de servidor o de proyecto: la base se pide
por ``database_id`` y el resto sale del inventario.

LA ECONOMÍA DE CONTEXTO ES UNA JERARQUÍA DE COSTO (plan 12 §6.2)
----------------------------------------------------------------
``list_objects`` es el índice barato (nombres por colección). ``get_schema`` es el detalle, y su
lista de objetos es **obligatoria y acotada**: no hay modo "dame todo", porque un ``get_schema``
sin lista sobre una base de 4000 tablas es la forma exacta de reventar el contexto.

LOS MAPEADORES SON LA LISTA BLANCA
----------------------------------
Cada salida se arma campo por campo desde el DTO interno hacia un modelo de ``app.schemas.mcp``
con ``extra="forbid"``. Los cuerpos de vistas, rutinas y triggers existen en el DTO interno y
**no tienen campo destino**: en ``get_schema`` salen como ``body_omitted_reason:
"scope_disabled"`` (un literal único: ``flag_off`` y ``too_large`` son razones de ``get_definition``).

``list_objects`` dice la VERDAD sobre los cuerpos de cada objeto: ``body_available`` sale del scope
``data.definitions`` del llamador y del motor/versión (``ToolContext.body_availability``), sin leer
ningún cuerpo. "Disponible" significa "se puede pedir con ``get_definition``", no "el motor lo va a
entregar": un privilegio faltante lo dice ``get_definition`` por objeto.
"""

from __future__ import annotations

from app.core.environments import MCP_MAX_OBJECTS, MCP_MAX_OBJECTS_PER_CALL
from app.exceptions import AppHttpException
from app.mcp.context import ToolContext
from app.mcp.tools._envelope import Tracker, clean, fingerprint, now_iso
from app.schemas import mcp as out
from app.services import mcp_catalog as codes

KINDS: tuple[str, ...] = ("table", "view", "routine", "trigger", "sequence")
#: ``list_objects`` suma ``event`` (MySQL/MariaDB). ``get_schema`` conserva ``KINDS``: no lee events.
LIST_KINDS: tuple[str, ...] = KINDS + ("event",)
_WITH_BODY = frozenset({"view", "routine", "trigger", "event"})
_NAME_MAX = 128
#: Motores donde ``information_schema.ROUTINES`` puede salir vacío por falta de privilegio, sin error.
_ENGINES_WITH_HIDDEN_ROUTINES = frozenset({"mysql", "mariadb"})


def _invalid(message: str) -> AppHttpException:
    return AppHttpException(
        message=message, status_code=422, public_context={"code": codes.CODE_INVALID_ARGUMENT}
    )


def _database_id(params: dict, key: str = "database_id") -> int:
    valor = params.get(key)
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise _invalid(f"'{key}' tiene que ser un entero (el id que devuelve list_databases).")
    return valor


def _warnings(resuelta, facade, *, bodies_requested: bool) -> list[out.WarningOut]:
    """Los warnings viajan en la respuesta: un límite que solo ve el operador no se compensa."""
    w: list[out.WarningOut] = []
    if resuelta.quarantined:
        w.append(
            out.WarningOut(
                code=codes.WARN_DATABASE_QUARANTINED,
                message=(
                    "La base está en cuarentena: su esquema no corresponde a ninguna versión "
                    "declarada del blueprint."
                ),
            )
        )
    engine = resuelta.database.engine
    if engine == "postgresql":
        w.append(
            out.WarningOut(
                code=codes.WARN_PG_PUBLIC_SCHEMA_ONLY,
                message="En PostgreSQL solo se lee el schema 'public'.",
            )
        )
    if not facade.consistent_structure:
        w.append(
            out.WarningOut(
                code=codes.WARN_MYSQL_STRUCTURE_NOT_ATOMIC,
                message=(
                    "El catálogo de este motor no es atómico: un ALTER concurrente puede verse a "
                    "mitad de la lectura."
                ),
            )
        )
    for _ in facade.warnings:
        # El texto de la degradación es del gateway, pero se resume en un código fijo: no hay
        # motivo para que el agente lea qué directiva rechazó el motor.
        w.append(
            out.WarningOut(
                code=codes.WARN_SESSION_DIRECTIVE_REJECTED,
                message="El motor rechazó una directiva de la sesión de lectura.",
            )
        )
        break
    if bodies_requested:
        w.append(
            out.WarningOut(
                code=codes.WARN_BODIES_UNAVAILABLE,
                message=(
                    "Los cuerpos de vistas, rutinas y triggers no se entregan acá: se piden con "
                    "get_definition, que exige el scope data.definitions."
                ),
            )
        )
    return w


def _envelope(
    data, *, resuelta, tracker: Tracker, warnings, engine_version: str | None = None
) -> dict:
    """
    El sobre común de las tools de catálogo.

    ``engine_version`` ya viene LIMPIO (``11.8.3``, ver ``ToolContext.engine_version``) y es
    ``None`` cuando la tool no tiene una conexión al motor de la que leerlo: este sobre nunca abre
    una conexión por la versión. ``DatabaseRefOut`` rechaza cualquier valor que no sean dígitos.
    """
    env = out.ToolEnvelope(
        notice=out.UNTRUSTED_NOTICE,
        data=data,
        source="managed_database",
        untrusted_content=True,
        untrusted_fields=tracker.untrusted,
        clipped_fields=tracker.clipped,
        objects_omitted=False,
        warnings=warnings,
        generated_at=now_iso(),
        database=out.DatabaseRefOut(
            database_id=resuelta.database.database_id,
            engine=resuelta.database.engine,
            engine_version=engine_version,
        ),
    )
    return env.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# list_objects                                                                 #
# --------------------------------------------------------------------------- #


def _kinds(params: dict) -> tuple[str, ...]:
    pedidos = params.get("kinds")
    if pedidos is None:
        return LIST_KINDS
    if not isinstance(pedidos, list) or not pedidos:
        raise _invalid("'kinds' tiene que ser una lista no vacía.")
    desconocidos = [k for k in pedidos if k not in LIST_KINDS]
    if desconocidos:
        raise _invalid(f"'kinds' admite solo {list(LIST_KINDS)}.")
    return tuple(k for k in LIST_KINDS if k in pedidos)


def _body_flags(kind: str, disponibilidad) -> tuple[bool | None, str | None]:
    """
    ``(body_available, unavailable_reason)`` de un objeto del índice. Tablas y secuencias no tienen
    cuerpo: ``(None, None)``. Para el resto, disponible si no hay razón, y si la hay, esa razón.
    """
    if kind not in _WITH_BODY:
        return None, None
    reason = disponibilidad.reasons.get(kind)
    return reason is None, reason


def _routines_not_visible_warning(
    *,
    engine: str,
    kinds: tuple[str, ...],
    engine_may_hide_routines: bool,
    routines_listed_count: int,
) -> out.WarningOut | None:
    """
    Aviso ``routines_not_visible`` de ``list_objects``, o ``None`` si no corresponde.

    Corresponde en MySQL/MariaDB cuando las rutinas forman parte del listado pedido Y se cumple
    alguna de dos señales: el motor/versión/bandera dice que puede ocultarlas, o el índice no listó
    NINGUNA. La segunda es la señal barata y veraz del caso real: sin privilegio de rutina,
    ``information_schema.ROUTINES`` devuelve cero filas sin error. Se mide sobre el índice completo
    y no sobre lo filtrado por ``name_prefix``: un prefijo que no coincide no es "cero rutinas".
    Nunca se afirma certeza: cero puede ser "no hay" o "no las veo". PostgreSQL no se toca.
    """
    if engine not in _ENGINES_WITH_HIDDEN_ROUTINES:
        return None
    if "routine" not in kinds:
        return None
    zero_routines_listed = routines_listed_count == 0
    if not engine_may_hide_routines and not zero_routines_listed:
        return None
    if engine_may_hide_routines:
        message = (
            "Este motor puede ocultar rutinas a la credencial de solo lectura: que una rutina no "
            "aparezca en el índice no prueba que no exista."
        )
    else:
        message = (
            "Cero rutinas listadas: puede que no existan o que esta cuenta no las vea. Si esperabas "
            "alguna, regenerá la credencial de solo lectura del servidor (MariaDB >= 11.3) o "
            "habilitá la lectura de cuerpos de rutinas (motores más antiguos)."
        )
    return out.WarningOut(code=codes.WARN_ROUTINES_NOT_VISIBLE, message=message)


def list_objects(ctx: ToolContext, params: dict) -> dict:
    """
    El índice barato de una base: tipo y nombre de cada objeto, sin estructura.

    ``kinds`` y ``name_prefix`` filtran, y si dejan algo afuera se avisa con
    ``mcp.warn.objects_omitted_by_filter``: el agente tiene que poder distinguir "la base no tiene
    vistas" de "pediste sin vistas". Superar ``MCP_MAX_OBJECTS`` es un ERROR que pide filtrar,
    nunca un recorte: una lista cortada le haría creer que no hay más.

    ``include_column_counts=true`` agrega ``column_count`` a cada tabla. Es el camino CARO (una
    consulta de catálogo por tabla), así que tiene un tope propio y más chico,
    ``MCP_MAX_OBJECTS_PER_CALL`` tablas, que se evalúa después del índice y ANTES de contar: si
    el filtro deja más tablas, es un error que pide acotar con ``name_prefix``.
    """
    database_id = _database_id(params)
    kinds = _kinds(params)
    prefijo = params.get("name_prefix")
    if prefijo is not None and (not isinstance(prefijo, str) or len(prefijo) > _NAME_MAX):
        raise _invalid(f"'name_prefix' tiene que ser una cadena de hasta {_NAME_MAX} caracteres.")
    con_conteos = _bool_param(params, "include_column_counts", False)

    with ctx.open_readonly(database_id) as (resuelta, facade):
        indice = facade.object_index()
        warnings = _warnings(resuelta, facade, bodies_requested=False)
        # UNA consulta de VERSION(), compartida entre la disponibilidad de cuerpos y
        # ``engine_version``: jamás un SHOW CREATE. Se calcula con la sesión abierta porque el
        # façade se cierra al salir.
        raw_server_version = facade.server_version()
        engine_version = ctx.public_engine_version(raw_server_version)
        disponibilidad = ctx.body_availability(resuelta, facade, raw_server_version)
        routines_listed_count = len(indice.get("routine", []))
        routines_not_visible_warning = _routines_not_visible_warning(
            engine=resuelta.database.engine,
            kinds=kinds,
            engine_may_hide_routines=disponibilidad.routines_may_be_hidden,
            routines_listed_count=routines_listed_count,
        )
        if routines_not_visible_warning is not None:
            warnings.append(routines_not_visible_warning)
        elegidos: list[tuple[str, str]] = []
        total = 0
        for kind in LIST_KINDS:
            for nombre in indice.get(kind, []):
                total += 1
                if kind in kinds and (not prefijo or str(nombre).startswith(prefijo)):
                    elegidos.append((kind, nombre))
        if len(elegidos) > MCP_MAX_OBJECTS:
            raise AppHttpException(
                message=(
                    f"La base tiene más de {MCP_MAX_OBJECTS} objetos con ese filtro. Acotá con "
                    "'kinds' o 'name_prefix': no se trunca, porque una lista cortada haría creer "
                    "que no hay más."
                ),
                status_code=413,
                public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
            )
        conteos: dict[str, int] = {}
        if con_conteos:
            tablas = [n for (k, n) in elegidos if k == "table"]
            if len(tablas) > MCP_MAX_OBJECTS_PER_CALL:
                raise AppHttpException(
                    message=(
                        f"Contar columnas de {len(tablas)} tablas supera el tope de "
                        f"{MCP_MAX_OBJECTS_PER_CALL} por llamada. Acotá con 'name_prefix' o "
                        "pedí el índice sin conteos."
                    ),
                    status_code=413,
                    public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
                )
            conteos = facade.column_counts(tablas)

    tracker = Tracker()
    objetos = [
        out.ObjectOut(
            kind=kind,
            name=clean(nombre),
            body_available=_body_flags(kind, disponibilidad)[0],
            unavailable_reason=_body_flags(kind, disponibilidad)[1],
            column_count=conteos.get(nombre) if (con_conteos and kind == "table") else None,
        )
        for (kind, nombre) in elegidos
    ]
    if len(objetos) < total:
        warnings.append(
            out.WarningOut(
                code=codes.WARN_OBJECTS_OMITTED_BY_FILTER,
                message=f"El filtro dejó afuera {total - len(objetos)} objeto(s) del índice.",
            )
        )
    data = out.ObjectIndexOut(objects=objetos, count=len(objetos))
    return _envelope(
        data, resuelta=resuelta, tracker=tracker, warnings=warnings, engine_version=engine_version
    )


# --------------------------------------------------------------------------- #
# check_freshness                                                              #
# --------------------------------------------------------------------------- #


def check_freshness(ctx: ToolContext, params: dict) -> dict:
    """
    ¿Sigue vigente lo que el agente leyó antes? Marcadores de versión, sin estructura ni datos.

    Abre la sesión de lectura (no una conexión suelta): así corre con los timeouts y el ``READ
    ONLY`` del MCP, que importan justo acá porque es la tool que un agente en loop más llama
    (plan 12 §6.2). La versión se lee con UN ``SELECT`` sobre la tabla de Alembic de la base, sin
    ``MigrationContext``, y una base sin tabla devuelve ``None`` sin crear nada.

    La regla de invalidación viaja en ``rule``: la versión es condición NECESARIA de frescura,
    nunca suficiente.
    """
    from app.controllers.target_resolution import freshness_facts

    database_id = _database_id(params)
    with ctx.open_readonly(database_id) as (resuelta, facade):
        version = facade.applied_version(resuelta.database.model_slug)
        warnings = _warnings(resuelta, facade, bodies_requested=False)
        engine_version = ctx.engine_version(facade)

    hechos = freshness_facts(resuelta.database.database_id, version)
    data = out.FreshnessOut(
        applied_version=clean(version),
        inventory_version=resuelta.database.model_version,
        trust=hechos["trust"],
        has_partial_application=hechos["has_partial_application"],
        blueprint=resuelta.database.model_slug,
        rule=out.FRESHNESS_RULE,
    )
    return _envelope(
        data,
        resuelta=resuelta,
        tracker=Tracker(),
        warnings=warnings,
        engine_version=engine_version,
    )


# --------------------------------------------------------------------------- #
# get_schema                                                                   #
# --------------------------------------------------------------------------- #


def _requested_objects(params: dict) -> list[tuple[str, str]]:
    """
    Valida ``objects`` ANTES de abrir la conexión: el tope pre-motor falla gratis y no toca la
    base del cliente. El dispatcher solo valida las claves de primer nivel; las de cada elemento
    se validan acá con la misma regla de schema cerrado.
    """
    crudos = params.get("objects")
    if not isinstance(crudos, list) or not crudos:
        raise _invalid("'objects' es obligatorio: una lista de {kind, name} (no hay 'dame todo').")
    if len(crudos) > MCP_MAX_OBJECTS_PER_CALL:
        raise AppHttpException(
            message=(
                f"Se pidieron {len(crudos)} objetos y el tope por llamada es "
                f"{MCP_MAX_OBJECTS_PER_CALL}. Partí el pedido en lotes."
            ),
            status_code=413,
            public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
        )
    pedidos: list[tuple[str, str]] = []
    for item in crudos:
        if not isinstance(item, dict) or set(item) - {"kind", "name"}:
            raise _invalid("Cada elemento de 'objects' es {kind, name} y nada más.")
        kind, name = item.get("kind"), item.get("name")
        if kind not in KINDS:
            raise _invalid(f"'kind' admite solo {list(KINDS)}.")
        if not isinstance(name, str) or not name or len(name) > _NAME_MAX:
            raise _invalid(f"'name' tiene que ser una cadena de 1 a {_NAME_MAX} caracteres.")
        if (kind, name) not in pedidos:
            pedidos.append((kind, name))
    return pedidos


def _bool_param(params: dict, key: str, default: bool) -> bool:
    valor = params.get(key, default)
    if not isinstance(valor, bool):
        raise _invalid(f"'{key}' tiene que ser booleano.")
    return valor


def _map_table(t, i: int, tracker: Tracker, *, indexes: bool, fks: bool) -> out.TableOut:
    base = f"data.objects[{i}]"
    columnas = []
    for j, c in enumerate(t.columns):
        computed = getattr(c, "computed", None)
        identity = getattr(c, "identity", None)
        columnas.append(
            out.ColumnOut(
                name=clean(c.name),
                type=clean(c.type),
                nullable=bool(c.nullable),
                default=clean(c.default),
                primary_key=bool(c.primary_key),
                autoincrement=bool(c.autoincrement),
                comment=tracker.free_text(c.comment, f"{base}.columns[{j}].comment"),
                collation=clean(c.collation),
                charset=clean(c.charset),
                generated_expression=clean(computed.sqltext) if computed else None,
                generated_stored=bool(computed.persisted) if computed else None,
                identity_always=bool(identity.always) if identity else None,
                on_update=clean(c.on_update),
            )
        )
    tabla = out.TableOut(
        kind="table",
        name=clean(t.table),
        comment=tracker.free_text(t.comment, f"{base}.comment"),
        columns=columnas,
        primary_key=[clean(c) for c in t.primary_key],
        indexes=[
            out.IndexOut(
                name=clean(ix.name),
                columns=[clean(c) for c in ix.columns],
                unique=bool(ix.unique),
                method=clean(ix.method),
                predicate=clean(ix.predicate),
                expressions=[clean(e) for e in ix.expressions],
                include_columns=[clean(c) for c in ix.include_columns],
            )
            for ix in t.indexes
        ]
        if indexes
        else None,
        foreign_keys=[
            out.ForeignKeyOut(
                name=clean(fk.name),
                columns=[clean(c) for c in fk.columns],
                referred_table=clean(fk.referred_table),
                referred_columns=[clean(c) for c in fk.referred_columns],
                on_delete=clean(fk.on_delete),
                on_update=clean(fk.on_update),
            )
            for fk in t.foreign_keys
        ]
        if fks
        else None,
        check_constraints=[
            out.CheckConstraintOut(name=clean(ck.name), expression=clean(ck.sqltext))
            for ck in t.check_constraints
        ],
        unique_constraints=[
            out.UniqueConstraintOut(name=clean(uc.name), columns=[clean(c) for c in uc.columns])
            for uc in t.unique_constraints
        ],
        identity_fingerprint="",
    )
    return _with_fingerprint(tabla)


def _with_fingerprint(obj):
    """El fingerprint se calcula sobre el DTO de SALIDA: describe exactamente lo que el agente ve."""
    datos = obj.model_dump(mode="json", exclude={"identity_fingerprint"})
    return obj.model_copy(update={"identity_fingerprint": fingerprint(datos)})


def _map_view(v) -> out.ViewOut:
    return _with_fingerprint(
        out.ViewOut(
            kind="view",
            name=clean(v.name),
            is_materialized=bool(v.is_materialized),
            columns=[clean(c) for c in v.columns],
            body_omitted_reason="scope_disabled",
            identity_fingerprint="",
        )
    )


def _map_routine(r) -> out.RoutineOut:
    return _with_fingerprint(
        out.RoutineOut(
            kind="routine",
            name=clean(r.name),
            routine_kind=clean(r.kind),
            parameters=[
                out.RoutineParamOut(name=clean(p.name), mode=clean(p.mode), type=clean(p.type))
                for p in r.parameters
            ],
            return_type=clean(r.return_type),
            language=clean(r.language),
            deterministic=r.deterministic,
            body_omitted_reason="scope_disabled",
            identity_fingerprint="",
        )
    )


def _map_trigger(t) -> out.TriggerOut:
    return _with_fingerprint(
        out.TriggerOut(
            kind="trigger",
            name=clean(t.name),
            table=clean(t.table),
            timing=clean(t.timing),
            events=[clean(e) for e in t.events],
            level=clean(t.level),
            body_omitted_reason="scope_disabled",
            identity_fingerprint="",
        )
    )


def _map_sequence(s) -> out.SequenceOut:
    return _with_fingerprint(
        out.SequenceOut(
            kind="sequence",
            name=clean(s.name),
            data_type=clean(s.data_type),
            increment=s.increment,
            min_value=s.min_value,
            max_value=s.max_value,
            start_value=s.start_value,
            cycle=bool(s.cycle),
            identity_fingerprint="",
        )
    )


def get_schema(ctx: ToolContext, params: dict) -> dict:
    """
    La estructura de los objetos pedidos. Lo que no existe vuelve en ``missing``, explícito.

    Primero el índice (barato) y después el detalle solo de lo que existe: así un nombre
    inventado nunca llega a una consulta de catálogo por tabla.
    """
    database_id = _database_id(params)
    pedidos = _requested_objects(params)
    con_indices = _bool_param(params, "include_indexes", True)
    con_fks = _bool_param(params, "include_foreign_keys", True)

    with ctx.open_readonly(database_id) as (resuelta, facade):
        indice = {k: set(v) for k, v in facade.object_index().items()}
        existentes = [(k, n) for (k, n) in pedidos if n in indice.get(k, set())]
        faltantes = [(k, n) for (k, n) in pedidos if n not in indice.get(k, set())]
        por_kind: dict[str, list[str]] = {}
        for k, n in existentes:
            por_kind.setdefault(k, []).append(n)

        tablas = facade.table_schemas(por_kind.get("table", []))
        vistas = facade.views() if "view" in por_kind else []
        rutinas = facade.routines() if "routine" in por_kind else []
        triggers = facade.triggers() if "trigger" in por_kind else []
        secuencias = facade.sequences() if "sequence" in por_kind else []
        warnings = _warnings(resuelta, facade, bodies_requested=bool(set(por_kind) & _WITH_BODY))
        engine_version = ctx.engine_version(facade)

    tracker = Tracker()
    objetos: list = []
    for t in tablas:
        objetos.append(_map_table(t, len(objetos), tracker, indexes=con_indices, fks=con_fks))
    pedidas = {k: set(v) for k, v in por_kind.items()}
    objetos += [_map_view(v) for v in vistas if v.name in pedidas.get("view", set())]
    objetos += [_map_routine(r) for r in rutinas if r.name in pedidas.get("routine", set())]
    objetos += [_map_trigger(t) for t in triggers if t.name in pedidas.get("trigger", set())]
    objetos += [_map_sequence(s) for s in secuencias if s.name in pedidas.get("sequence", set())]

    data = out.SchemaOut(
        objects=objetos,
        missing=[out.ObjectRefOut(kind=k, name=clean(n)) for (k, n) in faltantes],
    )
    return _envelope(
        data, resuelta=resuelta, tracker=tracker, warnings=warnings, engine_version=engine_version
    )


# --------------------------------------------------------------------------- #
# diff_schemas                                                                 #
# --------------------------------------------------------------------------- #


def diff_schemas(ctx: ToolContext, params: dict) -> dict:
    """
    Qué difiere entre la estructura de dos bases que el token alcanza. Solo la lista de cambios.

    **Desvío aprobado del plan 12 §4** (``analyze`` estaba fuera de la v1 porque el preview REST
    entrega un ``confirm_token``). Lo que lo hace seguro acá, y que no se puede aflojar:

    - Se calcula **en memoria** con ``diff_snapshots`` (función pura): no se persiste ninguna
      ``SchemaComparison``, así que no existe un plan ejecutable ni un token que confirmarlo.
    - No se renderiza SQL: sin ``sql``, ``down_sql``, cuerpos ni valores por defecto. El DTO de
      salida no tiene campos donde puedan caer.
    - Las DOS bases pasan por el gate completo, cada una con su credencial de solo lectura. Una
      base de otro proyecto responde ``mcp.not_found`` igual que en las demás tools.

    ``source`` es el lado deseado y ``target`` el actual, con la misma convención que el diff REST.
    """
    from app.controllers.target_resolution import structural_changes

    origen = _database_id(params, "source_database_id")
    destino = _database_id(params, "target_database_id")
    if origen == destino:
        raise _invalid("'source_database_id' y 'target_database_id' tienen que ser distintas.")

    with ctx.open_readonly(origen) as (res_origen, fac_origen):
        snap_origen = fac_origen.snapshot()
        warnings = _warnings(res_origen, fac_origen, bodies_requested=False)
        # El bloque ``database`` del sobre describe el lado origen, así que la versión es la suya.
        engine_version = ctx.engine_version(fac_origen)
    with ctx.open_readonly(destino) as (res_destino, fac_destino):
        snap_destino = fac_destino.snapshot()
        for w in _warnings(res_destino, fac_destino, bodies_requested=False):
            if w.code not in {x.code for x in warnings}:
                warnings.append(w)

    cambios, cross_flavor = structural_changes(snap_origen, snap_destino)
    if len(cambios) > MCP_MAX_OBJECTS:
        raise AppHttpException(
            message=(
                f"Las bases difieren en más de {MCP_MAX_OBJECTS} objetos. No se trunca: una lista "
                "de cambios cortada haría creer que las bases son más parecidas de lo que son."
            ),
            status_code=413,
            public_context={"code": codes.CODE_TOO_MANY_OBJECTS},
        )
    data = out.SchemaDiffOut(
        source_database_id=res_origen.database.database_id,
        target_database_id=res_destino.database.database_id,
        changes=[
            out.SchemaChangeOut(
                object_type=c["object_type"],
                object_name=clean(c["object_name"]),
                parent_table=clean(c["parent_table"]),
                change_type=c["change_type"],
                changed_attributes=[clean(a) for a in c["changed_attributes"]],
                destructive=c["destructive"],
            )
            for c in cambios
        ],
        count=len(cambios),
        cross_flavor_warning=cross_flavor,
    )
    return _envelope(
        data,
        resuelta=res_origen,
        tracker=Tracker(),
        warnings=warnings,
        engine_version=engine_version,
    )
