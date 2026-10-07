import os
import secrets
from pathlib import Path

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import db
from . import core


def en_railway() -> bool:
    return any(k.startswith("RAILWAY_") for k in os.environ)


def _secret_key(raiz: Path) -> str:
    k = os.environ.get("SECRET_KEY", "").strip()
    if k:
        return k
    if en_railway():
        raise RuntimeError("Define la variable SECRET_KEY en Railway.")
    # Desarrollo local: una clave aleatoria guardada para que las sesiones sobrevivan reinicios.
    f = raiz / "db" / ".secret_key"
    f.parent.mkdir(exist_ok=True)
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


def create_app(engine=None):
    raiz = Path(__file__).resolve().parent.parent.parent
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(raiz),
        MAX_CONTENT_LENGTH=160 * 1024 * 1024,   # varios Excel de hasta 40 MB por vez
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=en_railway(),
        JSON_AS_ASCII=False,
        ENGINE=engine or db.make_engine(),
    )
    db.init_db(app.config["ENGINE"])
    # gunicorn --preload crea la app UNA vez y luego la copia (fork) a cada proceso de trabajo. Si quedara una conexión
    # abierta en el pool, los procesos compartirían el mismo socket SSL y PostgreSQL respondería «bad record mac».
    # Cerrar el pool aquí hace que cada proceso abra sus propias conexiones.
    app.config["ENGINE"].dispose()
    if en_railway():
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from .views import bp
    app.register_blueprint(bp)

    @app.before_request
    def _csrf():
        if request.endpoint != "static":
            core.verificar_csrf()

    @app.after_request
    def _cabeceras(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        def qs(**cambios):
            """URL de la página actual cambiando/quitando parámetros (None quita)."""
            args = {k: v for k, v in request.args.items() if k != "pg"}
            for k, v in cambios.items():
                if v is None:
                    args.pop(k, None)
                else:
                    args[k] = v
            from flask import url_for
            return url_for(request.endpoint, **{**(request.view_args or {}), **args})
        return dict(usuario=core.usuario_actual(), puede=core.puede, csrf_token=core.csrf_token,
                    periodo_label=core.periodo_label, fmt_fecha=core.fmt_fecha, dia_sem=core.dia_sem, qs=qs)

    return app
