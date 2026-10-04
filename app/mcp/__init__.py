"""
Servidor MCP del gateway: contexto de esquema para agentes.

POR QUÉ ES UN PAQUETE Y NO SE DISEMINA EN routes/ + controllers/ + services/
----------------------------------------------------------------------------
El control de seguridad más fuerte de este diseño es un **guard de importaciones**: ningún módulo
bajo ``app/mcp/**`` importa la capa de motor (``remote_engine``, ``common.build_target``,
``factory.get_adapter``, ``Database``). Ese guard es un test que corre **sin motor**, y solo se
puede escribir contra un **prefijo de ruta** — repartido en tres carpetas se vuelve una lista de
archivos que se desactualiza en el primer PR.

La convención pseudo-MVC del repo se respeta *adentro*: ``dispatch.py`` es la ruta, ``tools/*``
son los controllers, y el resolvedor y el façade son los services.

Y lo que el guard **no** compra, escrito para que nadie lo sobreestime: no prueba que no haya
puerta de atrás. Es evadible por transitividad —``context.py`` importa el resolvedor, que
necesariamente importa la capa de motor, así que está siempre a un salto— y por
``importlib.import_module``, que no produce ningún nodo ``Import``. Lo que compra es **evitar la
deriva accidental**, que no es trivial y sí es valioso.

EL INVARIANTE (fase 2: ``run_select`` lo reemplazó)
---------------------------------------------------
**El MCP ejecuta únicamente ``SELECT`` únicos validados, dentro de una transacción READ ONLY y bajo
una credencial por base con ``SELECT`` solamente.** Antes era "nunca EJECUTA SQL del agente". No es
prudencia genérica: ``sqlglot`` no tokeniza el contenido de los comentarios ejecutables ``/*!`` de
MySQL ni ``/*M!`` de MariaDB, así que todo guard por AST sobre SQL arbitrario es **evadible** — fue
una vulnerabilidad real de la consola SQL de este mismo repo, corregida en dos rondas. Por eso el
validador (``app/services/db_admin/agent_sql_policy.py``) es defensa en profundidad y la barrera real
es la cuenta del motor con ``SELECT`` sobre una sola base, la transacción READ ONLY y el timeout del
lado del servidor.

``draft_query`` ACEPTA el texto, lo clasifica y devuelve solo texto, con ``touches_engine`` en
``false``: esa ruta no tiene credencial ni façade. ``run_select`` (scope ``data.query``) ejecuta lo que
pasa el validador y devuelve como texto todo lo demás. Este paquete no puede importar la capa de
motor. Lo que el parseo no ve (vistas con ``DEFINER``, tablas ``FEDERATED``/``CONNECT``, diferencias
entre el parser y el motor) lo cierra el motor; los PII no se filtran (lista diferida, enmienda S14) y
una fila puede contener una inyección de prompt: riesgos aceptados.

Para ver datos hay dos vías: tools **parametrizados** (``sample_rows``, ``distinct_values``,
``count_rows``: no hay texto que interpretar) y ``run_select`` cuando la pregunta no entra en ellos.
"""
