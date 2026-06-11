# 03 — Aprovisionamiento de servidores (API de proveedor / Terraform)

**Estado:** Pendiente (futuro) · **Depende de:** 01 · **Esfuerzo:** alto

## Objetivo

Adquirir servidores en la nube vía API de un proveedor (DigitalOcean, AWS, Hetzner…),
preferiblemente con **Terraform** como motor de aprovisionamiento, y registrarlos
automáticamente en el inventario (`Server`) listos para el plan 04 (instalar motor).

## Enfoque recomendado

- **Terraform** como fuente de verdad de la infraestructura (idempotente, planificable,
  destruible). El gateway invoca Terraform y consume su `output` (IP, id, credenciales SSH).
- Abstracción `ProvisionProvider` para no acoplarse a un solo proveedor.

## Modelo de datos (extiende 01)

### `ProvisioningRequest` (`provisioning_requests`)
| Campo | Notas |
|---|---|
| `id`, timestamps | |
| `provider` | `digitalocean\|aws\|hetzner\|...` |
| `region`, `size`, `image` | parámetros del plan |
| `status` | `pending\|provisioning\|ready\|failed\|destroyed` |
| `terraform_workspace` | identificador del workspace/estado |
| `server_id` | FK→`servers.id` nullable (se llena al completar) |
| `ssh_*` | referencia (cifrada) a la credencial SSH generada |
| `error` | detalle si falla |

> El estado de Terraform NO se guarda en la BD: vive en su backend (local/remoto).
> El gateway solo referencia el workspace y consume outputs.

## Componentes

- `app/services/provisioning/` con:
  - `terraform_runner.py` — ejecuta `terraform init/plan/apply/destroy` en un workspace
    aislado por request, parsea outputs JSON. Ejecutar **en jobs en background** (plan 06).
  - `providers/` — plantillas `.tf` por proveedor + mapeo de parámetros.
- Credenciales del proveedor y claves SSH: cifradas con `app/core/crypto.py` o, mejor,
  un gestor de secretos externo (ver plan 06).

## API (`/api/v1`)

| Método | Path | Descripción |
|---|---|---|
| POST | `/provisioning/servers` | solicita un servidor (provider, region, size) → job |
| GET | `/provisioning/requests/{id}` | estado del aprovisionamiento |
| POST | `/provisioning/requests/{id}/destroy` | `terraform destroy` |
| (auto) | al quedar `ready` | crea/asocia un `Server` en el inventario |

## Decisiones a confirmar

- Proveedor(es) objetivo y si ya hay cuenta/credenciales.
- Backend de estado de Terraform (local vs. remoto S3/GCS/Terraform Cloud).
- ¿El gateway ejecuta Terraform localmente (necesita el binario y red saliente) o se
  delega a un runner/worker dedicado?

## Riesgos

- Operaciones largas y costosas (crean recursos facturables) → confirmación explícita,
  auditoría obligatoria (plan 06), y límites/cuotas.
- Seguridad de las credenciales del proveedor (máximo cuidado; preferir secret manager).

## Verificación

- Aprovisionar en un proyecto sandbox del proveedor, verificar que el `Server` queda
  registrado y alcanzable, y que `destroy` elimina el recurso y marca la request.
