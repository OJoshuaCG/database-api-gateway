# BD de metadatos del gateway — representación SQL

Esta carpeta es una **foto legible del esquema** de la base de datos interna del
gateway (el plano de metadatos), para entender a nivel SQL cómo está construida.

> **Alembic sigue siendo la fuente de verdad.** Los cambios de esquema se hacen con
> migraciones (`alembic/versions/`) y se aplican con `alembic upgrade head`. Esta
> carpeta NO se aplica automáticamente; es referencia y documentación.

## Estructura

```
gateway/
├── schema.sql        # esquema completo en un solo archivo
├── tables/           # una tabla por archivo (DDL MySQL/MariaDB)
├── views/            # vistas (ninguna por ahora)
├── procedures/       # procedimientos almacenados (ninguno por ahora)
├── triggers/         # triggers (ninguno por ahora)
└── seeds/
    └── privileges.sql  # llenado del catálogo de privilegios (referencia)
```

El DDL de `tables/` y `schema.sql` se **genera desde los modelos ORM** (dialecto
MySQL/MariaDB, el motor de metadatos) para que no haya divergencia con la app.

## Regenerar

Tras cambiar un modelo o agregar una migración:

```bash
uv run python database/generate_schema.py
```

## Catálogo de privilegios

`seeds/privileges.sql` refleja el catálogo `privileges` (qué privilegios existen por
motor y cuáles controla la plataforma, vía `is_active`). El llenado **real** en
ejecución lo hace `app/services/privilege_catalog.py` al arrancar (idempotente y
preservando el `is_active` que haya tocado un operador). La validación de seguridad
(qué token es válido y a qué nivel) vive en `app/services/db_admin/privileges.py`.
