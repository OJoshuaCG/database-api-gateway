"""
Modelo Environment — CATÁLOGO DE POLÍTICA que clasifica cada base gestionada por su
entorno de despliegue (desarrollo / staging / producción).

NO CONFUNDIR con ``app/core/environments.py``. Ese módulo es la configuración del PROCESO
(``APP_ENV``, credenciales, flags de arranque) y gobierna comportamiento de seguridad del
propio gateway: el flag ``Secure`` de la cookie, la exigencia de ``ADMIN_PASSWORD`` y
``SESSION_SECRET``, el rechazo del wildcard de CORS. Esta tabla, en cambio, clasifica las
bases de datos DE TERCEROS que el gateway administra. Los dos usan los mismos valores
(``development`` / ``production``) para cosas distintas, y ``GET /health`` ya devuelve un
campo llamado ``environment`` con el valor de ``APP_ENV``. Regla para no confundirlos: en el
código, la config se referencia SIEMPRE por su constante (``APP_ENV``) y esta tabla SIEMPRE
como ``Environment``; nunca "el entorno" a secas.

Es una tabla de POLÍTICA y no de presentación: ``blocks_destructive_migrations`` se evalúa
antes de aplicar una migración y puede NEGAR la operación. Molde:
``app/models/charset_collation_option.py``, que es la otra tabla del repo cuyo contenido
decide si algo llega o no al motor.

REGLA DE ALCANCE DE ESTA ENTREGA: **cero flags inertes.** El §2 del plan 11 propone cinco
flags de política; acá existe UNO, porque es el único que el servidor hace cumplir hoy. Los
otros cuatro (``requires_confirmation``, ``requires_previous_environment``,
``max_databases_per_apply``, ``allows_agent_queries``) se agregan en la tarea que los
implemente, con su guard. Un booleano que la API expone y nadie lee, la SPA lo pinta como un
control activo: es peor que su ausencia.

``rank`` NO ES ÚNICO, y es deliberado. La versión original de este diseño lo hacía único
"para que el predecesor sea determinista", y eso traía dos problemas:
    1. Rompía el seed EN SILENCIO. Si el operador renombra el slug ``production``, el seed
       idempotente intenta insertar la fila de nuevo con ``rank=30``, choca el único, y el
       ``except`` del patrón de seed se traga el ``IntegrityError`` y loguea. Desde ese
       momento el seed queda muerto para siempre sin que nada falle.
    2. No hacía falta. El orden total **(rank, id)** ya define el predecesor sin ambigüedad
       (ver ``environment_sort_key``), y sin único un reordenamiento no colisiona en el paso
       intermedio de un swap — que en MySQL/MariaDB no se puede diferir.
Consecuencia aceptada: dos entornos pueden empatar en ``rank``, y ahí el desempate es por
``id``. Es raro, no es incorrecto.

``is_default`` a lo sumo en una fila. NO se hace cumplir con un índice único parcial
(``WHERE is_default``) porque no es portable: MySQL 8 no tiene índices parciales, y el truco
funcional ``UNIQUE ((CASE WHEN is_default THEN 1 END))`` existe en MySQL 8.0.13+ pero NO en
MariaDB 11. Se hace cumplir en ``EnvironmentController``, y **con bloqueo de filas**: el
patrón "apagar los demás y después encenderme" tiene una carrera real que deja DOS defaults
sin ningún error (ver el docstring de ``EnvironmentController._claim_default``). Nota para el
próximo lector: ``charset_catalog.update_option`` tiene esa misma carrera SIN cerrar y su
docstring afirma un invariante que el mecanismo no garantiza — no es un precedente a copiar.

``slug`` se compara contra input del usuario (es el gesto de confirmación para debilitar la
política), así que se normaliza y se compara EN PYTHON, no con un ``WHERE slug = :x``:
MySQL/MariaDB comparan case-insensitive por default y PostgreSQL case-sensitive, así que la
misma fila sería violación de único en un motor y dos filas distintas en el otro. Mismo
criterio y mismo motivo que ``app/services/charset_catalog.py``.
"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Environment(Base, TimestampMixin):
    __tablename__ = "environments"
    __table_args__ = (
        {"comment": "Entornos de despliegue: clasifican las BDs gestionadas y su política"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del entorno"
    )

    name: Mapped[str] = mapped_column(
        String(60),
        unique=True,
        index=True,
        nullable=False,
        comment="Nombre legible del entorno (p. ej. 'Producción'), único",
    )

    slug: Mapped[str] = mapped_column(
        String(60),
        unique=True,
        index=True,
        nullable=False,
        comment="Identificador estable en minúsculas (p. ej. 'production'). Es lo que se audita",
    )

    # NO único: ver el docstring del módulo. El orden total lo da (rank, id).
    rank: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        default=0,
        server_default="0",
        comment=(
            "Orden de promoción: MENOR = MÁS TEMPRANO (desarrollo antes que producción). "
            "No es único; el desempate es por id"
        ),
    )

    # Enum cerrado y no string libre: el `tone` del Badge de la SPA es una unión cerrada de
    # seis valores, así que un color arbitrario habría que inyectarlo en `style` con el
    # problema de sanitización que eso arrastra. Se valida en el schema Pydantic.
    color: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment=(
            "Color de presentación. Uno de: neutral|primary|success|error|warning|info. "
            "Solo presentación: no participa de ninguna decisión"
        ),
    )

    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment=(
            "Entorno que se asigna a una BD nueva que no lo especifica (a lo sumo uno True). "
            "OJO: el default es el entorno más permisivo, así que 'nace clasificada' no "
            "equivale a 'nace protegida'"
        ),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Si se puede asignar a una BD. Es la vía de retiro de un entorno con BDs",
    )

    blocks_destructive_migrations: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment=(
            "POLÍTICA APLICADA: rechaza aplicar versiones con sentencias destructivas "
            "(DROP/TRUNCATE/DELETE sin WHERE/ALTER DROP COLUMN) a las BDs de este entorno"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Environment(id={self.id}, slug='{self.slug}', rank={self.rank}, "
            f"blocks_destructive={self.blocks_destructive_migrations})>"
        )


def environment_sort_key(rank: int, env_id: int) -> tuple[int, int]:
    """
    Orden total de promoción: ``(rank, id)``, menor = más temprano.

    Existe como función y no inline en cada query porque es el criterio que van a compartir
    el listado y el futuro guard de ``requires_previous_environment``. Con ``rank`` no único
    (ver el docstring del módulo) el desempate por ``id`` es lo que sostiene el determinismo,
    y si cada llamador lo escribe a mano, dos lugares del sistema pueden responder distinto a
    "¿cuál es el entorno anterior?".
    """
    return (rank, env_id)
