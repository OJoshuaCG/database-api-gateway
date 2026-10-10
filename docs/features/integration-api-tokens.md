# Tokens de integración y API `/integration`

Permite que el proyecto web de una persona automatice, con un **bearer REST**, un conjunto **cerrado** de
operaciones sobre las bases que administra el gateway: listar, crear una base vacía, crear un usuario del motor,
asignarle perfiles, asignar un blueprint, aplicar migraciones hacia adelante y, con un nivel aparte, revertir
migraciones y marcar versiones (`rollback`/`stamp`). No es el MCP (que es para agentes
de IA, de solo lectura) ni la sesión de la SPA: es una tercera credencial, con su propio vocabulario de scopes.

Contrato de rutas, cuerpos y códigos: [`api-reference-v45.md`](../api-reference-v45.md). El porqué, en
[`decisiones-e-incidentes.md`](../development/decisiones-e-incidentes.md) (sección "Tokens de integración").

Módulos: `app/core/integration_auth.py` (autenticación), `app/services/integration_scope_catalog.py`
(vocabulario), `app/controllers/integration_token_controller.py` y `app/routes/v1/integration_tokens.py`
(gestión), `app/controllers/integration_ops_controller.py` y `app/routes/v1/integration.py` (operaciones).

## Cómo funciona

```
persona (sesión + step-up) ──▶ POST /integration-tokens ──▶ datumint.<public_id>.<secret>  (una sola vez)

proyecto web ──▶ Authorization: Bearer datumint.… ──▶ require_integration(scope, target)
   1 kill switch → 2 credencial → 3 scope efectivo → 4 cupos por token
   → 5 allowlist de servidores / blueprints → 6 capa 2 (rol del emisor en el destino) → controlador
```

- **El token no tiene poder propio**: su alcance efectivo es `scopes guardados ∩ vocabulario cerrado ∩ lo que el
  emisor tiene hoy`, calculado en **cada** llamada. Bajarle el rol al emisor suspende el scope en la siguiente
  llamada; subírselo lo reactiva sin reemitir.
- **Cada scope mapea a una capacidad existente** (no se crean capacidades nuevas ni aparecen en `/authz/catalog`),
  y el emisor tiene que tener esa capacidad **y** el rol en el servidor de destino (capa 2).
- **Sin step-up en tiempo de ejecución**: una máquina no puede responder un prompt de contraseña. La persona lo
  paga al emitir, editar o revocar el token (todo método no seguro de la gestión).
- **El bearer y la sesión no se mezclan**: la cookie no autentica `/integration/*` y el bearer no autentica nada
  fuera de ahí (ni la gestión de tokens ni `/mcp/`). El chequeo 9 de `scripts/check_route_capabilities.py` lo
  verifica en CI.
- **Una base no se enumera**: servidor inexistente y servidor fuera de la allowlist dan el mismo `403`.

## Qué queda fuera, a propósito

Borrados de cualquier cosa, editar un servidor, revelar contraseñas, reconciliar parcialmente, aplicar a toda la
flota, los flags `force`/`purge`/`dry_run` del nivel destructivo, cualquier acceso a datos (`data.*`) y la gestión
de tokens por bearer.

## Nivel destructivo: `migrations.rollback` y `migrations.stamp`

Doce scopes en tres niveles (lectura, escritura, destructivo). Los dos destructivos se evalúan contra
`blueprints.apply` y viven con un sobre más estricto; el contrato completo está en
[`api-reference-v45.md`](../api-reference-v45.md).

- **Emisión**: agregar o editar un scope destructivo pide un **step-up propio** de la persona (`blueprints.apply`;
  la guarda de la ruta de gestión mira `access.admin`, no esa capacidad), allowlist de blueprints obligatoria y TTL
  máximo de 7 días. Con `STEP_UP_ENFORCED=false` rige el interruptor global, como en el resto del gateway.
- **En cada llamada**: la base debe tener un blueprint de la allowlist; entorno protegido o **sin clasificar**,
  cuarentena y contabilidad huérfana (stamp) se rechazan con `409` y código estable; cupo propio de `5/minute`.
- **Rollback**: `from_version` y `to_version` obligatorias (no hay "un paso" ni "a la base") y **prueba de
  historial**: solo se revierte lo que este gateway aplicó, con la definición que el blueprint tiene hoy. Un `stamp`
  no deja esa prueba, así que stamp y rollback encadenados no permiten deshacer lo que nunca corrió.
- **Stamp**: compare-and-set con `expected_current_version`; marcar la versión actual es un no-op auditado.
- **Rastro**: `record_intent` falla cerrado **antes** de delegar en el controlador humano, que no se modificó.

## Habilitar

1. Fijar `INTEGRATION_API_ENABLED=True` y **reiniciar** (se lee al importar `app/core/environments.py`).
2. Una persona con `access.admin` o `integration_tokens.own` emite el token (`POST /api/v1/integration-tokens`):
   un token por proyecto o pipeline, con la **allowlist de servidores** (obligatoria) y, si hace falta, la de
   blueprints. Guardar el bearer en un secret store: no se vuelve a mostrar.
3. El cliente lee `GET /integration/databases/{id}/migrations/version` antes de aplicar migraciones.

### Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `INTEGRATION_API_ENABLED` | `false` | Kill switch. Apagado: `/integration/*` responde `503` y la gestión no crea ni edita (listar y revocar siguen) |
| `INTEGRATION_TOKEN_MAX_TTL_DAYS` | `90` | Vida máxima de un token con solo lectura |
| `INTEGRATION_WRITE_TOKEN_MAX_TTL_DAYS` | `30` | Vida máxima con algún scope de escritura; no puede superar la anterior (el gateway no arranca) |
| `INTEGRATION_RATE_LIMIT` | `120/minute` | Cupo por token |
| `INTEGRATION_WRITE_RATE_LIMIT` | `20/minute` | Cupo adicional por token en scopes de escritura |
| `INTEGRATION_ALLOW_NON_EXPIRING_TOKENS` | `false` | Permite emitir tokens SIN expiración (`never_expires: true`), de lectura y/o escritura. Nunca con `migrations.rollback`/`migrations.stamp`, que conservan su tope. Apagarlo después no invalida los ya emitidos: se revocan a mano |
| `INTEGRATION_DESTRUCTIVE_TOKEN_MAX_TTL_DAYS` | `7` | Vida máxima con `migrations.rollback`/`migrations.stamp`; no puede superar la de escritura (si esa se baja de 7, bajar esta también o el gateway no arranca) |
| `INTEGRATION_DESTRUCTIVE_RATE_LIMIT` | `5/minute` | Cupo adicional por token en scopes destructivos |
| `INTEGRATION_AUTH_FAILURE_RATE_LIMIT` | `30/minute` | Credenciales rechazadas por IP; superado, esa IP recibe `429` sin tocar la BD |

Todas documentadas también en `.env.example`. Cada llamada exenta el límite por IP de SlowAPI a propósito: su
clave es la dirección del cliente y haría que todas las integraciones detrás de un mismo NAT compartan cupo.

## Operar

- **Revocar** (`DELETE /integration-tokens/{id}`) corta en la siguiente llamada. Con el kill switch apagado sigue
  funcionando.
- **Auditoría**: `GET /audit-log?actor_type=integration` (requiere `audit.read`). Cada llamada es
  `integration.call` con el id del token; los rechazos de credencial se agregan por IP y ventana (una fila con la
  cuenta de lo omitido) para que un atacante no pueda inundar la tabla.
- **Un token sospechoso**: revocarlo; si se sospecha del emisor, desactivar la cuenta (el emisor se relee en cada
  llamada, así que todos sus tokens dejan de valer).

## Revertir

Apagar el kill switch (`INTEGRATION_API_ENABLED=false` + reinicio) detiene toda la superficie sin tocar datos. La
migración `b7d9f1a3c5e8` crea `integration_tokens`, `integration_token_servers`, `integration_token_blueprints` y
la columna `audit_log.integration_token_id`; su `downgrade()` es idempotente. Revertir el código sin bajar la
migración es seguro: las tablas quedan inertes.

## Pruebas

No se ejecutan automáticamente (ver la regla de `CLAUDE.md`). Para verificar: `.venv/bin/python
scripts/run_tests_direct.py tests.test_integration_ops_api` (y `tests.test_integration_auth`,
`tests.test_integration_tokens_api`, `tests.test_integration_scope_catalog`, `tests.test_integration_destructive_ops`,
`tests.test_route_capabilities_integration`); la cobertura de rutas, con `python scripts/check_route_capabilities.py`.
