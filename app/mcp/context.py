"""
``ToolContext`` — la ÚNICA puerta del handler al plano gestionado.

ES LA CAPA QUE DE VERDAD CIERRA LA PUERTA, Y POR ESO ES LA PRIMERA
------------------------------------------------------------------
Un handler recibe esto y nada más: sin ``Server``, sin ``ServerTarget``, sin ``get_adapter``. No
porque sea prolijo, sino porque **no entrega nada reusable**: no hay ningún objeto que un tool
pueda guardar, replicar o apuntar a otra base.

Es la diferencia con confiar en un tipo "recibo del gate": un ``@dataclass(frozen=True)`` tiene
``__init__`` público y ``dataclasses.replace`` devuelve un objeto válido con el gate ya pasado. Un
contexto que no expone la credencial no tiene ese problema, porque no hay nada que reescribir.

Las tools que leen el catálogo del motor van a recibir acá un ``open_readonly(database_id)`` que
resuelve, gatea y devuelve **la sesión ya abierta** — nunca la credencial. Todavía no existe: llega
con el façade de solo lectura.
"""

from dataclasses import dataclass

from app.core.actor import Actor


@dataclass(frozen=True, slots=True)
class ToolContext:
    """
    Lo que un handler puede ver. ``actor`` es de solo lectura y frozen.

    No lleva la ``Request``: un handler no tiene por qué poder leer headers, cookies ni el
    cuerpo crudo. Lo que necesite del transporte se lo pasa el despachador ya normalizado.
    """

    actor: Actor

    @property
    def project_id(self) -> int:
        """El proyecto del token. Es el alcance de TODO lo que el agente puede ver."""
        return self.actor.project_id or 0

    def reachable_databases(self):
        """
        Las bases que este agente alcanza. Delega en el resolvedor único.

        El handler no filtra por proyecto ni por política: si lo hiciera, habría dos lugares
        donde está escrito el gate y uno de los dos se relajaría sin que nadie lo note.
        """
        from app.controllers.target_resolution import reachable_databases

        return reachable_databases(self.actor)
