"""
Endpoint de administración del cifrado.

`POST /admin/crypto/rotate` rota la clave de datos (DEK) y re-cifra TODAS las
credenciales almacenadas, sin cambiar `SECRET_KEY` ni requerir reinicio. Solo admin.
"""

from fastapi import APIRouter

from app.core.auth import AdminDep
from app.schemas.crypto import CryptoRotationOut
from app.services import audit, crypto_rotation
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/admin/crypto", tags=["Admin"])


@router.post("/rotate", response_model=ApiResponse[CryptoRotationOut])
def rotate_encryption(admin: AdminDep):
    result = crypto_rotation.rotate_data_key()
    audit.record(
        "crypto.rotate",
        admin=admin,
        target_type="crypto_key",
        touched_engine=False,
        detail=(
            f"DEK rotada; {result['servers_reencrypted']} servidores y "
            f"{result['server_users_reencrypted']} usuarios re-cifrados"
        ),
    )
    return success(
        data=result, message="Clave de cifrado rotada; credenciales re-cifradas."
    )
