import inspect
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.environments import ROOT_DIR


class AppHttpException(HTTPException):
    def __init__(
        self,
        message: str = "Error interno del servidor",
        status_code: int = 500,
        context: str | list | dict | None = None,
        public_context: dict | None = None,
        **extra,
    ):
        self.message = message
        self.status_code = status_code
        self.context = context
        # A diferencia de `context` (solo visible en development, es debug info interna),
        # `public_context` es dato ESTRUCTURADO que el cliente necesita para operar (p.ej.
        # qué versiones bloquean un rollback) y viaja SIEMPRE, en cualquier entorno.
        self.public_context = public_context
        self.loc = self.__get_caller_info()

        super().__init__(
            status_code=status_code,
            detail={
                "msg": self.message,
                "context": self.context,
                "public_context": self.public_context,
                "loc": self.loc,
                "extra": extra,
            },
        )

    def __get_caller_info(self) -> dict[str, Any]:
        """Obtiene informacion de dónde se lanzo la excepción"""
        # stack()[0] = _get_caller_info
        # stack()[1] = __init__
        # stack()[2] = quien creó la excepción
        frame = inspect.stack()[2]
        project_root = Path(ROOT_DIR)
        absolute_path = Path(frame.filename)
        depth = 2

        # Intentar obtener ruta relativa al proyecto
        if project_root:
            try:
                relative_path = absolute_path.relative_to(project_root)
                file_path = str(relative_path).replace("\\", "/")
            except ValueError:
                # Si esta fuera del proyecto, usar depth
                parts = absolute_path.parts[-depth:]
                file_path = "/".join(parts)
        else:
            # Si no hay raiz configurada, usar depth
            parts = absolute_path.parts[-depth:]
            file_path = "/".join(parts)

        return {
            # 'file': frame.filename.split('/')[-1],
            # 'file': frame.split('/')[-1],
            "file": file_path,
            "function": frame.function,
            "line": frame.lineno,
            "code": frame.code_context[0].strip() if frame.code_context else None,
        }
