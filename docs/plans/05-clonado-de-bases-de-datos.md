# 05 — Clonado de bases de datos entre servidores

**Estado:** Pendiente (futuro) · **Depende de:** 01, 02 · **Esfuerzo:** alto

## Objetivo

Clonar una base de datos (o todas las de un usuario) de un servidor a otro —p. ej.
de un servidor de cliente a uno de desarrollo—, creando en el destino el/los usuario(s),
la(s) BD(s) y copiando su contenido. Soporta clonado **por una sola BD** o **por usuario
completo** (un usuario → muchas BDs).

## Alcance y modos

- **Por base de datos:** clona una `ManagedDatabase` al servidor destino.
- **Por usuario:** clona todas las BDs de un `ServerUser` (origen) al destino, recreando
  el usuario propietario y sus BDs.
- **Mismo motor** origen→destino (MySQL→MySQL, PG→PG). El cross-engine queda fuera de alcance.
- **Decisión a confirmar:** ¿clonar solo **estructura** (coherente con "no nos importan
  los datos") o **estructura + datos**? El requisito de clonado sugiere copiar contenido;
  confirmar. Si es solo estructura, se apoya en el plan 02 (aplicar el blueprint/migraciones).

## Enfoque técnico

- **Estructura + datos (mismo motor):**
  - MySQL/MariaDB: `mysqldump` (vía SSH o cliente) → restaurar en destino; o streaming
    dump→restore. Requiere binarios cliente o ejecución SSH en los servidores.
  - PostgreSQL: `pg_dump`/`pg_restore` (formato custom) o `pg_dump | psql`.
- **Solo estructura:** introspección del origen + recreación, o reaplicar el modelo/migraciones
  (plan 02) en el destino. Más limpio si el origen sigue un blueprint conocido.
- Orquestar como **job en background** (plan 06) con progreso por BD.

## Componentes

- `app/services/cloning/` con `cloner.py` (estrategia por motor) usando los adapters
  para crear usuario/BD/grants en destino y el método de copia (dump/restore) elegido.
- Reusa `create_user`/`create_database`/`grant_database` de los adapters (plan 01).

## Modelo de datos

### `CloneJob` (`clone_jobs`)
| Campo | Notas |
|---|---|
| `id`, timestamps | |
| `mode` | `database\|user` |
| `source_server_id` / `target_server_id` | FKs |
| `source_ref` | id de `ManagedDatabase` o `ServerUser` origen |
| `include_data` | bool |
| `status` | `pending\|running\|success\|partial\|failed` |
| `report` | `Text`/JSON — resultado por BD |
| `error` | detalle |

## API (`/api/v1`)

| Método | Path | Descripción |
|---|---|---|
| POST | `/clone/database` | clona una BD a un servidor destino → job |
| POST | `/clone/user` | clona todas las BDs de un usuario → job |
| GET | `/clone-jobs/{id}` | estado + reporte |

## Decisiones a confirmar

- ¿Estructura + datos, o solo estructura? (define toda la estrategia).
- ¿Cómo se ejecuta el dump/restore: clientes locales en el gateway, o vía SSH en los
  servidores (requiere conectividad servidor↔servidor o pasar por el gateway)?
- Manejo de colisiones en destino (BD/usuario ya existen): abortar, sufijar, o sobrescribir.

## Riesgos

- Volumen de datos (si se copian) → operaciones largas, uso de disco/red; imprescindible
  job en background y límites.
- Consistencia: para datos vivos, considerar lock/snapshot o avisar de inconsistencia.
- Seguridad: mover datos entre entornos puede implicar PII → confirmar políticas.

## Verificación

- Clonar una BD entre dos servidores sandbox: usuario y BD creados en destino, estructura
  idéntica (y datos si aplica). Clonar por usuario: todas sus BDs replicadas; reporte correcto.
