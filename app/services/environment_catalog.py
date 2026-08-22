"""
Catálogo de ENTORNOS: vocabulario de códigos de error, filas de seed y siembra de arranque.

Dos cosas viven acá y conviene decir por qué juntas: los códigos de error (que el frontend
consume) y el seed (que define la política inicial). Las dos son el "contrato estable" del
módulo, y las dos se rompen en silencio si alguien las escribe inline en el controller.

--------------------------------------------------------------------------------
1. CÓDIGOS DE ERROR — viajan en ``public_context``, NO en ``context``
--------------------------------------------------------------------------------
``context`` se expone ÚNICAMENTE en development (``app/exceptions/HandlerExceptions.py``),
mientras ``public_context`` viaja siempre, y el frontend lee exactamente
``detail.public_context.code``. Un código puesto en ``context`` no existe en producción, y el
cliente termina matcheando la PROSA del mensaje con expresiones regulares.

Esto no es hipotético: es la deuda ``T-260822-lz-clon-contrato-frontend``, donde 17
excepciones del clon usaban ``context=``. Y el molde más cercano a esta feature
(``ProjectController``) tiene el MISMO defecto en sus seis excepciones — no es un precedente a
copiar. El precedente correcto es el vocabulario cerrado de
``app/services/db_admin/clone_spec.py`` / ``export_spec.py``.

--------------------------------------------------------------------------------
2. SEED — se siembra SOLO SI LA TABLA ESTÁ VACÍA
--------------------------------------------------------------------------------
Acá hay una diferencia DELIBERADA con ``charset_catalog``, y hay que dejarla escrita porque
el docstring de la migración de charsets dice que divergir migración↔servicio *"no hace daño
(el seed del lifespan solo AGREGA lo que falte y nunca pisa toggles)"*. **Esa frase NO vale
acá y no hay que trasladarla.** Vale para un MENÚ de charsets, que crece entre versiones. Acá
la fila **es** la política, y un seed que agrega tiene tres modos de fallo concretos:

    1. RESURRECCIÓN. El operador borra ``production`` a propósito. Reinicia. El seed la
       re-crea con la política estricta, pero NO restaura el ``environment_id`` de las BDs que
       la apuntaban. Queda una fila de política que nadie apunta.
    2. DOBLE DEFAULT. El operador mueve el default a ``staging`` y borra ``development``. El
       seed re-crea ``development`` con ``is_default=True`` ⇒ dos defaults, y como el
       invariante solo vive en el controller, nada lo repara.
    3. DOS POLÍTICAS PARA EL MISMO CÓDIGO. Si alguien afloja el ``production`` de este archivo
       y no el de la migración, un gateway provisionado por Alembic queda estricto y uno
       provisionado por ``Base.metadata.create_all`` queda permisivo. En charsets eso es una
       diferencia de menú; acá decide si el DDL destructivo llega a producción.

De ahí la regla: **si hay al menos una fila, el seed no toca nada.** Un conjunto de políticas
se configura una vez y después es del operador. Así, borrar ``production`` queda borrado y
``is_default`` no se puede duplicar.

Hace falta además de la migración porque un esquema creado con ``create_all`` (tests, dev
rápido) no pasa por Alembic.

Las filas se declaran literalmente en la migración TAMBIÉN, y ``tests/test_api_environments``
compara las dos listas: es lo único que impide la divergencia del modo de fallo 3.
"""

from app.core.database import Database
from app.core.logger import get_logger
from app.models.environment import Environment

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Vocabulario cerrado de códigos de error                                      #
# --------------------------------------------------------------------------- #
CODE_NOT_FOUND = "environment.not_found"
CODE_INACTIVE = "environment.inactive"
CODE_HAS_DATABASES = "environment.has_databases"
CODE_NAME_TAKEN = "environment.name_taken"
CODE_SLUG_TAKEN = "environment.slug_taken"
CODE_DEFAULT_MUST_BE_ACTIVE = "environment.default_must_be_active"
CODE_DEFAULT_REQUIRED = "environment.default_required"
CODE_FILTER_CONFLICT = "environment.filter_conflict"
CODE_CONFIRMATION_REQUIRED = "environment.confirmation_required"
CODE_DESTRUCTIVE_BLOCKED = "environment.destructive_blocked"
CODE_DATABASES_OUTSIDE = "environment.databases_outside_environment"

ERROR_CODES = frozenset(
    {
        CODE_NOT_FOUND,
        CODE_INACTIVE,
        CODE_HAS_DATABASES,
        CODE_NAME_TAKEN,
        CODE_SLUG_TAKEN,
        CODE_DEFAULT_MUST_BE_ACTIVE,
        CODE_DEFAULT_REQUIRED,
        CODE_FILTER_CONFLICT,
        CODE_CONFIRMATION_REQUIRED,
        CODE_DESTRUCTIVE_BLOCKED,
        CODE_DATABASES_OUTSIDE,
    }
)

# Colores admitidos: son los seis `tone` del Badge de la SPA. Cerrado a propósito (ver el
# docstring del modelo): un color libre no se puede pintar sin inyectarlo en `style`.
ENVIRONMENT_COLORS = ("neutral", "primary", "success", "error", "warning", "info")


# --------------------------------------------------------------------------- #
# Seed                                                                         #
# --------------------------------------------------------------------------- #
def environment_seed_rows() -> list[dict]:
    """
    Filas iniciales. **Tienen que coincidir exactamente** con el ``op.bulk_insert`` de la
    migración ``environments_and_managed_db_link``; hay un test que lo verifica.

    ``development`` es el default por decisión explícita del usuario. Consecuencia que hay que
    tener presente y que está documentada en el modelo: el default es el entorno MÁS
    PERMISIVO, así que una BD nueva "nace clasificada" pero no "nace protegida". La red de
    seguridad es el filtro ``only_unassigned`` del listado, no el default.
    """
    return [
        {
            "name": "Desarrollo",
            "slug": "development",
            "rank": 10,
            "color": "info",
            "is_default": True,
            "is_active": True,
            "blocks_destructive_migrations": False,
        },
        {
            "name": "Staging",
            "slug": "staging",
            "rank": 20,
            "color": "warning",
            "is_default": False,
            "is_active": True,
            "blocks_destructive_migrations": False,
        },
        {
            "name": "Producción",
            "slug": "production",
            "rank": 30,
            "color": "error",
            "is_default": False,
            "is_active": True,
            "blocks_destructive_migrations": True,
        },
    ]


def seed_environments() -> None:
    """
    Siembra los entornos iniciales **solo si la tabla está vacía**.

    NO es un top-up fila por fila, a diferencia de ``seed_charset_options``. El motivo está en
    el docstring del módulo: acá cada fila es política, y re-crear una que el operador borró a
    propósito (o duplicar el default) es un cambio de política ejecutado por un reinicio.
    """
    session = Database().get_declarative_base_session()
    try:
        if session.query(Environment.id).first() is not None:
            return  # ya configurado: es del operador, no se toca
        for row in environment_seed_rows():
            session.add(Environment(**row))
        session.commit()
        logger.info(
            "Catálogo de entornos sembrado: %d filas (tabla vacía).",
            len(environment_seed_rows()),
        )
    except Exception:  # noqa: BLE001 — el seeding no debe tumbar el arranque
        session.rollback()
        logger.exception("No se pudo sembrar el catálogo de entornos.")
    finally:
        session.close()
