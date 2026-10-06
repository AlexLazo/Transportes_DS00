import csv
import hashlib
import io
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import respaldo
from . import core
from .core import execute, log_accion, one, requiere, rows, scalar


def en_railway():
    return any(k.startswith("RAILWAY_") for k in os.environ)

bp = Blueprint("web", __name__)

PAGINA = 100
EXT_EVIDENCIA = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "xlsx", "xls", "docx", "doc", "txt", "eml", "msg"}

# Filtro común: ruta/día de uso con día operativo (considera el ajuste manual de días)
USO = """FROM uso_rutas u
         JOIN dias d ON d.fecha = u.fecha
         LEFT JOIN dias_config dc ON dc.fecha = u.fecha
         LEFT JOIN justificaciones j ON j.fecha = u.fecha AND j.ruta = u.ruta
         LEFT JOIN motivos m ON m.id = j.motivo_id
         WHERE u.periodo = :p AND COALESCE(dc.operativo, d.auto_operativo) = :t"""


_REQ = "__request__"


def _filtros_uso(extra="", cat=_REQ, p=None, ct=_REQ):
    """Filtros de categoría/contratista comunes a tablero, rutas, matriz y reporte.
    Sin argumentos toma `cat` y `ct` de la URL; se pueden fijar para armar el reporte (GC y AJ por separado)."""
    prm = dict(p=p or core.periodo_actual(), t=True)
    sql = ""
    cat = request.args.get("cat") if cat == _REQ else cat
    if cat == "GC":   # sin lista oficial (ene–jul) las rutas no traen categoría: se tratan como GC
        sql += " AND (u.categoria = 'GC' OR u.categoria IS NULL)"
    elif cat == "AJ":
        sql += " AND u.categoria = 'AJ'"
    ct = request.args.get("ct") if ct == _REQ else ct
    if ct:
        sql += " AND u.contratista = :ct"
        prm["ct"] = ct
    return sql + extra, prm


def kpis_uso(p, cat=None, ct=None):
    """Uso de rutas fijas de un periodo (opcionalmente solo GC o solo AJ)."""
    extra, prm = _filtros_uso(cat=cat, p=p, ct=ct)
    k = one(f"""SELECT COUNT(*) AS total,
                       SUM(CASE WHEN u.estado <> 'NO_USADA' THEN 1 ELSE 0 END) AS usadas,
                       SUM(CASE WHEN u.estado = 'USADA_CAB' THEN 1 ELSE 0 END) AS por_cabecera,
                       SUM(CASE WHEN u.estado = 'USADA_AJ' THEN 1 ELSE 0 END) AS por_control_aj,
                       SUM(CASE WHEN u.estado = 'NO_USADA' THEN 1 ELSE 0 END) AS no_usadas,
                       SUM(CASE WHEN u.estado = 'NO_USADA' AND j.id IS NOT NULL THEN 1 ELSE 0 END) AS justificadas,
                       COUNT(DISTINCT u.fecha) AS dias, COUNT(DISTINCT u.ruta) AS rutas
                {USO}{extra}""", **prm) or {}
    k = {a: (b or 0) for a, b in k.items()}
    k["pendientes"] = k["no_usadas"] - k["justificadas"]
    k["pct_uso"] = (100 * k["usadas"] / k["total"]) if k["total"] else None
    k["pct_just"] = (100 * k["justificadas"] / k["no_usadas"]) if k["no_usadas"] else None
    return k


def _csv(nombre, cabecera, filas):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cabecera)
    for f in filas:
        w.writerow(["" if v is None else (v.isoformat() if isinstance(v, (date, datetime)) else v) for v in f])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={nombre}"})


def _pagina(total):
    pg = max(1, request.args.get("pg", 1, type=int))
    paginas = max(1, -(-total // PAGINA))
    pg = min(pg, paginas)
    return pg, paginas, (pg - 1) * PAGINA


# ───────────── acceso ─────────────

_intentos = {}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not scalar("SELECT COUNT(*) FROM usuarios"):
        if os.environ.get("SETUP_TOKEN"):
            # /setup exige el token y responde 404 sin él: en vez de mandar al visitante a un 404, se le explica.
            return render_template("sin_admin.html")
        return redirect(url_for("web.setup"))
    if request.method == "POST":
        clave = (request.remote_addr, request.form.get("usuario", "").lower())
        n, hasta = _intentos.get(clave, (0, 0))
        if hasta > time.time():
            flash("Demasiados intentos. Espera unos minutos.", "error")
            return render_template("login.html"), 429
        u = one("SELECT * FROM usuarios WHERE usuario=:u AND activo=:a", u=request.form.get("usuario", "").strip().lower(), a=True)
        if u and check_password_hash(u["hash"], request.form.get("clave", "")):
            _intentos.pop(clave, None)
            session.clear()
            session["uid"] = u["id"]
            session.permanent = False
            core.csrf_token()
            log_accion("login", "usuario", u["id"])
            nxt = request.args.get("next", "")
            seguro = nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt
            return redirect(nxt if seguro else url_for("web.inicio"))
        _intentos[clave] = (n + 1, time.time() + 300 if n + 1 >= 5 else 0)
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Primer uso: crea el administrador. Se bloquea apenas existe un usuario."""
    if scalar("SELECT COUNT(*) FROM usuarios"):
        return redirect(url_for("web.login"))
    # En Railway la URL es pública: sin este token cualquiera podría crear el primer administrador.
    token = os.environ.get("SETUP_TOKEN", "")
    if en_railway() and not token:
        abort(503, "Falta definir la variable SETUP_TOKEN en Railway para crear el administrador.")
    if token and not secrets.compare_digest(request.values.get("token", ""), token):
        abort(404)
    if request.method == "POST":
        usuario, nombre, c1, c2 = (request.form.get(k, "").strip() for k in ("usuario", "nombre", "clave", "clave2"))
        if not usuario or not nombre:
            flash("Usuario y nombre son obligatorios.", "error")
        elif len(c1) < 10:
            flash("La contraseña debe tener al menos 10 caracteres.", "error")
        elif c1 != c2:
            flash("Las contraseñas no coinciden.", "error")
        else:
            execute("""INSERT INTO usuarios (usuario, nombre, rol, hash, activo, creado_en)
                       VALUES (:u, :n, 'admin', :h, :a, :c)""",
                    u=usuario.lower(), n=nombre, h=generate_password_hash(c1), a=True, c=datetime.now())
            flash("Administrador creado. Inicia sesión.", "ok")
            return redirect(url_for("web.login"))
    return render_template("setup.html")


@bp.route("/logout", methods=["POST"])
def logout():
    log_accion("logout")
    session.clear()
    return redirect(url_for("web.login"))


@bp.route("/healthz")
def healthz():
    scalar("SELECT 1")
    return "ok"


# ───────────── tablero ─────────────

@bp.route("/")
@requiere()
def inicio():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    extra, prm = _filtros_uso()
    k = kpis_uso(p, _REQ, _REQ)

    por_dia = rows(f"""SELECT u.fecha, COUNT(*) AS total,
                              SUM(CASE WHEN u.estado = 'NO_USADA' THEN 1 ELSE 0 END) AS no_usadas
                       {USO}{extra} GROUP BY u.fecha ORDER BY u.fecha""", **prm)
    por_ct = rows(f"""SELECT COALESCE(u.contratista, '(sin contratista)') AS contratista, COUNT(*) AS total,
                             SUM(CASE WHEN u.estado = 'NO_USADA' THEN 1 ELSE 0 END) AS no_usadas,
                             SUM(CASE WHEN u.estado = 'NO_USADA' AND j.id IS NULL THEN 1 ELSE 0 END) AS pendientes
                      {USO}{extra} GROUP BY COALESCE(u.contratista, '(sin contratista)')
                      ORDER BY 4 DESC, 3 DESC LIMIT 12""", **prm)
    dum = one("""SELECT COUNT(*) AS filas,
                        SUM(CASE WHEN dummy='SI' THEN 1 ELSE 0 END) AS dummys,
                        SUM(CASE WHEN dummy='SI' AND quincena=1 THEN 1 ELSE 0 END) AS q1,
                        SUM(CASE WHEN dummy='SI' AND quincena=2 THEN 1 ELSE 0 END) AS q2,
                        SUM(CASE WHEN excepcion='SI' THEN 1 ELSE 0 END) AS excepciones,
                        SUM(CASE WHEN recarga='SI' THEN 1 ELSE 0 END) AS recargas,
                        SUM(CASE WHEN dummy='SI' AND firmada IS NULL THEN 1 ELSE 0 END) AS sin_firma
                 FROM excepciones WHERE periodo=:p""", p=p) or {}
    aus = scalar("SELECT COUNT(*) FROM ausentismos WHERE periodo=:p", p=p) or 0
    n_hall = sum(h["cantidad"] for h in hallazgos(p) if h["severidad"] == "alta")
    return render_template("inicio.html", p=p, k=k, por_dia=por_dia, por_ct=por_ct, dum=dum, aus=aus,
                           n_hall=n_hall, aj=aj_resumen_cargas(p), ajuso=kpis_uso(p, "AJ"), aus_r=aus_resumen(p),
                           cerrado=core.cerrado(p),
                           cambios=core.cambios_tras_cierre(p) if core.cerrado(p) else [],
                           periodos=core.periodos_disponibles(), contratistas=_contratistas(p))


def _contratistas(p):
    return [r["c"] for r in rows("SELECT DISTINCT contratista AS c FROM uso_rutas WHERE periodo=:p "
                                 "AND contratista IS NOT NULL ORDER BY 1", p=p)]


# ───────────── rutas fijas: uso y justificación ─────────────

def _no_usadas(estado, q):
    """Rutas-día no usadas del periodo con su justificación y los datos para sugerir."""
    extra, prm = _filtros_uso(" AND u.estado = 'NO_USADA'")
    if estado == "pendientes":
        extra += " AND j.id IS NULL"
    elif estado == "justificadas":
        extra += " AND j.id IS NOT NULL"
    if q:
        extra += " AND u.ruta LIKE :q"
        prm["q"] = f"%{q.upper()}%"
    sql = f"""SELECT u.fecha, u.ruta, u.categoria, u.contratista, u.supervisor, u.nota_excel,
                     j.id AS j_id, j.motivo_id, j.detalle, j.origen, m.nombre AS motivo, m.requiere_evidencia,
                     (SELECT COUNT(*) FROM evidencias ev WHERE ev.justificacion_id = j.id) AS n_evid,
                     CASE WHEN EXISTS (SELECT 1 FROM ausentismos a WHERE a.fecha = u.fecha AND a.ruta = u.ruta)
                          THEN 1 ELSE 0 END AS hay_aus,
                     CASE WHEN EXISTS (SELECT 1 FROM rutas_desactivadas r WHERE r.ruta = u.ruta)
                          THEN 1 ELSE 0 END AS desact
              {USO}{extra}"""
    return sql, prm


def _con_sugerencia(filas):
    mot = {m["nombre"]: m["id"] for m in rows("SELECT id, nombre FROM motivos WHERE activo=:a", a=True)}
    nombres = {v: k for k, v in mot.items()}
    for f in filas:
        f["sug"] = None
        if not f["j_id"]:
            s = core.sugerir(f["nota_excel"], f["hay_aus"], f["desact"], mot)
            if s:
                f["sug"] = dict(motivo_id=s[0], motivo=nombres[s[0]], detalle=s[1], fuente=s[2])
    return filas



def _orden(permitidos, defecto, dir_defecto="desc"):
    """Orden elegido en la URL (?orden=…&dir=asc|desc) restringido a una lista blanca. -> (clave, dir, 'EXPR DIR')"""
    clave = request.args.get("orden")
    if clave not in permitidos:
        clave, d = defecto, dir_defecto
    else:
        d = "asc" if request.args.get("dir") == "asc" else "desc"
    return clave, d, f"{permitidos[clave]} {d.upper()}"


def ranking_rutas(estado, q):
    """Rutas agrupadas: cuántos días no se usaron, cuántos están sin explicar y cuántos con ausentismo."""
    sql, prm = _no_usadas(estado, q)
    por = {}
    for f in rows(sql, **prm):
        r = por.setdefault(f["ruta"], dict(ruta=f["ruta"], contratista=f["contratista"], categoria=f["categoria"],
                                           no_usadas=0, pendientes=0, ausentismos=0, ultima=f["fecha"]))
        r["no_usadas"] += 1
        r["pendientes"] += 0 if f["j_id"] else 1
        r["ausentismos"] += 1 if f["hay_aus"] else 0
        r["ultima"] = max(r["ultima"], f["fecha"])
    clave, d, _ = _orden({k: k for k in ("no_usadas", "pendientes", "ausentismos", "ruta", "contratista", "ultima")},
                         "no_usadas")
    texto = clave in ("ruta", "contratista")
    lista = sorted(por.values(), key=lambda r: ((r[clave] or "").lower() if texto else r[clave], r["ruta"]),
                   reverse=(d == "desc"))
    return lista, clave, d

@bp.route("/rutas")
@requiere()
def rutas():
    p = core.periodo_actual()
    estado = request.args.get("estado", "pendientes")
    q = request.args.get("q", "").strip()
    if request.args.get("vista") == "ranking" and p:
        lista, orden, dir_ = ranking_rutas(estado, q)
        return render_template("rutas.html", p=p, vista="ranking", ranking=lista, orden=orden, dir=dir_, filas=[],
                               total=len(lista), pg=1, paginas=1, estado=estado, q=q, n_sug=0,
                               periodos=core.periodos_disponibles(), contratistas=_contratistas(p),
                               cerrado=core.cerrado(p))
    sql, prm = _no_usadas(estado, q)
    total = scalar(f"SELECT COUNT(*) FROM ({sql}) x", **prm) or 0
    pg, paginas, off = _pagina(total)
    orden, dir_, order_sql = _orden({
        "fecha": "u.fecha", "ruta": "u.ruta", "contratista": "u.contratista", "nota": "u.nota_excel",
        "estado": "CASE WHEN j.id IS NULL THEN 0 ELSE 1 END"}, "fecha")
    filas = rows(sql + f" ORDER BY {order_sql}, u.fecha DESC, u.ruta LIMIT {PAGINA} OFFSET {off}", **prm) if p else []
    filas = _con_sugerencia(filas)
    n_sug = 0
    if estado != "justificadas" and p and puede_editar(p):
        todas = _con_sugerencia(rows(_no_usadas("pendientes", q)[0], **_no_usadas("pendientes", q)[1]))
        n_sug = sum(1 for f in todas if f["sug"])
    return render_template("rutas.html", p=p, vista="dias", orden=orden, dir=dir_, filas=filas, total=total, pg=pg,
                           paginas=paginas, estado=estado, q=q,
                           periodos=core.periodos_disponibles(), contratistas=_contratistas(p) if p else [],
                           n_sug=n_sug, cerrado=core.cerrado(p) if p else False)


def puede_editar(p):
    return core.puede("captura") and not core.cerrado(p)


@bp.route("/rutas/aceptar-sugerencias", methods=["POST"])
@requiere("captura")
def aceptar_sugerencias():
    p = request.form.get("p")
    if not p or core.cerrado(p):
        flash("El periodo está cerrado o no es válido.", "error")
        return redirect(url_for("web.rutas"))
    solo = request.form.get("solo")  # "fecha|ruta" para aceptar una sola
    sql, prm = _no_usadas("pendientes", request.form.get("q", "").strip())
    filas = _con_sugerencia(rows(sql, **prm))
    n = 0
    for f in filas:
        if not f["sug"] or (solo and solo != f"{f['fecha'].isoformat()}|{f['ruta']}"):
            continue
        execute("""INSERT INTO justificaciones (fecha, ruta, motivo_id, detalle, origen, creado_por, creado_en)
                   VALUES (:f, :r, :m, :d, 'EXCEL', :u, :c)""",
                f=f["fecha"], r=f["ruta"], m=f["sug"]["motivo_id"], d=f["sug"]["detalle"],
                u=core.usuario_actual()["usuario"], c=datetime.now())
        n += 1
    log_accion("aceptar_sugerencias", "periodo", p, dict(cantidad=n, una=solo))
    flash(f"Se registraron {n} justificaciones a partir de lo que ya decían los Excel.", "ok")
    return redirect(request.referrer or url_for("web.rutas", p=p))


@bp.route("/rutas/<fecha>/<ruta>")
@requiere()
def ruta_detalle(fecha, ruta):
    f = _fecha_o_404(fecha)
    u = one("""SELECT u.*, j.id AS j_id, j.motivo_id, j.detalle, j.origen, j.creado_por, j.creado_en,
                      j.actualizado_por, j.actualizado_en
               FROM uso_rutas u LEFT JOIN justificaciones j ON j.fecha=u.fecha AND j.ruta=u.ruta
               WHERE u.fecha=:f AND u.ruta=:r""", f=f, r=ruta) or abort(404)
    evid = rows("""SELECT id, nombre_original, tam, subido_por, subido_en FROM evidencias
                   WHERE justificacion_id=:j ORDER BY id""", j=u["j_id"]) if u["j_id"] else []
    aus = rows("SELECT * FROM ausentismos WHERE fecha=:f AND ruta=:r", f=f, r=ruta)
    desact = one("SELECT * FROM rutas_desactivadas WHERE ruta=:r ORDER BY periodo DESC", r=ruta)
    transp = one("""SELECT t.transporte, t.cod_transportista, t.ruta_original, t.contratista, t.comentario
                    FROM cambios_cabecera t WHERE t.fecha=:f AND t.nueva_ruta=:r""", f=f, r=ruta) \
        if u["estado"] == "USADA_CAB" else None
    historial = rows("""SELECT fecha, estado FROM uso_rutas WHERE ruta=:r AND fecha BETWEEN :a AND :b ORDER BY fecha""",
                     r=ruta, a=f - timedelta(days=13), b=f + timedelta(days=13))
    motivos = rows("SELECT * FROM motivos WHERE activo=:a ORDER BY id", a=True)
    mot = {m["nombre"]: m["id"] for m in motivos}
    sug = core.sugerir(u["nota_excel"], bool(aus), bool(desact), mot) if not u["j_id"] else None
    return render_template("ruta_detalle.html", u=u, evid=evid, aus=aus, desact=desact, transp=transp,
                           historial=historial, motivos=motivos, sug=sug, sug_nombre={v: k for k, v in mot.items()},
                           editable=puede_editar(u["periodo"]), cerrado=core.cerrado(u["periodo"]))


def _fecha_o_404(s):
    try:
        return date.fromisoformat(s)
    except ValueError:
        abort(404)


@bp.route("/rutas/<fecha>/<ruta>/justificar", methods=["POST"])
@requiere("captura")
def justificar(fecha, ruta):
    f = _fecha_o_404(fecha)
    if core.bloquear_si_cerrado(f):
        return redirect(url_for("web.ruta_detalle", fecha=fecha, ruta=ruta))
    if not one("SELECT 1 AS x FROM uso_rutas WHERE fecha=:f AND ruta=:r AND estado='NO_USADA'", f=f, r=ruta):
        flash("Esa ruta sí se usó ese día: no requiere justificación.", "error")
        return redirect(url_for("web.ruta_detalle", fecha=fecha, ruta=ruta))
    motivo_id = request.form.get("motivo_id", type=int)
    detalle = request.form.get("detalle", "").strip()
    m = one("SELECT * FROM motivos WHERE id=:i AND activo=:a", i=motivo_id, a=True)
    if not m:
        flash("Elige un motivo.", "error")
    elif m["nombre"].startswith("Otro") and len(detalle) < 10:
        flash("Con el motivo «Otro» debes explicar lo ocurrido en el detalle (mínimo 10 caracteres).", "error")
    else:
        u = core.usuario_actual()["usuario"]
        ya = one("SELECT id FROM justificaciones WHERE fecha=:f AND ruta=:r", f=f, r=ruta)
        if ya:
            execute("""UPDATE justificaciones SET motivo_id=:m, detalle=:d, origen='MANUAL',
                       actualizado_por=:u, actualizado_en=:n WHERE id=:i""",
                    m=motivo_id, d=detalle, u=u, n=datetime.now(), i=ya["id"])
        else:
            execute("""INSERT INTO justificaciones (fecha, ruta, motivo_id, detalle, origen, creado_por, creado_en)
                       VALUES (:f, :r, :m, :d, 'MANUAL', :u, :n)""",
                    f=f, r=ruta, m=motivo_id, d=detalle, u=u, n=datetime.now())
        log_accion("justificar" if not ya else "editar_justificacion", "ruta_dia", f"{fecha}|{ruta}",
                   dict(motivo=m["nombre"], detalle=detalle))
        flash("Justificación guardada.", "ok")
    return redirect(url_for("web.ruta_detalle", fecha=fecha, ruta=ruta))


@bp.route("/rutas/<fecha>/<ruta>/borrar-justificacion", methods=["POST"])
@requiere("captura")
def borrar_justificacion(fecha, ruta):
    f = _fecha_o_404(fecha)
    if not core.bloquear_si_cerrado(f):
        j = one("SELECT id FROM justificaciones WHERE fecha=:f AND ruta=:r", f=f, r=ruta)
        if j:
            execute("DELETE FROM evidencias WHERE justificacion_id=:i", i=j["id"])
            execute("DELETE FROM justificaciones WHERE id=:i", i=j["id"])
            log_accion("borrar_justificacion", "ruta_dia", f"{fecha}|{ruta}")
            flash("Justificación eliminada.", "ok")
    return redirect(url_for("web.ruta_detalle", fecha=fecha, ruta=ruta))


@bp.route("/rutas/<fecha>/<ruta>/evidencia", methods=["POST"])
@requiere("captura")
def subir_evidencia(fecha, ruta):
    f = _fecha_o_404(fecha)
    destino = redirect(url_for("web.ruta_detalle", fecha=fecha, ruta=ruta))
    if core.bloquear_si_cerrado(f):
        return destino
    j = one("SELECT id FROM justificaciones WHERE fecha=:f AND ruta=:r", f=f, r=ruta)
    if not j:
        flash("Primero guarda la justificación y después adjunta la evidencia.", "error")
        return destino
    arch = request.files.get("archivo")
    if not arch or not arch.filename:
        flash("Elige un archivo.", "error")
        return destino
    nombre = arch.filename.replace("\\", "/").split("/")[-1][:255]
    ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    if ext not in EXT_EVIDENCIA:
        flash(f"Tipo de archivo no permitido (.{ext}). Permitidos: {', '.join(sorted(EXT_EVIDENCIA))}.", "error")
        return destino
    datos = arch.read()
    if not datos:
        flash("El archivo está vacío.", "error")
        return destino
    execute("""INSERT INTO evidencias (justificacion_id, nombre_original, mime, tam, sha256, contenido, subido_por, subido_en)
               VALUES (:j, :n, :m, :t, :h, :c, :u, :e)""",
            j=j["id"], n=nombre, m=arch.mimetype, t=len(datos), h=hashlib.sha256(datos).hexdigest(), c=datos,
            u=core.usuario_actual()["usuario"], e=datetime.now())
    log_accion("subir_evidencia", "ruta_dia", f"{fecha}|{ruta}", dict(archivo=nombre, bytes=len(datos)))
    flash("Evidencia adjuntada.", "ok")
    return destino


@bp.route("/evidencia/<int:eid>")
@requiere()
def ver_evidencia(eid):
    e = one("SELECT nombre_original, mime, contenido FROM evidencias WHERE id=:i", i=eid) or abort(404)
    seguro = (e["mime"] or "").startswith(("image/", "application/pdf"))
    log_accion("ver_evidencia", "evidencia", eid)
    return Response(bytes(e["contenido"]), mimetype=e["mime"] or "application/octet-stream", headers={
        "Content-Disposition": f"{'inline' if seguro else 'attachment'}; filename*=UTF-8''{_quote(e['nombre_original'])}",
        "Content-Security-Policy": "sandbox"})


def _quote(s):
    from urllib.parse import quote
    return quote(s)


@bp.route("/evidencia/<int:eid>/borrar", methods=["POST"])
@requiere("captura")
def borrar_evidencia(eid):
    e = one("""SELECT e.id, e.nombre_original, j.fecha, j.ruta FROM evidencias e
               JOIN justificaciones j ON j.id=e.justificacion_id WHERE e.id=:i""", i=eid) or abort(404)
    if not core.bloquear_si_cerrado(e["fecha"]):
        execute("DELETE FROM evidencias WHERE id=:i", i=eid)
        log_accion("borrar_evidencia", "evidencia", eid, dict(archivo=e["nombre_original"]))
        flash("Evidencia eliminada.", "ok")
    return redirect(url_for("web.ruta_detalle", fecha=e["fecha"].isoformat(), ruta=e["ruta"]))


GLIFOS_CARGA = (("dummy", "◆", "Dummy"), ("recarga", "↺", "Recarga"), ("reposicion", "⇄", "Reposición"))


def matriz_datos(p, cat=_REQ, ct=_REQ, solo_inc=True):
    """Matriz ruta × día. Cada cuadro trae su color, una letra con la causa y un texto para el cursor."""
    extra, prm = _filtros_uso(cat=cat, ct=ct, p=p)
    a, m = map(int, p.split("-"))
    dias_mes = (date(a + (m == 12), 1 if m == 12 else m + 1, 1) - date(a, m, 1)).days
    datos = rows(f"""SELECT u.fecha, u.ruta, u.categoria, u.contratista, u.estado, u.nota_excel,
                            j.id AS j_id, mo.nombre AS motivo,
                            CASE WHEN COALESCE(dc.operativo, d.auto_operativo) = :t THEN 1 ELSE 0 END AS op
                     FROM uso_rutas u JOIN dias d ON d.fecha = u.fecha
                     LEFT JOIN dias_config dc ON dc.fecha = u.fecha
                     LEFT JOIN justificaciones j ON j.fecha = u.fecha AND j.ruta = u.ruta
                     LEFT JOIN motivos mo ON mo.id = j.motivo_id
                     WHERE u.periodo = :p {extra}""", **prm)
    aus = {(r["fecha"], r["ruta"]): r["n"] for r in rows(
        "SELECT fecha, ruta, COUNT(*) AS n FROM ausentismos WHERE periodo = :p GROUP BY fecha, ruta", p=p)}
    glifos = {}
    for r in rows("SELECT fecha, ruta, dummy, recarga, reposicion FROM cargas_aj WHERE periodo = :p", p=p):
        glifos.setdefault((r["fecha"], r["ruta"]), []).extend(
            (g, nombre) for campo, g, nombre in GLIFOS_CARGA if r[campo] == "SI")
    for r in rows("SELECT fecha, ruta FROM recolecciones_aj WHERE periodo = :p", p=p):
        glifos.setdefault((r["fecha"], r["ruta"]), []).append(("▣", "Recolección"))

    rutas_m, incid, vistos = {}, set(), set()
    for r in datos:
        k = (r["fecha"], r["ruta"])
        vistos.add(k)
        hay_aus = aus.get(k)
        gl = glifos.get(k, [])
        texto = [f"{r['ruta']} · {core.fmt_fecha(r['fecha'])}"]
        if not r["op"]:
            cls, letra = "o", ""
            texto.append("día no operativo")
        elif r["estado"] == "NO_USADA":
            incid.add(r["ruta"])
            if r["j_id"]:
                cls, letra = "j", core.letra_motivo(r["motivo"])
                texto.append(f"no usada · justificada: {r['motivo']}")
            elif hay_aus:
                cls, letra = "e", "A"
                texto.append("no usada · ausentismo registrado (sin aceptar como justificación)")
            elif r["nota_excel"]:
                mot = core.motivo_de_nota(r["nota_excel"])
                cls, letra = "e", (core.letra_motivo(mot) if mot else "·")
                texto.append(f"no usada · nota del Excel diario: {r['nota_excel']}")
            else:
                cls, letra = "n", ""
                texto.append("no usada · SIN EXPLICAR")
        else:
            cls = "c" if r["estado"] == "USADA_CAB" else "u"
            letra = ""
            texto.append({"USADA": "usada", "USADA_CAB": "usada por cambio de cabecera",
                          "USADA_AJ": "usada según el control de AJ (SAP no la muestra)"}[r["estado"]])
            if hay_aus:
                letra = "A"
                incid.add(r["ruta"])
                texto.append("hubo ausentismo del operador (la ruta salió con otro personal)")
            elif gl:
                letra = gl[0][0]
        if gl and r["op"]:
            texto.append("AJ: " + ", ".join(sorted({n for _, n in gl})))
        x = rutas_m.setdefault(r["ruta"], dict(ruta=r["ruta"], contratista=r["contratista"],
                                               categoria=r["categoria"], celdas={}))
        x["celdas"][r["fecha"].day] = dict(cls=cls, letra=letra, titulo=" · ".join(texto), fecha=r["fecha"],
                                           ruta=r["ruta"], link=bool(r["op"]))
    lista = sorted((v for k, v in rutas_m.items() if not solo_inc or k in incid),
                   key=lambda v: ((v["contratista"] or "~"), v["ruta"]))
    return dict(lista=lista, dias_mes=dias_mes, anio=a, mes=m,
                dow=[core.DIAS_SEM[date(a, m, d).weekday()][0].upper() for d in range(1, dias_mes + 1)],
                con_datos={r["fecha"].day for r in datos},
                aus_fuera=sum(1 for k in aus if k not in vistos), aus_total=len(aus))


@bp.route("/matriz")
@requiere()
def matriz():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    solo_inc = request.args.get("todas") != "1"
    return render_template("matriz.html", p=p, m=matriz_datos(p, solo_inc=solo_inc), solo_inc=solo_inc,
                           periodos=core.periodos_disponibles(), contratistas=_contratistas(p))


@bp.route("/export/rutas.csv")
@requiere()
def export_rutas():
    p = core.periodo_actual()
    sql, prm = _no_usadas(request.args.get("estado", "todas"), request.args.get("q", "").strip())
    filas = rows(sql + " ORDER BY u.fecha, u.ruta", **prm)
    return _csv(f"rutas_no_usadas_{p}.csv",
                ["Fecha", "Ruta", "Categoría", "Contratista", "Supervisor", "Nota Excel diario", "Estado", "Motivo",
                 "Detalle", "Origen", "Evidencias"],
                [[f["fecha"], f["ruta"], f["categoria"], f["contratista"], f["supervisor"], f["nota_excel"],
                  "Justificada" if f["j_id"] else "PENDIENTE", f["motivo"], f["detalle"], f["origen"], f["n_evid"]]
                 for f in filas])


# ───────────── dummies y pagos ─────────────

@bp.route("/dummies")
@requiere()
def dummies():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    resumen = rows("""SELECT COALESCE(contratista_n, '(SIN CONTRATISTA)') AS contratista,
                    SUM(CASE WHEN dummy='SI' AND quincena=1 THEN 1 ELSE 0 END) AS d1,
                    SUM(CASE WHEN dummy='SI' AND quincena=2 THEN 1 ELSE 0 END) AS d2,
                    SUM(CASE WHEN dummy='SI' THEN 1 ELSE 0 END) AS dummys,
                    SUM(CASE WHEN dummy='SI' AND ruta_fija='SI' THEN 1 ELSE 0 END) AS dum_fija,
                    SUM(CASE WHEN spot1='SI' THEN 1 ELSE 0 END) AS spot1,
                    SUM(CASE WHEN spot_inc='SI' THEN 1 ELSE 0 END) AS spot_inc,
                    SUM(CASE WHEN excepcion='SI' THEN 1 ELSE 0 END) AS excepciones,
                    SUM(CASE WHEN recarga='SI' THEN 1 ELSE 0 END) AS recargas,
                    SUM(CASE WHEN envases='SI' THEN 1 ELSE 0 END) AS envases,
                    SUM(CASE WHEN dummy='SI' AND firmada IS NULL THEN 1 ELSE 0 END) AS sin_firma
                  FROM excepciones WHERE periodo=:p
                  GROUP BY COALESCE(contratista_n, '(SIN CONTRATISTA)')
                  ORDER BY 4 DESC, 1""", p=p)
    tot = {k: sum(r[k] or 0 for r in resumen) for k in
           ("d1", "d2", "dummys", "dum_fija", "spot1", "spot_inc", "excepciones", "recargas", "envases", "sin_firma")}
    ct = request.args.get("ct", "")
    qn = request.args.get("quincena", type=int)
    tipo = request.args.get("tipo", "")
    q = request.args.get("q", "").strip()
    where, prm = "WHERE periodo=:p", dict(p=p)
    if ct:
        where += " AND COALESCE(contratista_n, '(SIN CONTRATISTA)') = :ct"
        prm["ct"] = ct
    if qn in (1, 2):
        where += " AND quincena = :qn"
        prm["qn"] = qn
    col = {"dummy": "dummy", "excepcion": "excepcion", "recarga": "recarga", "envases": "envases",
           "spot1": "spot1", "spot_inc": "spot_inc", "ruta_fija": "ruta_fija"}.get(tipo)
    if col:
        where += f" AND {col} = 'SI'"
    if q:
        where += " AND (CAST(transporte AS TEXT) LIKE :q OR UPPER(comentario) LIKE :qu OR ruta_liquidada LIKE :qu)"
        prm["q"], prm["qu"] = f"%{q}%", f"%{q.upper()}%"
    total = scalar(f"SELECT COUNT(*) FROM excepciones {where}", **prm) or 0
    pg, paginas, off = _pagina(total)
    orden, dir_, order_sql = _orden({"fecha": "fecha", "quincena": "quincena", "transporte": "transporte",
                                     "ruta": "ruta_liquidada", "contratista": "contratista_n"}, "fecha")
    detalle = rows(f"""SELECT * FROM excepciones {where} ORDER BY {order_sql}, id LIMIT {PAGINA} OFFSET {off}""", **prm)
    return render_template("dummies.html", p=p, orden=orden, dir=dir_, resumen=resumen, tot=tot, detalle=detalle,
                           total=total, pg=pg,
                           paginas=paginas, ct=ct, qn=qn, tipo=tipo, q=q, periodos=core.periodos_disponibles())


@bp.route("/export/dummies.csv")
@requiere()
def export_dummies():
    p = core.periodo_actual()
    filas = rows("SELECT * FROM excepciones WHERE periodo=:p ORDER BY fecha, id", p=p)
    return _csv(f"dummies_excepciones_{p}.csv",
                ["Fecha", "Quincena", "Transporte", "Ruta", "Ruta liquidada", "Dummy", "Recarga", "Excepción",
                 "Ruta fija", "Spot 1", "Spot incremental", "Envases", "Contratista", "Código OD", "Firmada",
                 "Comentario"],
                [[f["fecha"], f["quincena"], f["transporte"], f["ruta"], f["ruta_liquidada"], f["dummy"], f["recarga"],
                  f["excepcion"], f["ruta_fija"], f["spot1"], f["spot_inc"], f["envases"], f["contratista"],
                  f["cod_od"], f["firmada"], f["comentario"]] for f in filas])


@bp.route("/ausentismos")
@requiere()
def ausentismos():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    por_motivo = rows("""SELECT COALESCE(motivo, '(sin motivo)') AS motivo, COUNT(*) AS n FROM ausentismos
                         WHERE periodo=:p GROUP BY COALESCE(motivo, '(sin motivo)') ORDER BY 2 DESC""", p=p)
    por_ct = rows("""SELECT COALESCE(contratista_n, '(SIN CONTRATISTA)') AS contratista, COUNT(*) AS n FROM ausentismos
                     WHERE periodo=:p GROUP BY COALESCE(contratista_n, '(SIN CONTRATISTA)') ORDER BY 2 DESC""", p=p)
    detalle = rows("""SELECT a.*, u.estado AS uso FROM ausentismos a
                      LEFT JOIN uso_rutas u ON u.fecha=a.fecha AND u.ruta=a.ruta
                      WHERE a.periodo=:p ORDER BY a.fecha DESC, a.ruta""", p=p)
    return render_template("ausentismos.html", p=p, por_motivo=por_motivo, por_ct=por_ct, detalle=detalle,
                           periodos=core.periodos_disponibles())



# ───────────── AJ (agua y jugos): control de la supervisora ─────────────

def _mes_rango(p):
    a, m = map(int, p.split("-"))
    return date(a, m, 1), date(a + (m == 12), 1 if m == 12 else m + 1, 1)


def aj_control(p):
    """Cruce del archivo de la supervisora contra SAP y consigo mismo. Devuelve listas listas para mostrar."""
    ini, fin = _mes_rango(p)
    todas_sin_sap = rows("""SELECT c.fecha, c.transporte, c.ruta, c.transportista, c.recarga, c.reposicion,
                             CASE WHEN EXISTS (SELECT 1 FROM dias d WHERE d.fecha = c.fecha)
                                  THEN 'No está en el archivo diario de ese día (¿número mal escrito?)'
                                  ELSE 'No hay archivo diario válido de ese día' END AS motivo
                      FROM cargas_aj c
                      WHERE c.periodo = :p
                        AND NOT EXISTS (SELECT 1 FROM sap_transportes s WHERE s.transporte = c.transporte)
                        AND NOT EXISTS (SELECT 1 FROM cambios_cabecera x WHERE x.transporte = c.transporte)
                      ORDER BY c.fecha, c.transporte""", p=p)
    # Las recargas y reposiciones se crean después de la facturación del día: el Excel diario no las trae,
    # así que no son un error de la supervisora; solo no se pueden verificar contra SAP.
    sin_sap = [r for r in todas_sin_sap if r["recarga"] != "SI" and r["reposicion"] != "SI"]
    no_verificables = [dict(r, tipo=("Recarga" if r["recarga"] == "SI" else "Reposición"))
                       for r in todas_sin_sap if r["recarga"] == "SI" or r["reposicion"] == "SI"]
    sap_sin_control = rows("""SELECT s.fecha, s.transporte, s.ruta, s.cantidad FROM sap_transportes s
                      WHERE s.fecha >= :ini AND s.fecha < :fin
                        AND s.fecha >= (SELECT MIN(fecha) FROM cargas_aj) AND s.fecha <= (SELECT MAX(fecha) FROM cargas_aj)
                        AND s.ruta IN (SELECT ruta FROM rutas_oficiales WHERE categoria = 'AJ'
                                       UNION SELECT ruta FROM cargas_aj)
                        AND NOT EXISTS (SELECT 1 FROM cargas_aj c WHERE c.transporte = s.transporte)
                        AND NOT EXISTS (SELECT 1 FROM recargas_aj r WHERE r.transporte = s.transporte)
                      ORDER BY s.fecha, s.transporte""", ini=ini, fin=fin)
    en_sap = {r["fecha"]: r["n"] for r in rows("""SELECT s.fecha, COUNT(*) AS n FROM sap_transportes s
                      WHERE s.fecha >= :ini AND s.fecha < :fin
                        AND s.fecha >= (SELECT MIN(fecha) FROM cargas_aj) AND s.fecha <= (SELECT MAX(fecha) FROM cargas_aj)
                        AND s.ruta IN (SELECT ruta FROM rutas_oficiales WHERE categoria = 'AJ'
                                       UNION SELECT ruta FROM cargas_aj)
                      GROUP BY s.fecha""", ini=ini, fin=fin)}
    faltan = {}
    for r in sap_sin_control:
        faltan[r["fecha"]] = faltan.get(r["fecha"], 0) + 1
    dias_sin_control = [dict(fecha=f, transportes_sap=en_sap.get(f, n), sin_registrar=n,
                             situacion="Día completo sin registrar" if n >= en_sap.get(f, n) else "Faltan algunos")
                        for f, n in sorted(faltan.items())]
    recargas = rows("""SELECT r.fecha, r.transporte, r.ruta,
                              'Está en la hoja Recargas pero no en la hoja de Cargas' AS motivo
                       FROM recargas_aj r WHERE r.periodo = :p
                         AND NOT EXISTS (SELECT 1 FROM cargas_aj c WHERE c.transporte = r.transporte)
                       UNION ALL
                       SELECT c.fecha, c.transporte, c.ruta,
                              'Marcada RECARGA en Cargas pero no está en la hoja Recargas'
                       FROM cargas_aj c WHERE c.periodo = :p AND c.recarga = 'SI'
                         AND NOT EXISTS (SELECT 1 FROM recargas_aj r WHERE r.transporte = c.transporte)
                       ORDER BY 1, 2""", p=p)
    avisos = rows("""SELECT fecha, transporte, ruta, aviso FROM cargas_aj
                     WHERE periodo = :p AND aviso IS NOT NULL ORDER BY fecha""", p=p)
    fecha_dif = rows("""SELECT c.transporte, c.ruta, c.fecha AS fecha_control, s.fecha AS fecha_sap
                        FROM cargas_aj c JOIN sap_transportes s ON s.transporte = c.transporte
                        WHERE c.periodo = :p AND c.fecha <> s.fecha ORDER BY c.fecha""", p=p)
    cantidad_dif = rows("""SELECT c.fecha, c.transporte, c.ruta, c.cantidad AS cajas_control, s.cantidad AS cajas_sap
                           FROM cargas_aj c JOIN sap_transportes s ON s.transporte = c.transporte
                           WHERE c.periodo = :p AND c.cantidad IS NOT NULL AND s.cantidad IS NOT NULL
                             AND c.cantidad <> s.cantidad ORDER BY c.fecha""", p=p)
    return dict(sin_sap=sin_sap, no_verificables=no_verificables, dias_sin_control=dias_sin_control,
                sap_sin_control=sap_sin_control, recargas=recargas, avisos=avisos,
                fecha_dif=fecha_dif, cantidad_dif=cantidad_dif)


def aj_resumen_cargas(p):
    k = one("""SELECT COUNT(*) AS total,
                      SUM(CASE WHEN carga_dia = 'SI' THEN 1 ELSE 0 END) AS carga_dia,
                      SUM(CASE WHEN dummy = 'SI' THEN 1 ELSE 0 END) AS dummys,
                      SUM(CASE WHEN recarga = 'SI' THEN 1 ELSE 0 END) AS recargas,
                      SUM(CASE WHEN reposicion = 'SI' THEN 1 ELSE 0 END) AS reposiciones,
                      COUNT(DISTINCT fecha) AS dias, COUNT(DISTINCT ruta) AS rutas
               FROM cargas_aj WHERE periodo = :p""", p=p) or {}
    k = {a: (b or 0) for a, b in k.items()}
    rec = one("""SELECT COUNT(*) AS n, SUM(envases) AS envases, SUM(racks) AS racks
                 FROM recolecciones_aj WHERE periodo = :p""", p=p) or {}
    k["recolecciones"] = rec.get("n") or 0
    k["envases"] = rec.get("envases") or 0
    k["racks"] = rec.get("racks") or 0
    return k


def aj_por_transportista(p):
    """Mismo formato que Dummys y excepciones: por transportista y quincena (1 = días 1–15, 2 = 16 en adelante)."""
    nombres = {r["cod"]: r["nombre"] for r in rows(
        "SELECT cod_sap AS cod, MAX(transportista) AS nombre FROM rutas_oficiales GROUP BY cod_sap")}
    out = {}
    for f in rows("""SELECT fecha, transportista, carga_dia, dummy, recarga, reposicion
                     FROM cargas_aj WHERE periodo = :p""", p=p):
        cod = f["transportista"] or "(sin código)"
        t = out.setdefault(cod, dict(codigo=cod, nombre=nombres.get(cod), d1=0, d2=0, dummys=0, recargas=0,
                                     reposiciones=0, carga_dia=0, total=0))
        t["total"] += 1
        if f["dummy"] == "SI":
            t["d1" if f["fecha"].day <= 15 else "d2"] += 1
            t["dummys"] += 1
        t["recargas"] += f["recarga"] == "SI"
        t["reposiciones"] += f["reposicion"] == "SI"
        t["carga_dia"] += f["carga_dia"] == "SI"
    lista = sorted(out.values(), key=lambda t: -t["total"])
    tot = {k: sum(t[k] for t in lista) for k in ("d1", "d2", "dummys", "recargas", "reposiciones", "carga_dia", "total")}
    return lista, tot


@bp.route("/aj")
@requiere()
def aj():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    k = aj_resumen_cargas(p)
    por_ruta = rows("""SELECT c.ruta, COUNT(*) AS total,
                       SUM(CASE WHEN c.carga_dia = 'SI' THEN 1 ELSE 0 END) AS carga_dia,
                       SUM(CASE WHEN c.dummy = 'SI' THEN 1 ELSE 0 END) AS dummys,
                       SUM(CASE WHEN c.recarga = 'SI' THEN 1 ELSE 0 END) AS recargas,
                       SUM(CASE WHEN c.reposicion = 'SI' THEN 1 ELSE 0 END) AS reposiciones,
                       (SELECT COUNT(*) FROM recolecciones_aj r WHERE r.periodo = :p AND r.ruta = c.ruta) AS recolecciones
                       FROM cargas_aj c WHERE c.periodo = :p GROUP BY c.ruta ORDER BY 2 DESC, 1""", p=p)
    por_transp, tot = aj_por_transportista(p)
    solo_inc = request.args.get("todas") != "1"
    return render_template("aj.html", p=p, k=k, uso=kpis_uso(p, "AJ"), por_ruta=por_ruta, por_transp=por_transp,
                           tot=tot, m=matriz_datos(p, cat="AJ", ct=None, solo_inc=solo_inc), solo_inc=solo_inc,
                           recolecciones=rows("SELECT * FROM recolecciones_aj WHERE periodo = :p ORDER BY fecha, ruta", p=p),
                           ctl=aj_control(p) if k["total"] else {}, periodos=core.periodos_disponibles())


def aus_resumen(p):
    """Qué pasó con los ausentismos registrados: ¿explican una ruta no usada o la ruta salió igual?"""
    r = one("""SELECT COUNT(*) AS total,
                      SUM(CASE WHEN u.estado = 'NO_USADA' THEN 1 ELSE 0 END) AS explican,
                      SUM(CASE WHEN u.estado IS NOT NULL AND u.estado <> 'NO_USADA' THEN 1 ELSE 0 END) AS cubiertos,
                      SUM(CASE WHEN u.ruta IS NULL THEN 1 ELSE 0 END) AS sin_ruta
               FROM ausentismos a LEFT JOIN uso_rutas u ON u.fecha = a.fecha AND u.ruta = a.ruta
               WHERE a.periodo = :p""", p=p) or {}
    return {a: (b or 0) for a, b in r.items()}


@bp.route("/explicar")
@requiere()
def explicar():
    """Vista unificada: todo lo que hay que explicar del mes, ordenado por gravedad."""
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    hall = hallazgos(p)
    grupos = dict(
        graves=[h for h in hall if h["cantidad"] and h["severidad"] == "alta"],
        revisar=[h for h in hall if h["cantidad"] and h["severidad"] == "media"],
        info=[h for h in hall if h["cantidad"] and h["severidad"] == "info"],
        ok=[h for h in hall if not h["cantidad"]])
    aj_k = aj_resumen_cargas(p)
    return render_template("explicar.html", p=p, gc=kpis_uso(p, "GC"), ajuso=kpis_uso(p, "AJ"), aj=aj_k,
                           aus=aus_resumen(p), grupos=grupos, cerrado=core.cerrado(p),
                           periodos=core.periodos_disponibles())

@bp.route("/export/aj.csv")
@requiere()
def export_aj():
    p = core.periodo_actual()
    filas = rows("SELECT * FROM cargas_aj WHERE periodo = :p ORDER BY fecha, transporte", p=p)
    return _csv(f"cargas_aj_{p}.csv",
                ["Fecha", "Transporte", "Ruta", "Carga del día", "Dummy", "Recarga", "Reposición", "Viaje", "Cajas",
                 "Transportista", "Aviso"],
                [[f["fecha"], f["transporte"], f["ruta"], f["carga_dia"], f["dummy"], f["recarga"], f["reposicion"],
                  f["viaje"], f["cantidad"], f["transportista"], f["aviso"]] for f in filas])


# ───────────── calidad de datos / hallazgos ─────────────

def hallazgos(p):
    """Controles automáticos del periodo. Cada uno trae su detalle para revisarlo."""
    out = []

    def add(codigo, titulo, severidad, ayuda, cols, filas):
        out.append(dict(codigo=codigo, titulo=titulo, severidad=severidad, ayuda=ayuda, cols=cols,
                        filas=filas[:300], cantidad=len(filas)))

    f = rows("""SELECT e.fecha, e.transporte, e.contratista, e.ruta_liquidada, e.comentario FROM excepciones e
                WHERE e.periodo=:p AND e.transporte IN (SELECT transporte FROM excepciones WHERE transporte IS NOT NULL
                GROUP BY transporte HAVING COUNT(*) > 1) ORDER BY e.transporte, e.fecha""", p=p)
    add("dup", "Transportes repetidos en Excepciones", "alta",
        "El mismo transporte aparece más de una vez: riesgo de pagar dos veces.",
        ["fecha", "transporte", "contratista", "ruta_liquidada", "comentario"], f)

    f = rows("""SELECT fecha, contratista, ruta, ruta_liquidada, comentario FROM excepciones
                WHERE periodo=:p AND transporte IS NULL ORDER BY fecha""", p=p)
    add("sin_transp", "Filas de Excepciones sin número de transporte", "media",
        "Sin transporte no se puede comprobar el pago contra SAP.",
        ["fecha", "contratista", "ruta", "ruta_liquidada", "comentario"], f)

    f = rows("""SELECT e.fecha, e.transporte, e.contratista, e.ruta_liquidada, e.comentario FROM excepciones e
                WHERE e.periodo=:p AND e.transporte IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM sap_transportes s WHERE s.transporte = e.transporte)
                  AND NOT EXISTS (SELECT 1 FROM cambios_cabecera c WHERE c.transporte = e.transporte)
                  AND EXISTS (SELECT 1 FROM dias d WHERE d.fecha = e.fecha)
                ORDER BY e.fecha""", p=p)
    add("sin_sap", "Pagos sin respaldo en los archivos diarios (SAP)", "alta",
        "El transporte no aparece en ningún archivo diario cargado, aunque ese día sí hay archivo.",
        ["fecha", "transporte", "contratista", "ruta_liquidada", "comentario"], f)

    f = rows("""SELECT e.fecha, e.transporte, e.contratista, e.ruta_liquidada AS en_excepciones, c.nueva_ruta AS en_sap
                FROM excepciones e JOIN cambios_cabecera c ON c.transporte = e.transporte AND c.fecha = e.fecha
                WHERE e.periodo=:p AND e.ruta_liquidada IS NOT NULL AND c.nueva_ruta IS NOT NULL
                  AND e.ruta_liquidada <> c.nueva_ruta ORDER BY e.fecha""", p=p)
    add("ruta_dif", "La ruta liquidada no coincide con el cambio de cabecera", "media",
        "Excepciones dice una ruta y el archivo diario (cambio de cabecera) otra.",
        ["fecha", "transporte", "contratista", "en_excepciones", "en_sap"], f)

    f = rows("""SELECT fecha, transporte, contratista, ruta_liquidada, comentario FROM excepciones
                WHERE periodo=:p AND dummy='SI' AND firmada IS NULL ORDER BY fecha""", p=p)
    add("sin_firma", "Dummys sin firma de conformidad", "alta",
        "La columna FIRMADA está vacía: no hay constancia de que la dummy se cobró/recibió conforme.",
        ["fecha", "transporte", "contratista", "ruta_liquidada", "comentario"], f)

    a, m = map(int, p.split("-"))
    ult = min(date.today(), date(a + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1))
    con_archivo = {r["fecha"] for r in rows("SELECT fecha FROM dias")}
    faltan, d = [], date(a, m, 1)
    while d <= ult:
        if d not in con_archivo:
            faltan.append(dict(fecha=d, dia=core.DIAS_SEM[d.weekday()],
                               nota="domingo (puede no haber operación)" if d.weekday() == 6 else "falta el archivo"))
        d += timedelta(days=1)
    add("sin_diario", "Días sin archivo diario", "media",
        "Sin el archivo del día no se puede validar el uso de rutas ni el respaldo de los pagos.",
        ["fecha", "dia", "nota"], faltan)

    f = rows("""SELECT v.ruta, COUNT(*) AS dias, MAX(v.contratista) AS contratista, MAX(v.tipo) AS tipo
                FROM validacion_diaria v
                WHERE v.fecha >= :ini AND v.fecha < :fin AND v.tipo IN ('FIJA','FIJA TEMPORADA','FLETERA FIJA')
                  AND EXISTS (SELECT 1 FROM rutas_oficiales o WHERE o.periodo = :p)
                  AND NOT EXISTS (SELECT 1 FROM rutas_oficiales o WHERE o.periodo = :p AND o.ruta = v.ruta)
                GROUP BY v.ruta ORDER BY v.ruta""",
             ini=date(a, m, 1), fin=date(a + (m == 12), 1 if m == 12 else m + 1, 1), p=p)
    add("fija_no_oficial", "Rutas marcadas FIJA en el Excel diario que no están en la lista oficial del mes", "media",
        "El archivo diario las trata como fijas pero el archivo mensual de rutas activas no las incluye.",
        ["ruta", "contratista", "tipo", "dias"], f)

    extra, prm = _filtros_uso(" AND u.estado = 'NO_USADA' AND j.id IS NULL")
    f = rows(f"SELECT u.fecha, u.ruta, u.contratista, u.nota_excel {USO}{extra} ORDER BY u.fecha, u.ruta", **{**prm, "p": p})
    add("pend", "Rutas fijas sin uso y sin justificación", "alta",
        "Ruta activa que no salió y de la que aún no se explica por qué.",
        ["fecha", "ruta", "contratista", "nota_excel"], f)

    f = rows("""SELECT j.fecha, j.ruta, m.nombre AS motivo FROM justificaciones j
                JOIN motivos m ON m.id = j.motivo_id
                WHERE m.requiere_evidencia = :t AND j.fecha >= :ini AND j.fecha < :fin
                  AND NOT EXISTS (SELECT 1 FROM evidencias e WHERE e.justificacion_id = j.id) ORDER BY j.fecha""",
             t=True, ini=date(a, m, 1), fin=date(a + (m == 12), 1 if m == 12 else m + 1, 1))
    add("sin_evid", "Justificaciones que exigen evidencia y no la tienen", "alta",
        "El motivo requiere soporte (por ejemplo incapacidad) y no hay archivo adjunto.",
        ["fecha", "ruta", "motivo"], f)

    f = rows("""SELECT ruta, tipo AS tipo_archivo, estado, mensaje FROM archivos
                WHERE estado = 'ERROR' OR mensaje LIKE '%El nombre dice%' OR mensaje LIKE '%copia sin actualizar%'
                ORDER BY ruta""")
    add("archivos", "Archivos con errores, copias sin actualizar o nombre distinto a su contenido", "media",
        "Revisa que el archivo correcto esté cargado. No depende del periodo: aplica a toda la carpeta.",
        ["ruta", "tipo_archivo", "estado", "mensaje"], f)


    if scalar("SELECT COUNT(*) FROM cargas_aj WHERE periodo = :p", p=p):
        c = aj_control(p)
        add("aj_sin_sap", "AJ: cargas de la supervisora que no están en SAP", "alta",
            "Aparecen en el control de AJ pero no en ningún archivo diario: número mal escrito o archivo del día faltante.",
            ["fecha", "transporte", "ruta", "motivo"], c["sin_sap"])
        add("aj_dias", "AJ: días en que SAP tiene cargas de AJ y el control no las registra", "alta",
            "Suele significar que ese día no se llenó el control de la supervisora.",
            ["fecha", "transportes_sap", "sin_registrar", "situacion"], c["dias_sin_control"])
        add("aj_sap_sin_control", "AJ: transportes de SAP que no están en el control de la supervisora", "alta",
            "Salió una carga en una ruta de AJ y no quedó registrada en su control.",
            ["fecha", "transporte", "ruta", "cantidad"], c["sap_sin_control"])
        add("aj_no_verif", "AJ: recargas y reposiciones que no se pueden comprobar en SAP", "info",
            "El Excel diario no trae las recargas/reposiciones; para respaldarlas hace falta el reporte de transportes de SAP.",
            ["fecha", "transporte", "ruta", "tipo"], c["no_verificables"])
        add("aj_recargas", "AJ: la hoja Recargas no coincide con las cargas marcadas como recarga", "media",
            "Las dos hojas deben decir lo mismo.", ["fecha", "transporte", "ruta", "motivo"], c["recargas"])
        add("aj_fecha", "AJ: fecha del control distinta a la de SAP", "media",
            "El mismo transporte está en otro día en el Excel diario.",
            ["transporte", "ruta", "fecha_control", "fecha_sap"], c["fecha_dif"])
        add("aj_cantidad", "AJ: cajas del control distintas a las de SAP", "media",
            "El mismo transporte tiene otra cantidad en SAP.",
            ["fecha", "transporte", "ruta", "cajas_control", "cajas_sap"], c["cantidad_dif"])
        add("aj_valores", "AJ: valores corregidos al leer el archivo (errores de captura)", "media",
            "Por ejemplo «N0» con cero en lugar de «NO». Corrígelos en el Excel.",
            ["fecha", "transporte", "ruta", "aviso"], c["avisos"])

    if core.cerrado(p):
        add("post_cierre", "Archivos modificados después del cierre del mes", "alta",
            "Cambió algún Excel que respaldaba el periodo ya cerrado.", ["archivo", "situacion"],
            [dict(archivo=k, situacion=v) for k, v in core.cambios_tras_cierre(p)])
    return out


@bp.route("/calidad")
@requiere()
def calidad():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    return render_template("calidad.html", p=p, hall=hallazgos(p), periodos=core.periodos_disponibles())


@bp.route("/export/hallazgos.csv")
@requiere()
def export_hallazgos():
    p = core.periodo_actual()
    h = next((x for x in hallazgos(p) if x["codigo"] == request.args.get("c")), None) or abort(404)
    return _csv(f"hallazgo_{h['codigo']}_{p}.csv", h["cols"], [[f.get(c) for c in h["cols"]] for f in h["filas"]])


# ───────────── archivos y días ─────────────

@bp.route("/archivos")
@requiere()
def archivos():
    lista = rows("""SELECT id, ruta, tipo, periodo, fecha, filas, estado, mensaje, importado_en, tam
                    FROM archivos ORDER BY CASE estado WHEN 'ERROR' THEN 0 WHEN 'DUPLICADO' THEN 1 ELSE 2 END,
                    tipo, ruta""")
    resumen = rows("SELECT tipo, estado, COUNT(*) AS n FROM archivos GROUP BY tipo, estado ORDER BY tipo, estado")
    return render_template("archivos.html", lista=lista, resumen=resumen,
                           ultimas=rows("SELECT * FROM importaciones ORDER BY id DESC LIMIT 8"))


@bp.route("/dias", methods=["GET", "POST"])
@requiere("consulta")
def dias():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    if request.method == "POST":
        if not core.puede("captura"):
            abort(403)
        f = _fecha_o_404(request.form.get("fecha", ""))
        if not core.bloquear_si_cerrado(f):
            op = request.form.get("operativo") == "1"
            nota = request.form.get("nota", "").strip()[:200]
            if request.form.get("quitar"):
                execute("DELETE FROM dias_config WHERE fecha=:f", f=f)
                log_accion("dia_automatico", "dia", f.isoformat())
            else:
                execute("DELETE FROM dias_config WHERE fecha=:f", f=f)
                execute("INSERT INTO dias_config (fecha, operativo, nota, por, en) VALUES (:f,:o,:n,:u,:e)",
                        f=f, o=op, n=nota, u=core.usuario_actual()["usuario"], e=datetime.now())
                log_accion("dia_manual", "dia", f.isoformat(), dict(operativo=op, nota=nota))
            flash("Día actualizado y recalculado.", "ok")
        return redirect(url_for("web.dias", p=p))
    lista = rows("""SELECT d.fecha, d.transportes, d.auto_operativo, dc.operativo AS manual, dc.nota, dc.por,
                           COALESCE(dc.operativo, d.auto_operativo) AS efectivo,
                           (SELECT SUM(CASE WHEN estado='NO_USADA' THEN 1 ELSE 0 END) FROM uso_rutas x
                             WHERE x.fecha=d.fecha) AS no_usadas,
                           (SELECT COUNT(*) FROM uso_rutas x WHERE x.fecha=d.fecha) AS total
                    FROM dias d LEFT JOIN dias_config dc ON dc.fecha=d.fecha
                    WHERE d.fecha >= :ini AND d.fecha < :fin ORDER BY d.fecha""",
                 ini=date(int(p[:4]), int(p[5:]), 1),
                 fin=date(int(p[:4]) + (p[5:] == "12"), 1 if p[5:] == "12" else int(p[5:]) + 1, 1))
    return render_template("dias.html", p=p, lista=lista, periodos=core.periodos_disponibles(),
                           cerrado=core.cerrado(p))


# ───────────── cierre de mes ─────────────

@bp.route("/cierre", methods=["GET", "POST"])
@requiere("admin")
def cierre():
    if request.method == "POST":
        p = request.form.get("p", "")
        accion = request.form.get("accion")
        if p not in core.periodos_disponibles():
            abort(400)
        if accion == "cerrar" and not core.cerrado(p):
            extra, prm = _filtros_uso(" AND u.estado = 'NO_USADA' AND j.id IS NULL")
            pend = scalar(f"SELECT COUNT(*) {USO}{extra}", **{**prm, "p": p}) or 0
            if pend and request.form.get("forzar") != "1":
                flash(f"Hay {pend} rutas sin justificar. Marca la casilla para cerrar de todos modos.", "error")
            else:
                execute("""INSERT INTO cierres (periodo, cerrado_por, cerrado_en, nota, huellas)
                           VALUES (:p, :u, :c, :n, :h)""",
                        p=p, u=core.usuario_actual()["usuario"], c=datetime.now(),
                        n=request.form.get("nota", "").strip() or None,
                        h=json.dumps(core.huellas_periodo(p)))
                log_accion("cerrar_periodo", "periodo", p, dict(pendientes=pend))
                flash(f"{core.periodo_label(p)} cerrado.", "ok")
        elif accion == "reabrir" and core.cerrado(p):
            motivo = request.form.get("nota", "").strip()
            if len(motivo) < 10:
                flash("Para reabrir explica el motivo (mínimo 10 caracteres).", "error")
            else:
                execute("DELETE FROM cierres WHERE periodo=:p", p=p)
                log_accion("reabrir_periodo", "periodo", p, dict(motivo=motivo))
                flash(f"{core.periodo_label(p)} reabierto.", "ok")
        return redirect(url_for("web.cierre"))
    lista = []
    for p in core.periodos_disponibles():
        c = one("SELECT * FROM cierres WHERE periodo=:p", p=p)
        extra, prm = _filtros_uso(" AND u.estado = 'NO_USADA' AND j.id IS NULL")
        lista.append(dict(periodo=p, cierre=c,
                          pendientes=scalar(f"SELECT COUNT(*) {USO}{extra}", **{**prm, "p": p}) or 0,
                          cambios=core.cambios_tras_cierre(p) if c else []))
    return render_template("cierre.html", lista=lista)


# ───────────── reporte de auditoría ─────────────

@bp.route("/reporte")
@requiere()
def reporte():
    p = core.periodo_actual()
    if not p:
        return render_template("vacio.html")
    sql, pr = _no_usadas("todas", "")
    por_transp, tot = aj_por_transportista(p)
    aj_k = aj_resumen_cargas(p)
    return render_template(
        "reporte.html", p=p, gc=kpis_uso(p, "GC"), ajuso=kpis_uso(p, "AJ"), aj=aj_k, aus=aus_resumen(p),
        mg=matriz_datos(p, cat="GC", ct=None), ma=matriz_datos(p, cat="AJ", ct=None),
        detalle=rows(sql + " ORDER BY u.ruta, u.fecha", **pr), resumen=dummies_resumen(p),
        por_transp=por_transp, tot=tot, ctl=aj_control(p) if aj_k["total"] else {}, hall=hallazgos(p),
        cierre=one("SELECT * FROM cierres WHERE periodo=:p", p=p), ahora=datetime.now())


def dummies_resumen(p):
    return rows("""SELECT COALESCE(contratista_n, '(SIN CONTRATISTA)') AS contratista,
                    SUM(CASE WHEN dummy='SI' AND quincena=1 THEN 1 ELSE 0 END) AS d1,
                    SUM(CASE WHEN dummy='SI' AND quincena=2 THEN 1 ELSE 0 END) AS d2,
                    SUM(CASE WHEN dummy='SI' THEN 1 ELSE 0 END) AS dummys,
                    SUM(CASE WHEN excepcion='SI' THEN 1 ELSE 0 END) AS excepciones,
                    SUM(CASE WHEN recarga='SI' THEN 1 ELSE 0 END) AS recargas
                  FROM excepciones WHERE periodo=:p GROUP BY COALESCE(contratista_n, '(SIN CONTRATISTA)')
                  ORDER BY 4 DESC""", p=p)


# ───────────── administración ─────────────

@bp.route("/bitacora")
@requiere("admin")
def bitacora():
    u = request.args.get("u", "").strip()
    a = request.args.get("a", "").strip()
    where, prm = "WHERE 1=1", {}
    if u:
        where += " AND usuario = :u"
        prm["u"] = u
    if a:
        where += " AND accion = :a"
        prm["a"] = a
    total = scalar(f"SELECT COUNT(*) FROM bitacora {where}", **prm) or 0
    pg, paginas, off = _pagina(total)
    lista = rows(f"SELECT * FROM bitacora {where} ORDER BY id DESC LIMIT {PAGINA} OFFSET {off}", **prm)
    return render_template("bitacora.html", lista=lista, total=total, pg=pg, paginas=paginas, u=u, a=a,
                           usuarios=[r["usuario"] for r in rows("SELECT usuario FROM usuarios ORDER BY 1")],
                           acciones=[r["accion"] for r in rows("SELECT DISTINCT accion FROM bitacora ORDER BY 1")])


@bp.route("/usuarios", methods=["GET", "POST"])
@requiere("admin")
def usuarios():
    if request.method == "POST":
        accion = request.form.get("accion")
        yo = core.usuario_actual()
        if accion == "crear":
            usuario = request.form.get("usuario", "").strip().lower()
            clave = request.form.get("clave", "")
            rol = request.form.get("rol")
            if not usuario or rol not in core.NIVEL or len(clave) < 10:
                flash("Datos incompletos. La contraseña debe tener al menos 10 caracteres.", "error")
            elif one("SELECT 1 AS x FROM usuarios WHERE usuario=:u", u=usuario):
                flash("Ese usuario ya existe.", "error")
            else:
                execute("""INSERT INTO usuarios (usuario, nombre, rol, hash, activo, creado_en)
                           VALUES (:u, :n, :r, :h, :a, :c)""",
                        u=usuario, n=request.form.get("nombre", "").strip() or usuario, r=rol,
                        h=generate_password_hash(clave), a=True, c=datetime.now())
                log_accion("crear_usuario", "usuario", usuario, dict(rol=rol))
                flash("Usuario creado.", "ok")
        elif accion in ("activar", "desactivar", "clave", "rol"):
            uid = request.form.get("id", type=int)
            if accion == "desactivar" and uid == yo["id"]:
                flash("No puedes desactivarte a ti mismo.", "error")
            elif accion == "rol" and uid == yo["id"]:
                flash("No puedes cambiar tu propio rol.", "error")
            elif accion == "clave":
                c = request.form.get("clave", "")
                if len(c) < 10:
                    flash("La contraseña debe tener al menos 10 caracteres.", "error")
                else:
                    execute("UPDATE usuarios SET hash=:h WHERE id=:i", h=generate_password_hash(c), i=uid)
                    log_accion("cambiar_clave", "usuario", uid)
                    flash("Contraseña actualizada.", "ok")
            elif accion == "rol":
                rol = request.form.get("rol")
                if rol in core.NIVEL:
                    execute("UPDATE usuarios SET rol=:r WHERE id=:i", r=rol, i=uid)
                    log_accion("cambiar_rol", "usuario", uid, dict(rol=rol))
                    flash("Rol actualizado.", "ok")
            else:
                execute("UPDATE usuarios SET activo=:a WHERE id=:i", a=accion == "activar", i=uid)
                log_accion(accion + "_usuario", "usuario", uid)
                flash("Usuario actualizado.", "ok")
        return redirect(url_for("web.usuarios"))
    return render_template("usuarios.html", lista=rows("SELECT id, usuario, nombre, rol, activo FROM usuarios ORDER BY id"))


@bp.route("/motivos", methods=["GET", "POST"])
@requiere("admin")
def motivos():
    if request.method == "POST":
        if request.form.get("accion") == "crear":
            n = request.form.get("nombre", "").strip()
            if n and not one("SELECT 1 AS x FROM motivos WHERE nombre=:n", n=n):
                execute("INSERT INTO motivos (nombre, requiere_evidencia, activo) VALUES (:n, :e, :a)",
                        n=n, e=request.form.get("requiere_evidencia") == "1", a=True)
                log_accion("crear_motivo", "motivo", n)
        else:
            mid = request.form.get("id", type=int)
            execute("UPDATE motivos SET requiere_evidencia=:e, activo=:a WHERE id=:i",
                    e=request.form.get("requiere_evidencia") == "1", a=request.form.get("activo") == "1", i=mid)
            log_accion("editar_motivo", "motivo", mid)
        return redirect(url_for("web.motivos"))
    return render_template("motivos.html", lista=rows("""SELECT m.*, (SELECT COUNT(*) FROM justificaciones j
                           WHERE j.motivo_id = m.id) AS usos FROM motivos m ORDER BY m.id"""))


# ───────────── respaldos ─────────────

@bp.route("/respaldos")
@requiere("admin")
def respaldos():
    conteos = {t: scalar(f"SELECT COUNT(*) FROM {t}") for t in respaldo.TABLAS_APP}
    ultimos = rows("SELECT ts, usuario, detalle FROM bitacora WHERE accion = 'respaldo' ORDER BY id DESC LIMIT 8")
    return render_template("respaldos.html", conteos=conteos, ultimos=ultimos, motor=engine_name())


def engine_name():
    return core.engine().dialect.name


@bp.route("/respaldos/descargar", methods=["POST"])
@requiere("admin")
def respaldo_descargar():
    solo_app = request.form.get("alcance") == "app"
    buf = io.BytesIO()
    manifest = respaldo.exportar(core.engine(), buf, solo_app=solo_app)
    datos = buf.getvalue()
    log_accion("respaldo", "base", None, dict(alcance=manifest["alcance"], bytes=len(datos),
                                              filas=sum(t["filas"] for t in manifest["tablas"].values())))
    nombre = f"respaldo_transportes_{datetime.now():%Y%m%d_%H%M}{'_app' if solo_app else ''}.zip"
    return Response(datos, mimetype="application/zip", headers={"Content-Disposition": f"attachment; filename={nombre}"})
