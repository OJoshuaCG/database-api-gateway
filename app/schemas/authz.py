"""
Schemas de la autorización del gateway.

OJO CON EL PLANO: esto son capacidades **del gateway**. Los privilegios **del motor** son
otros schemas (`app/schemas/privilege.py`, `app/schemas/permission_profile.py`) y otro
vocabulario. Ver el docstring de ``app/services/capability_catalog.py``.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ScopeRoleOut(BaseModel):
    """Un rol por alcance. La SPA lo necesita para saber si el botón aplica a ESTA base."""

    scope_type: str = Field(..., description="global | environment | server")
    scope_id: int = Field(..., description="Id del entorno o servidor; 0 si es global")
    role: str = Field(..., description="viewer | operator | owner")


class MeOut(BaseModel):
    """
    Identidad y capacidades EFECTIVAS del actor de la sesión.

    Extiende lo que ``/auth/me`` devolvía (``id``, ``username``) **sin quitar nada**: la SPA de
    hoy sigue funcionando. Y una trampa concreta del frontend de este repo, ya anotada en su
    ``TODO.md``: hace ``safeParse`` del envelope COMPLETO, así que una divergencia de un campo
    descarta la respuesta entera — cada campo nuevo tiene que declararse `.nullish()` allá,
    nunca `.optional()`.

    ``capabilities`` NO es una lista paralela: se deriva del MISMO predicado que hace cumplir
    ``require()``. Publicar una promesa que el servidor no cumple es peor que no publicarla.

    **Es una pista de UI. Decide el servidor, siempre.**
    """

    id: int
    username: str
    role: str | None = Field(None, description="Rol efectivo (máximo sobre los alcances)")
    capabilities: list[str] = Field(
        default_factory=list,
        description="Capacidades efectivas: exactamente lo que require() va a aceptar",
    )
    global_capabilities: list[str] = Field(
        default_factory=list, description="access_admin | security_officer"
    )
    scope_roles: list[ScopeRoleOut] = Field(
        default_factory=list, description="Rol por alcance, para decidir por destino en la UI"
    )
    step_up_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Subconjunto de 'capabilities' que va a exigir reautenticación. Se publica para "
            "que la UI pida la contraseña ANTES de mandar la operación, en vez de descubrirlo "
            "por un error. El mecanismo todavía no está implementado."
        ),
    )
    previous_login_at: datetime | None = Field(
        None,
        description=(
            "Login exitoso ANTERIOR al actual (UTC). Es el anterior y no el último a propósito: "
            "cuando la SPA pide esto, el último YA es el login en curso. Sirve para que el "
            "usuario note un acceso que no hizo, y es la única detección que no depende de que "
            "alguien lea el audit_log"
        ),
    )
    last_failed_at: datetime | None = Field(
        None, description="Último intento fallido sobre esta cuenta (UTC)"
    )
    catalog_version: str = Field(
        ..., description="sha256 corto del catálogo, para invalidar caché del cliente"
    )


class CapabilityRowOut(BaseModel):
    """Una fila del catálogo publicado."""

    id: str
    module: str
    level: str
    label: str
    mutates: bool
    discloses: bool
    requires_step_up: bool
    agent_allowed: bool
    scope_axis: str
    roles: list[str]
    global_capabilities: list[str]


class ScopeReadinessServerOut(BaseModel):
    """Un servidor y a qué entorno derivaría si se activara el alcance por destino."""

    server_id: int
    server_name: str
    engine: str
    databases: int = Field(..., description="BDs del inventario en este servidor")
    unclassified: int = Field(..., description="Cuántas no tienen entorno asignado")
    derived_environment_slug: str | None = Field(
        None,
        description=(
            "Entorno que el guard le atribuiría a una operación a nivel SERVIDOR sobre este "
            "servidor. Sale de la MISMA regla que aplica el guard, no de un criterio paralelo"
        ),
    )
    derived_from_gap: bool = Field(
        ...,
        description=(
            "True si el entorno derivado sale del hueco de datos (sin bases, o con alguna sin "
            "clasificar) y NO de una clasificación real. Es la fila que hay que arreglar"
        ),
    )


class ScopeReadinessOut(BaseModel):
    """
    Preparación para otorgar acceso por alcance.

    Se pide ANTES de crear el primer grant restrictivo, porque una BD sin entorno se trata como
    el entorno más protegido: si hay filas sin clasificar, otorgar "lector en producción" le
    saca a esa persona el acceso a bases que nadie clasificó todavía.
    """

    total_databases: int
    unclassified_databases: int
    ready: bool = Field(
        ..., description="True cuando no queda ninguna BD sin entorno: se puede otorgar sin sorpresas"
    )
    fallback_environment_slug: str | None = Field(
        None, description="El entorno más protegido: a este resuelve todo lo que no esté clasificado"
    )
    servers: list[ScopeReadinessServerOut] = Field(default_factory=list)
