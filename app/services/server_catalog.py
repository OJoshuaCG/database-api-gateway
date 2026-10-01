"""
Vocabulario cerrado de códigos de error del inventario de SERVIDORES.

Viajan en ``public_context["code"]``, nunca en ``context`` (solo visible en ``development``).
Los emite ``ServerController``.
"""

#: El ``PATCH`` re-apunta el servidor (cambia ``host``, ``port`` o ``engine``) o debilita su TLS
#: (desde ``require``/``verify-ca``/``verify-full`` hacia un modo más débil) sin traer
#: ``root_password``. La credencial guardada no se reutiliza contra un destino distinto del
#: que se registró: hay que volver a enviarla en el mismo request. ``public_context.fields``
#: lista qué campos dispararon la exigencia.
CODE_CREDENTIAL_REQUIRED_FOR_REBIND = "server.credential_required_for_rebind"

ERROR_CODES = frozenset({CODE_CREDENTIAL_REQUIRED_FOR_REBIND})
