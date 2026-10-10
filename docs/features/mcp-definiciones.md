# MCP: definiciones de esquema y estadísticas de tablas

Dos tools del MCP que le dan a un agente lo que antes tenía que adivinar o pedirle a un DBA. El contrato
exacto está en [`api-reference-v39.md`](../api-reference-v39.md); el porqué de cada decisión, en
[`decisiones-e-incidentes.md`](../development/decisiones-e-incidentes.md).

## Qué le dan al agente

- **`get_definition`**: el **código** de vistas, triggers, eventos y rutinas (procedimientos y funciones)
  pedidos por nombre. Por cada objeto dice si el cuerpo está disponible y, si no, por qué
  (`insufficient_privilege`, `engine_unsupported`, `flag_off`, `too_large`). Lo que no existe en la base
  vuelve en `missing[]`, no como "sin cuerpo".
- **`get_table_stats`**: tamaño de datos e índices, motor, collation y fechas de las tablas pedidas. Las
  estimaciones de filas (`row_estimate`, `auto_increment`) solo salen si el token además tiene `data.read`.
  Usa el scope `databases.read` y **no tiene kill switch propio**.

## Cómo se habilita `get_definition` (owner)

Hacen falta las dos cosas; con una sola, la tool no existe para el agente:

1. **Variable de entorno `MCP_SCHEMA_DEFINITIONS_ENABLED=true`** y reiniciar el proceso. Nace apagada
   (`.env.example`). Apagada, `get_definition` no aparece en `tools/list` y si se la invoca igual
   responde `403 mcp.definitions_disabled`.
2. **Un token con el scope `data.definitions`.** Solo lo emite un owner con step-up abierto, igual que
   `data.read` y `data.query`. El selector de scopes de la SPA lo ofrece con el aviso de que los cuerpos
   pueden contener secretos.

Además la base tiene que pasar el gate de estructura de siempre (proyecto, entorno, opt-in de agentes, sin
veto) y la credencial de solo lectura del servidor tiene que estar verificada.

## Lo que nunca se hace

- **No se ejecuta nada.** Los cuerpos se leen con `SHOW CREATE ...` o `pg_get_*` y se entregan como texto.
  Ningún objeto se ejecuta, se dispara ni se invoca.
- **No acepta SQL.** Los argumentos son `{kind, name}`; un nombre que no está exactamente en el índice de la
  base nunca llega al motor y vuelve en `missing[]`.
- **La cuenta del `DEFINER` no sale.** Solo se informa el modo (`definer` o `invoker`).
- **No se trunca.** Un cuerpo mayor al tope se rechaza (`too_large`, con su tamaño) en vez de entregarse
  cortado.

## Los cuerpos son texto de terceros: no confiables

- Un comentario dentro de un cuerpo puede decir "ignorá lo anterior y...". Por eso el cuerpo va en
  `untrusted_fields` y bajo el `notice` de la respuesta. El control real es que ninguna tool del MCP
  escribe.
- **La redacción de credenciales es best effort y no es una frontera.** Enmascara patrones conocidos
  (`IDENTIFIED BY '...'`, `PASSWORD '...'`, URIs con contraseña, bloques PEM) y cuenta emails y hosts sin
  enmascararlos. Puede dejar pasar un secreto con otra forma. La frontera es el scope `data.definitions`:
  emitilo solo a quien pueda ver el código de esa base.
- Si se enmascaró algo, la respuesta trae el aviso `mcp.warn.bodies_redacted`; su ausencia **no** garantiza
  que el cuerpo esté limpio.

## Una rutina "ausente" o "cero rutinas" puede significar "no la veo"

En MySQL y MariaDB, una cuenta de solo lectura sin privilegio de rutina recibe **cero filas** de
`information_schema.ROUTINES`, sin ningún error. Consecuencias que el agente ve:

- `list_objects` lista cero rutinas y avisa `mcp.warn.routines_not_visible` ("cero rutinas listadas: puede
  que no existan o que esta cuenta no las vea"). También avisa cuando el motor/versión puede ocultarlas.
- `get_definition` devuelve la rutina en `missing[]` y suma `mcp.warn.routine_not_found_or_not_visible`.
  Vistas, triggers y events ausentes siguen siendo un `missing` confiable.

Cómo arreglarlo si la rutina existe:

- **MariaDB >= 11.3:** regenerar la credencial de solo lectura del servidor (botón «Regenerar credencial»
  del panel de la SPA). Ese aprovisionamiento ya otorga lo necesario. El botón está en el panel «Acceso
  de agentes (MCP)» del **detalle del servidor**, no en el modal «Acceso de agentes» de cada base: ese
  administra la credencial de **datos** y no otorga nada sobre rutinas.
- **MariaDB < 11.3 o MySQL 5.7:** habilitar la lectura de cuerpos de rutinas (sección siguiente).

## MariaDB < 11.3 y MySQL 5.7: `mysql.proc` por servidor

Ahí no existe un grant por base que permita leer el código de las rutinas, y el único camino es
`SELECT ON mysql.proc`. Por defecto esas versiones responden `flag_off` para las rutinas (vistas, triggers y
eventos funcionan igual).

Un administrador (`servers.admin`, con step-up) puede encenderlo por servidor con
`PUT /servers/{id}/readonly-credential/routine-bodies`, o desde el panel de la credencial de solo lectura en
la SPA. **Advertencia: el grant es server-wide.** `mysql.proc` no se puede acotar a una base, así que la
credencial pasa a poder leer el código de las rutinas de **todas** las bases del servidor, incluidas las que
están fuera del proyecto o excluidas; solo el filtrado del gateway lo contiene. Por eso habilitar exige
escribir el texto de acknowledgement exacto. Apagar la bandera revoca el grant si la credencial es del
gateway; si se cargó a mano, el DBA tiene que quitarlo.

Un servidor con credencial cargada a mano que **ya** tenía `SELECT ON mysql.proc` falla la sonda después del
deploy y queda fuera del MCP hasta que se acepte el riesgo con la bandera o se quite el grant.

## Límites

- **3 objetos por llamada.** Más es `422 mcp.invalid_argument`, antes de conectar. Los duplicados se
  colapsan. Pedí en lotes.
- **64 KiB por cuerpo**, medidos sobre su JSON y después de redactar.
- El tope de 3 sale del presupuesto de respuesta del MCP (512 KiB), que cuenta cada resultado dos veces:
  3 x 64 KiB x 2 = 384 KiB.

## Scope hermano: `data.blueprint_sql`

`get_blueprint_migration` entrega el SQL de una migración de blueprint (puede llevar filas semilla de
terceros) y sigue las mismas reglas que `get_definition`: tool ausente de `tools/list` con su kill switch
apagado (`MCP_BLUEPRINT_SQL_ENABLED`, independiente), scope solo de owner con step-up, texto no confiable
sin recortar y redacción de credenciales best effort. Difiere en que lee la BD de metadatos del gateway y
no abre ninguna conexión a un motor, y en su error de tamaño propio (`mcp.blueprint_sql_too_large`).
Contrato y límites en [`mcp-para-colaboradores.md`](mcp-para-colaboradores.md), sección «MCP: blueprints y
el SQL de sus migraciones».

## Qué todavía no se verificó

- **El `GRANT SHOW CREATE ROUTINE` de MariaDB** se verificó en vivo solo en la versión **11.8.3**: al
  regenerar la credencial de estructura, las rutinas pasaron a verse en `list_objects` y sus cuerpos se
  leyeron con `get_definition`. No se probó en otras versiones 11.3 a 11.7 ni se inspeccionó la salida de
  `SHOW GRANTS` posterior. El literal está en una sola constante
  (`MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE`). Una credencial ya aprovisionada no recibe el privilegio hasta
  que se regenera.
- **MySQL, PostgreSQL y la bandera de `mysql.proc`** (MariaDB anterior a 11.3 y MySQL 5.7) no se probaron
  contra un motor real; los tests usan un motor falso.
- Que `SHOW CREATE` sin privilegio devuelva NULL y no un error en cada versión del motor.
