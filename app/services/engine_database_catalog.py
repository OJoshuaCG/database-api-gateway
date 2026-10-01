"""
Vocabulario cerrado de códigos de error sobre QUÉ base del motor puede usarse como origen
o destino de un módulo (snapshot, blueprint desde snapshot, comparación de esquemas).

Viaja en ``public_context["code"]``, nunca en ``context`` (solo visible en ``development``).
Lo emite ``db_admin.database_scope.assert_database_in_scope``.
"""

#: La base elegida es una base de SISTEMA del motor (``mysql``, ``sys``,
#: ``information_schema``, ``performance_schema``; en PostgreSQL ``postgres`` y los
#: templates) o la PROPIA base de metadatos del gateway. ``public_context.reason`` dice cuál
#: (``system_database`` | ``gateway_metadata``) y ``public_context.side`` qué lado del
#: pedido la nombró (``source`` | ``target``).
CODE_SCOPE_NOT_ALLOWED = "engine_database.scope_not_allowed"

ERROR_CODES = frozenset({CODE_SCOPE_NOT_ALLOWED})
