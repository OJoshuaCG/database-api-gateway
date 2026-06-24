# 07 — Gestión granular de permisos (GRANT/REVOKE cross-engine)

**Estado:** 🟡 Fase 1 implementada — Fase 2/3 pendiente · **Depende de:** 01 (inventario) ✅ · **Esfuerzo:** alto · **Criticidad:** alta (DCL dinámico)

Hoy el gateway solo otorga permisos **a nivel de base de datos completa** (`grant_database`/
`revoke_database`) con una validación de privilegios **laxa** (regex que acepta cualquier
palabra en mayúsculas). Este plan agrega la gestión de **todos** los privilegios reales de
MariaDB 11.x y PostgreSQL 17, en **todos los niveles de entidad**, con la regla dura:
**el gateway solo puede otorgar/revocar lo que su propia credencial puede delegar**.

> Fuentes verificadas: MariaDB GRANT/SHOW GRANTS + system tables; PostgreSQL 17 GRANT,
> Privileges (Tabla 5.2/ACL), System Information Functions. Catálogo confirmado contra
> documentación oficial; **falta** verificación contra motores reales (ver §8).

---

## 1. Catálogo real de privilegios por nivel

### MariaDB 11.x — niveles: `*.*` (global) · `db.*` · `db.tbl` · columna · rutina · proxy

- **Global-only (administrativos):** `SUPER`, `PROCESS`, `RELOAD`, `SHUTDOWN`, `FILE`,
  `CREATE USER`, `SHOW DATABASES`, `REPLICATION CLIENT`, `REPLICATION SLAVE`, y los del
  split de SUPER: `BINLOG ADMIN`, `BINLOG MONITOR`, `BINLOG REPLAY`, `CONNECTION ADMIN`,
  `FEDERATED ADMIN`, `READ_ONLY ADMIN`, `REPLICA MONITOR`, `REPLICATION MASTER ADMIN`,
  `REPLICATION SLAVE ADMIN`, `SET USER`, `SLAVE MONITOR`. *(El token `SHOW_ROUTINE` debe
  confirmarse con `SHOW PRIVILEGES;` en el motor real antes de fijarlo.)*
- **Database:** `CREATE`, `CREATE ROUTINE`, `CREATE TEMPORARY TABLES`, `CREATE VIEW`,
  `DROP`, `EVENT`, `GRANT OPTION`, `LOCK TABLES`, `ALTER ROUTINE`, `SHOW CREATE ROUTINE` +
  todos los de tabla.
- **Table:** `ALTER`, `CREATE`, `CREATE VIEW`, `DELETE`, `DELETE HISTORY`, `DROP`,
  `GRANT OPTION`, `INDEX`, `INSERT`, `REFERENCES`, `SELECT`, `SHOW VIEW`, `TRIGGER`, `UPDATE`.
- **Column (solo 4):** `INSERT`, `SELECT`, `UPDATE`, `REFERENCES`.
- **Routine (FUNCTION/PROCEDURE/PACKAGE):** `ALTER ROUTINE`, `EXECUTE`, `GRANT OPTION`.
- **Proxy:** `PROXY ON 'u'@'h'`.
- Especiales: `USAGE` (cero privilegios), `ALL [PRIVILEGES]` (todo el nivel, **excluye** `GRANT OPTION`).

### PostgreSQL 17 — niveles: database · schema · table · columna · sequence · routine · type/domain · language · FDW · foreign server · tablespace · large object · parameter

Matriz token→objeto (Tabla 5.2 oficial):

| Privilegio | Aplica a |
|---|---|
| `SELECT` | TABLE, columna, SEQUENCE, LARGE OBJECT |
| `INSERT` / `UPDATE` | TABLE, columna (`UPDATE` además SEQUENCE, LARGE OBJECT) |
| `DELETE` / `TRUNCATE` / `TRIGGER` | TABLE |
| `REFERENCES` | TABLE, columna |
| `MAINTAIN` *(PG17 nuevo)* | TABLE (VACUUM/ANALYZE/REINDEX/CLUSTER/REFRESH/LOCK) |
| `CREATE` | DATABASE, SCHEMA, TABLESPACE |
| `CONNECT` / `TEMPORARY` | DATABASE |
| `EXECUTE` | FUNCTION/PROCEDURE/ROUTINE |
| `USAGE` | SCHEMA, SEQUENCE, DOMAIN, TYPE, LANGUAGE, FDW, FOREIGN SERVER |
| `SET` / `ALTER SYSTEM` | PARAMETER |

- **Column (solo 4):** `SELECT`, `INSERT`, `UPDATE`, `REFERENCES`.
- **Roles predefinidos** (membresía): `pg_read_all_data`, `pg_write_all_data`, `pg_monitor`,
  `pg_maintain` (PG17), `pg_signal_backend`, `pg_read_server_files`, `pg_write_server_files`,
  `pg_execute_server_program`, etc.
- **PUBLIC:** pseudo-rol; defaults `CONNECT`+`TEMP` en DB, `EXECUTE` en funciones, `USAGE` en
  lenguajes/tipos. Conviene `REVOKE ... FROM PUBLIC` por menor privilegio.

---

## 2. Sintaxis GRANT/REVOKE (resumen)

- **MariaDB:** `GRANT priv [(cols)] ON priv_level TO 'user'@'host' [WITH GRANT OPTION]`.
  Niveles: `*.*` / `` `db`.* `` / `` `db`.`tbl` `` / `SELECT (`c1`,`c2`) ON ...` /
  `ON FUNCTION|PROCEDURE|PACKAGE db.fn`. `FLUSH PRIVILEGES` **NO** es necesario (y requiere
  `RELOAD`): **se elimina** de las operaciones nuevas.
- **PostgreSQL:** `GRANT priv|ALL ON <objeto> TO role [WITH GRANT OPTION]`. Objetos: `ON
  DATABASE/SCHEMA/[TABLE]/SEQUENCE/{FUNCTION|PROCEDURE|ROUTINE}/...`, `ON ALL ... IN SCHEMA`,
  columna `priv (col) ON tbl`. Objetos **futuros** requieren `ALTER DEFAULT PRIVILEGES [FOR
  ROLE owner] IN SCHEMA ...`. `REVOKE [GRANT OPTION FOR] ... [CASCADE|RESTRICT]` (default
  `RESTRICT`).
- Roles a roles: MariaDB `GRANT rol TO 'u'@'h' [WITH ADMIN OPTION]`; PG `GRANT rol TO role
  [WITH {ADMIN|INHERIT|SET} ...]`.

---

## 3. Introspección de grants actuales

- **MariaDB:** `information_schema.{USER,SCHEMA,TABLE,COLUMN}_PRIVILEGES` (columna
  `IS_GRANTABLE`); rutinas/proxy en `mysql.procs_priv`/`mysql.proxies_priv`; `SHOW GRANTS FOR`
  como respaldo (parsear texto). Grant option = `IS_GRANTABLE='YES'`.
- **PostgreSQL:** `aclexplode(relacl|nspacl|datacl|proacl|...)` → `(grantor, grantee,
  privilege_type, is_grantable)` (vía más completa); `information_schema.role_*_grants`;
  funciones `has_*_privilege(role, obj, 'PRIV WITH GRANT OPTION')`. Owner implícito
  (`relowner`/`datdba`/`nspowner`) **no** aparece en ACL → reportarlo aparte.

---

## 4. Modelo de datos cross-engine

```
GrantSpec
  grantee:            { name, host? }          # host solo MySQL/MariaDB
  level:              GLOBAL|DATABASE|SCHEMA|TABLE|COLUMN|SEQUENCE|ROUTINE|
                      TYPE|LANGUAGE|FDW|FOREIGN_SERVER|TABLESPACE|LARGE_OBJECT|
                      PARAMETER|PROXY|ROLE_MEMBERSHIP
  object_ref:         { database?, schema?, table?, columns?, routine?, sequence?, name? }
  privileges:         list[str]                # tokens canónicos validados por (motor, level)
  with_grant_option:  bool
  admin_option:       bool                     # solo ROLE_MEMBERSHIP
```

Sin equivalencia entre motores (documentar y degradar con 422 claro):
capa **SCHEMA** de PG (en MySQL schema≡database; un "acceso a BD" PG = CONNECT + USAGE/CREATE),
parte **host** de MySQL, **owner nativo** PG (derechos implícitos invisibles en ACL),
`MAINTAIN`/PARAMETER/type/lang/FDW/large-object (solo PG), `PROXY` y administrativos split
de SUPER (solo MariaDB), roles-a-roles con flags INHERIT/SET (PG16+, más rico que el
`ADMIN OPTION` único de MariaDB).

---

## 5. Extensión del contrato `ServerAdapter`

Nuevos DTOs en `dtos.py` (`GrantLevel`, `ObjectRef`, `GrantInfo`) y métodos abstractos:

```python
def grant_object(self, grantee, level, object_ref, privileges, *, with_grant_option=False) -> None
def revoke_object(self, grantee, level, object_ref, privileges, *, grant_option_only=False, cascade=False) -> None
def list_grants(self, grantee) -> list[GrantInfo]
def can_grant(self, level, object_ref, privileges) -> bool        # capability del grantor
```

Reglas de implementación (reutilizan `validate_identifier`/`quote_identifier`/
`quote_string_literal`/`_execute_server`/`_execute_database`):
- El privilegio **nunca** se interpola desde input: el token se valida contra un enum cerrado
  `{motor: {nivel: frozenset(tokens)}}` y se interpola la **constante interna**, no el string.
- Tabla de compatibilidad **privilegio × nivel × motor** (ej.: `EXECUTE` no aplica a tabla).
- Columnas: validar **elemento por elemento** (nunca como string con comas) y quotear cada una.
- Rutinas PG: resolver por identidad de catálogo/OID, no por firma escrita por el usuario.
- `grant_database`/`revoke_database` actuales se reimplementan como caso particular de
  `grant_object` (retrocompat).

---

## 6. Seguridad — política obligatoria (veredictos bloqueantes)

1. **Reemplazar `validate_privileges`** (regex laxo) por enumeraciones cerradas por motor y
   nivel, con mapeo a constantes internas y tabla de compatibilidad. *(bloqueante)*
2. **Pre-chequeo grantor-capability HÍBRIDO**: introspección `can_grant(...)` ANTES del GRANT
   (no confiar solo en el error del motor — la credencial pseudo-root casi nunca es rechazada)
   + el error del motor como segunda red (ventana TOCTOU) mapeado a 403/409. *(bloqueante)*
3. **Set DENY** (no otorgables por esta feature, nunca): MySQL `SUPER`, `FILE`, `PROCESS`,
   `SHUTDOWN`, `RELOAD`, `CREATE USER`, `GRANT OPTION` global, `SET USER`, dynamic admin privs;
   PG atributos `SUPERUSER/CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS` y roles predefinidos
   peligrosos (`pg_read/write_server_files`, `pg_execute_server_program`, ...). Nivel **global
   deshabilitado** salvo allowlist mínima. *(bloqueante)*
4. **Set GATE** (otorgables con **doble confirmación**, patrón del DROP actual): `WITH GRANT
   OPTION`/`ADMIN OPTION`, `ALL PRIVILEGES` a nivel db/schema, PG `MAINTAIN`, `CASCADE` en
   REVOKE, membresía de rol con alta concentración. *(bloqueante)*
5. **Auditoría ampliada** (`audit_log` + migración): `grantee`, `privilege`, `object_level`,
   `object_name`, `with_grant_option`, `grantor`. Auditar la **intención** (`status="attempt"`)
   antes de ejecutar para REVOKE y para el set GATE; esa auditoría de intención es
   **fail-closed** (si no se persiste, abortar — no best-effort silencioso). *(bloqueante)*
6. **Anti auto-lockout**: rechazar (409) REVOKE cuyo `grantee` resuelva a la propia credencial
   del gateway (o rol del que es miembro). **REVOKE sobre owner PG**: 409 (el owner conserva
   derechos implícitos; hay que reasignar ownership, no revocar). *(bloqueante)*
7. **CASCADE** en REVOKE solo con flag explícito + confirmación; default `RESTRICT`, y 409 con
   detalle de dependencias. Atributos de cuenta (`CREATEDB`, `CREATE USER`) van en endpoint
   aparte, no en el GRANT granular de objetos. *(recomendación)*

---

## 7. API propuesta

```
GET    /server-users/{id}/grants                 # introspección en vivo (list_grants)
POST   /server-users/{id}/grants                 # grant_object (body = GrantSpec)
DELETE /server-users/{id}/grants                 # revoke_object (body = GrantSpec + confirm/cascade)
GET    /servers/{id}/grantable                   # qué puede delegar el gateway (capability)
```
Todas con `ApiResponse[T]`, `AppHttpException`, admin autenticado, y `def` (I/O remoto).

---

## 8. Fases

- **Fase 1 (núcleo seguro) 🟡:** whitelists por motor/nivel + tabla de compatibilidad; reemplazo
  de `validate_privileges`; `grant_object`/`revoke_object`/`list_grants`/`can_grant`; niveles
  DATABASE, SCHEMA(PG), TABLE, COLUMN, SEQUENCE(PG), ROUTINE(EXECUTE); privilegios object-level
  (ALLOW) + GATE con confirmación; DENY de admin; ampliación de `AuditLog` + migración;
  endpoints `/grants` y `/grantable`; tests de contrato contra **motores reales** (Docker).
  — Parcialmente completo: endpoints y adapters listos y verificados; **AuditLog ampliado y
  tests de integración formales pendientes** (ver §11).
- **Fase 2:** membresía de roles (`GRANT rol TO rol`), default privileges generalizados a
  objetos futuros (sequences/functions), `MAINTAIN`/roles predefinidos gateados, endpoint de
  atributos de cuenta.
- **Fase 3:** niveles raros PG (type/lang/FDW/foreign server/tablespace/large object/parameter),
  `PROXY` y administrativos split de MariaDB, reconciliación inventario↔motor.

---

## 9. Verificación

- Catálogo (§1) y sintaxis (§2) verificados contra documentación oficial.
- **Pendiente contra motor real** (stack Docker `target-mariadb` 11.x / `target-postgres` 17):
  ejecución de cada GRANT/REVOKE por nivel, parseo de `SHOW GRANTS`/`aclexplode`, spelling de
  `SHOW_ROUTINE`, códigos de error nativos (extender `map_driver_error`).
- Batería de tests anti-evasión del enum y de `can_grant`; gate de CI que falle si aparece un
  privilegio no clasificado en DENY/GATE/ALLOW.
- `uv run pytest` en verde (incluida la suite de integración parametrizada por motor).

---

## 10. Implementado (incrementos entregados)

- **Validación (código, autoridad de seguridad):** `app/services/db_admin/privileges.py`
  — whitelist cerrada por (motor, nivel), clasificación ALLOW/GATE/DENY,
  `controlled_tokens()`/`token_is_sensitive()`. 35 tests.
- **Catálogo persistido (data-driven):** tabla `privileges` (`app/models/privilege.py`)
  con `engine, name, category, context, description, is_sensitive, is_active`. Se
  **siembra desde el catálogo de código** (`app/services/privilege_catalog.py` +
  `privilege_seed.py`) en el arranque, idempotente y **preservando el `is_active`** que
  toque un operador. Sirve para "traer solo los permisos activos de un motor" sin
  exponer los administrativos que no se controlan. Migración Alembic
  `a1b2c3d4e5f6_privileges_catalog`. API: `GET /api/v1/privileges?engine=&active=` y
  `PATCH /api/v1/privileges/{id}`. 14 tests (incl. consistencia catálogo↔código).
- **Carpeta `database/`:** representación SQL de referencia del esquema de metadatos,
  generada desde el ORM (`database/generate_schema.py`) — tablas, `schema.sql`, seed del
  catálogo y carpetas reservadas para vistas/procedimientos/triggers. Alembic sigue
  siendo la fuente de verdad.
- **Perfiles de permisos (plantillas):** tablas `permission_profiles` +
  `permission_profile_items` (`app/models/permission_profile.py`) + migración
  `d4e5f6a7b8c9_permission_profiles`. CRUD completo en `/api/v1/permission-profiles`
  (GET/POST/GET{id}/PATCH/DELETE). **DECISIÓN: clasificados POR MOTOR** — los privilegios
  no son portables entre motores, así que cada perfil tiene `engine` y sus items se
  **validan contra el catálogo cerrado** de ese motor al crearse (privilegio inválido/
  DENY/nivel no soportado → 422). **Modelo SNAPSHOT**: un perfil es solo una plantilla
  (nivel → privilegios); asignarlo aplicará los GRANT en el momento, SIN relación viva
  usuario↔perfil. Un item con privilegios GATE expone `requires_confirmation=true`. 12 tests.
- **GRANT/REVOKE granular — primitivo de adapter (keystone, incremento 1):**
  `grant_object`/`revoke_object` en `base_adapter` (abstractos) + `MySQLAdapter`/
  `MariaDBAdapter` y `PostgresAdapter`. DTOs `ObjectRef`/`RoutineRef`. Niveles: MySQL
  DATABASE/TABLE/COLUMN/ROUTINE; PG DATABASE/SCHEMA/TABLE/COLUMN/SEQUENCE/ROUTINE
  (DATABASE a nivel servidor; el resto conectado a la BD). Privilegios validados contra
  el catálogo cerrado; identificadores quoteados; columnas validadas una a una;
  `GRANT OPTION` → cláusula `WITH GRANT OPTION` (MySQL). 23 tests del DCL generado.
- **`list_grants` (introspección en vivo):** implementado en `MySQLAdapter`/`MariaDBAdapter`
  y `PostgresAdapter`. MariaDB: UNION de 4 vistas `information_schema`
  (`USER_PRIVILEGES`, `SCHEMA_PRIVILEGES`, `TABLE_PRIVILEGES`, `COLUMN_PRIVILEGES`).
  PostgreSQL: UNION de `role_table_grants`, `role_column_grants`, `role_routine_grants`,
  `role_usage_grants`; **requiere** parámetro `database` (422 si no se pasa).
  Verificado contra `gw-it-mariadb` (127.0.0.1:33061) y `gw-it-postgres` (127.0.0.1:54321).
- **`can_grant` (pre-chequeo de capability del grantor):** implementado en ambos adapters.
  MariaDB: consulta `USER_PRIVILEGES` de `CURRENT_USER` con `IS_GRANTABLE='YES'`; bugfix:
  `GRANT OPTION` nunca aparece como `PRIVILEGE_TYPE` — se detecta con `bool(grantable)`.
  PostgreSQL: check de `rolsuper` primero; si no, `has_*_privilege(current_user, obj,
  'PRIV WITH GRANT OPTION')`. Verificado contra motores reales.
- **Endpoints GRANT/REVOKE/LIST/GRANTABLE/PROVISION/APPLY-PROFILE (22 checks MariaDB,
  16 checks PostgreSQL — todos OK):**
  - `GET /api/v1/server-users/{id}/grants?database=` — introspección de grants efectivos
    (`list_grants`).
  - `POST /api/v1/server-users/{id}/grants` — otorgar privilegios con pre-check
    `can_grant` → 403 si el admin no puede delegar.
  - `DELETE /api/v1/server-users/{id}/grants` — revocar privilegios.
  - `POST /api/v1/servers/{id}/grantable` — verificar capability del admin del gateway.
  - `POST /api/v1/server-users/provision` — crear usuario + aprovisionar + grants
    iniciales (best-effort: grants fallan sin abortar el provisioning).
  - `POST /api/v1/server-users/{id}/apply-profile/{profile_id}` — aplicar perfil
    guardado (best-effort).
  Schemas Pydantic: `app/schemas/grant.py` (`GrantRequest`, `RevokeRequest`,
  `GrantableRequest`, `GrantableResult`, `LevelObjectMapping`, `ApplyProfileRequest`,
  `ApplyProfileResult`); `app/schemas/server_user.py` (`GrantOnCreate`,
  `ServerUserFullCreate`, `GrantApplyResult`, `ServerUserFullOut`).
  Controllers: `app/controllers/grant_controller.py`,
  `app/controllers/server_user_controller.py` (`provision_with_grants`).
- **Estado de tests:** 251/251 tests pytest en verde. Scripts de verificación end-to-end
  corridos contra motores reales (stack Docker). Catálogo cerrado cableado a todos los
  flujos DCL.

---

## 11. Pendiente (Fase 1 incompleto)

Los siguientes ítems forman parte del alcance de la Fase 1 pero **no se implementaron** en
este incremento:

- **`AuditLog` ampliado:** agregar campos granulares (`grantee`, `privilege`,
  `object_level`, `object_name`, `with_grant_option`, `grantor`) al modelo existente +
  migración Alembic correspondiente. Hoy las operaciones DCL se registran en el log
  genérico pero sin los campos específicos de DCL.
- **Tests de integración formales:** batería `@pytest.mark.integration` parametrizada por
  motor (MariaDB / PostgreSQL) que ejecute cada combinación de GRANT/REVOKE/LIST en el
  stack Docker. Hoy la verificación fue manual con scripts end-to-end; no está
  automatizada en CI.
- **Anti auto-lockout explícito (código):** la regla "rechazar REVOKE cuyo `grantee`
  resuelva a la credencial del gateway" (§6 punto 6) fue verificada implícitamente por el
  motor (el motor lo rechaza), pero no existe guard explícito en el controller que devuelva
  un 409 con mensaje claro antes de intentar la operación.
- **CASCADE en REVOKE con confirmación:** actualmente no soportado. `REVOKE ... CASCADE`
  sería bloqueante (RESTRICT por defecto); diseño e implementación con confirmación
  pendientes (§6 punto 7).
- **Fase 2/3:** membresía de roles (`GRANT rol TO rol`), default privileges generalizados,
  niveles raros PG (type/lang/FDW/large object/parameter), administrativos split de
  MariaDB, reconciliación inventario↔motor (ver §8 Fases).
