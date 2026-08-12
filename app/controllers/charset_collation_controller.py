"""
Controller del catálogo global de charsets/collations.

Solo orquesta y traduce a HTTP: la política (qué está habilitado, qué es válido dar de alta,
cómo se resuelve una combinación) vive en ``app/services/charset_catalog.py``, que es también
lo que consume ``create_database`` para hacer el enforcement.

Modificar el catálogo NO toca ningún servidor destino, pero SÍ cambia qué DDL podrá emitir el
gateway más adelante: por eso se audita (``touched_engine=False``).
"""

from app.exceptions import AppHttpException
from app.services import audit, charset_catalog


class CharsetCollationController:
    def list_options(
        self, engine_family: str | None = None, only_enabled: bool = False
    ) -> list:
        family = (
            charset_catalog.normalize_family(engine_family)
            if engine_family is not None
            else None
        )
        return charset_catalog.list_options(
            engine_family_filter=family, only_enabled=only_enabled
        )

    def create_option(self, data: dict, *, admin: dict | None = None):
        family = charset_catalog.normalize_family(data.get("engine_family", ""))
        charset, collation = charset_catalog.validate_option_values(
            family, data.get("charset", ""), data.get("collation")
        )
        row = charset_catalog.create_option(
            family=family,
            charset=charset,
            collation=collation,
            enabled=bool(data.get("enabled", False)),
        )
        audit.record(
            "charset_collation_option.create",
            admin=admin,
            target_type="charset_collation_option",
            target_id=row.id,
            touched_engine=False,
            detail=(
                f"{family}: {charset}"
                + (f"/{collation}" if collation else "")
                + f" (enabled={row.enabled})"
            ),
        )
        return row

    def update_option(self, option_id: int, data: dict, *, admin: dict | None = None):
        enabled = data.get("enabled")
        is_default = data.get("is_default")
        if enabled is None and is_default is None:
            raise AppHttpException(
                message="Nada para actualizar: envía 'enabled' y/o 'is_default'.",
                status_code=422,
                context={"option_id": option_id},
            )
        row = charset_catalog.update_option(
            option_id, enabled=enabled, is_default=is_default
        )
        audit.record(
            "charset_collation_option.update",
            admin=admin,
            target_type="charset_collation_option",
            target_id=row.id,
            touched_engine=False,
            detail=f"enabled={row.enabled} is_default={row.is_default}",
        )
        return row
