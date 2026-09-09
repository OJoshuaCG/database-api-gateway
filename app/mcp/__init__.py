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

EL INVARIANTE QUE SOBREVIVE A TODA VERSIÓN FUTURA
-------------------------------------------------
**El MCP nunca acepta SQL del agente.** No es prudencia genérica: ``sqlglot`` no tokeniza el
contenido de los comentarios ejecutables ``/*!`` de MySQL ni ``/*M!`` de MariaDB, así que todo
guard por AST sobre SQL arbitrario es **evadible** — fue una vulnerabilidad real de la consola SQL
de este mismo repo, corregida en dos rondas.

Cuando haga falta ver datos, la vía son tools **parametrizados** (``sample_rows``,
``distinct_values``, ``count_rows``): cubren el caso real y son imposibles de volver destructivos,
porque no hay texto que interpretar.
"""
