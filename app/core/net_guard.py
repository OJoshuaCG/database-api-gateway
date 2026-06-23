"""
Guard anti-SSRF para el host de un servidor destino (SEGURIDAD).

Al registrar/editar un Server, ``validate_remote_host`` rechaza destinos que
convertirían al gateway en un proxy hacia su propia red:
  - loopback (127.0.0.0/8, ::1),
  - link-local / metadata de nube (169.254.0.0/16 — incluye 169.254.169.254 — y fe80::/10),
  - multicast, no especificados (0.0.0.0) y reservados.

Los rangos PRIVADOS se permiten por defecto (las BD destino suelen ser internas).
Si ``REMOTE_ALLOWED_CIDRS`` está definido, además exige que el host resuelva DENTRO
de esos CIDRs (allowlist estricta).

Caveat conocido: la validación es en tiempo de REGISTRO. Un atacante con DNS bajo su
control podría hacer rebinding (resolver distinto al conectar). Mitigación futura:
revalidar la IP justo antes de conectar. Hoy cubrimos el vector principal (registro).
"""

import ipaddress
import socket

from app.core import environments
from app.exceptions import AppHttpException


def _resolve_ips(host: str) -> list[str]:
    """IPs a las que apunta el host. Si es IP literal, ella misma; si es nombre, DNS."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AppHttpException(
            message="No se pudo resolver el host del servidor destino.",
            status_code=422,
            context={"reason": "dns_resolution_failed"},
        ) from exc
    return list({info[4][0] for info in infos})


def _is_dangerous(ip_obj: ipaddress._BaseAddress) -> bool:
    return (
        ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
    )


def validate_remote_host(host: str) -> None:
    """
    Valida el host destino. Lanza AppHttpException(422) si está bloqueado.
    No-op si REMOTE_SSRF_GUARD_ENABLED es False.
    """
    if not environments.REMOTE_SSRF_GUARD_ENABLED:
        return

    allowed = environments.REMOTE_ALLOWED_CIDRS
    for ip in _resolve_ips(host):
        ip_obj = ipaddress.ip_address(ip)
        if _is_dangerous(ip_obj):
            raise AppHttpException(
                message="Destino no permitido (loopback, link-local/metadata, multicast o reservado).",
                status_code=422,
                context={"reason": "blocked_range"},
            )
        if allowed and not any(ip_obj in net for net in allowed):
            raise AppHttpException(
                message="El destino no está dentro de los rangos permitidos (REMOTE_ALLOWED_CIDRS).",
                status_code=422,
                context={"reason": "not_in_allowlist"},
            )
