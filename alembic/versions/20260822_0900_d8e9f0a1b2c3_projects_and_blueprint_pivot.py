"""projects + pivote N:M con database_models (agrupación de blueprints)

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-22 09:00:00.000000

Un ``Project`` agrupa blueprints y nada más: nombre + descripción larga. La relación es
N:M y opcional en los dos sentidos, así que va en tabla pivote y no en una columna
``project_id`` de ``database_models`` — esa columna forzaría "un blueprint, un proyecto" y
habría que migrarla el primer día que dos iniciativas compartan una base.

``project_database_models`` tiene clave primaria COMPUESTA ``(project_id, model_id)``: el
par ES la identidad de la fila, y así un doble vínculo es imposible incluso ante un bug del
controller.

Los DOS ``ondelete='CASCADE'`` apuntan al VÍNCULO, nunca al objeto del otro lado. Borrar un
proyecto suelta sus vínculos y deja los blueprints intactos (regla dura del módulo); borrar
un blueprint suelta sus pertenencias y deja los proyectos intactos.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projects',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del proyecto'),
        sa.Column('name', sa.String(length=150), nullable=False, comment="Nombre legible del proyecto (p. ej. 'Omnicanal'), único"),
        sa.Column('description', sa.Text(), nullable=True, comment='Descripción larga del proyecto'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
        comment='Proyectos: agrupación lógica de blueprints',
    )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_projects_name'), ['name'], unique=True)

    op.create_table(
        'project_database_models',
        sa.Column('project_id', sa.Integer(), nullable=False, comment='Proyecto al que pertenece el vínculo'),
        sa.Column('model_id', sa.Integer(), nullable=False, comment='Blueprint agrupado'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['model_id'], ['database_models.id'], name=op.f('fk_project_database_models_model_id_database_models'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_database_models_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'model_id', name=op.f('pk_project_database_models')),
        comment='Vínculos N:M entre proyectos y blueprints (solo agrupación)',
    )
    with op.batch_alter_table('project_database_models', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_project_database_models_model_id'), ['model_id'], unique=False)


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    NOTA (mismo patrón que export_jobs / clone_jobs / schema_comparison): NO se emiten
    ``drop_index(...)`` explícitos antes de ``drop_table(...)``. En MySQL/MariaDB soltar el
    índice que respalda una FK de la propia tabla falla con "Cannot drop index ...: needed
    in a foreign key constraint", y ``drop_table`` ya elimina índices y FKs con la tabla.
    El autogenerado los emite igual; hay que quitarlos a mano cada vez.

    Se suelta la HIJA antes que la PADRE: ``project_database_models`` referencia
    ``projects``. ``database_models`` no se toca: esta migración no la creó.
    """
    op.drop_table('project_database_models')
    op.drop_table('projects')
