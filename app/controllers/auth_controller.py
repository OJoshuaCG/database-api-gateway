"""Controller de autenticación: verifica credenciales contra la tabla users."""

from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.utils.security import verify_password


class AuthController:
    def __init__(self):
        self.user_model = UserModel()

    def authenticate(self, username: str, password: str) -> dict:
        """
        Verifica usuario+password. Devuelve {id, username} o lanza 401 genérico
        (sin distinguir usuario inexistente de password incorrecto).
        """
        user = self.user_model.find_by_username(username)
        if (
            not user
            or not user.get("is_active")
            or not verify_password(password, user["hashed_password"])
        ):
            raise AppHttpException(message="Credenciales inválidas.", status_code=401)
        return {"id": user["id"], "username": user["username"]}
