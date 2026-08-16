"""
Empaquetado del artefacto: organización, fragmentación y compresión (§10.3 del diseño).

Es la pieza que hay entre el writer —que produce TEXTO etiquetado con el archivo lógico al
que pertenece— y el spool, que solo sabe de bytes, tamaño y checksum. Traduce lo primero en
lo segundo aplicando tres decisiones del spec:

- **organización**: un flujo único, o un archivo por objeto (lo que permite versionar el
  esquema en un repositorio, §15.3);
- **fragmentación**: partir un archivo cada ``split_max_bytes`` con la convención
  ``{base}.part{NN}.{ext}`` y el ORDEN DE EJECUCIÓN documentado en el propio nombre;
- **compresión**: ``gzip`` para un flujo, ``zip`` como contenedor (el único apto para
  multiarchivo).

**Todo es streaming, sin excepción** (§8.1). El zip se escribe con ``zipfile`` sobre un
destino NO buscable: Python lo soporta desde 3.7 y en ese modo cada entrada lleva su
descriptor de datos al final, así que nunca hace falta volver atrás a corregir cabeceras —y
por lo tanto nunca hace falta tener el archivo entero en memoria ni en un temporal. Lo único
que se retiene son los nombres de las entradas y, por entrada, su trozo de encabezado para
poder repetirlo en cada fragmento.

Dos detalles que existen por el **determinismo del §8.3** y no por gusto: el gzip se escribe
con ``mtime=0`` y las entradas del zip con una fecha fija. Con la marca de tiempo real, dos
exportaciones idénticas producirían archivos distintos byte a byte y la comparación de dos
volcados —que es media razón de ser del formato ``per_object``— dejaría de funcionar.
"""

from __future__ import annotations

import gzip
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.environments import EXPORT_MAX_PARTS
from app.services.db_admin import export_spec as espec
from app.services.db_admin.export_writer import Chunk

# Extensión del archivo LÓGICO por formato (la de cada entrada del contenedor).
_ENTRY_EXTENSION: dict[str, str] = {
    espec.Format.sql.value: ".sql",
    espec.Format.csv.value: ".csv",
    espec.Format.json.value: ".json",
    espec.Format.ndjson.value: ".ndjson",
}

# Tipo MIME del artefacto ENTREGADO (no el de sus entradas).
_CONTENT_TYPE: dict[str, str] = {
    espec.Format.sql.value: "application/sql",
    espec.Format.csv.value: "text/csv",
    espec.Format.json.value: "application/json",
    espec.Format.ndjson.value: "application/x-ndjson",
}
ZIP_CONTENT_TYPE = "application/zip"
GZIP_CONTENT_TYPE = "application/gzip"

# Fecha fija de las entradas del zip (el mínimo que admite el formato). Ver el determinismo
# del §8.3 en el docstring del módulo.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Nivel de compresión. 6 y no 9: en un volcado de gigabytes el 9 multiplica el tiempo de CPU
# para arañar un porcentaje, y esta compresión corre dentro del worker que sostiene una
# transacción de lectura contra la base de un tercero — cada segundo de más ahí es undo que
# se acumula en el origen.
_COMPRESS_LEVEL = 6

# Nombre del índice que se agrega al contenedor. Empieza con ``000-`` para que quede primero
# al listar y al descomprimir, aunque físicamente se escriba al final (que es cuando se
# conocen todas las entradas).
_INDEX_ENTRY = "000-INDICE.txt"
# Aviso de artefacto parcial dentro del contenedor (§14).
_INCOMPLETE_ENTRY = "000-EXPORTACION-INCOMPLETA.txt"


class PackagingError(Exception):
    """
    Fallo de empaquetado. No es una ``AppHttpException``: quien la ve es el worker.

    Se usa para las incoherencias entre el writer y el empaquetador (un artefacto de varios
    archivos que se pidió sin contenedor) y para el tope de fragmentos. Fail-closed: es
    preferible un job fallido con un motivo claro a un artefacto que el cliente no puede
    abrir.
    """


def entry_extension(spec: espec.ExportSpec) -> str:
    """Extensión de cada archivo lógico (``.sql``, ``.csv``, …)."""
    return _ENTRY_EXTENSION.get(str(spec.format), ".txt")


def artifact_extension(spec: espec.ExportSpec) -> str:
    """
    Extensión del archivo que descarga el usuario, ya con la compresión aplicada.

    Sale de ``effective_compression`` y no de ``spec.output.compression`` porque un artefacto
    multiarchivo se eleva a zip aunque no se haya pedido comprimir: el nombre tiene que
    describir lo que realmente se entrega.
    """
    compression = espec.effective_compression(spec)
    if compression == espec.Compression.zip:
        return ".zip"
    if compression == espec.Compression.gzip:
        return f"{entry_extension(spec)}.gz"
    return entry_extension(spec)


def content_type(spec: espec.ExportSpec) -> str:
    """Tipo MIME del artefacto entregado."""
    compression = espec.effective_compression(spec)
    if compression == espec.Compression.zip:
        return ZIP_CONTENT_TYPE
    if compression == espec.Compression.gzip:
        return GZIP_CONTENT_TYPE
    return _CONTENT_TYPE.get(str(spec.format), "text/plain")


class _ByteSink:
    """
    Adaptador de solo escritura sobre el spool, para ``gzip`` y ``zipfile``.

    **No expone ``tell`` ni ``seek`` a propósito.** ``zipfile`` prueba ``tell()``, recibe un
    ``AttributeError`` y pasa a su modo de flujo no buscable, que es exactamente el que hace
    falta: escribe descriptores de datos al final de cada entrada en vez de volver atrás a
    parchear la cabecera. Darle un ``tell()`` lo haría creer que puede buscar, y el primer
    ``seek`` reventaría a mitad del artefacto.
    """

    def __init__(self, write: Callable[[bytes], None]):
        self._write = write

    def write(self, data: bytes) -> int:
        self._write(data)
        return len(data)

    def flush(self) -> None:  # zipfile/gzip lo llaman al cerrar
        return None


class ArtifactPackager:
    """
    Recibe ``Chunk`` del writer y escribe el artefacto final en el destino de bytes.

    Un ``Chunk`` con ``entry=None`` pertenece al flujo único; con nombre, a ese archivo
    lógico. Los trozos marcados ``prologue`` (el encabezado de un CSV, la marca de orden de
    bytes) se **repiten al principio de cada fragmento**: sin eso, el ``part02`` de un CSV es
    un archivo que ningún importador lee igual que el ``part01``.
    """

    def __init__(
        self,
        spec: espec.ExportSpec,
        sink: Callable[[bytes], None],
        *,
        base_name: str,
        max_parts: int = EXPORT_MAX_PARTS,
    ):
        self.spec = spec
        self.encoding = spec.output.file_encoding
        self.compression = espec.effective_compression(spec)
        self.split_max_bytes = int(spec.output.split_max_bytes or 0)
        self.extension = entry_extension(spec)
        self.base_name = base_name or "export"
        self.max_parts = max(1, int(max_parts))
        self.entries: list[str] = []

        self._sink = _ByteSink(sink)
        self._raw_write: Callable[[bytes], None] = sink
        self._gzip: gzip.GzipFile | None = None
        self._zip: zipfile.ZipFile | None = None
        self._open_entry = None  # handle de la entrada del zip en curso
        self._entry_key: str | None = None  # archivo lógico en curso
        self._entry_prologue: str = ""
        self._entry_bytes = 0
        # Bytes de DATOS del fragmento en curso (sin el encabezado repetido). Es lo que
        # decide si hay algo que cortar: un fragmento con solo el encabezado no es un
        # fragmento, es un archivo vacío con cabecera.
        self._entry_payload = 0
        self._entry_part = 1
        self._stream_started = False

        if self.compression == espec.Compression.zip:
            self._zip = zipfile.ZipFile(
                self._sink, mode="w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=_COMPRESS_LEVEL,
            )
        elif self.compression == espec.Compression.gzip:
            # ``mtime=0`` y ``filename=''``: sin ellos el gzip lleva la hora y el nombre
            # dentro de la cabecera y dos exportaciones idénticas dejarían de ser iguales
            # byte a byte (§8.3).
            self._gzip = gzip.GzipFile(
                filename="", mode="wb", fileobj=self._sink, compresslevel=_COMPRESS_LEVEL,
                mtime=0,
            )

    # ------------------------------------------------------------------ #
    # Escritura                                                           #
    # ------------------------------------------------------------------ #
    def write(self, chunk: Chunk) -> None:
        if not chunk.text:
            return
        if self._zip is None:
            self._write_stream(chunk)
            return
        self._write_zip(chunk)

    def _write_stream(self, chunk: Chunk) -> None:
        """Flujo único (sin contenedor): gzip o texto plano."""
        key = chunk.entry or ""
        if not self._stream_started:
            self._entry_key = key
            self._stream_started = True
        elif key != self._entry_key:
            # El writer produjo varios archivos lógicos para un artefacto que no es un
            # contenedor. Es una incoherencia entre el spec y el generador, no un caso de
            # uso: concatenarlos produciría un archivo sin sentido (dos CSV pegados).
            raise PackagingError(
                "el artefacto tiene más de un archivo lógico pero no se resolvió un "
                "contenedor"
            )
        self._emit(chunk.text)

    def _write_zip(self, chunk: Chunk) -> None:
        key = chunk.entry or self.base_name
        if key != self._entry_key:
            self._close_entry()
            self._entry_key = key
            self._entry_prologue = ""
            self._entry_part = 1
            self._open_part()
        data = chunk.text.encode(self.encoding, errors="strict")
        if chunk.prologue:
            # Se recuerda para repetirlo en cada fragmento posterior.
            self._entry_prologue += chunk.text
        elif (
            self.split_max_bytes
            and self._entry_payload
            and self._entry_bytes + len(data) > self.split_max_bytes
        ):
            # Se corta ANTES de pasarse, no después. El corte solo puede caer entre trozos
            # —el writer nunca parte una fila ni una sentencia por la mitad—, así que el tope
            # se respeta salvo en un caso irreducible: cuando un solo registro (más el
            # encabezado repetido) ya lo supera. Ahí el fragmento se pasa, porque la
            # alternativa sería emitir media fila y romper el archivo.
            self._entry_part += 1
            self._open_part()
        self._emit_bytes(data)
        if not chunk.prologue:
            self._entry_payload += len(data)

    def _emit(self, text: str) -> None:
        self._emit_bytes(text.encode(self.encoding, errors="strict"))

    def _emit_bytes(self, data: bytes) -> None:
        self._entry_bytes += len(data)
        if self._open_entry is not None:
            self._open_entry.write(data)
        elif self._gzip is not None:
            self._gzip.write(data)
        else:
            self._raw_write(data)

    # ------------------------------------------------------------------ #
    # Entradas del contenedor                                             #
    # ------------------------------------------------------------------ #
    def _part_name(self) -> str:
        """
        ``{base}.{ext}`` o ``{base}.part{NN}.{ext}`` cuando hay fragmentación.

        ``NN`` va con dos dígitos como mínimo y crece de forma natural; el relleno con ceros
        es lo que hace que el orden alfabético del listado coincida con el de ejecución hasta
        99 fragmentos, y por encima de eso el índice del contenedor sigue siendo la
        referencia. El tope duro de ``EXPORT_MAX_PARTS`` está para que un
        ``split_max_bytes`` ridículo no genere miles de entradas.
        """
        base = self._entry_key or self.base_name
        if not self.split_max_bytes:
            return f"{base}{self.extension}"
        return f"{base}.part{self._entry_part:02d}{self.extension}"

    def _open_part(self) -> None:
        self._close_part()
        if len(self.entries) >= self.max_parts:
            raise PackagingError(
                f"el artefacto superó el máximo de {self.max_parts} archivos; subí "
                "output.split_max_bytes o reducí la selección"
            )
        name = self._part_name()
        info = zipfile.ZipInfo(filename=name, date_time=_ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        # ``force_zip64``: en un flujo no buscable el tamaño se desconoce al abrir la
        # entrada, y sin zip64 una tabla de más de 4 GiB rompería el contenedor recién al
        # final. El coste es unos bytes por entrada y compatibilidad con lectores de este
        # siglo.
        self._open_entry = self._zip.open(info, mode="w", force_zip64=True)
        self.entries.append(name)
        self._entry_bytes = 0
        self._entry_payload = 0
        if self._entry_prologue:
            self._emit(self._entry_prologue)

    def _close_part(self) -> None:
        if self._open_entry is not None:
            self._open_entry.close()
            self._open_entry = None

    def _close_entry(self) -> None:
        self._close_part()
        self._entry_bytes = 0
        self._entry_payload = 0

    # ------------------------------------------------------------------ #
    # Cierre                                                              #
    # ------------------------------------------------------------------ #
    def finish(self, *, complete: bool, job_id: int | None = None) -> None:
        """
        Cierra el artefacto dejando, si hace falta, la marca de INCOMPLETO (§14).

        La marca depende del formato porque no todos admiten texto suelto al final: un
        comentario SQL pegado a un CSV o a un JSON los corrompe. Por eso:

        - en un contenedor, una entrada propia (visible en el listado, sin tocar los datos);
        - en un script ``sql``, el comentario al final, que es la forma histórica;
        - en ``ndjson``, una línea ``{"incomplete":true}`` que el writer ya emitió;
        - en ``json`` va la clave ``complete`` dentro del documento, también del writer.

        Nunca se entrega un artefacto parcial sin marca: además de esto están la cabecera
        ``X-Export-Complete``, el estado del job y el manifiesto.
        """
        banner = (
            "-- EXPORTACIÓN INCOMPLETA — ver el reporte de incidencias del job"
            + (f" {job_id}" if job_id is not None else "")
            + "\n"
        )
        if self._zip is not None:
            self._close_entry()
            if not complete:
                self._write_container_entry(_INCOMPLETE_ENTRY, banner)
            self._write_container_entry(_INDEX_ENTRY, self._index_text())
        elif not complete and self.spec.format == espec.Format.sql:
            self._emit(banner)
        self.close()

    def _index_text(self) -> str:
        """Inventario del contenedor; en ``sql``, además, el orden de ejecución."""
        if self.spec.format == espec.Format.sql:
            head = (
                "Orden de EJECUCIÓN de los archivos de este artefacto.\n"
                "Ejecutalos de arriba abajo: el prefijo numérico ya es ese orden.\n"
                "Un fragmento .partNN continúa el archivo anterior; no lo saltees.\n\n"
            )
        else:
            head = (
                "Contenido de este artefacto, en el orden en que se generó.\n"
                "Son datos, no un script: no hay nada que ejecutar.\n\n"
            )
        return head + "".join(f"{i:>4}. {name}\n" for i, name in enumerate(self.entries, 1))

    def _write_container_entry(self, name: str, text: str) -> None:
        info = zipfile.ZipInfo(filename=name, date_time=_ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        with self._zip.open(info, mode="w", force_zip64=True) as handle:
            handle.write(text.encode(self.encoding, errors="strict"))
        self.entries.append(name)

    def close(self) -> None:
        self._close_part()
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._gzip is not None:
            self._gzip.close()
            self._gzip = None

    @property
    def part_count(self) -> int:
        """Cuántos archivos hay DENTRO del artefacto (1 si no es un contenedor)."""
        return len(self.entries) or 1


@contextmanager
def packager(
    spec: espec.ExportSpec, sink: Callable[[bytes], None], *, base_name: str
) -> Iterator[ArtifactPackager]:
    """
    Abre el empaquetador y garantiza su cierre.

    El cierre es obligatorio incluso ante una excepción: un zip sin su directorio central o
    un gzip sin su bloque final no son archivos truncados, son archivos **ilegibles**. El
    spool descarta el ``.part`` en ese caso, pero cerrar igual evita depender de ese orden.
    """
    handle = ArtifactPackager(spec, sink, base_name=base_name)
    try:
        yield handle
    finally:
        handle.close()


__all__ = [
    "GZIP_CONTENT_TYPE",
    "ZIP_CONTENT_TYPE",
    "ArtifactPackager",
    "PackagingError",
    "artifact_extension",
    "content_type",
    "entry_extension",
    "packager",
]
