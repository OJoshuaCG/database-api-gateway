# Cifrado de Credenciales

El gateway almacena credenciales muy sensibles (el password pseudo-root de cada
servidor destino). Se guardan **cifradas en reposo** con cifrado simétrico
**Fernet**, cuya clave se deriva de `SECRET_KEY`.

Módulo: `app/core/crypto.py`.

## Cómo funciona

```
SECRET_KEY ──HKDF-SHA256(salt=CRYPTO_KEY_SALT, info="db-gateway-fernet-v1")──▶ clave 32B ──▶ Fernet
```

- **Derivación:** `HKDF-SHA256` produce 32 bytes a partir de `SECRET_KEY`, que se
  codifican en base64 urlsafe (el formato que exige Fernet). No se guarda ninguna
  clave aparte: se deriva de forma determinística.
- **Fernet** aporta cifrado autenticado (AES-CBC + HMAC) con IV y timestamp; dos
  cifrados del mismo texto producen tokens distintos.
- **Lazy + cache:** `get_fernet()` usa `@lru_cache`, así que la clave **no** se evalúa
  al importar el módulo (no rompe procesos que no cifran, como Alembic).

## API del módulo

```python
from app.core import crypto

token = crypto.encrypt("s3cr3t")     # str -> token Fernet (str)
plano = crypto.decrypt(token)        # token -> str

crypto.try_decrypt(None)             # None (no lanza)
crypto.try_decrypt("corrupto")       # None
```

| Función | Comportamiento |
|---|---|
| `encrypt(plaintext)` | Cifra un string **no vacío**. Lanza `CryptoError` si está vacío; `CryptoConfigError` si falta `SECRET_KEY`. |
| `decrypt(token)` | Descifra. Lanza `CryptoError` si el token es inválido o se cifró con otra clave. |
| `try_decrypt(token)` | Variante tolerante: devuelve `None` ante fallo o `None`/vacío. Útil en listados/health-checks. |

### Excepciones propias

`crypto.py` es infraestructura: lanza `CryptoConfigError` / `CryptoError`, **no**
`AppHttpException`. Los controllers traducen a HTTP (p. ej. 500 "no se pudo
descifrar/cifrar la credencial").

## Uso en el proyecto

El `ServerController` cifra al guardar y descifra solo en memoria al operar:

```python
# Al crear/actualizar un Server
server.root_password_encrypted = crypto.encrypt(payload.root_password)

# Al construir el ServerTarget para conectarse al motor
password = crypto.decrypt(server.root_password_encrypted)
target = ServerTarget(..., admin_password=password)
```

La credencial descifrada **nunca** se persiste, se serializa en respuestas (`ServerOut`
no la incluye) ni se loguea (ver [sanitización de logs](logging.md) y `dict_utils`).

## Configuración

```env
SECRET_KEY=...                         # obligatorio en producción; base de la derivación
CRYPTO_KEY_SALT=db-gateway-static-salt # sal NO secreta del HKDF
```

> ⚠️ **Cambiar `SECRET_KEY` o `CRYPTO_KEY_SALT` invalida todos los secretos ya
> cifrados** (no se podrán descifrar). Trátalos como configuración estable del entorno.

## Rotación de clave (a futuro)

Fernet admite `MultiFernet([clave_nueva, clave_vieja])` para descifrar con la vieja y
re-cifrar con la nueva. El parámetro `info="db-gateway-fernet-v1"` del HKDF versiona la
derivación, dejando preparada una futura rotación (ver
[plan 06](../plans/06-operacion-seguridad-observabilidad.md)).

## Pruebas

`tests/test_crypto.py`: round-trip, tokens no deterministas, rechazo de vacío/corrupto,
`try_decrypt` tolerante, y fallo al descifrar con otra clave.

---

**Siguiente**: [Autenticación](authentication.md)
