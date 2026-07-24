"""
Guard anti "rogue MySQL server" para LOAD DATA LOCAL INFILE (hallazgo B1).

pymysql, con ``local_infile=True``, sirve al servidor el archivo cuyo path viene EN EL
PAQUETE que manda el SERVIDOR (``LoadLocalPacketWrapper.filename``), no el que el cliente
puso en su SQL. Un servidor comprometido puede así leer archivos arbitrarios del proceso
gateway. ``data_copy._guarded_read_load_local_packet`` rechaza (fail-closed) todo filename
que no coincida EXACTAMENTE con el FIFO esperado, armado en un thread-local solo durante la
ventana del LOAD DATA que el propio gateway disparó.

Puro: NO abre conexiones ni requiere un servidor MySQL real. Simula el ``first_packet``.
"""

import pytest
from pymysql.err import OperationalError

from app.services.db_admin import data_copy


class _FakePacket:
    """Imita lo que ``LoadLocalPacketWrapper`` consume del paquete crudo.

    En pymysql, ``filename == get_all_data()[1:]`` (byte 0 es el comando 0xFB). Devolvemos
    bytes para reproducir el tipo REAL (verificado en pymysql 1.1.2)."""

    def __init__(self, filename: bytes):
        self._data = b"\xfb" + filename

    def is_load_local_packet(self) -> bool:
        return True

    def get_all_data(self) -> bytes:
        return self._data


class _OriginalCalled(Exception):
    """Centinela: si el original se invoca, el guard NO cortó a tiempo (falla el test)."""


@pytest.fixture
def sentinel_original(monkeypatch):
    """Reemplaza el método original por un centinela que registra si fue llamado."""
    calls: list[tuple] = []

    def _sentinel(self, first_packet):
        calls.append((self, first_packet))
        return "ORIGINAL_RESULT"

    monkeypatch.setattr(data_copy, "_original_read_load_local_packet", _sentinel)
    # Asegura estado limpio del thread-local antes y después.
    monkeypatch.setattr(data_copy._expected_local_infile_path, "path", None, raising=False)
    return calls


def test_rejects_when_not_armed(sentinel_original):
    # Sin path esperado (expected is None) => cualquier LOAD LOCAL se rechaza fail-closed.
    packet = _FakePacket(b"/etc/passwd")
    with pytest.raises(OperationalError):
        data_copy._guarded_read_load_local_packet(object(), packet)
    assert sentinel_original == []  # el original NUNCA se llamó


def test_rejects_mismatched_filename(sentinel_original):
    # Armado con un FIFO, pero el servidor pide OTRO archivo => rechazo, sin tocar original.
    data_copy._expected_local_infile_path.path = "/dev/shm/gw_clone_expected.tsv"
    try:
        packet = _FakePacket(b"/proc/self/environ")
        with pytest.raises(OperationalError):
            data_copy._guarded_read_load_local_packet(object(), packet)
        assert sentinel_original == []
    finally:
        data_copy._expected_local_infile_path.path = None


def test_allows_exact_match(sentinel_original):
    # El filename del servidor coincide con el FIFO esperado => pasa al original.
    fifo = "/dev/shm/gw_clone_deadbeef.tsv"
    data_copy._expected_local_infile_path.path = fifo
    try:
        packet = _FakePacket(fifo.encode("utf-8"))
        result = data_copy._guarded_read_load_local_packet(object(), packet)
        assert result == "ORIGINAL_RESULT"
        assert len(sentinel_original) == 1  # el original SÍ se invocó
    finally:
        data_copy._expected_local_infile_path.path = None


def test_patch_is_installed():
    # El método de la clase debe ser nuestro wrapper y estar marcado (idempotencia).
    import pymysql.connections as pc

    assert getattr(pc.MySQLResult, "_gw_load_local_patched", False) is True
    assert pc.MySQLResult._read_load_local_packet is data_copy._guarded_read_load_local_packet
