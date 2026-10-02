"""
Helper de CSRF para los tests, sin efectos al importarse.

Vive fuera de ``conftest.py`` a propósito: importar ``tests.conftest`` desde un test crea una
segunda instancia de ese archivo, que al importarse crea otro tmpdir y pisa ``DB_NAME``.
"""


def attach_csrf(client) -> None:
    """
    Copia el token CSRF de la cookie al header por defecto del client.

    Es lo que hace el JS de la SPA, y va acá —en UN solo lugar— para que los ~44 archivos de
    tests que usan ``admin_client`` no tengan que saber del token. Lo importante es lo que NO
    se hizo: el guard **no** está detrás de un flag que los tests apaguen. Un control de
    seguridad que la suite desactiva es un control que nadie verifica; así, en cambio, un test
    nuevo que se olvide del header falla con 403 y eso es la señal correcta.
    """
    from app.core.csrf import CSRF_HEADER, cookie_name

    token = client.cookies.get(cookie_name())
    assert token, "el middleware no publicó la cookie de CSRF"
    client.headers[CSRF_HEADER] = token
