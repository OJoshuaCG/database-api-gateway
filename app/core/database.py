from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.environments import DB_ENGINE, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions.AppHttpException import AppHttpException
from app.utils import dict_utils


def _get_engine_kwargs(engine_prefix: str) -> dict:
    """
    Retorna kwargs para create_engine según el motor de base de datos.

    Motores soportados:
      - mysql / mariadb  → charset utf8mb4, init_command
      - postgresql       → connect_timeout
      - sqlite           → check_same_thread=False
    """
    base = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 1800,   # recicla conexiones cada 30 min
        "pool_pre_ping": True,  # verifica la conexión antes de usarla (evita stale connections)
    }

    if engine_prefix in ("mysql", "mariadb"):
        base["connect_args"] = {
            "charset": "utf8mb4",
            "init_command": "SET NAMES utf8mb4 COLLATE utf8mb4_general_ci",
        }
    elif engine_prefix == "postgresql":
        base["connect_args"] = {"connect_timeout": 10}
    elif engine_prefix == "sqlite":
        base["connect_args"] = {"check_same_thread": False}

    return base


class Database:
    """
    Singleton de conexión a base de datos.

    El engine y el pool de conexiones se crean UNA sola vez y se comparten
    entre todas las instancias durante el ciclo de vida de la aplicación.
    Esto evita la apertura de múltiples conexiones por request y el
    acumulamiento de conexiones en estado sleep en el servidor de base de datos.

    Uso:
        db = Database()   # siempre retorna la misma instancia
    """

    _instance: "Database | None" = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        db_name: str = DB_NAME,
        db_user: str = DB_USER,
        db_pass: str = DB_PASS,
        db_host: str = DB_HOST,
        db_port: int = DB_PORT,
        db_engine: str = DB_ENGINE,
    ):
        if self._initialized:
            return

        self.__db_name: str = db_name
        self.__db_user: str = db_user
        self.__db_pass: str = db_pass
        self.__db_host: str = db_host
        self.__db_port: str = db_port
        self.__db_engine: str = db_engine

        engine_prefix = db_engine.split("+")[0].lower()

        if engine_prefix == "sqlite":
            DB_URL = f"{db_engine}:///{db_name}"
        else:
            DB_URL = f"{db_engine}://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

        engine_kwargs = _get_engine_kwargs(engine_prefix)

        self.engine = create_engine(DB_URL, **engine_kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self._initialized = True

    @contextmanager
    def get_session(self):
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def get_declarative_base_session(self):
        """
        Retorna sesión para uso con modelos ORM SQLAlchemy.

        Permite coexistencia de SQL directo (execute_query) y ORM.
        La sesión debe cerrarse manualmente después de su uso.

        Uso:
            from app.models import User
            session = db.get_declarative_base_session()
            try:
                user = session.query(User).filter(User.id == 1).first()
                session.commit()
            finally:
                session.close()

        Returns:
            Session: Sesión SQLAlchemy para operaciones ORM
        """
        return self.SessionLocal()

    def execute_query(
        self,
        query,
        params: dict = {},
        fetchone: bool | None = None,
        commit: bool | None = False,
    ):
        with self.get_session() as session:
            try:
                _query = text(query)
                result = session.execute(_query, params)
                session.commit()

                if fetchone:
                    row = result.fetchone()
                    return dict(row._mapping) if row else None
                elif fetchone is False:
                    return [dict(row._mapping) for row in result.fetchall()]

                if hasattr(result, "lastrowid") and result.lastrowid:
                    return result.lastrowid
                return result.rowcount

            except AppHttpException:
                raise
            except Exception as e:
                session.rollback()
                context = {
                    "error_type": type(e).__name__,
                    "query": query,
                    "params": dict_utils._sanitize_dict(params),
                }

                if hasattr(e, "orig"):
                    context["message"] = str(e.orig)
                if hasattr(e, "statement"):
                    context["sql"] = e.statement
                if hasattr(e, "params"):
                    context["params"] = dict_utils._sanitize_dict(e.params)

                raise AppHttpException(
                    message="Ocurrio un error inesperado en el servidor",
                    status_code=500,
                    context=context,
                )

    def call_procedure(self, procedure_name: str, params: list = []):
        try:
            with self.engine.begin() as conn:
                cursor = conn.connection.cursor()
                try:
                    cursor.callproc(procedure_name, params)
                    results = []

                    if cursor.description:
                        rows = cursor.fetchall()
                        columns = [desc[0] for desc in cursor.description]
                        results.append(
                            [dict(zip(columns, row, strict=False)) for row in rows]
                        )

                    while cursor.nextset():
                        if cursor.description:
                            rows = cursor.fetchall()
                            columns = [desc[0] for desc in cursor.description]
                            results.append(
                                [dict(zip(columns, row, strict=False)) for row in rows]
                            )

                    if not results:
                        return False
                    return results[0] if len(results) == 1 else results

                finally:
                    cursor.close()

        except AppHttpException:
            raise
        except Exception as e:
            context = {"error_type": type(e).__name__}

            if e.args:
                context["error_code"] = str(e.args[0])
            if len(e.args) > 1:
                context["message"] = str(e.args[1])

            if procedure_name:
                context["sp"] = procedure_name
            if params:
                context["params"] = dict_utils._sanitize_dict(params)

            if e.args and e.args[0] == 1644:
                raise AppHttpException(
                    f"Ocurrio un error inesperado en el servidor: {e.args[1]}",
                    status_code=500,
                    context=context,
                )

            raise AppHttpException(
                message="Ocurrio un error inesperado en el servidor",
                status_code=500,
                context=context,
            )

    def get_host(self):
        return self.__db_host

    def get_port(self):
        return self.__db_port

    def get_name(self):
        return self.__db_name

    def get_user(self):
        return self.__db_user
