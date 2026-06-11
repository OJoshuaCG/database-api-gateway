# 04 — Instalación y configuración del motor vía SSH

**Estado:** Pendiente (futuro) · **Depende de:** 03 · **Esfuerzo:** alto

## Objetivo

Tomar un servidor "desde cero" (recién aprovisionado o existente) y dejarlo listo
como servidor de BD destino: instalar dependencias, configurar firewall y puertos,
instalar y configurar el motor (MySQL/MariaDB/PostgreSQL), y crear el usuario
**pseudo-root** que el gateway usará. Al terminar, el `Server` queda operativo y
`test-connection` responde OK.

## Enfoque recomendado

- **SSH** con `asyncssh` (async) o `paramiko` (sync). Dado que ejecutaremos en jobs
  en background (plan 06), cualquiera sirve; `asyncssh` integra mejor si el runner es async.
- **Idempotencia y orquestación:** considerar **Ansible** (playbooks por motor) en lugar
  de scripts shell sueltos. Ventaja: idempotente, repetible, mantenible. Alternativa
  mínima: scripts bash versionados ejecutados por SSH.
- Una abstracción `EngineInstaller` por motor (`mysql`, `mariadb`, `postgresql`).

## Componentes

- `app/services/installer/`:
  - `ssh_client.py` — conexión SSH (clave privada referenciada por el `Server`/request,
    descifrada en memoria), ejecución de comandos con captura de stdout/stderr y código.
  - `engine_installer.py` + `playbooks/` (o `scripts/`) por motor y por distro.
- Pasos típicos del playbook:
  1. `apt/yum update`, instalar dependencias.
  2. Configurar firewall (ufw/firewalld) y abrir el puerto del motor solo a orígenes permitidos.
  3. Instalar el motor; habilitar y arrancar el servicio.
  4. Endurecer config (bind-address, `require_secure_transport`/SSL, límites).
  5. Crear usuario **pseudo-root** con privilegios de administración; guardar su
     credencial **cifrada** en el `Server`.
  6. Verificar con `test-connection`.

## Modelo de datos (extiende 03)

### `InstallationJob` (`installation_jobs`)
| Campo | Notas |
|---|---|
| `id`, timestamps | |
| `server_id` | FK→`servers.id` |
| `engine`, `engine_version` | objetivo |
| `status` | `pending\|running\|success\|failed` |
| `log` | `Text` — salida (saneada, sin secretos) |
| `error` | detalle si falla |

## API (`/api/v1`)

| Método | Path | Descripción |
|---|---|---|
| POST | `/servers/{id}/install-engine` | lanza la instalación (job en background) |
| GET | `/installation-jobs/{id}` | estado + log saneado |

## Decisiones a confirmar

- ¿Ansible o scripts SSH propios? (recomendado Ansible por idempotencia).
- Distros/versiones objetivo (Ubuntu/Debian/RHEL) y versiones de motor soportadas.
- Origen y custodia de la clave SSH (preferir secret manager — plan 06).

## Riesgos

- Operación privilegiada y peligrosa (root sobre un servidor). Exigir autorización
  explícita, auditoría completa, y **nunca** registrar credenciales/clave en el `log`.
- Diferencias entre distros/versiones → playbooks probados por combinación.

## Verificación

- Sobre un servidor sandbox limpio: ejecutar instalación de cada motor, confirmar
  servicio activo, firewall correcto, usuario pseudo-root creado y `test-connection` OK.
