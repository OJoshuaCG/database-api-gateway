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

Las tools que leen el catálogo del motor usan ``open_readonly(database_id)``, que resuelve, gatea
y rinde **el façade ya abierto** sobre la credencial de solo lectura — nunca la credencial.

LA CAPACIDAD LA FIJA EL DISPATCHER, NO LA TOOL
----------------------------------------------
``capability`` es el ``ToolSpec.scope`` de la tool que se está ejecutando. Las dos puertas de
abajo la usan para el eje 2 del gate, así que un handler no puede elegir una capacidad más débil
que la que declaró: no hay parámetro para hacerlo.
"""

from dataclasses import dataclass

from app.core.actor import Actor
from app.services.capability_catalog import Capability


@dataclass(frozen=True, slots=True)
class ToolContext:
    """
    Lo que un handler puede ver. ``actor`` es de solo lectura y frozen.

    No lleva la ``Request``: un handler no tiene por qué poder leer headers, cookies ni el
    cuerpo crudo. Lo que necesite del transporte se lo pasa el despachador ya normalizado.
    """

    actor: Actor
    capability: Capability

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

        return reachable_databases(self.actor, self.capability)

    def open_readonly(self, database_id: int):
        """
        Context manager: gate completo de UNA base, credencial de solo lectura y sesión de
        lectura. Rinde ``(base_resuelta, facade)`` y cierra la sesión al salir.

        El façade solo tiene métodos de lectura (composición, no herencia): ver
        ``app/services/db_admin/readonly_introspector.py``.
        """
        from app.controllers.target_resolution import open_readonly

        return open_readonly(self.actor, database_id, self.capability)
