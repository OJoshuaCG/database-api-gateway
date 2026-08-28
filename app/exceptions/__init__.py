from .AppHttpException import AppHttpException
from .HandlerExceptions import (
    app_exception_handler,
    generic_exception_handler,
    rate_limit_handler,
    validation_exception_handler,
)

# Este módulo es un RE-EXPORT: los nombres se importan para que el resto del código haga
# `from app.exceptions import AppHttpException` sin conocer la estructura interna. Sin
# `__all__` un linter los ve como imports huérfanos y "limpiarlos" rompería a todos los
# consumidores, así que la intención va declarada y no inferida.
__all__ = [
    "AppHttpException",
    "app_exception_handler",
    "generic_exception_handler",
    "rate_limit_handler",
    "validation_exception_handler",
]
