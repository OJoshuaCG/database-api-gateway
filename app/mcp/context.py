"""
``ToolContext`` — la ÚNICA puerta del handler al plano gestionado.

ES LA CAPA QUE DE VERDAD CIERRA LA PUERTA, Y POR ESO ES LA PRIMERA
------------------------------------------------------------------
Un handler recibe esto y nada más: sin ``Server``, sin ``ServerTarget``, sin ``get_adapter``. No
porque sea prolijo, sino porque **no entrega nada reusable**: no hay ningún objeto que un tool
pueda guardar, replicar o apuntar a otra base.

Es la diferencia con confiar en un tipo "recibo del gate": un ``@dataclass(frozen=True)`` tiene
``__init__`` público y ``dataclasses.replace`` devuelve un objeto válido con el gate ya pasado. Un
contexto que no expone la credencial no tiene ese problema, porque no hay nada que reescribir.

Las tools que leen el catálogo del motor usan ``open_readonly(database_id)``, que resuelve, gatea
y rinde **el façade ya abierto** sobre la credencial de solo lectura — nunca la credencial.

LA CAPACIDAD LA FIJA EL DISPATCHER, NO LA TOOL
----------------------------------------------
``capability`` es el ``ToolSpec.scope`` de la tool que se está ejecutando. Las dos puertas de
abajo la usan para el eje 2 del gate, así que un handler no puede elegir una capacidad más débil
que la que declaró: no hay parámetro para hacerlo.
"""

from dataclasses import dataclass

from app.core.actor import Actor
from app.services.capability_catalog import Capability


@dataclass(frozen=True, slots=True)
class ToolContext:
    """
    Lo que un handler puede ver. ``actor`` es de solo lectura y frozen.

    No lleva la ``Request``: un handler no tiene por qué poder leer headers, cookies ni el
    cuerpo crudo. Lo que necesite del transporte se lo pasa el despachador ya normalizado.
    """

    actor: Actor
    capability: Capability

    @property
    def project_id(self) -> int:
        """El proyecto del token. Es el alcance de TODO lo que el agente puede ver."""
        return self.actor.project_id or 0

    def reachable_databases(self):
        """
        Las bases que este agente alcanza. Delega en el resolvedor único.

        El handler no filtra por proyecto ni por política: si lo hiciera, habría dos lugares
        donde está escrito el gate y uno de los dos se relajaría sin que nadie lo note.
        """
        from app.controllers.target_resolution import reachable_databases

        return reachable_databases(self.actor, self.capability)

    def open_readonly(self, database_id: int):
        """
        Context manager: gate completo de UNA base, credencial de solo lectura y sesión de
        lectura. Rinde ``(base_resuelta, facade)`` y cierra la sesión al salir.

        El façade solo tiene métodos de lectura (composición, no herencia): ver
        ``app/services/db_admin/readonly_introspector.py``.
        """
        from app.controllers.target_resolution import open_readonly

        return open_readonly(self.actor, database_id, self.capability)

    def list_blueprints(self):
        """
        Los blueprints visibles para este token (``BlueprintSummary``), sin SQL ni autoría.

        Visible es "vinculado a mi proyecto y a ningún otro": la regla vive en el resolvedor, que
        también responde el 413 si hay más de los que se pueden listar sin recortar.
        """
        from app.controllers.target_resolution import reachable_blueprints

        return reachable_blueprints(self.actor, self.capability)

    def list_blueprint_migrations(
        self, blueprint_id: int, after_version: str | None, limit: int
    ):
        """
        Una página de migraciones de un blueprint visible (``BlueprintMigrationPage``), sin cuerpos.

        Un blueprint que el token no ve —ajeno, compartido o inexistente— responde igual:
        ``mcp.not_found``. ``after_version`` y ``limit`` ya vienen validados por el handler.
        """
        from app.controllers.target_resolution import blueprint_migration_page

        return blueprint_migration_page(
            self.actor, self.capability, blueprint_id, after_version, limit
        )

    def assert_blueprint_sql_enabled(self) -> None:
        """
        Kill switch de ``get_blueprint_migration``: levanta 403 ``mcp.blueprint_sql_disabled`` si
        está apagado.

        Lo llama el handler ANTES de validar argumentos: apagado, no hay diferencia observable con
        una tool que no existe. ``get_blueprint_migration`` lo vuelve a mirar.
        """
        from app.controllers.target_resolution import assert_blueprint_sql_enabled

        assert_blueprint_sql_enabled()

    def get_blueprint_migration(self, blueprint_id: int, version: str):
        """
        El SQL de UNA migración de un blueprint visible (``BlueprintMigrationSql``), ya redactado.

        Recibe ``blueprint_id`` y ``version`` YA validados por el handler. Un blueprint que el token
        no ve y una versión que no existe responden igual: ``mcp.not_found``. La auditoría
        fail-closed y la lectura viven en ``target_resolution.read_blueprint_migration``; no abre
        ninguna conexión a un motor.
        """
        from app.controllers.target_resolution import read_blueprint_migration

        return read_blueprint_migration(self.actor, self.capability, blueprint_id, version)

    def draft_query(self, database_id: int, sql: str) -> dict:
        """
        Clasifica el SQL de un agente SIN ejecutarlo y devuelve el sobre ya armado.

        Pasa por el gate de la base pero no abre ninguna sesión: no hay façade ni credencial en
        esta ruta. Es la única puerta por la que texto SQL de un agente entra al paquete, y de
        ella solo sale texto (``touches_engine`` es siempre ``False``).
        """
        from app.controllers.target_resolution import draft_agent_query

        return draft_agent_query(self.actor, database_id, sql, self.capability)

    def sample_rows(
        self, database_id: int, table: str, columns: list[str] | None, limit: int | None
    ) -> dict:
        """
        Filas de una tabla del catálogo con la credencial de DATOS de la base. Recibe
        IDENTIFICADORES, nunca SQL: el gateway arma la sentencia, la pasa por el validador y la
        ejecuta en una transacción READ ONLY. El handler no ve credencial, target ni conexión: sale
        el sobre ya armado (filas como arreglos, marcadas no confiables).
        """
        from app.controllers.target_resolution import sample_rows_query

        return sample_rows_query(self.actor, database_id, table, columns, limit, self.capability)

    def distinct_values(
        self, database_id: int, table: str, column: str, limit: int | None
    ) -> dict:
        """Valores distintos de una columna del catálogo. Mismo camino y garantías que ``sample_rows``."""
        from app.controllers.target_resolution import distinct_values_query

        return distinct_values_query(self.actor, database_id, table, column, limit, self.capability)

    def run_select(self, database_id: int, sql: str, limit: int | None) -> dict:
        """
        Ejecuta el ``SELECT`` de un agente SOLO si pasa el validador compartido, dentro de una
        transacción READ ONLY y con la credencial de DATOS de la base. Cualquier otra cosa (write,
        ddl, bloqueado, ilegible) vuelve como el sobre del borrador, sin conexión. El handler no ve
        credencial, target ni conexión: sale el sobre ya armado.
        """
        from app.controllers.target_resolution import run_agent_select_query

        return run_agent_select_query(self.actor, database_id, sql, limit, self.capability)

    def count_rows(self, database_id: int, table: str) -> dict:
        """``COUNT(*)`` de una tabla del catálogo. Mismo camino y garantías que ``sample_rows``."""
        from app.controllers.target_resolution import count_rows_query

        return count_rows_query(self.actor, database_id, table, self.capability)

    def assert_definitions_enabled(self) -> None:
        """
        Kill switch de ``get_definition``: levanta 403 ``mcp.definitions_disabled`` si está apagado.

        Lo llama el handler ANTES de validar argumentos (apagado, ni siquiera se valida: no hay
        diferencia observable con una tool que no existe). ``get_definitions`` lo vuelve a mirar.
        """
        from app.controllers.target_resolution import assert_definitions_enabled

        assert_definitions_enabled()

    def get_definitions(self, database_id: int, objects: list[tuple[str, str, str | None]]):
        """
        El código de hasta 3 objetos de UNA base, ya redactado y medido (``DefinitionBatch``).

        Recibe ``(tipo, nombre, tipo_de_rutina)`` YA validados por el handler: nunca SQL. El gate,
        la auditoría fail-closed y la lectura viven en ``target_resolution.read_definitions``; el
        handler solo recibe resultados, sin façade ni credencial.
        """
        from app.controllers.target_resolution import read_definitions

        return read_definitions(self.actor, database_id, objects, self.capability)

    def redact_text(self, text: str) -> str:
        """
        Enmascara credenciales de un texto con el redactor de definiciones (best effort).

        Existe para la segunda pasada de ``Tracker.code_body``: el paquete no importa la capa de
        servicios, así que el redactor entra por el resolvedor, como el resto de las operaciones.
        No cuenta lo enmascarado: los conteos del aviso salen de la lectura (``DefinitionResult``).
        """
        from app.controllers.target_resolution import redact_definition_text

        return redact_definition_text(text)

    def get_table_stats(self, database_id: int, tables: list[str]):
        """
        Estadísticas de almacenamiento de hasta ``MCP_MAX_OBJECTS_PER_CALL`` tablas de UNA base
        (``TableStatsBatch``).

        Recibe NOMBRES ya validados por el handler: nunca SQL. El gate, el índice y la lectura
        viven en ``target_resolution.read_table_stats``; si el llamador ve el estimado de filas lo
        decide esa función según su scope, no el handler.
        """
        from app.controllers.target_resolution import read_table_stats

        return read_table_stats(self.actor, database_id, tables, self.capability)

    def body_availability(self, resuelta, facade, server_version: str | None = None):
        """
        Disponibilidad del cuerpo por tipo para este llamador. Ver ``target_resolution``.

        ``server_version`` es la cadena cruda ya leída, para no repetir el ``VERSION()``.
        """
        from app.controllers.target_resolution import body_availability

        return body_availability(self.actor, resuelta, facade, server_version)

    def engine_version(self, facade) -> str | None:
        """
        Versión LIMPIA del motor (``11.8.3``) leída con el façade ya abierto: sin conexión extra.

        Es lo que va en ``database.engine_version`` de la respuesta. Nunca devuelve la cadena
        cruda de ``VERSION()``: ver ``readonly_probe.public_engine_version`` para el porqué.
        """
        from app.controllers.target_resolution import read_engine_version

        return read_engine_version(facade)

    def public_engine_version(self, raw_version: str | None) -> str | None:
        """Igual que ``engine_version`` pero a partir de una cadena cruda que el handler ya leyó."""
        from app.controllers.target_resolution import public_engine_version

        return public_engine_version(raw_version)
