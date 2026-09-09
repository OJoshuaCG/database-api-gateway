"""
User Model - Interacción con Base de Datos

Este modelo maneja la interacción con la tabla 'users' usando SQL directo.
Es llamado desde el UserController siguiendo el patrón MVC.

Patrón: Routes → Controllers → Models → Database
"""

from datetime import UTC, datetime

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER


def _utcnow() -> datetime:
    """Ahora en UTC, naive — la convención de las columnas ``DateTime`` de este repo."""
    return datetime.now(UTC).replace(tzinfo=None)


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

    def count_active_access_admins(self, *, exclude_user_id: int | None = None) -> int:
        """
        Usuarios ACTIVOS con la capacidad global ``access_admin``.

        Es la consulta del invariante que evita el bloqueo total: *siempre debe existir al menos
        uno*. ``exclude_user_id`` responde la pregunta que importa —"¿quedaría alguno si saco a
        éste?"— en una sola consulta, en vez de contar y restar en Python, que es donde una
        carrera mete el error.

        Cuenta ``access_admin`` y no el rol ``owner`` a propósito: `owner` es el rol OPERATIVO y
        no administra accesos. Quedarse sin ningún `owner` es un problema de operación;
        quedarse sin ningún `access_admin` es no poder arreglarlo.
        """
        # `hashed_password <> ''` NO es una redundancia: una invitación pendiente nace ACTIVA y
        # sin credencial, así que sin este filtro un usuario que todavía no aceptó la invitación
        # **satisfaría el invariante siendo incapaz de entrar**. El bloqueo total quedaría
        # disfrazado de "hay un administrador".
        query = """
            SELECT COUNT(*) AS total
            FROM user_global_capabilities g
            JOIN users u ON u.id = g.user_id
            WHERE g.capability = 'access_admin'
              AND u.is_active = 1
              AND u.hashed_password <> ''
        """
        params: dict = {}
        if exclude_user_id is not None:
            query += " AND u.id <> :excluido"
            params["excluido"] = exclude_user_id
        fila = self.db.execute_query(query, params, fetchone=True)
        return (fila or {}).get("total") or 0

    def mark_login_success(self, user_id: int) -> None:
        """
        Sella el login exitoso. Un UPDATE, sin leer primero.

        Es best-effort por diseño: si esto falla, el login **igual procede**. Sellar la traza no
        puede ser condición para entrar — sería dejar afuera a todo el mundo por un problema de
        la BD de metadatos, justo cuando hay una incidencia. El rastro que sí es obligatorio es
        el ``audit_log``, y ese lo escribe el controller aparte.
        """
        # OJO CON EL ORDEN de las dos asignaciones, y es una diferencia real entre motores:
        # MySQL/MariaDB evalúan el SET de IZQUIERDA A DERECHA y las asignaciones posteriores ven
        # los valores ya escritos, mientras PostgreSQL y SQLite evalúan todas contra la fila
        # ORIGINAL. Con `previous_login_at` primero el resultado es el mismo en los tres; al
        # revés, MySQL copiaría el timestamp NUEVO y `previous_login_at` sería igual a
        # `last_login_at` para siempre, sin que nada falle.
        self.db.execute_query(
            "UPDATE users SET previous_login_at = last_login_at, last_login_at = :ahora "
            "WHERE id = :id",
            {"ahora": _utcnow(), "id": user_id},
            commit=True,
        )

    def mark_login_failure(self, user_id: int) -> None:
        """
        Sella el intento fallido. Mismo criterio best-effort que ``mark_login_success``.

        Solo se llama cuando la fila EXISTE: un username inexistente no tiene dónde sellarse, y
        crear una fila para registrarlo sería regalarle al atacante la confirmación de que su
        intento quedó anotado en algún lado.
        """
        self.db.execute_query(
            "UPDATE users SET last_failed_at = :ahora WHERE id = :id",
            {"ahora": _utcnow(), "id": user_id},
            commit=True,
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

        Devuelve ``{"role": str, "grants": [(scope_type, scope_id, role)], "globals": [str]}``.
        Un usuario sin filas devuelve listas vacías, que es el lado seguro: el rol base manda.

        **``grants`` conserva el ``scope_type``, y eso no es un detalle.** La versión anterior
        devolvía un dict ``{scope_id: role}`` y perdía el tipo, así que un grant con alcance de
        SERVIDOR se leía como si fuera de entorno: el entorno 3 y el servidor 3 son cosas
        distintas y colisionaban en silencio. Para la capa 1 era inocuo —solo mira los valores—
        pero la capa 2 resuelve por destino y ahí el tipo ES la pregunta.

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
            "grants": [(g["scope_type"], g["scope_id"], g["role"]) for g in grants],
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
