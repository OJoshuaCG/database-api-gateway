"""
Catálogo de la conversión de charset/collation: vocabulario CERRADO de códigos.

Los códigos viajan en ``public_context``, NUNCA en ``context``
---------------------------------------------------------------
``context`` se expone ÚNICAMENTE en development (``app/exceptions/HandlerExceptions.py``),
mientras ``public_context`` viaja siempre y el frontend lee exactamente
``detail.public_context.code``. Un código puesto en ``context`` no existe en producción, y el
cliente termina matcheando la PROSA del mensaje con expresiones regulares.

En este módulo eso **ya pasó** y es la deuda que este archivo empieza a saldar: el wizard de
la SPA (``collation-conversions/wizard/messages.ts``) clasifica **ocho** errores del módulo
con expresiones regulares sobre el texto en español, y su propio docstring explica el motivo
("el backend no expone un código de razón estructurado para los 409/422 de este módulo"). El
efecto es que reescribir un mensaje —algo que nadie considera un cambio de contrato— degrada
la UI en silencio.

Ojo con la documentación vieja: ``docs/api-reference-v8.md`` §3.0 afirma como regla dura que
*"en todo el resto del módulo no hay ``public_context``"* y *"nunca leas ``detail.context``"*.
Estos códigos la superseden, y ``docs/api-reference-v14.md`` tiene que decirlo explícitamente
o el frontend va a seguir el doc viejo. El parser del cliente no hay que tocarlo:
``errors.ts`` ya extrae ``public_context.code`` de forma genérica.

Por qué un vocabulario CERRADO y no strings inline
--------------------------------------------------
Un código inline es una cadena que solo existe en el sitio donde se lanza la excepción: nadie
puede enumerar el conjunto, el frontend no puede construir su mapa de mensajes de forma
exhaustiva, y un typo produce un código que jamás matchea sin que nada falle. Declararlos acá
los vuelve enumerables (``ALL_CODES``) y testeables. Mismo criterio que
``app/services/db_admin/clone_spec.py`` y ``export_spec.py``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Alcance de la conversión                                                     #
# --------------------------------------------------------------------------- #

# La BD elegida ES la base de metadatos del gateway. Convertirla reescribiría `audit_log`
# —el único control compensatorio declarado del sistema— y `servers`, que guarda las
# credenciales pseudo-root cifradas. Los otros tres módulos de la familia (clon, export,
# consola SQL) ya cierran esto; este no lo hacía.
CODE_SCOPE_NOT_ALLOWED = "collation.scope_not_allowed"

# --------------------------------------------------------------------------- #
# Lote por blueprint                                                           #
# --------------------------------------------------------------------------- #

# El conjunto de BDs que el cliente echó de vuelta no coincide con el previsualizado. Es
# fail-closed a propósito: recortar en silencio convertiría bases que el operador no confirmó,
# o dejaría afuera otras que sí. Molde de `apply_all`, que rechaza en vez de recortar.
CODE_BATCH_DATABASE_SET_MISMATCH = "collation.batch_database_set_mismatch"

# Falta el re-tipeo del nombre de una BD cuyo entorno bloquea migraciones destructivas. Un
# lote reemplaza N re-tipeos por uno, y esto REPONE el control donde `TODO.md` declara que
# vive: el doble factor por base.
CODE_BATCH_CONFIRMATION_REQUIRED = "collation.batch_confirmation_required"

# El lote no está en un estado que admita la operación pedida (ya ejecutado, ya cancelado…).
CODE_BATCH_NOT_PENDING = "collation.batch_not_pending"

# El blueprint no tiene ninguna BD elegible (`status=active` y del motor que aplica).
CODE_BATCH_NO_ELIGIBLE_DATABASES = "collation.batch_no_eligible_databases"

# Ítem del lote: la BD es de un motor al que el objetivo pedido no aplica (p. ej. un
# `target_charset` contra PostgreSQL). No aborta el lote: sale como ítem con `ok:false`.
CODE_ENGINE_NOT_APPLICABLE = "collation.engine_not_applicable"

ALL_CODES: frozenset[str] = frozenset(
    {
        CODE_SCOPE_NOT_ALLOWED,
        CODE_BATCH_DATABASE_SET_MISMATCH,
        CODE_BATCH_CONFIRMATION_REQUIRED,
        CODE_BATCH_NOT_PENDING,
        CODE_BATCH_NO_ELIGIBLE_DATABASES,
        CODE_ENGINE_NOT_APPLICABLE,
    }
)
