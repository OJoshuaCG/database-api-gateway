# Despliegue en Dokploy

Esta guía cubre el despliegue del gateway en [Dokploy](https://dokploy.com/) (PaaS self-hosted
tipo Coolify/Heroku) usando el archivo **`docker-compose.dokploy.yml`** incluido en el repo.

> **Diferencia clave con `docs/docker-deployment.md`**: esa guía usa `docker-compose.yml`, que
> trae su propio `nginx` + Certbot para terminar TLS. Dokploy ya trae **su propio proxy Traefik**,
> que ocupa los puertos 80/443 del host y gestiona dominios + certificados Let's Encrypt desde su
> panel. Por eso `docker-compose.dokploy.yml` **no incluye `nginx`**: si lo incluyera, colisionaría
> por los puertos 80/443 con el Traefik de Dokploy y duplicaría la terminación TLS. Todo lo demás
> (Dockerfile, entrypoint, migraciones automáticas, variables de entorno) es idéntico.

## Qué levanta `docker-compose.dokploy.yml`

| Servicio | Imagen         | Rol                                                    |
|----------|----------------|---------------------------------------------------------|
| `db`     | `mariadb:11`   | BD de metadatos del gateway (servers, users, audit, etc.)|
| `valkey` | `valkey/valkey:8-alpine` | Backend de rate limiting compartido (SlowAPI, wire-compatible con Redis) |
| `api`    | build local (`Dockerfile`, target `production`) | FastAPI + Uvicorn (Python 3.13, uv) |

No incluye `nginx` ni los motores destino de prueba (`target-mariadb`/`target-postgres`, perfil
`test` de `docker-compose.yml`) — esos son solo para desarrollo local, no para un despliegue real.

El contenedor `api` no publica ningún puerto de host; solo `expose: 8000`. Dokploy enruta tráfico
externo directo a ese puerto interno vía Traefik, configurado desde su panel (paso 5).

## Prerrequisitos

- Una instancia de Dokploy ya corriendo y accesible (self-hosted en tu propio servidor/VPS).
- Un dominio (o subdominio) con un registro DNS **A** apuntando a la IP del servidor Dokploy.
- Acceso al repositorio Git del proyecto desde Dokploy (SSH key o token configurado en el panel).

## 1. Crear el proyecto en Dokploy

1. En el panel de Dokploy: **Create Project** → nombre libre (ej. `database-api-gateway`).
2. Dentro del proyecto, **Create Service** → tipo **Compose**.
3. Conectar el repositorio Git (rama a desplegar, ej. `main`).
4. En **Compose Path**, indicar `docker-compose.dokploy.yml` (no el `docker-compose.yml` por
   defecto — ese trae nginx y colisiona con el Traefik de Dokploy).
5. Dejar el modo de Compose en el que ejecuta `docker compose` estándar (no "Stack"/Swarm),
   salvo que tu instancia de Dokploy esté configurada específicamente en modo Swarm.

## 2. Configurar variables de entorno

Copia el contenido de [`.env.dokploy.example`](../.env.dokploy.example), complétalo con valores
reales y pégalo en la pestaña **Environment** del servicio Compose en Dokploy.

Genera valores fuertes y **distintos entre sí** para:

```bash
# SECRET_KEY (clave Fernet)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# SESSION_SECRET / CRYPTO_KEY_SALT / contraseñas de BD
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Variables **obligatorias** a cambiar (la app se niega a arrancar en `APP_ENV=production` si
faltan `SECRET_KEY`, `ADMIN_PASSWORD`, `SESSION_SECRET`, o si `CORS_ORIGINS` sigue en `*`):

| Variable          | Por qué                                                        |
|-------------------|-----------------------------------------------------------------|
| `SECRET_KEY`      | Deriva la clave de cifrado (Fernet) de credenciales de servidores destino |
| `SESSION_SECRET`  | Firma la cookie de sesión; debe ser independiente de `SECRET_KEY` |
| `CRYPTO_KEY_SALT` | Sal única por despliegue para la derivación de clave (HKDF)     |
| `ADMIN_PASSWORD`  | Contraseña del admin único sembrado al primer arranque          |
| `DB_PASS` / `DB_ROOT_PASS` | Credenciales de la BD de metadatos (MariaDB)             |
| `CORS_ORIGINS`    | Dominios exactos permitidos (nunca `*` en producción)            |

`DB_HOST`, `DB_ENGINE`, `RATE_LIMIT_REDIS_ENABLED` y `RATE_LIMIT_REDIS_URL` se sobrescriben
siempre en `docker-compose.dokploy.yml` — no hace falta (ni sirve) tocarlos en Dokploy.

## 3. Deploy (build)

Dispara el deploy desde el panel de Dokploy. Internamente:

1. Dokploy clona el repo y ejecuta `docker compose -f docker-compose.dokploy.yml build`.
2. Arranca `db` y `valkey`, espera a que ambos healthchecks estén `healthy`.
3. Arranca `api`: el `entrypoint.sh` espera la conexión a MariaDB, corre
   `alembic upgrade head` (migraciones automáticas de la BD de metadatos) y lanza Uvicorn con
   `WORKERS` workers (default `1`).

No hay ningún paso manual de migración: ocurre en cada arranque del contenedor `api`, sea el
primer deploy o uno posterior.

## 4. Configurar el dominio (Traefik + TLS de Dokploy)

En la pestaña **Domains** del servicio Compose:

1. **Service Name**: `api` (el servicio del compose que debe recibir tráfico).
2. **Container Port**: `8000`.
3. **Host**: tu dominio (ej. `gateway.tudominio.com`).
4. Habilitar **HTTPS** — Dokploy solicita y renueva el certificado Let's Encrypt automáticamente
   vía su Traefik interno.

> Dokploy actualiza esta UI con frecuencia; si los nombres de campo difieren de los descritos
> aquí, el punto fijo es: el servicio a exponer es `api` y el puerto de contenedor es `8000`.

## 5. Verificar el despliegue

```bash
# Liveness: el proceso está vivo (no valida BD)
curl https://gateway.tudominio.com/health

# Readiness: valida que la BD de metadatos es alcanzable (SELECT 1)
curl https://gateway.tudominio.com/health/ready
```

Ambos deben responder `200` con `{"status": "ok" | "ready", ...}`. Revisa los logs del servicio
`api` desde el panel de Dokploy si alguno falla (Dokploy muestra el stdout del contenedor —
la app loguea todo a consola, sin archivos de log).

## 6. Ejecutar comandos manuales (Alembic, shell)

Desde la pestaña de terminal/exec del servicio `api` en Dokploy (o `docker compose exec api ...`
si tienes acceso SSH directo al host):

```bash
alembic current           # ver estado de migraciones aplicadas
alembic upgrade head       # forzar migración manual (normalmente innecesario, ya es automático)
alembic history            # ver historial completo
```

## 7. Escalar workers / réplicas

`WORKERS` controla los workers de Uvicorn **dentro** de un mismo contenedor `api` (ver
`docker/scripts/entrypoint.sh`). El rate limiting ya está compartido vía `valkey`
(`RATE_LIMIT_REDIS_ENABLED=True` forzado en el compose), así que subir `WORKERS` — o correr
varias réplicas del servicio `api` desde Dokploy — no rompe la consistencia del rate limiting.

El worker de clonado de bases de datos (`app/services/clone_runner.py`) corre in-process
(`ThreadPoolExecutor`) dentro de cada réplica de `api`; no requiere ningún servicio adicional. Si
escalas a varias réplicas, la serialización por BD destino la da un advisory lock a nivel de motor
(no el pool de hilos en sí), así que es seguro tener varias réplicas activas simultáneamente.

## 8. Backups

Opciones:

- **Backup nativo de Dokploy**: Dokploy puede programar backups del volumen `mariadb_data` desde
  su propia UI (revisa la sección de Backups/Volumes de tu instancia).
- **Script manual** (equivalente al de `docs/docker-deployment.md`, adaptado a exec de Dokploy):

```bash
docker compose -f docker-compose.dokploy.yml exec -T db \
    mariadb-dump -u root -p"${DB_ROOT_PASS}" "${DB_NAME}" \
    | gzip > backup_$(date +%Y%m%d_%H%M%S).sql.gz
```

## 9. Diferencias vs. despliegue VPS clásico (`docker-compose.yml`)

| Aspecto              | `docker-compose.yml` (VPS plano)         | `docker-compose.dokploy.yml` (Dokploy)     |
|-----------------------|--------------------------------------------|----------------------------------------------|
| Reverse proxy / TLS  | `nginx` + Certbot propios, puertos 80/443  | Traefik de Dokploy (gestionado desde el panel)|
| Servicio `nginx`     | Incluido                                    | No incluido (evita conflicto de puertos)      |
| Motores de prueba    | `target-mariadb`/`target-postgres` (perfil `test`) | No incluidos                          |
| Variables de entorno | Archivo `.env` en el servidor               | Pestaña "Environment" del proyecto en Dokploy |
| `db`, `valkey`, `api` | Idénticos                                   | Idénticos                                     |

Si necesitas levantar el gateway en un VPS sin Dokploy (o con nginx/Certbot manual), usa
`docs/docker-deployment.md` y `docker-compose.yml` en su lugar.

## Checklist de despliegue

- [ ] Dominio con registro DNS `A` apuntando al servidor Dokploy
- [ ] Variables de entorno completas en la pestaña Environment (ver `.env.dokploy.example`)
- [ ] `SECRET_KEY`, `SESSION_SECRET`, `CRYPTO_KEY_SALT` generados y distintos entre sí
- [ ] `ADMIN_PASSWORD` y contraseñas de `DB_*` fuertes y únicas
- [ ] `CORS_ORIGINS` con dominios exactos (no `*`)
- [ ] `DOCS_ENABLED=False` (o protegido con `DOCS_PASSWORD_ENABLED`)
- [ ] Deploy completado, `db`/`valkey` healthy, `api` corriendo
- [ ] Dominio configurado en la pestaña Domains (`api`, puerto `8000`, HTTPS habilitado)
- [ ] `GET /health` y `GET /health/ready` responden `200`
- [ ] Backup de `mariadb_data` configurado (nativo de Dokploy o script manual)
