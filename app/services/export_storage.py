"""
Almacenamiento efímero del artefacto de una exportación (§9.3 y §10 del diseño).

Es la PRIMERA infraestructura de artefactos del proyecto: hasta ahora la única descarga
(el ``export`` de schema-comparisons) armaba el ``.sql`` entero en memoria, ``uploads/`` es
un buzón de entrada sin TTL ni limpieza, y ninguna tabla de jobs tenía purga. Todo lo de
este módulo existe por esa razón, y cada regla resuelve una tensión concreta del §10.1
("el artefacto no se conserva" vs. "siempre asíncrona"):

- **Spool a disco, no memoria.** El writer es un generador (§8.1) y acá se lo consume
  trozo a trozo hacia un archivo. El consumo de memoria del gateway es plano e
  independiente del tamaño de la base exportada.
- **Directorio ``0700`` y archivo ``0600``.** El artefacto lleva los datos del origen EN
  CLARO —este módulo no tiene enmascarado (§9.6)—, así que es un objeto sensible en reposo.
- **Nombre opaco** (``secrets.token_urlsafe(32)``). El cliente solo maneja el id del job:
  nunca envía ni recibe una ruta. Eso es lo que neutraliza el recorrido de directorios de
  raíz, en vez de intentar sanear una ruta que llegó de afuera.
- **``sha256`` y tamaño incrementales**, calculados sobre los MISMOS bytes que se escriben
  (el texto se codifica una sola vez, acá): así el ``ETag`` de la descarga y el checksum del
  manifiesto no pueden divergir del archivo servido.
- **Espacio libre comprobado ANTES de arrancar.** Llenar el disco del gateway no degrada la
  exportación: tumba el gateway entero. La estimación del preview es gruesa, así que además
  hay un tope duro por artefacto que corta la escritura a mitad.
- **Borrado garantizado**: al descargar (un solo uso), al vencer el TTL (purga periódica) y
  al arrancar (barrido de huérfanos). Sin el tercero, un ``kill -9`` deja artefactos
  sensibles en disco para siempre — que es exactamente lo que hoy pasa con ``uploads/``.

Los archivos EN CURSO llevan sufijo ``.part`` y se renombran al nombre definitivo recién al
terminar. Es lo que permite que el barrido de huérfanos distinga "basura de un proceso
muerto" de "artefacto que se está escribiendo ahora mismo".
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    EXPORT_ARTIFACT_DIR,
    EXPORT_ARTIFACT_MAX_BYTES,
    EXPORT_ARTIFACT_TTL_MINUTES,
    EXPORT_DISK_MIN_FREE_BYTES,
)
from app.core.logger import get_logger
from app.models.export_job import (
    EXPORT_ARTIFACT_AVAILABLE,
    EXPORT_ARTIFACT_CONSUMED,
    EXPORT_ARTIFACT_PURGED,
    ExportArtifact,
)
from app.services import audit

logger = get_logger(__name__)

# Sufijo de un artefacto EN CURSO. Un archivo con este sufijo pertenece a un job vivo (o a
# un proceso que murió); nunca se sirve y nunca lo borra el barrido periódico.
PARTIAL_SUFFIX = ".part"

# MIME del artefacto ``sql``. La descarga lo usa tal cual; la entrega en línea fuerza
# ``text/plain`` porque el cliente la pega en el portapapeles.
SQL_CONTENT_TYPE = "application/sql"


class ArtifactTooLarge(Exception):
    """
    El artefacto superó ``EXPORT_ARTIFACT_MAX_BYTES`` mientras se escribía.

    No es una ``AppHttpException``: quien la ve es el worker, no una request. El archivo
    parcial se borra — es preferible un job fallido con un motivo claro a un disco lleno.
    """


class InsufficientDiskSpace(Exception):
    """No queda espacio suficiente para el artefacto estimado (§9.7)."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session():
    """Sesión corta y dedicada — nunca la del request, nunca la del motor destino."""
    return Database(
        DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT
    ).get_declarative_base_session()


# --------------------------------------------------------------------------- #
# Directorio de spool                                                          #
# --------------------------------------------------------------------------- #


def artifact_dir() -> Path:
    """
    El directorio de spool, creado con modo ``0700`` si no existía.

    El ``chmod`` posterior es deliberado y no redundante: ``mkdir(exist_ok=True)`` NO
    corrige los permisos de un directorio que ya existe (un volumen creado por una versión
    anterior, o montado con otro modo), y el modo de ``mkdir`` además pasa por el ``umask``
    del proceso. Es best-effort: en un volumen cuyo dueño es otro usuario el ``chmod`` falla
    y se registra en vez de impedir la exportación.
    """
    path = Path(EXPORT_ARTIFACT_DIR)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if (path.stat().st_mode & 0o777) != 0o700:
            path.chmod(0o700)
    except OSError:
        logger.warning(
            "No se pudo fijar el modo 0700 en el directorio de artefactos %s", path
        )
    return path


def new_storage_name() -> str:
    """Nombre OPACO del archivo. El cliente nunca lo ve: solo maneja el id del job."""
    return secrets.token_urlsafe(32)


def path_for(storage_name: str) -> Path:
    """
    Ruta absoluta de un artefacto ya registrado.

    ``storage_name`` viene SIEMPRE de la BD del gateway (lo generó ``new_storage_name``), no
    del cliente. Aun así se compara la ruta resuelta contra el directorio de spool: es una
    invariante barata que convierte un futuro bug de "guardar un nombre con ``../``" en un
    fallo ruidoso en vez de una lectura arbitraria del sistema de archivos.
    """
    directory = artifact_dir().resolve()
    candidate = (directory / storage_name).resolve()
    if candidate.parent != directory:
        raise ValueError("storage_name fuera del directorio de artefactos")
    return candidate


def ensure_capacity(estimated_bytes: int) -> None:
    """
    Comprueba que quede espacio libre suficiente ANTES de arrancar (§9.7).

    Usa la estimación GRUESA del preview: no pretende ser exacta, sino evitar el caso obvio
    de lanzar un volcado de 40 GB contra un disco con 2 GB libres. El tope duro por artefacto
    (``EXPORT_ARTIFACT_MAX_BYTES``) cubre el caso en que la estimación se quede corta.
    """
    if EXPORT_DISK_MIN_FREE_BYTES <= 0:
        return
    try:
        free = shutil.disk_usage(artifact_dir()).free
    except OSError:
        # Sin lectura del disco no se puede afirmar que haya espacio, pero tampoco que no lo
        # haya: bloquear acá dejaría el módulo inservible en un sistema de archivos exótico.
        # Se registra y decide el tope duro durante la escritura.
        logger.warning("No se pudo medir el espacio libre del directorio de artefactos")
        return
    needed = max(0, int(estimated_bytes)) + EXPORT_DISK_MIN_FREE_BYTES
    if free < needed:
        raise InsufficientDiskSpace(
            f"espacio libre {free} B; se necesitan {needed} B "
            f"(estimado {estimated_bytes} B + margen {EXPORT_DISK_MIN_FREE_BYTES} B)"
        )


# --------------------------------------------------------------------------- #
# Escritura                                                                    #
# --------------------------------------------------------------------------- #


class Spool:
    """
    El archivo en curso: recibe texto, lo codifica UNA vez y lleva tamaño y checksum.

    ``write`` es el ``sink`` que consume ``export_writer.iter_sql``. Codificar acá (y no en
    el writer) es lo que garantiza que el ``sha256``, el ``Content-Length`` y los bytes
    servidos describan exactamente el mismo contenido.
    """

    def __init__(self, path: Path, storage_name: str, *, encoding: str, max_bytes: int):
        self.path = path
        self.storage_name = storage_name
        self.encoding = encoding
        self.max_bytes = max_bytes
        self.size = 0
        self._digest = hashlib.sha256()
        # ``O_EXCL``: el nombre es un token aleatorio, así que una colisión sería un fallo de
        # entropía y hay que verlo, no sobrescribir en silencio. El modo 0600 va en el
        # ``open`` y no en un ``chmod`` posterior para no dejar ni una ventana con el archivo
        # legible por otros.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._fh = os.fdopen(fd, "wb")

    def write(self, chunk: str) -> None:
        """Texto, codificado con la codificación del artefacto."""
        if not chunk:
            return
        self.write_bytes(chunk.encode(self.encoding, errors="strict"))

    def write_bytes(self, data: bytes) -> None:
        """
        Bytes ya formados. Es la entrada del empaquetador (§10.3).

        Un artefacto comprimido no tiene "texto": lo que va al disco es la salida de gzip o
        de zip, y es sobre ESO —los bytes servidos— que tienen que calcularse el tamaño y el
        ``sha256``. Codificar acá y medir en otro lado es exactamente cómo el ``ETag`` deja
        de describir el archivo entregado.
        """
        if not data:
            return
        if self.max_bytes > 0 and self.size + len(data) > self.max_bytes:
            raise ArtifactTooLarge(
                f"el artefacto superó el tope de {self.max_bytes} bytes"
            )
        self._fh.write(data)
        self._digest.update(data)
        self.size += len(data)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            # ``fsync`` a propósito: el artefacto se entrega desde disco y el job va a
            # afirmar en la BD que existe con ese checksum. Que el proceso muera con los
            # bytes solo en la caché del sistema operativo dejaría una fila que promete un
            # archivo incompleto.
            os.fsync(self._fh.fileno())
            self._fh.close()

    def discard(self) -> None:
        """Cierra y BORRA el archivo (cancelación, fallo duro, tope superado)."""
        self.close()
        self.path.unlink(missing_ok=True)


@contextmanager
def spool(
    *, encoding: str = "utf-8", max_bytes: int = EXPORT_ARTIFACT_MAX_BYTES
) -> Iterator[Spool]:
    """
    Abre el archivo en curso (``<token>.part``) y garantiza su cierre.

    Ante cualquier excepción el archivo se BORRA: un artefacto a medio escribir que nadie
    registró es exactamente el residuo sensible que este módulo existe para evitar. El
    llamador que termina bien lo materializa con ``finalize``.
    """
    name = new_storage_name()
    part = artifact_dir() / f"{name}{PARTIAL_SUFFIX}"
    handle = Spool(part, name, encoding=encoding, max_bytes=max_bytes)
    try:
        yield handle
    except BaseException:
        handle.discard()
        raise
    else:
        handle.close()


def finalize(
    job_id: int,
    handle: Spool,
    *,
    content_type: str = SQL_CONTENT_TYPE,
    ttl_minutes: int = EXPORT_ARTIFACT_TTL_MINUTES,
    part_count: int = 1,
) -> dict:
    """
    Renombra el ``.part`` al nombre definitivo y registra la fila ``export_artifacts``.

    El orden importa: **primero el rename, después la fila**. Si el proceso muere en medio,
    queda un archivo sin fila —que el barrido de huérfanos borra al arrancar— en vez de una
    fila que promete un archivo inexistente y hace fallar la descarga con un 500.
    """
    handle.close()
    final = artifact_dir() / handle.storage_name
    os.replace(handle.path, final)

    expires_at = _utcnow() + timedelta(minutes=max(1, ttl_minutes))
    session = _session()
    try:
        # Un job re-ejecutado (o un reintento manual) no puede dejar dos filas: la columna es
        # UNIQUE, así que la anterior se borra junto con su archivo.
        previous = (
            session.query(ExportArtifact)
            .filter(ExportArtifact.job_id == job_id)
            .one_or_none()
        )
        if previous is not None:
            _unlink_quietly(previous.storage_name)
            session.delete(previous)
            session.flush()
        row = ExportArtifact(
            job_id=job_id,
            storage_name=handle.storage_name,
            byte_size=handle.size,
            sha256=handle.sha256,
            content_type=content_type,
            part_count=part_count,
            state=EXPORT_ARTIFACT_AVAILABLE,
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        return {
            "storage_name": row.storage_name,
            "byte_size": row.byte_size,
            "sha256": row.sha256,
            "content_type": row.content_type,
            "part_count": row.part_count,
            "state": row.state,
            "expires_at": row.expires_at,
        }
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Lectura y ciclo de vida                                                      #
# --------------------------------------------------------------------------- #


def describe(job_id: int) -> dict | None:
    """Metadatos del artefacto de un job (``None`` si nunca existió)."""
    session = _session()
    try:
        row = (
            session.query(ExportArtifact)
            .filter(ExportArtifact.job_id == job_id)
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "storage_name": row.storage_name,
            "byte_size": row.byte_size,
            "sha256": row.sha256,
            "content_type": row.content_type,
            "part_count": row.part_count,
            "state": row.state,
            "expires_at": row.expires_at,
            "downloaded_at": row.downloaded_at,
            "download_count": row.download_count,
            "expired": row.expires_at < _utcnow(),
        }
    finally:
        session.close()


def mark_downloaded(job_id: int) -> None:
    """Registra la entrega (contador + marca temporal), sin cambiar el estado."""
    session = _session()
    try:
        row = (
            session.query(ExportArtifact)
            .filter(ExportArtifact.job_id == job_id)
            .one_or_none()
        )
        if row is None:
            return
        row.downloaded_at = _utcnow()
        row.download_count = (row.download_count or 0) + 1
        session.commit()
    finally:
        session.close()


def consume(job_id: int) -> None:
    """
    Descarga de UN SOLO USO: borra el archivo y deja el artefacto en ``consumed``.

    Se invoca DESPUÉS de que la respuesta terminó de enviarse (tarea de fondo de Starlette).
    Borrar antes dejaría al cliente con una descarga cortada a mitad.
    """
    session = _session()
    try:
        row = (
            session.query(ExportArtifact)
            .filter(ExportArtifact.job_id == job_id)
            .one_or_none()
        )
        if row is None:
            return
        _unlink_quietly(row.storage_name)
        row.state = EXPORT_ARTIFACT_CONSUMED
        session.commit()
    finally:
        session.close()


def purge_expired() -> int:
    """
    Borra los artefactos vencidos y deja la fila en ``purged``. **I/O SÍNCRONO.**

    La fila se conserva (no se borra) a propósito: es el rastro de que ese job PRODUJO un
    artefacto y de que se retiró por retención. Borrarla haría desaparecer esa evidencia
    junto con el archivo, que es justo lo contrario de lo que pide la auditoría del §9.4.

    Nunca lanza: la invoca el ``lifespan`` (arranque y tarea periódica) y un fallo de una
    pasada no puede tumbar el proceso.
    """
    try:
        session = _session()
        try:
            rows = (
                session.query(ExportArtifact)
                .filter(
                    ExportArtifact.state == EXPORT_ARTIFACT_AVAILABLE,
                    ExportArtifact.expires_at < _utcnow(),
                )
                .all()
            )
            for row in rows:
                _unlink_quietly(row.storage_name)
                row.state = EXPORT_ARTIFACT_PURGED
            session.commit()
            purged = len(rows)
        finally:
            session.close()
        if purged:
            logger.info("Purga por TTL: %d artefacto(s) de exportación eliminados.", purged)
            audit.record(
                "database_export.purge",
                touched_engine=False,
                detail=f"{purged} artefacto(s) de exportación purgados por TTL",
            )
        return purged
    except Exception:
        logger.warning("No se pudo purgar los artefactos vencidos", exc_info=True)
        return 0


def sweep_orphans(*, include_partials: bool = True) -> int:
    """
    Borra los archivos del directorio que no tienen una fila viva detrás.

    Sin esto, un ``kill -9`` deja artefactos sensibles en disco para siempre. Solo se llama
    **al arrancar**, y por eso puede borrar también los ``.part``: en ese momento no hay
    ningún job de este proceso escribiendo. La tarea periódica NO lo llama —borraría el
    archivo en curso de una exportación viva—; ella solo purga por TTL.
    """
    try:
        directory = artifact_dir()
        session = _session()
        try:
            known = {
                name
                for (name,) in session.query(ExportArtifact.storage_name)
                .filter(ExportArtifact.state == EXPORT_ARTIFACT_AVAILABLE)
                .all()
            }
        finally:
            session.close()

        removed = 0
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.name.endswith(PARTIAL_SUFFIX):
                if not include_partials:
                    continue
            elif entry.name in known:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                logger.warning("No se pudo borrar el artefacto huérfano %s", entry.name)
        if removed:
            logger.info(
                "Barrido de arranque: %d archivo(s) de exportación huérfanos eliminados.",
                removed,
            )
        return removed
    except Exception:
        logger.warning("No se pudo barrer los artefactos huérfanos", exc_info=True)
        return 0


def _unlink_quietly(storage_name: str) -> None:
    try:
        path_for(storage_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.warning("No se pudo borrar el archivo de artefacto %s", storage_name)


__all__ = [
    "PARTIAL_SUFFIX",
    "SQL_CONTENT_TYPE",
    "ArtifactTooLarge",
    "InsufficientDiskSpace",
    "Spool",
    "artifact_dir",
    "consume",
    "describe",
    "ensure_capacity",
    "finalize",
    "mark_downloaded",
    "new_storage_name",
    "path_for",
    "purge_expired",
    "spool",
    "sweep_orphans",
]
