# database-api-gateway — Guía para Agentes de IA

Gateway FastAPI que administra **servidores de bases de datos remotos** (MySQL/MariaDB/
PostgreSQL) con credenciales pseudo-root: crea y migra bases, gestiona usuarios y permisos,
clona, compara esquemas, exporta y ejecuta SQL ad-hoc. Un error acá se ejecuta sobre la base
de producción de un tercero, así que la prudencia operativa es el default, no una virtud.

---

## Reglas de ESTE archivo — leelas antes de agregarle una línea

Este archivo se carga **completo, en cada sesión de agente**. Cada línea se paga en todas las
conversaciones, incluida la que solo venía a cambiar un endpoint. En agosto de 2026 llegó a
2316 líneas (~40.000 tokens por sesión) porque cada fix agregaba su propio post-mortem, y
cada agregado, uno por uno, parecía justificado. Ese es exactamente el modo de fallo: **nadie
agrega 2000 líneas de una vez.**

1. **La prueba de entrada, en dos partes. Tiene que pasar las dos.** (a) ¿Un agente lo
   deduciría leyendo el código en cinco minutos? Si sí, **no va**. (b) ¿Si no lo sabe de
   antemano, rompe algo? Si no rompe nada, **no va**. Lo que sobrevive es el conocimiento que
   no está en el código y cuya ausencia causa daño.

2. **Prohibidas las fechas.** Una fecha (`(fix, 2026-08-14)`, `(2ª pasada, …)`) significa que
   estás escribiendo una entrada de changelog en el archivo equivocado. La historia vive en
   git y en `docs/development/decisiones-e-incidentes.md`.

3. **El POR QUÉ de un fix va en el docstring de la función que lo implementa.** Ahí lo lee
   quien está por romperlo. Acá no lo lee nadie y lo pagan todas las sesiones. Si el porqué es
   largo, va al archivo de decisiones e incidentes, no acá.

4. **Cero índices, cero listas de archivos, cero enumeración de módulos.** Un índice de `docs/`
   es `ls docs/` con pasos extra: nace desactualizado y no aporta nada que la convención de
   nombres no diga ya. Las referencias van **inline**, donde el tema aparece.

5. **Nada que ya viva en una skill, un hook o una memoria.** Si un `UserPromptSubmit` ya lo
   recuerda o una skill ya lo detalla, repetirlo acá no lo refuerza: crea una segunda copia que
   se desincroniza en silencio y después nadie sabe cuál manda.

6. **Tope duro: 250 líneas.** Si tu agregado no entra, algo tiene que salir. No se sube el tope
   "por esta vez": ese es el mecanismo exacto por el que llegó a 2316.

7. **Todo agregado tiene que nombrar qué archivo NO alcanzó.** Si no podés decir por qué
   `docs/features/`, el docstring o la skill no servían, la respuesta es que sí servían.

---

## Antes de trabajar: reclamá la tarea

Invocá la skill **`clickup-task-flow`** (o `/tarea P-XX`) antes de implementar, arreglar,
verificar o refactorizar. El protocolo completo está en la skill; no se repite acá.

- El **detalle** de cada tarea vive en `TODO.md`. El **estado** y **quién trabaja** viven en
  ClickUp. Ante discrepancia de estado, **gana ClickUp**.
- Si una tarea está `in progress`, **se interrumpe** y se informa quién la tiene.
- Al arrancar el día, `/tarea bloqueos`: son las que el frontend devolvió en `on hold` y no
  aparecen en ningún otro filtro, con una implementación parada del otro lado.

Responder preguntas, leer código o explicar cosas **no** requiere reclamar tarea.

---

## Dónde buscar el porqué

Tres destinos, por tipo de pregunta. No hay índice: la convención de nombres alcanza.

- **«¿Cómo se usa X?»** → `docs/features/<feature>.md`
- **«¿Por qué X está hecho así?» / «¿puedo simplificar este guard?»** →
  `docs/development/decisiones-e-incidentes.md`
- **«¿Qué contrato consume el frontend?»** → `docs/api-reference.md` primero, y **después** el
  addendum de la feature puntual

**Los `api-reference-vN.md` NO son versiones del mismo documento: son addendums por feature.**
`api-reference.md` es el consolidado, pero solo absorbió v2–v5; de **v6 en adelante siguen
sueltos**. Así que el número más alto no es "lo más nuevo del contrato", es el último addendum
escrito — v14 son 149 líneas sobre descongelar una versión de blueprint, no la API. Buscá el
addendum por su **título**, no por su número.

**Antes de "limpiar" código que parece paranoia excesiva, buscalo en el archivo de decisiones
e incidentes.** Casi todo lo que sobra a primera vista es el resultado de un incidente real de
producción, con la reproducción documentada. La estructura del repo y el flujo para crear una
feature nueva están en `docs/development/estructura-y-componentes.md`.

Para preguntas estructurales (dónde vive un símbolo, quién llama a qué, radio de impacto),
**CodeGraph antes que grep**: el repo está indexado.

---

## Reglas duras de código

**Respuestas.** Todo endpoint usa `ApiResponse[T]` como `response_model` y los helpers
`success()` / `paginated()` / `empty()` de `app/utils/response.py`. Devolver un dict pelado
rompe el formato que consume el frontend.

**Errores.** Siempre `AppHttpException`, nunca `HTTPException` de FastAPI. Captura archivo,
función y línea automáticamente.

```python
raise AppHttpException("Usuario no encontrado", 404, {"user_id": user_id})
```

**Códigos de error nuevos van en `public_context["code"]`, NUNCA en `context`.** `context` solo
se expone en `development`, así que en producción el operador recibiría el error sin saber cuál
ni por qué. El vocabulario es cerrado y vive en los catálogos de `app/services/*_catalog.py`.

**Nunca volcar `str(exc)` del motor en una respuesta.** Puede llevar host, usuario o fragmentos
de sentencia. El detalle va a `logger.exception` con el Request ID.

**SQL siempre parametrizado.** Concatenar es inyección, sin excepciones.

```python
db.execute_query("SELECT * FROM users WHERE id = :id", {"id": user_id})
```

**Nombres.** Modelos ORM `post.py` → `Post`; modelos SQL `post_model.py` → `PostModel`;
controladores `post_controller.py` → `PostController`; rutas `routes/v1/posts.py` → `router`;
schemas `schemas/post.py` → `PostCreate`/`PostOut`. Clases `PascalCase`, funciones `snake_case`.
Todo modelo nuevo **debe** importarse en `app/models/__init__.py` o Alembic no lo ve.

---

## NO ejecutes `pytest` salvo pedido explícito

La suite (~690 tests) tarda varios minutos por el I/O de WSL2 sobre `/mnt/`, y correrla como
chequeo automático tras cada cambio genera carga real en la máquina del usuario sin que se haya
pedido. Vale tanto para vos como para cualquier subagente que delegues: **no le agregues "y
corré los tests" por las dudas.**

Verificá por otros medios — lectura del diff, `ast.parse`, ejecución directa de las funciones
puras — y **decí explícitamente qué quedó sin verificar**, para que el usuario decida si lo
corre él.

**Esto ya no es solo una instrucción: es un `permissions.deny` en `.claude/settings.json`.** Si
te topás con el bloqueo, la regla está funcionando — **no lo rodees** (ni `sh -c`, ni un alias,
ni un runner distinto): pedile al usuario que corra la suite él. Un deny **no admite
excepciones**, así que `--collect-only` cae también, aunque no ejecute ningún test.

---

## Migraciones del gateway: UN SOLO head, siempre

Sobre el `alembic/` propio del gateway (su BD de metadatos), no sobre el módulo de migraciones
de blueprints.

**Al crear una migración, su `down_revision` apunta al head ACTUAL, y después del merge tiene
que seguir habiendo un solo head.** Verificalo con `python scripts/check_migration_graph.py`
(lee el fuente con `ast`, no necesita BD ni `.env`).

Dos cosas que no se ven venir: el DAG de Alembic vive **dentro** del campo `down_revision`, así
que **git mergea dos migraciones bifurcadas sin conflicto** y el fallo aparece recién en
producción, con el contenedor en loop de reinicios. Y los `revision` de este repo se eligen **a
mano** con forma secuencial, así que dos personas del mismo día eligen plausiblemente el mismo
ID — daño **peor** que una bifurcación, porque una de las dos migraciones queda inalcanzable y
su DDL nunca se aplica **sin que nada falle**.

**Al arreglar una bifurcación: ENCADENAR, no `alembic merge heads`.** El merge deja head único
pero conserva la bifurcación en la historia. La causa raíz completa, el incidente y por qué el
guard no usa `ScriptDirectory` están en el archivo de decisiones e incidentes.

---

## Trampas que cuestan un incidente

**Hay DOS cosas llamadas "environment".** `app/core/environments.py` es la config del PROCESO
(`APP_ENV`) y gobierna seguridad real del gateway (flag `Secure` de la cookie, exigencia de
`ADMIN_PASSWORD`, rechazo del wildcard de CORS, si `context` se expone). La tabla
`environments` clasifica las **BDs de terceros** que el gateway administra. Usan los mismos
valores para cosas distintas. En el código: la config siempre por su constante (`APP_ENV`), la
tabla siempre como `Environment`. **Nunca "el entorno" a secas.**

**`force` NO saltea el guard de entornos.** `force` es override de cuarentena y nada más. En la
SPA es un `Switch` sin fricción: si algún día saltea el guard, la barrera de producción se abre
con un click.

**La contabilidad interna del gateway (`_gw_v_`, `_gw_stg_`) no es esquema del usuario.**
Cualquier camino nuevo que enumere tablas debe excluirla con
`identifiers.exclude_gateway_internal_tables`. Olvidarlo ya tiró producción una vez: el diff
emitió `DROP TABLE _gw_v_{slug}` sobre la propia tabla de versión de Alembic.

**Toda variable de entorno nueva va en `app/core/environments.py` y en `.env.example`.**

---

## Stack

FastAPI (sub-apps por versión, `app/core/versioned_app.py`) · SQLAlchemy 2.0 (`Mapped[]`) ·
Alembic · Pydantic v2 · SlowAPI · sqlglot · **uv** (`uv run …`, `uv add …`) · Ruff · Python 3.13+.

Swagger en `/api/v1/docs`, ReDoc en `/api/v1/redoc`, ambos sujetos a `DOCS_ENABLED`.
