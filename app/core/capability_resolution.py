"""
Resolvedor ÚNICO de las capacidades puntuales (``capability_grants``).

UN SOLO PREDICADO, TRES CONSUMIDORES
------------------------------------
Este módulo es el lugar donde se decide qué le agrega una capacidad puntual a un actor. Lo usan:

- la capa 1 (``admin_actor``): ``expand`` de cada capacidad puntual activa se UNE al conjunto del
  rol, en cualquier alcance (responde "¿podría, en algún alcance?");
- la capa 2 (``scope``): ``grants_allow``/``capability_at`` agregan las capacidades puntuales
  cuyo alcance COINCIDE con el destino;
- la vista de acceso efectivo (``explain``), que construye la procedencia con las mismas
  funciones, de modo que lo que se muestra no pueda divergir de lo que se hace cumplir.

LAS REGLAS (decisiones de negocio, no negociables acá)
------------------------------------------------------
- Una capacidad puntual SUMA al rol del alcance; nunca resta. Un ``viewer`` en producción con
  ``blueprints.apply`` en producción puede aplicar ahí y sigue siendo ``viewer`` en el resto.
- Solo cuentan las capacidades puntuales ``active`` (el SELECT ya filtra; acá no se re-evalúa).
  El lector descarta además toda fila cuya capacidad sea desconocida o NO otorgable
  (fail-closed, D3): una fila editada a mano con ``gateway.admin`` no acuña nada.
- Un destino global (sin entorno ni servidor) no coincide con ninguna capacidad puntual.
- Escribir/ejecutar implica la lectura de su módulo (``IMPLIED_READ``).
- Los tokens de agente nunca ganan capacidades puntuales: su rama no pasa por acá.

Este módulo no importa ``actor`` ni ``scope`` en el nivel superior (ambos lo importan a él); lo
que necesita de ``scope`` lo toma de forma diferida.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services.capability_catalog import (
    GLOBAL_CAPABILITIES,
    IMPLIED_READ,
    Capability,
    GatewayRole,
    GlobalCapability,
    is_grantable,
    role_capabilities,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.core.actor import Actor
    from app.core.scope import ScopePoint

#: Alcances sobre los que se puede otorgar una capacidad puntual (sin global, D2).
GRANT_SCOPE_TYPES: tuple[str, ...] = ("environment", "server")

#: ``(capacidad, scope_type, scope_id)``: la forma TIPADA que lleva el ``Actor``.
CapabilityGrantKey = tuple[Capability, str, int]


def expand(capability: Capability) -> frozenset[Capability]:
    """La capacidad más la lectura que trae implícita. Otorgar ``X`` hace efectiva su lectura."""
    return frozenset({capability}) | IMPLIED_READ.get(capability, frozenset())


def parse_capability_grants(
    rows: Iterable[Any] | None,
) -> list[tuple[Capability, str, int, int | None]]:
    """
    Filas crudas → ``(capacidad, scope_type, scope_id, grant_id)``. Fail-closed en el lector.

    Se descarta, sin romper, toda fila con capacidad desconocida o no otorgable, alcance de tipo
    desconocido o ``scope_id`` no positivo. Una fila legada o editada a mano no puede tumbar la
    autenticación ni acuñar una capacidad (D3). Acepta ``dict`` (con ``id``) o tupla
    ``(capacidad, scope_type, scope_id[, id])``.
    """
    out: list[tuple[Capability, str, int, int | None]] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                raw_cap, scope_type = row["capability"], row["scope_type"]
                scope_id, grant_id = row["scope_id"], row.get("id")
            else:
                raw_cap, scope_type, scope_id, *resto = row
                grant_id = resto[0] if resto else None
            if not is_grantable(raw_cap) or scope_type not in GRANT_SCOPE_TYPES:
                continue
            sid = int(scope_id)
            if sid <= 0:
                continue
            out.append((Capability(raw_cap), scope_type, sid, grant_id))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def layer1_capabilities(
    base_caps: Iterable[Capability], grants: Iterable[CapabilityGrantKey]
) -> frozenset[Capability]:
    """Capa 1: capacidades del rol/globales UNIDAS a ``expand`` de toda capacidad puntual."""
    caps = set(base_caps)
    for capability, _, _ in grants:
        caps |= expand(capability)
    return frozenset(caps)


def grants_allow(
    actor: "Actor",
    capability: Capability,
    *,
    environment_id: int | None,
    server_id: int | None,
) -> bool:
    """
    ¿Alguna capacidad puntual del actor concede ``capability`` en este destino?

    Coincide por entorno (``environment_id`` del destino YA resuelto, fail-closed) o por servidor.
    Un destino sin entorno ni servidor no coincide con nada. Pura: no toca la BD.
    """
    if actor.kind != "admin":
        return False
    for granted, scope_type, scope_id in actor.capability_grants:
        if capability not in expand(granted):
            continue
        if scope_type == "environment" and environment_id is not None:
            if scope_id == environment_id:
                return True
        elif scope_type == "server" and server_id is not None and scope_id == server_id:
            return True
    return False


def has_relevant_grant(actor: "Actor", capability: Capability) -> bool:
    """¿Hay alguna capacidad puntual que, en ALGÚN alcance, concedería ``capability``?"""
    return actor.kind == "admin" and any(
        capability in expand(granted) for granted, _, _ in actor.capability_grants
    )


def needs_target_resolution(actor: "Actor", capability: Capability) -> bool:
    """
    ¿Hay que resolver el destino (BD) para decidir la capa 2? Camino rápido de D8.

    No hace falta si el actor no es humano, o si no tiene alcances por rol NI capacidades
    puntuales relevantes para ``capability``. Sí hace falta si tiene alcances por rol (como
    siempre). Con capacidades puntuales relevantes y sin alcances, el rol base manda salvo que
    ya lo permita —en cuyo caso tampoco hace falta resolver—; la decisión es de ``capability_at``.
    """
    if actor.kind != "admin":
        return False
    return bool(actor.scope_roles) or has_relevant_grant(actor, capability)


def capability_at(
    actor: "Actor",
    capability: Capability,
    *,
    server_id: int | None,
    managed_database_id: int | None,
) -> bool:
    """
    ¿Tiene el actor ``capability`` EN este destino? Rol del alcance ∪ globales ∪ puntuales.

    Camino rápido (D8): sin alcances por rol y sin capacidad puntual relevante, o con el rol base
    ya permitiéndolo, no se toca la BD. Los keywords son obligatorios y sin default, igual que en
    ``assert_scope``: declarar el destino no es opcional.
    """
    from app.core.scope import (
        ScopePoint,
        _permits,
        resolve_environment_id,
        role_at_point,
    )

    if actor.role is None:
        # Token: su alcance por destino es el `project_id`, otra frontera. Nunca gana puntuales.
        return _permits(actor, GatewayRole.VIEWER, capability)

    base = actor.base_role if actor.base_role is not None else actor.role
    if not actor.scope_roles:
        # Sin alcances por rol manda el base; solo las puntuales pueden agregar algo.
        if _permits(actor, base, capability) or not has_relevant_grant(actor, capability):
            return _permits(actor, base, capability)

    env_id = resolve_environment_id(
        server_id=server_id, managed_database_id=managed_database_id
    )
    point = ScopePoint(environment_id=env_id, server_id=server_id)
    return _permits(actor, role_at_point(actor, point), capability, point)


def capability_at_point(actor: "Actor", capability: Capability, point: "ScopePoint") -> bool:
    """
    ¿Tiene el actor ``capability`` en un punto YA resuelto? Es el techo de quien otorga.

    Misma regla que ``capability_at`` pero sin resolver el destino: el llamador arma el
    ``ScopePoint`` (``(E, None)`` para un entorno; ``(entorno peor del servidor, S)`` para un
    servidor). Rol del alcance ∪ globales ∪ capacidades puntuales del propio actor, con las
    lecturas implícitas contando. Un token (sin rol) nunca llega acá.
    """
    from app.core.scope import _permits, role_at_point

    if actor.role is None:
        return False
    return _permits(actor, role_at_point(actor, point), capability, point)


# --------------------------------------------------------------------------- #
# Procedencia (vista de acceso efectivo)                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Entry:
    """Una capacidad efectiva y de DÓNDE sale. Una capacidad con varias fuentes repite filas."""

    capability: Capability
    #: ``role`` | ``scoped_role`` | ``capability_grant`` | ``global``
    source: str
    scope_type: str | None = None
    scope_id: int | None = None
    grant_id: int | None = None
    implied_by: Capability | None = None
    #: Solo para ``capability_grant`` de un usuario desactivado: retenida pero sin efecto.
    inert: bool = False


@dataclass(frozen=True, slots=True)
class ParsedAccess:
    """El contexto de acceso ya leído con tolerancia: lo que consumen el Actor y ``explain``."""

    base: GatewayRole
    scope_roles: tuple[tuple[str, int, GatewayRole], ...]
    globals_: frozenset[GlobalCapability]
    capability_grants: tuple[tuple[Capability, str, int, int | None], ...]


def parse_access_context(ctx: dict) -> ParsedAccess:
    """
    ``find_access_context`` → tipos del dominio. Fail-closed en el lector, en UN solo lugar.

    Un rol o una global desconocidos se ignoran (el rol cae a ``viewer``); un alcance de tipo
    desconocido se DESCARTA, no se degrada a otro tipo; una capacidad puntual no otorgable se
    descarta. Lo comparten ``get_current_actor`` y el endpoint de acceso efectivo.
    """
    try:
        base = GatewayRole(ctx["role"])
    except (KeyError, ValueError):
        base = GatewayRole.VIEWER

    scope_roles: list[tuple[str, int, GatewayRole]] = []
    for scope_type, scope_id, role in ctx.get("grants") or []:
        if scope_type not in ("environment", "server"):
            continue
        try:
            scope_roles.append((scope_type, int(scope_id), GatewayRole(role)))
        except (TypeError, ValueError):
            continue

    globals_: set[GlobalCapability] = set()
    for name in ctx.get("globals") or []:
        try:
            globals_.add(GlobalCapability(name))
        except ValueError:
            continue

    return ParsedAccess(
        base=base,
        scope_roles=tuple(scope_roles),
        globals_=frozenset(globals_),
        capability_grants=tuple(parse_capability_grants(ctx.get("capability_grants"))),
    )


def explain(ctx: dict, *, active: bool = True) -> list[Entry]:
    """
    Procedencia de cada capacidad de la capa 1, desde el MISMO contexto que acuña el ``Actor``.

    Invariante (lo prueba un test): el conjunto de capacidades de las entradas no ``inert`` es
    exactamente ``Actor.capabilities``. Con ``active=False`` (usuario desactivado) las entradas de
    capacidad puntual salen ``inert`` y no cuentan.
    """
    parsed = parse_access_context(ctx)
    entries: list[Entry] = []

    for cap in sorted(role_capabilities(parsed.base), key=lambda c: c.value):
        entries.append(Entry(cap, "role"))

    for scope_type, scope_id, role in sorted(parsed.scope_roles, key=lambda t: (t[0], t[1])):
        for cap in sorted(role_capabilities(role), key=lambda c: c.value):
            entries.append(Entry(cap, "scoped_role", scope_type, scope_id))

    for g in sorted(parsed.globals_, key=lambda g: g.value):
        for cap in sorted(GLOBAL_CAPABILITIES[g], key=lambda c: c.value):
            entries.append(Entry(cap, "global"))

    for granted, scope_type, scope_id, grant_id in parsed.capability_grants:
        for cap in sorted(expand(granted), key=lambda c: c.value):
            entries.append(
                Entry(
                    cap,
                    "capability_grant",
                    scope_type,
                    scope_id,
                    grant_id,
                    None if cap == granted else granted,
                    inert=not active,
                )
            )
    return entries
