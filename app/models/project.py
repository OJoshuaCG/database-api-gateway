"""
Modelos de PROYECTO — agrupación de blueprints (``DatabaseModel``).

Un ``Project`` es una entidad deliberadamente VACÍA: nombre + descripción larga, y nada
más. No tiene versión, ni motor, ni estado de despliegue, ni credenciales. Su única razón
de existir es dar un nombre al conjunto de blueprints que participan de una misma
iniciativa: un proyecto "Citas" que involucra 2 bases de datos distintas son 2 blueprints;
un "Omnicanal" con 4 bases son 4 blueprints. Antes de esto, los blueprints estaban sueltos
y esa pertenencia solo existía en la cabeza de quien operaba.

La relación es N:M y OPCIONAL en los dos sentidos: un blueprint puede no pertenecer a
ningún proyecto, o pertenecer a varios (una BD compartida entre iniciativas es el caso
normal, no la excepción). Por eso hay tabla pivote y no una columna ``project_id`` en
``database_models``: esa columna forzaría "un blueprint, un proyecto" y habría que
migrarla el día que alguien comparta una base — que es el primer día.

REGLA DURA del ciclo de vida: borrar un proyecto borra la entidad y sus VÍNCULOS, nunca
los blueprints. Un blueprint es el esquema que replican N bases de datos reales con datos
reales; que un agrupador se lo lleve por delante sería catastrófico y silencioso. El
``ondelete="CASCADE"`` de esta tabla apunta al VÍNCULO, no al blueprint, y el controller
además borra los vínculos explícitamente antes del proyecto (ver
``ProjectController.delete_project``): SQLite no aplica claves foráneas por defecto, así
que depender solo del motor dejaría filas huérfanas en los entornos de test.
"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = ({"comment": "Proyectos: agrupación lógica de blueprints"},)

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del proyecto"
    )

    name: Mapped[str] = mapped_column(
        String(150),
        unique=True,
        index=True,
        nullable=False,
        comment="Nombre legible del proyecto (p. ej. 'Omnicanal'), único",
    )

    # ``Text`` y no ``String(5000)``: en MySQL/MariaDB un VARCHAR de ese largo consume
    # presupuesto de la fila (límite de 65 535 bytes por fila) y con utf8mb4 son 4 bytes
    # por carácter. El tope de 5000 se valida en el schema Pydantic, que es donde el
    # requisito puede subir sin migración de esquema.
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Descripción larga del proyecto"
    )

    def __repr__(self) -> str:
        return f"<Project(id={self.id}, name='{self.name}')>"


class ProjectDatabaseModel(Base, TimestampMixin):
    """
    Pivote proyecto ↔ blueprint. Tabla de PURO vínculo: sin campos propios.

    Clave primaria COMPUESTA a propósito, en vez del ``id`` sintético + ``UniqueConstraint``
    que usan otras tablas hija del repo: acá el par ES la identidad de la fila, y con PK
    compuesta un doble vínculo es imposible incluso ante un bug del controller. Asignar dos
    veces el mismo blueprint al mismo proyecto no es un error del usuario, es una no-op, y
    el controller la resuelve leyendo los vínculos existentes antes de insertar.
    """

    __tablename__ = "project_database_models"
    __table_args__ = (
        {"comment": "Vínculos N:M entre proyectos y blueprints (solo agrupación)"},
    )

    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Proyecto al que pertenece el vínculo",
    )

    # CASCADE también de este lado: si un blueprint se borra, lo que desaparece es su
    # PERTENENCIA a los proyectos, no los proyectos.
    model_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
        comment="Blueprint agrupado",
    )

    def __repr__(self) -> str:
        return f"<ProjectDatabaseModel(project_id={self.project_id}, model_id={self.model_id})>"
