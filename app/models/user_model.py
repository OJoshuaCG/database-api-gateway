"""
User Model - Interacción con Base de Datos

Este modelo maneja la interacción con la tabla 'users' usando SQL directo.
Es llamado desde el UserController siguiendo el patrón MVC.

Patrón: Routes → Controllers → Models → Database
"""

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER


class UserModel:
    """Model para operaciones CRUD de usuarios"""

    def __init__(self):
        """Inicializar conexión a base de datos"""
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def find_by_id(self, user_id: int) -> dict | None:
        """
        Buscar usuario por ID

        Args:
            user_id: ID del usuario

        Returns:
            dict | None: Datos del usuario o None si no existe
        """
        return self.db.execute_query(
            "SELECT * FROM users WHERE id = :id", {"id": user_id}, fetchone=True
        )

    def find_by_username(self, username: str) -> dict | None:
        """
        Buscar usuario por username

        Args:
            username: Username del usuario

        Returns:
            dict | None: Datos del usuario o None si no existe
        """
        return self.db.execute_query(
            "SELECT * FROM users WHERE username = :username",
            {"username": username},
            fetchone=True,
        )

    def find_by_email(self, email: str) -> dict | None:
        """
        Buscar usuario por email

        Args:
            email: Email del usuario

        Returns:
            dict | None: Datos del usuario o None si no existe
        """
        return self.db.execute_query(
            "SELECT * FROM users WHERE email = :email", {"email": email}, fetchone=True
        )

    def find_all(self, is_active: bool | None = None) -> list[dict]:
        """
        Listar todos los usuarios con filtros opcionales

        Args:
            is_active: Filtrar por estado activo (opcional)

        Returns:
            list[dict]: Lista de usuarios
        """
        if is_active is None:
            query = "SELECT * FROM users ORDER BY created_at DESC"
            params = {}
        else:
            query = "SELECT * FROM users WHERE is_active = :is_active ORDER BY created_at DESC"
            params = {"is_active": is_active}

        return self.db.execute_query(query, params, fetchone=False)

    def create(self, user_data: dict) -> int:
        """
        Crear nuevo usuario

        Args:
            user_data: Diccionario con datos del usuario
                - username (str): Username único
                - email (str): Email único
                - hashed_password (str): Password hasheado
                - full_name (str, optional): Nombre completo
                - notes (str, optional): Notas adicionales
                - is_active (bool, optional): Estado activo (default: True)
                - gateway_role (str, optional): Rol base en el gateway (default: "viewer")

        Returns:
            int: ID del usuario creado
        """
        query = """
            INSERT INTO users (
                username,
                email,
                hashed_password,
                full_name,
                notes,
                is_active,
                gateway_role
            ) VALUES (
                :username,
                :email,
                :hashed_password,
                :full_name,
                :notes,
                COALESCE(:is_active, 1),
                COALESCE(:gateway_role, 'viewer')
            )
        """

        # Retorna el ID del usuario creado
        return self.db.execute_query(query, user_data)

    def update(self, user_id: int, user_data: dict) -> int:
        """
        Actualizar usuario existente

        Args:
            user_id: ID del usuario
            user_data: Diccionario con datos a actualizar

        Returns:
            int: Número de filas afectadas
        """
        # Construir SET clause dinámicamente
        set_clause = ", ".join([f"{key} = :{key}" for key in user_data.keys()])

        query = f"UPDATE users SET {set_clause} WHERE id = :id"

        # Agregar user_id a params
        params = {**user_data, "id": user_id}

        # Retorna número de filas afectadas
        return self.db.execute_query(query, params)

    def delete(self, user_id: int) -> int:
        """
        Eliminar usuario permanentemente (hard delete)

        Args:
            user_id: ID del usuario

        Returns:
            int: Número de filas eliminadas
        """
        query = "DELETE FROM users WHERE id = :id"

        # Retorna número de filas eliminadas
        return self.db.execute_query(query, {"id": user_id})


    # ------------------------------------------------------------------ #
    # Acceso al gateway (plano de CONTROL)                                #
    # ------------------------------------------------------------------ #
    def grant_global_capabilities(self, username: str, capabilities: list[str]) -> None:
        """
        Otorga capacidades globales a un usuario, por username. Idempotente.

        Idempotente porque la tabla tiene PK compuesta ``(user_id, capability)``: un doble
        otorgamiento es imposible incluso ante un bug del llamador, así que se puede reintentar
        sin comprobar antes. Se usa un ``SELECT`` en el ``INSERT`` para no necesitar el id en el
        llamador — ``bootstrap_admin`` acaba de crear la fila y no lo tiene a mano.
        """
        for capability in capabilities:
            self.db.execute_query(
                """
                INSERT INTO user_global_capabilities (user_id, capability, created_at, updated_at)
                SELECT u.id, :capability, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM users u
                WHERE u.username = :username
                  AND NOT EXISTS (
                      SELECT 1 FROM user_global_capabilities g
                      WHERE g.user_id = u.id AND g.capability = :capability
                  )
                """,
                {"username": username, "capability": capability},
            )

    def find_access_context(self, user_id: int) -> dict:
        """
        Lo que hace falta para acuñar un ``Actor``: rol base, overrides por alcance y globales.

        Tres consultas locales a la BD del gateway, en el camino de CADA request autenticada.
        Es el precio de que **el rol no viaje en la cookie**: se relee siempre, igual que ya se
        hacía con ``is_active``. Un rol cacheado en la cookie sería un rol que no se puede
        revocar, porque el ``SessionMiddleware`` re-firma en cada respuesta y una sesión activa
        no expira nunca.

        Devuelve ``{"role": str, "overrides": {scope_id: role}, "globals": [str]}``. Un usuario
        sin filas devuelve overrides y globals vacíos, que es el lado seguro: el rol base manda.

        Los dos ``SELECT`` de lista pasan ``fetchone=False`` EXPLÍCITO y no lo omiten: el
        contrato de ``execute_query`` devuelve ``lastrowid``/``rowcount`` —o sea un ``int``—
        cuando ``fetchone`` es ``None``. Omitirlo daba un ``TypeError: 'int' object is not
        iterable`` recién con la tabla vacía, que es el caso normal el día del deploy.
        """
        row = self.db.execute_query(
            "SELECT gateway_role FROM users WHERE id = :id", {"id": user_id}, fetchone=True
        )
        grants = (
            self.db.execute_query(
                """
                SELECT scope_type, scope_id, role FROM access_grants
                WHERE user_id = :id AND scope_type <> 'global'
                """,
                {"id": user_id},
                fetchone=False,
            )
            or []
        )
        globals_ = (
            self.db.execute_query(
                "SELECT capability FROM user_global_capabilities WHERE user_id = :id",
                {"id": user_id},
                fetchone=False,
            )
            or []
        )
        return {
            "role": (row or {}).get("gateway_role") or "viewer",
            "overrides": {g["scope_id"]: g["role"] for g in grants},
            "globals": [g["capability"] for g in globals_],
        }
    def count(self, is_active: bool | None = None) -> int:
        """
        Contar usuarios con filtros opcionales

        Args:
            is_active: Filtrar por estado activo (opcional)

        Returns:
            int: Número de usuarios
        """
        if is_active is None:
            query = "SELECT COUNT(*) as total FROM users"
            params = {}
        else:
            query = "SELECT COUNT(*) as total FROM users WHERE is_active = :is_active"
            params = {"is_active": is_active}

        result = self.db.execute_query(query, params, fetchone=True)
        return result["total"] if result else 0
