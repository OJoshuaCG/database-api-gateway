"""
Vocabulario cerrado de códigos de error de la CAPTURA de resultados de SELECT.

Estos códigos viajan en ``public_context["code"]``, **nunca** en ``context``: ``context`` solo se
expone en ``development`` (ver ``HandlerExceptions``), así que en producción el operador recibiría
el mensaje sin poder clasificarlo. Y hay un canal donde el ``public_context`` de la respuesta HTTP
**no existe**: en ``apply_all`` el guard se evalúa por BD dentro del bucle, así que la ruta
responde **200** con el rechazo embebido en el ítem y el controller copia el código a
``item["error_code"]``. Sin código estable ahí, el cliente vuelve a matchear prosa con expresiones
regulares — que es justo lo que este vocabulario evita.

Módulo aparte, y no constantes sueltas en el controller, para que los tests y quien documente el
contrato puedan importarlo sin arrastrar el controller entero. Quinto catálogo con este molde
(``environment_catalog``, ``provisioning_catalog``, ``clone_spec``, ``export_spec``): el prefijo
propio ``migration.capture_*`` es lo que los hace encontrables por familia.
"""

# --------------------------------------------------------------------------- #
# Códigos                                                                      #
# --------------------------------------------------------------------------- #

#: ``apply``/``rollback``: hay versiones con ``capture_selects=true`` y ``reviewed=false`` entre
#: las que esta corrida iba a ejecutar. Extraen datos de la BD destino y los persisten (cifrados)
#: en el gateway, así que exigen aprobación explícita de LA CONSULTA — que se revoca sola si el
#: SQL cambia. Es el ÚNICO gate de la captura desde que se retiró el consentimiento por corrida.
#: **No tiene escape**: la salida es aprobar la versión.
CODE_UNREVIEWED_CAPTURE = "migration.capture_unreviewed"

#: ``stamp``: marcar una versión con captura sin revisar. Código APARTE del anterior y no un
#: detalle: acá ``force=true`` **sí** es un escape legítimo (una versión aplicada hace meses a la
#: que después se le activó la captura queda ``reviewed=false``, y una BD que perdió su puntero
#: necesita poder re-stampearse). Con un código único, la SPA ofrecería «Forzar» donde no sirve.
CODE_UNREVIEWED_CAPTURE_STAMP = "migration.capture_unreviewed_stamp"

ERROR_CODES = frozenset({CODE_UNREVIEWED_CAPTURE, CODE_UNREVIEWED_CAPTURE_STAMP})
