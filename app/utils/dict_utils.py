# Substrings que marcan una clave como sensible. Se compara por inclusión
# (no igualdad exacta) para cubrir variantes como root_password, hashed_password,
# new_password, api_key, etc. Erramos hacia enmascarar de más: en logs es preferible.
_SENSITIVE_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
)


def _is_sensitive_key(key) -> bool:
    k = str(key).lower()
    return any(s in k for s in _SENSITIVE_SUBSTRINGS)


def _sanitize_dict(data):
    """
    Enmascara recursivamente valores sensibles antes de loguearlos.
    Las claves que contengan un substring sensible se reemplazan con '***'.
    Recorre dicts y listas anidadas; otros valores se devuelven sin cambios.
    """
    if isinstance(data, dict):
        return {
            k: ("***" if _is_sensitive_key(k) else _sanitize_dict(v))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_sanitize_dict(v) for v in data]
    return data
