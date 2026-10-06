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

## Qué todavía no se verificó

- **El `GRANT SHOW CREATE ROUTINE` de MariaDB 11.3+** (nombre y sintaxis) sale de la documentación de
  MariaDB, no de un servidor probado. El literal está en una sola constante marcada para confirmar
  (`MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE`). Confirmalo, junto con la salida de `SHOW GRANTS`, antes de
  encender `MCP_SCHEMA_DEFINITIONS_ENABLED` en producción. Una credencial ya aprovisionada no recibe el
  privilegio hasta que se re-aprovisiona.
- **Ningún test de esta entrega corrió contra un motor real** (MySQL, MariaDB ni PostgreSQL); usan un motor
  falso.
- Que `SHOW CREATE` sin privilegio devuelva NULL y no un error en cada versión del motor.
