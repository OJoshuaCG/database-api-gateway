#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Init-script del motor DESTINO de prueba (MariaDB).
#
# La imagen oficial deja root restringido a localhost (default seguro). El gateway
# corre en OTRO contenedor (`api`), así que necesita un usuario admin alcanzable
# desde la red. Aquí creamos `gw_admin`@'%' con privilegios pseudo-root
# (ALL PRIVILEGES + GRANT OPTION) para que el gateway pueda crear/borrar BDs y
# usuarios sobre este motor.
#
# Se ejecuta UNA sola vez, al inicializar un volumen de datos vacío.
# La contraseña llega por entorno (TARGET_MARIADB_GW_PASS) — no se versiona aquí.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [ -z "${TARGET_MARIADB_GW_PASS:-}" ]; then
    echo "[init-mariadb] ERROR: TARGET_MARIADB_GW_PASS no está definido." >&2
    exit 1
fi

echo "[init-mariadb] Creando usuario admin del gateway: gw_admin@'%'"

mariadb -u root -p"${MARIADB_ROOT_PASSWORD}" <<-SQL
    CREATE USER IF NOT EXISTS 'gw_admin'@'%' IDENTIFIED BY '${TARGET_MARIADB_GW_PASS}';
    GRANT ALL PRIVILEGES ON *.* TO 'gw_admin'@'%' WITH GRANT OPTION;
    FLUSH PRIVILEGES;
SQL

echo "[init-mariadb] Listo. Registrar en el gateway con root_username=gw_admin."
