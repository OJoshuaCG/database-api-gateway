#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Bajar privilegios a appuser tras corregir el ownership de los volúmenes.
#
# El contenedor arranca como root (ver Dockerfile) SOLO para esto: un volumen
# nombrado (p. ej. exports_data) que ya existía con otro ownership -creado
# antes de este fix, o recreado por Dokploy/Compose sin copiar el ownership de
# la imagen- deja a appuser sin permiso de escritura, y eso no se corrige
# reconstruyendo la imagen (Docker solo copia ownership al CREAR el volumen).
# Corriendo esto en cada arranque, el fix se aplica con un simple redeploy,
# sin acceso SSH al host.
# ─────────────────────────────────────────────────────────────────────────────
if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/exports
    chown appuser:appuser /app/exports
    chmod 0700 /app/exports
    exec gosu appuser "$0" "$@"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Función: esperar a que la BD de metadatos acepte conexiones
#
# Distingue dos clases de fallo, porque reintentar las dos por igual es dañino:
#
# - TRANSITORIO (la BD no responde todavía, 2003/2013, red caída): se reintenta.
#   Es la ventana de arranque de la `db` del compose y los cortes breves de red.
# - PERMANENTE (credenciales, permisos, base inexistente): sale en el PRIMER
#   intento. Esperar no lo arregla, y reintentarlo no es inocuo: con el reinicio
#   en loop de `restart: unless-stopped`, 15 intentos por arranque son unos 19
#   logins fallidos por minuto, indefinidamente, contra un servidor que puede
#   ser externo. Si ese servidor tiene `max_password_errors`, BLOQUEA la cuenta
#   (y la contraseña correcta deja de funcionar hasta un `FLUSH PRIVILEGES`); si
#   tiene fail2ban, banea la IP del host. Ya pasó un deploy con `DB_PASS` mal
#   donde el log solo decía "no lista" y el loop siguió golpeando la BD.
#
# Clasificar un error como permanente por error cuesta poco: el contenedor sale,
# Docker lo reinicia y el siguiente arranque vuelve a intentar.
#
# El error de pymysql se imprime SIEMPRE (antes se descartaba con 2>/dev/null y
# un "Access denied" se veía igual que una BD arrancando). Su texto incluye
# usuario y host, nunca la contraseña.
# ─────────────────────────────────────────────────────────────────────────────
wait_for_db() {
    local max_retries=15
    local retry=0
    local status

    echo "[entrypoint] Esperando conexión a la BD de metadatos (${DB_HOST}:${DB_PORT:-3306})..."

    while true; do
        status=0
        python - <<'PYEOF' || status=$?
import os, sys

import pymysql

# Códigos de MySQL/MariaDB que esperar no arregla.
PERMANENT = {
    # MariaDB responde 1044 (no 1049) a un usuario sin privilegios globales cuando la base
    # NO EXISTE: no revela si existe. Por eso el mensaje nombra las dos causas.
    1044: "el usuario no tiene acceso a DB_NAME, o esa base no existe (revisar DB_NAME y el GRANT)",
    1045: "usuario o contraseña incorrectos (revisar DB_USER / DB_PASS)",
    1049: "DB_NAME no existe en el servidor",
    1129: "el servidor bloqueó este host por max_connect_errors (requiere FLUSH HOSTS)",
    1130: "el usuario no puede conectarse desde la IP de este host (revisar el host del CREATE USER)",
    1251: "el servidor exige un plugin de autenticación que el cliente no soporta",
    1698: "acceso denegado por el plugin de autenticación del usuario",
}

try:
    conn = pymysql.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        database=os.getenv("DB_NAME"),
        connect_timeout=3,
    )
    conn.close()
except Exception as e:
    code = e.args[0] if e.args and isinstance(e.args[0], int) else None
    print(f"[entrypoint]   {type(e).__name__}: {e}", file=sys.stderr)
    if code in PERMANENT:
        print(f"[entrypoint]   Error permanente {code}: {PERMANENT[code]}.", file=sys.stderr)
        sys.exit(2)
    sys.exit(1)
PYEOF
        if [ "$status" -eq 0 ]; then
            break
        fi
        if [ "$status" -eq 2 ]; then
            echo "[entrypoint] ERROR: la BD de metadatos rechazó la conexión. No se reintenta:"
            echo "[entrypoint] esperar no lo arregla, y repetir logins fallidos puede bloquear"
            echo "[entrypoint] la cuenta en el servidor. Corregir la variable y volver a desplegar."
            exit 1
        fi

        retry=$((retry + 1))
        if [ "$retry" -ge "$max_retries" ]; then
            echo "[entrypoint] ERROR: la BD de metadatos no respondió después de $max_retries intentos."
            exit 1
        fi
        echo "[entrypoint] BD de metadatos no disponible (intento $retry/$max_retries). Reintentando en 3s..."
        sleep 3
    done

    echo "[entrypoint] Conexión a la BD de metadatos establecida."
}

# ─────────────────────────────────────────────────────────────────────────────
# Función: aplicar migraciones Alembic
# ─────────────────────────────────────────────────────────────────────────────
run_migrations() {
    # Pre-vuelo del grafo de revisiones ANTES de invocar a Alembic.
    #
    # No es redundante con 'alembic upgrade head': cuando el árbol tiene dos
    # heads, Alembic responde "Multiple head revisions are present for given
    # argument 'head'" y nada más. Ese mensaje no dice CUÁLES son las dos
    # puntas, no dice que la BD quedó intacta, y no dice cómo salir — así que
    # el operador que mira un contenedor reiniciándose en loop no tiene con
    # qué arrancar. Pasó en producción el 2026-08-22 y costó un rato entender
    # que la base nunca se había tocado.
    #
    # El guard nombra las revisiones en conflicto y explica el arreglo. Es el
    # MISMO script que corre en el hook de pre-push y en CI, así que un árbol
    # que llegó hasta acá roto significa que se saltearon los dos.
    echo "[entrypoint] Verificando el grafo de migraciones..."
    if ! python scripts/check_migration_graph.py; then
        echo "[entrypoint] ERROR: el grafo de migraciones no es aplicable."
        echo "[entrypoint] La base de datos NO se modificó: esto falla al resolver"
        echo "[entrypoint] a qué revisión ir, antes de abrir cualquier transacción."
        echo "[entrypoint] Se arregla en el repo (ver el detalle de arriba) y se"
        echo "[entrypoint] vuelve a desplegar. No hace falta tocar la BD a mano."
        exit 1
    fi

    echo "[entrypoint] Aplicando migraciones Alembic..."
    alembic upgrade head
    echo "[entrypoint] Migraciones aplicadas correctamente."
}

# ─────────────────────────────────────────────────────────────────────────────
# Función: iniciar la aplicación FastAPI con Uvicorn
#
# WORKERS=1 por defecto. Con más de uno hace falta RATE_LIMIT_REDIS_ENABLED=True, y la app se
# NIEGA a arrancar sin él (guard en app/core/environments.py): con el almacenamiento en
# memoria de SlowAPI cada worker lleva su propio contador, así que el límite real sería N
# veces el configurado — y nadie se entera, porque cada worker cree estar cumpliendo.
# Ver: docs/features/rate-limiting.md
#
# TRUSTED_PROXY_IPS reemplaza al `--forwarded-allow-ips "*"` que estaba hardcodeado acá.
# Confiar en `X-Forwarded-For` de CUALQUIER origen deja que el cliente elija su propia clave
# de rate limit y la rote: los 5/min del login y los 3/min del DROP DATABASE se evadían con
# un header. El default (`127.0.0.1`) solo confía en localhost; en producción la app exige
# que se fije la IP o el CIDR del proxy reverso.
# ─────────────────────────────────────────────────────────────────────────────
start_app() {
    local workers="${WORKERS:-1}"
    local trusted_proxies="${TRUSTED_PROXY_IPS:-127.0.0.1}"
    echo "[entrypoint] Iniciando FastAPI con $workers worker(s); proxies confiables: $trusted_proxies"
    exec uvicorn main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers "$workers" \
        --no-access-log \
        --proxy-headers \
        --forwarded-allow-ips "$trusted_proxies"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
wait_for_db
run_migrations
start_app
