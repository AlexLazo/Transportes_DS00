"""Utilidades compartidas por las vistas: acceso a datos, sesión, permisos, bitácora, cierres."""
import json
import re
import secrets
from datetime import date, datetime
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session, url_for
from sqlalchemy import text

NIVEL = {"consulta": 1, "captura": 2, "admin": 3}
MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
         "Octubre", "Noviembre", "Diciembre"]
DIAS_SEM = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


# ───────────── base de datos ─────────────

def engine():
    return current_app.config["ENGINE"]


_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_FECHAHORA = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")


def _tipar(fila):
    """SQLite devuelve fechas como texto en consultas SQL directas; PostgreSQL ya las devuelve tipadas.
    Se normalizan para que las plantillas se comporten igual en local y en Railway."""
    for k, v in fila.items():
        if isinstance(v, str) and len(v) >= 10 and v[4] == "-":
            if _RE_FECHA.match(v):
                fila[k] = date.fromisoformat(v)
            elif _RE_FECHAHORA.match(v):
                fila[k] = datetime.fromisoformat(v.replace("T", " "))
    return fila


def rows(sql, **p):
    sqlite = engine().dialect.name == "sqlite"
    with engine().connect() as con:
        out = [dict(r) for r in con.execute(text(sql), p).mappings().all()]
    return [_tipar(r) for r in out] if sqlite else out


def one(sql, **p):
    r = rows(sql, **p)
    return r[0] if r else None


def scalar(sql, **p):
    with engine().connect() as con:
        return con.execute(text(sql), p).scalar()


def execute(sql, **p):
    with engine().begin() as con:
        return con.execute(text(sql), p)


def periodo_label(p):
    if not p:
        return ""
    a, m = p.split("-")
    return f"{MESES[int(m)]} {a}"


def periodos_disponibles():
    r = rows("""SELECT periodo FROM uso_rutas UNION SELECT periodo FROM excepciones
                UNION SELECT periodo FROM ausentismos ORDER BY periodo DESC""")
    return [x["periodo"] for x in r if x["periodo"]]


def periodo_actual():
    ps = periodos_disponibles()
    p = request.args.get("p") or request.form.get("p")
    return p if p in ps else (ps[0] if ps else None)


def fmt_fecha(v):
    return v.strftime("%d/%m/%Y") if hasattr(v, "strftime") else (v or "")


def dia_sem(v):
    return DIAS_SEM[v.weekday()] if hasattr(v, "weekday") else ""


# ───────────── sesión y permisos ─────────────

def usuario_actual():
    if "uid" not in session:
        return None
    if "usuario" not in g:
        g.usuario = one("SELECT id, usuario, nombre, rol FROM usuarios WHERE id=:i AND activo=:a",
                        i=session["uid"], a=True)
    return g.usuario


def requiere(nivel="consulta"):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = usuario_actual()
            if not u:
                return redirect(url_for("web.login", next=request.full_path))
            if NIVEL[u["rol"]] < NIVEL[nivel]:
                abort(403)
            return f(*a, **k)
        return w
    return deco


def puede(nivel):
    u = usuario_actual()
    return bool(u and NIVEL[u["rol"]] >= NIVEL[nivel])


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def verificar_csrf():
    if request.method == "POST":
        t = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not t or not secrets.compare_digest(t, session.get("_csrf", "")):
            abort(400, "Token de seguridad inválido; recarga la página e inténtalo de nuevo.")


# ───────────── bitácora ─────────────

def log_accion(accion, entidad=None, entidad_id=None, detalle=None):
    u = usuario_actual()
    execute("""INSERT INTO bitacora (ts, usuario, accion, entidad, entidad_id, detalle, ip)
               VALUES (:ts, :u, :a, :e, :ei, :d, :ip)""",
            ts=datetime.now(), u=(u or {}).get("usuario"), a=accion, e=entidad,
            ei=None if entidad_id is None else str(entidad_id),
            d=json.dumps(detalle, ensure_ascii=False, default=str) if detalle is not None else None,
            ip=request.headers.get("X-Forwarded-For", request.remote_addr))


# ───────────── cierres de mes ─────────────

def cerrado(periodo):
    return bool(scalar("SELECT 1 FROM cierres WHERE periodo=:p", p=periodo))


def periodo_de_fecha(f):
    return f"{f.year:04d}-{f.month:02d}"


def huellas_periodo(periodo):
    """Huella (hash) de cada archivo vigente que respalda el periodo."""
    a, m = periodo.split("-")
    ini = date(int(a), int(m), 1)
    fin = date(int(a) + (int(m) == 12), 1 if int(m) == 12 else int(m) + 1, 1)
    r = rows("""SELECT ruta, hash FROM archivos WHERE estado='OK' AND (
                   (tipo='DIARIO' AND fecha >= :ini AND fecha < :fin)
                   OR (tipo IN ('EXCEPCIONES','AUSENTISMOS','RUTAS_FIJAS') AND periodo=:p))""",
             ini=ini, fin=fin, p=periodo)
    return {x["ruta"]: x["hash"] for x in r}


def cambios_tras_cierre(periodo):
    """Archivos que cambiaron, aparecieron o desaparecieron después de cerrar el periodo."""
    c = one("SELECT huellas FROM cierres WHERE periodo=:p", p=periodo)
    if not c:
        return []
    antes, ahora = json.loads(c["huellas"]), huellas_periodo(periodo)
    out = []
    for k in sorted(set(antes) | set(ahora)):
        if k not in ahora:
            out.append((k, "ya no está en la base"))
        elif k not in antes:
            out.append((k, "archivo nuevo después del cierre"))
        elif antes[k] != ahora[k]:
            out.append((k, "modificado después del cierre"))
    return out


def bloquear_si_cerrado(fecha):
    p = periodo_de_fecha(fecha)
    if cerrado(p):
        flash(f"El periodo {periodo_label(p)} está cerrado: no se puede modificar. "
              "Pide a un administrador que lo reabra.", "error")
        return True
    return False


# ───────────── sugerencias de justificación ─────────────

MAPA_NOTA = [  # texto escrito a mano en el Excel diario → motivo del catálogo
    ("AUSENT", "Ausentismo del operador"),
    ("VACACION", "Vacaciones"),
    ("INCAPAC", "Incapacidad / permiso"),
    ("PERMISO", "Incapacidad / permiso"),
    ("DISPONIBLE", "Ruta de reserva (disponible)"),
    ("TALLER", "Unidad en taller / sin unidad"),
    ("UNIDAD", "Unidad en taller / sin unidad"),
    ("DESACTIV", "Ruta desactivada o en reestructura"),
    ("MANTENIMIENTO", "Unidad en taller / sin unidad"),
    ("NO TRABAJAN", "Cliente cerrado / feriado"),
]

# Letra que se dibuja dentro de cada cuadro de la matriz para explicar la causa
CAUSA_LETRA = {
    "Ausentismo del operador": "A", "Vacaciones": "V", "Incapacidad / permiso": "I",
    "Unidad en taller / sin unidad": "T", "Sin personal disponible": "P", "Ruta sin volumen / sin pedidos": "S",
    "Cliente cerrado / feriado": "F", "Cubierta por otra ruta (dummy / spot)": "C",
    "Ruta desactivada o en reestructura": "X", "Ruta de reserva (disponible)": "R",
    "Error de captura en el Excel": "E", "Otro (explicar en detalle)": "O",
}


def letra_motivo(nombre):
    return CAUSA_LETRA.get(nombre, "·")


def motivo_de_nota(nota):
    """Motivo del catálogo que corresponde a una nota escrita a mano en el Excel diario (o None)."""
    n = (nota or "").upper()
    for clave, motivo in MAPA_NOTA:
        if clave in n:
            return motivo
    return None


def sugerir(nota_excel, hay_ausentismo, desactivada, motivos_por_nombre):
    """(motivo_id, detalle, fuente) a partir de lo que ya dicen los Excel, o None."""
    if nota_excel:
        n = nota_excel.upper()
        for clave, motivo in MAPA_NOTA:
            if clave in n and motivo in motivos_por_nombre:
                return motivos_por_nombre[motivo], f"Nota del Excel diario: {nota_excel}", "Excel diario"
        otro = motivos_por_nombre.get("Otro (explicar en detalle)")
        if otro:
            return otro, f"Nota del Excel diario: {nota_excel}", "Excel diario"
    if hay_ausentismo and "Ausentismo del operador" in motivos_por_nombre:
        return motivos_por_nombre["Ausentismo del operador"], "Registrado en el archivo de Ausentismos", "Ausentismos"
    if desactivada and "Ruta desactivada o en reestructura" in motivos_por_nombre:
        return (motivos_por_nombre["Ruta desactivada o en reestructura"],
                "La ruta aparece en la lista de rutas desactivadas", "Rutas desactivadas")
    return None
