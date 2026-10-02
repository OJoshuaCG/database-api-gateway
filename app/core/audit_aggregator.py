"""
Agregador EN PROCESO de filas de auditoría que un tercero puede disparar a voluntad.

Hay eventos que alguien sin privilegio puede repetir gratis: un bearer basura contra el MCP, un
403 por capacidad, un POST cross-site rechazado por CSRF. "Una fila por evento" convierte cada
uno en una escritura gratis sobre ``audit_log`` —el DoS sobre la BD de metadatos más el rastro
real enterrado bajo ruido—, y "ninguna fila" deja el sondeo invisible. Este agregador es el
punto medio: **como mucho una fila por clave por ventana**, y esa fila declara cuántos eventos de
la misma clave quedaron sin fila desde la anterior.

Lo usan los rechazos del MCP (clave = IP) y las denegaciones de acceso (clave = actor + código +
ruta). Vive aparte para que las dos políticas no diverjan en la semántica de la cuenta.

LÍMITES, DECLARADOS:

- **Es por proceso.** Con N workers son hasta N filas por clave por ventana. Sigue acotado —la
  cota pasa de ∞ a N—, y llevarlo a Redis sería sumar un round-trip a cada evento para ganar un
  factor constante.
- **La cuenta de la última ventana se escribe recién con el próximo evento de esa clave.** Si el
  sondeo termina, esa cola no llega a ``audit_log``: lo que sí quedó es la fila que abrió la
  ventana, que es lo que dice que hubo un sondeo, de quién y contra qué.
- Si se supera ``max_keys`` se olvida la clave más vieja con su cuenta pendiente. Se prefiere
  perder un contador a dejar crecer la memoria.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from time import monotonic


class WindowedAggregator:
    """``admit(clave)`` decide si un evento escribe fila o solo suma al contador de su clave."""

    def __init__(
        self,
        *,
        window: float,
        max_keys: int,
        clock: Callable[[], float] = monotonic,
    ):
        self._window = window
        self._max_keys = max_keys
        # Inyectable para que los tests avancen el reloj sin dormir.
        self._clock = clock
        self._lock = threading.Lock()
        # clave -> [inicio de la ventana, eventos sin fila desde la última]
        self._keys: OrderedDict[Hashable, list[float | int]] = OrderedDict()

    @property
    def window(self) -> float:
        return self._window

    def admit(self, key: Hashable) -> int | None:
        """
        Cuenta un evento de ``key``. Devuelve ``None`` si NO corresponde escribir fila, o la
        cantidad de eventos previos que esa fila tiene que declarar como agregados.
        """
        ahora = self._clock()
        with self._lock:
            entrada = self._keys.get(key)
            if entrada is not None and ahora - entrada[0] < self._window:
                entrada[1] += 1
                return None
            pendientes = int(entrada[1]) if entrada is not None else 0
            self._keys[key] = [ahora, 0]
            self._keys.move_to_end(key)
            while len(self._keys) > self._max_keys:
                self._keys.popitem(last=False)
            return pendientes

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)

    def reset(self) -> None:
        with self._lock:
            self._keys.clear()
