# Guía de Despliegue

Esta guía cubre el despliegue de la aplicación en producción usando **Docker** (método recomendado) o directamente en un servidor Linux.

---

## Método 1: Docker + Nginx + MariaDB (Recomendado para VPS)

El método oficial incluye tres servicios orquestados con Docker Compose:

| Servicio | Imagen         | Descripción                          |
|----------|----------------|--------------------------------------|
| `db`     | `mariadb:11`   | Base de datos MariaDB 11             |
| `api`    | (build local)  | FastAPI + Uvicorn (Python 3.13, uv)  |
| `nginx`  | `nginx:alpine` | Reverse proxy, SSL, compresión       |

### Requisitos del VPS

- Ubuntu/Debian (o cualquier Linux con Docker)
- Docker Engine 25+ y Docker Compose Plugin 2.24+
- Puertos 80 y 443 abiertos en el firewall

```bash
# Instalar Docker en Ubuntu/Debian
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER
newgrp docker

# Verificar versiones
docker --version
docker compose version
```

### 1. Clonar el repositorio

```bash
git clone <tu-repositorio> /opt/myapp
cd /opt/myapp
```

### 2. Configurar variables de entorno

```bash
# Copiar el template de variables Docker
cp .env.docker.example .env

# Editar con los valores reales
nano .env
```

Variables **obligatorias** a cambiar:

| Variable      | Descripción                                    | Generar con                                         |
|---------------|------------------------------------------------|-----------------------------------------------------|
| `SECRET_KEY`  | Clave secreta de la aplicación                 | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `DB_ROOT_PASS`| Contraseña root de MariaDB                     | Contraseña segura aleatoria                         |
| `DB_PASS`     | Contraseña del usuario de la app en MariaDB    | Contraseña segura aleatoria                         |
| `CORS_ORIGINS`| Dominios permitidos en producción              | `https://tudominio.com`                             |

> **Nota:** `DB_HOST=db` y `DB_ENGINE=mysql+pymysql` son sobreescritos automáticamente por `docker-compose.yml`. No hace falta cambiarlos.

### 3. Levantar los servicios

```bash
# Construir la imagen y levantar todos los servicios en background
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f

# Ver logs de un servicio específico
docker compose logs -f api
docker compose logs -f db
docker compose logs -f nginx
```

La primera vez que se levanta:
1. MariaDB inicializa la base de datos (~20-30s)
2. El servicio `api` espera a que MariaDB esté sana
3. Se aplican las migraciones de Alembic automáticamente
4. Se inicia Uvicorn

### 4. Verificar el despliegue

```bash
# Health check de la API (debe responder 200)
curl http://localhost/health

# Estado de los contenedores
docker compose ps

# Estadísticas de recursos
docker stats
```

### 5. Comandos útiles

```bash
# Detener servicios (preserva los volúmenes/datos)
docker compose down

# Detener y eliminar volúmenes (¡BORRA LOS DATOS!)
docker compose down -v

# Reiniciar un servicio
docker compose restart api

# Reconstruir solo la imagen de la API (después de cambios en código)
docker compose up -d --build api

# Ejecutar migraciones manualmente
docker compose exec api alembic upgrade head

# Ver migraciones aplicadas
docker compose exec api alembic current

# Acceder a la shell del contenedor API
docker compose exec api bash

# Acceder a MariaDB directamente
docker compose exec db mariadb -u root -p
```

---

## Probar motores destino (perfil `test`)

Además de la BD de **metadatos** (`db`, MariaDB — donde corren las migraciones de Alembic),
el compose incluye dos **servidores destino de prueba** que el gateway puede administrar.
Solo arrancan con el perfil `test`, así que NO contaminan un despliegue de producción.

| Servicio          | Imagen        | Rol                                         | Puerto host |
|-------------------|---------------|---------------------------------------------|-------------|
| `db`              | `mariadb:11`  | BD de metadatos (Alembic)                   | —           |
| `target-mariadb`  | `mariadb:11`  | Motor DESTINO de prueba (se administra)     | `13306`     |
| `target-postgres` | `postgres:16` | Motor DESTINO de prueba (se administra)     | `15432`     |

```bash
# Levantar TODO, incluidos los motores destino de prueba
docker compose --profile test up -d --build

# (sin --profile test solo arrancan db + api + nginx)
```

Variables requeridas en `.env` (ver `.env.docker.example`):
`TARGET_MARIADB_ROOT_PASS`, `TARGET_MARIADB_GW_PASS`, `TARGET_POSTGRES_PASSWORD`.

### Registrar los destinos en el gateway

`root` de MariaDB queda restringido a localhost (default seguro); por eso el init-script
`docker/init/mariadb/01-gateway-admin.sh` crea `gw_admin`@'%' con privilegios pseudo-root.
PostgreSQL acepta `postgres` desde la red por defecto.

```bash
# 1. Login como admin (guarda la cookie de sesión)
curl -c cookies.txt -X POST http://localhost/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}'

# 2. Registrar el destino MariaDB
curl -b cookies.txt -X POST http://localhost/api/v1/servers \
  -H 'Content-Type: application/json' \
  -d '{"name":"mariadb-test","engine":"mariadb","host":"target-mariadb","port":3306,
       "root_username":"gw_admin","root_password":"<TARGET_MARIADB_GW_PASS>"}'

# 3. Registrar el destino PostgreSQL
curl -b cookies.txt -X POST http://localhost/api/v1/servers \
  -H 'Content-Type: application/json' \
  -d '{"name":"postgres-test","engine":"postgresql","host":"target-postgres","port":5432,
       "root_username":"postgres","root_password":"<TARGET_POSTGRES_PASSWORD>"}'
```

> Los hosts `target-mariadb` / `target-postgres` resuelven por DNS interno de Docker
> (red `backend`), por eso el contenedor `api` los alcanza por nombre de servicio.

---

## Configurar SSL con HTTPS (Let's Encrypt)

### Paso 1: Apuntar el dominio al servidor

Configura un registro DNS tipo `A` apuntando tu dominio a la IP del VPS antes de continuar.

### Paso 2: Instalar Certbot en el host

```bash
sudo apt install certbot -y
```

### Paso 3: Obtener el certificado

Con los servicios corriendo (para que el challenge ACME funcione a través de Nginx):

```bash
certbot certonly \
  --webroot \
  --webroot-path /var/lib/docker/volumes/$(basename $PWD)_certbot_www/_data \
  -d tudominio.com \
  -d www.tudominio.com \
  --email tu@email.com \
  --agree-tos \
  --non-interactive
```

### Paso 4: Habilitar HTTPS en Nginx

Editar `docker/nginx/conf.d/app.conf`:

1. En el bloque HTTP, reemplazar `location / { ... }` por: `return 301 https://$host$request_uri;`
2. Descomentar el bloque `server { listen 443 ssl ... }`
3. Actualizar `server_name` con tu dominio real
4. Verificar las rutas de `ssl_certificate` y `ssl_certificate_key`

Recargar Nginx:

```bash
docker compose exec nginx nginx -s reload
```

### Paso 5: Renovación automática

```bash
# Agregar al crontab del host (renovar cada 12h, recarga Nginx si hay cambios)
echo "0 */12 * * * root certbot renew --quiet --deploy-hook 'docker compose -f /opt/myapp/docker-compose.yml exec nginx nginx -s reload'" \
  | sudo tee /etc/cron.d/certbot-renew
```

---

## Actualizar la Aplicación

```bash
cd /opt/myapp

# Descargar cambios
git pull

# Reconstruir imagen y reiniciar (0 downtime con múltiples réplicas)
docker compose up -d --build api

# Verificar que la nueva versión está corriendo
docker compose ps
curl http://localhost/health
```

---

## Múltiples Workers y Rate Limiting

Por defecto `WORKERS=1`. Con un solo worker, el rate limiting in-memory de SlowAPI funciona correctamente.

Para escalar a múltiples workers, el rate limiting debe usar Redis como backend:

```python
# app/core/limiter.py
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri="redis://redis:6379",  # servicio Redis en docker-compose
)
```

```bash
uv add redis
```

Ver `docs/features/rate-limiting.md` para más detalles.

---

## Backups de Base de Datos

```bash
#!/bin/bash
# /opt/scripts/backup-db.sh

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/opt/backups"
PROJECT_DIR="/opt/myapp"

mkdir -p "$BACKUP_DIR"

docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db \
    mariadb-dump -u root -p"${DB_ROOT_PASS}" "${DB_NAME}" \
    | gzip > "$BACKUP_DIR/backup_$DATE.sql.gz"

# Mantener solo los últimos 7 días
find "$BACKUP_DIR" -name "backup_*.sql.gz" -mtime +7 -delete

echo "Backup completado: $BACKUP_DIR/backup_$DATE.sql.gz"
```

```bash
chmod +x /opt/scripts/backup-db.sh

# Agregar al crontab (backup diario a las 2 AM)
echo "0 2 * * * root /opt/scripts/backup-db.sh" | sudo tee /etc/cron.d/db-backup
```

---

## Troubleshooting

### La API no arranca

```bash
# Ver logs detallados del entrypoint
docker compose logs api

# Revisar si MariaDB está sana
docker compose ps db
docker compose exec db mariadb -u root -p -e "SHOW DATABASES;"
```

### Error de conexión a la base de datos

Verificar que las variables `DB_USER`, `DB_PASS`, `DB_NAME` en `.env` coincidan exactamente con las de `DB_ROOT_PASS` (MariaDB crea el usuario al iniciar solo si el volumen está vacío).

Si cambiaste credenciales con el volumen existente:

```bash
# Eliminar el volumen y recrear (¡BORRA LOS DATOS!)
docker compose down -v
docker compose up -d --build
```

### Nginx devuelve 502 Bad Gateway

```bash
# Verificar que la API está corriendo y escuchando
docker compose exec nginx wget -qO- http://api:8000/health

# Ver logs de Nginx
docker compose logs nginx
```

### Ver variables de entorno del contenedor API

```bash
docker compose exec api env | sort
```

---

## Método 2: Servidor Linux sin Docker (VPS bare-metal)

Para despliegues sin Docker, usando `systemd` y Nginx del sistema.

### 1. Instalar dependencias

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install nginx mariadb-server curl -y

# Instalar Python 3.13
sudo apt install python3.13 python3.13-venv -y

# Instalar uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 2. Configurar la aplicación

```bash
sudo useradd -m -s /bin/bash appuser
sudo -u appuser bash -c "
  git clone <repo> /home/appuser/app &&
  cd /home/appuser/app &&
  cp .env.example .env &&
  uv sync --no-dev
"
```

### 3. Configurar MariaDB

```bash
sudo mariadb -e "
  CREATE DATABASE fastapi_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER 'fastapi_user'@'localhost' IDENTIFIED BY 'password_seguro';
  GRANT ALL ON fastapi_db.* TO 'fastapi_user'@'localhost';
  FLUSH PRIVILEGES;
"
```

### 4. Aplicar migraciones

```bash
sudo -u appuser bash -c "cd /home/appuser/app && uv run alembic upgrade head"
```

### 5. Servicio systemd

Crear `/etc/systemd/system/fastapi.service`:

```ini
[Unit]
Description=FastAPI Application
After=network.target mariadb.service
Requires=mariadb.service

[Service]
Type=exec
User=appuser
Group=appuser
WorkingDirectory=/home/appuser/app
EnvironmentFile=/home/appuser/app/.env
ExecStart=/home/appuser/.local/bin/uv run uvicorn main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --no-access-log \
    --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fastapi
sudo systemctl status fastapi
```

### 6. Nginx como reverse proxy

```bash
sudo tee /etc/nginx/sites-available/fastapi <<'EOF'
upstream api_backend {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name tudominio.com;

    client_max_body_size 10M;

    location / {
        proxy_pass         http://api_backend;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/fastapi /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# SSL con Certbot
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d tudominio.com
```

---

## Checklist de Despliegue

- [ ] `.env` configurado con valores de producción
- [ ] `SECRET_KEY` generada de forma segura (`secrets.token_hex(32)`)
- [ ] Contraseñas de DB fuertes y únicas
- [ ] `DOCS_ENABLED=False` en producción (o ruta protegida)
- [ ] `CORS_ORIGINS` con dominios exactos (no `*` en producción)
- [ ] Migraciones aplicadas (`alembic upgrade head`)
- [ ] Health check respondiendo (`GET /health → 200`)
- [ ] SSL/HTTPS configurado y funcionando
- [ ] Certificados con renovación automática
- [ ] Puertos 80 y 443 abiertos, resto cerrados
- [ ] Backups automáticos de MariaDB configurados
- [ ] Logs monitoreados (`docker compose logs -f api`)
