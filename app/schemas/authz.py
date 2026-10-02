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


class MyCapabilityGrantOut(BaseModel):
    """Una capacidad puntual VIVA (``pending`` | ``active``) de la propia persona."""

    id: int
    capability: str
    scope_type: str = Field(..., description="environment | server")
    scope_id: int
    scope_name: str | None = Field(None, description="Nombre del entorno o servidor")
    status: str = Field(..., description="pending | active")
    expires_at: datetime | None = Field(
        None, description="Solo las pendientes vencen (UTC); null si ya está activa"
    )


class SodWarningOut(BaseModel):
    """Una regla de separación de deberes que la cuenta de la sesión viola, y qué la cubre."""

    rule: str = Field(..., description="owner_security_officer | access_admin_security_officer")
    status: str = Field(
        ...,
        description=(
            "grandfathered (combinación heredada, sin vencimiento) | override (break-glass "
            "vigente) | neutralized (sin excepción: security_officer descartado)"
        ),
    )
    reason: str | None = None
    since: datetime | None = Field(None, description="Desde cuándo rige la excepción (UTC)")
    expires_at: datetime | None = Field(None, description="Vencimiento del override (UTC)")


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
    base_role: str | None = Field(
        None,
        description=(
            "Rol base (gateway_role), sin la unión con los grants. Es el que rige en un destino "
            "donde ningún grant de 'scope_roles' aplica."
        ),
    )
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
            "por un error. Ver 'step_up_expires_at' y POST /auth/step-up."
        ),
    )
    step_up_enforced: bool = Field(
        True,
        description=(
            "Si el servidor exige step-up. En false (STEP_UP_ENFORCED=False) la SPA no debe "
            "pedir la contraseña: ninguna operación va a responder access.step_up_required"
        ),
    )
    step_up_expires_at: datetime | None = Field(
        None,
        description=(
            "Fin de la ventana de step-up de ESTA sesión (UTC), abierta por el login o por POST "
            "/auth/step-up. null = sin ventana. Puede estar en el pasado: la ventana no se "
            "estira con el uso"
        ),
    )
    capability_grants: list[MyCapabilityGrantOut] = Field(
        default_factory=list,
        description=(
            "Capacidades puntuales VIVAS (pending|active) de ESTA persona, y solo las suyas. Las "
            "activas ya están sumadas en 'capabilities'; las pendientes no conceden nada todavía"
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
    sod_warnings: list[SodWarningOut] = Field(
        default_factory=list,
        description=(
            "Avisos de separación de deberes de ESTA cuenta. Vacío en el caso normal. Campo "
            "nuevo: la SPA lo declara .nullish()"
        ),
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
    # Subconjunto de ``mutates``: destruye o cambia de forma irreversible datos o estructura del
    # tercero. Declarado acá o el ``response_model`` lo descarta y la SPA no lo ve.
    destructive: bool
    roles: list[str]
    global_capabilities: list[str]
    # Capacidades puntuales: si se pueden otorgar sueltas, si exigen segundo aprobador y qué
    # lectura implica cada escritura. Sin declararlas acá, response_model las descartaba.
    grantable: bool
    sensitive: bool
    implies: list[str]


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
    server_resolution_inventory_only: bool = Field(
        True,
        description=(
            "Siempre true (F-17). La resolución de entorno a nivel SERVIDOR considera solo las "
            "BDs del inventario del gateway: una BD que existe en el motor pero no está "
            "inventariada no cuenta para la regla del entorno más protegido, y el gateway no "
            "lista el motor durante la autorización. Inventariarla es lo que la incorpora"
        ),
    )


class EffectiveScopeRoleOut(BaseModel):
    scope_type: str = Field(..., description="environment | server")
    scope_id: int
    scope_name: str | None = None
    role: str = Field(..., description="viewer | operator | owner")


class EffectiveCapabilityOut(BaseModel):
    """
    Una capacidad efectiva y de DÓNDE sale. Una capacidad con varias fuentes repite filas (una
    por fuente y alcance): la UI distingue «por rol» de «puntual» por ``source``.
    """

    capability: str
    source: str = Field(..., description="role | scoped_role | capability_grant | global")
    scope_type: str | None = Field(None, description="environment | server; null si no es por alcance")
    scope_id: int | None = None
    scope_name: str | None = None
    grant_id: int | None = Field(None, description="Id de la capacidad puntual (source=capability_grant)")
    implied_by: str | None = Field(
        None, description="Capacidad puntual que la trae implícita (lectura implícita)"
    )
    inert: bool = Field(
        False,
        description="True: retenida pero sin efecto (persona desactivada); no cuenta como acceso",
    )


class EffectiveAccessOut(BaseModel):
    """
    Acceso efectivo de una persona, con procedencia. Lo calcula ``explain`` —el MISMO resolvedor
    que hace cumplir ``require()``— sobre el MISMO contexto que acuña el ``Actor``.
    """

    user_id: int
    username: str
    active: bool
    base_role: str | None
    scope_roles: list[EffectiveScopeRoleOut] = Field(default_factory=list)
    global_capabilities: list[str] = Field(default_factory=list)
    capabilities: list[EffectiveCapabilityOut] = Field(default_factory=list)
    catalog_version: str


class SodUserRefOut(BaseModel):
    id: int
    username: str


class SodExceptionOut(BaseModel):
    """Una excepción VIVA a la separación de deberes."""

    id: int
    user: SodUserRefOut
    user_active: bool | None = Field(
        None, description="null si la cuenta ya no tiene security_officer"
    )
    rule: str = Field(..., description="owner_security_officer | access_admin_security_officer")
    kind: str = Field(..., description="grandfathered | override")
    reason: str
    since: datetime | None = Field(None, description="Alta de la excepción (UTC)")
    expires_at: datetime | None = Field(None, description="null = heredada, sin vencimiento")
    requested_by: SodUserRefOut | None = None
    approved_by: SodUserRefOut | None = Field(
        None, description="Siempre null hasta que exista el segundo aprobador"
    )
    still_violating: bool = Field(..., description="La cuenta sigue violando la regla")


class SodUncoveredOut(BaseModel):
    """Una cuenta que viola una regla SIN excepción: al leer se le descarta security_officer."""

    user: SodUserRefOut
    user_active: bool
    rules: list[str]


class SodReportOut(BaseModel):
    exceptions: list[SodExceptionOut] = Field(default_factory=list)
    uncovered: list[SodUncoveredOut] = Field(default_factory=list)
