"""
Tools de inventario operativo: ``list_environments``, ``list_exports`` y ``list_clones``.

**No tocan ningún motor**: leen la BD de metadatos del gateway, a través de
``target_resolution``, y siempre como proyección de las bases que el token alcanza. Ninguna
devuelve contenido: los exports y clonados salen con estado y fechas, nunca con su plan, su
selección, sus artefactos, su ``confirm_token`` ni el texto de su error.

Cada tool exige su propio scope (``environments.read``, ``exports.read``, ``clones.read``), que el
dispatcher verifica antes de llamar al handler y que ``ToolContext`` reenvía al gate.
"""

from __future__ import annotations

from app.mcp.context import ToolContext
from app.mcp.tools._envelope import clean, iso
from app.schemas import mcp as out


def list_environments(ctx: ToolContext, params: dict) -> dict:
    from app.controllers.target_resolution import reachable_environments

    filas = reachable_environments(ctx.actor, ctx.capability)
    data = out.EnvironmentListOut(
        environments=[
            out.EnvironmentOut(
                slug=clean(f["slug"]),
                name=clean(f["name"]),
                rank=f["rank"],
                allows_agent_access=f["allows_agent_access"],
                blocks_destructive_migrations=f["blocks_destructive_migrations"],
                reachable_database_count=f["reachable_database_count"],
            )
            for f in filas
        ],
        count=len(filas),
    )
    return data.model_dump(mode="json")


def list_exports(ctx: ToolContext, params: dict) -> dict:
    from app.controllers.target_resolution import reachable_export_jobs

    filas = reachable_export_jobs(ctx.actor, ctx.capability)
    data = out.ExportJobListOut(
        jobs=[
            out.ExportJobOut(
                job_id=f["job_id"],
                database_id=f["database_id"],
                status=clean(f["status"]),
                phase=clean(f["phase"]),
                structure_drift_detected=f["structure_drift_detected"],
                has_error=f["has_error"],
                created_at=iso(f["created_at"]),
                started_at=iso(f["started_at"]),
                finished_at=iso(f["finished_at"]),
            )
            for f in filas
        ],
        count=len(filas),
    )
    return data.model_dump(mode="json")


def list_clones(ctx: ToolContext, params: dict) -> dict:
    from app.controllers.target_resolution import reachable_clone_jobs

    filas = reachable_clone_jobs(ctx.actor, ctx.capability)
    data = out.CloneJobListOut(
        jobs=[
            out.CloneJobOut(
                job_id=f["job_id"],
                source_database_id=f["source_database_id"],
                target_database_id=f["target_database_id"],
                status=clean(f["status"]),
                phase=clean(f["phase"]),
                include_data=f["include_data"],
                has_error=f["has_error"],
                created_at=iso(f["created_at"]),
                started_at=iso(f["started_at"]),
                finished_at=iso(f["finished_at"]),
            )
            for f in filas
        ],
        count=len(filas),
    )
    return data.model_dump(mode="json")


def list_catalogs(ctx: ToolContext, params: dict) -> dict:
    """
    Catálogos de referencia del gateway: privilegios, charsets/collations y plantillas de perfil.

    No depende de las bases que alcanza el token porque no describe ninguna: son datos globales
    del gateway, sin servidores, bases ni usuarios del motor (ver
    ``target_resolution.reference_catalogs``).
    """
    from app.controllers.target_resolution import reference_catalogs

    cat = reference_catalogs(ctx.actor, ctx.capability)
    data = out.CatalogsOut(
        privileges=[
            out.PrivilegeOut(
                engine=clean(p["engine"]),
                name=clean(p["name"]),
                category=clean(p["category"]),
                context=clean(p["context"]),
                description=clean(p["description"]),
                is_sensitive=p["is_sensitive"],
            )
            for p in cat["privileges"]
        ],
        charsets=[
            out.CharsetOut(
                engine_family=clean(c["engine_family"]),
                charset=clean(c["charset"]),
                collation=clean(c["collation"]),
                is_default=c["is_default"],
            )
            for c in cat["charsets"]
        ],
        permission_profiles=[
            out.PermissionProfileOut(
                name=clean(p["name"]),
                engine=clean(p["engine"]),
                items=[
                    out.ProfileItemOut(
                        level=clean(i["level"]), privileges=[clean(x) for x in i["privileges"]]
                    )
                    for i in p["items"]
                ],
            )
            for p in cat["permission_profiles"]
        ],
    )
    return data.model_dump(mode="json")
