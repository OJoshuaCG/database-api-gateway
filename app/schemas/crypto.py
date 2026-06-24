"""Schemas del módulo de cifrado (rotación de clave)."""

from pydantic import BaseModel


class CryptoRotationOut(BaseModel):
    servers_reencrypted: int
    server_users_reencrypted: int
