"""Importación incremental de los Excel a la base de datos.

- Solo reprocesa archivos nuevos o modificados (por hash).
- Si dos archivos dicen lo mismo (copias, "respaldo", mes equivocado), conserva uno y marca el otro DUPLICADO.
- Al final recalcula el cruce rutas activas vs. transportes reales.
"""
import hashlib
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, update

from .. import db
from . import parsers as P
from .analisis import recalcular

IGNORAR_DIRS = {".git", "__pycache__", "db", "transportes", ".venv", "venv", "node_modules"}
TABLAS_POR_ARCHIVO = [db.sap_transportes, db.cambios_cabecera, db.validacion_diaria, db.excepciones,
                      db.ausentismos, db.rutas_oficiales, db.rutas_desactivadas, db.cargas_aj, db.recargas_aj,
                      db.recolecciones_aj]
TIPOS_CON_FILAS = ("DIARIO", "EXCEPCIONES", "AUSENTISMOS", "RUTAS_FIJAS", "CARGAS_AJ")


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def descubrir(carpeta: Path):
    for raiz, dirs, files in os.walk(carpeta):
        dirs[:] = [d for d in dirs if d not in IGNORAR_DIRS and not d.startswith(".")]
        for n in sorted(files):
            if n.lower().endswith((".xlsx", ".xlsm")) and not n.startswith("~$"):
                yield Path(raiz) / n


def clasificar(path: Path):
    """Tipo según el nombre y, si hace falta, las hojas que contiene."""
    n = P.hnorm(path.name)
    if "BASE DE DETALLE" in n:
        return "OMITIDO" if ("RESPALDO" in n or "COPIA" in n) else "DIARIO"
    if "RUTAS FIJAS" in n:
        return "RUTAS_FIJAS"
    if "CARGAS" in n and "AJ" in n.split():
        return "CARGAS_AJ"
    try:
        wb = P.abrir(path)
        hojas = {P.hnorm(h) for h in wb.sheetnames}
        wb.close()
    except Exception:
        return "OTRO"
    if "DDBB" in hojas:
        return "EXCEPCIONES"
    if "RECARGAS" in hojas and any(h.startswith("CARGAS") for h in hojas):
        return "CARGAS_AJ"
    if "REGISTROS" in hojas:
        return "AUSENTISMOS"
    return "OTRO"


def _borrar_filas(con, archivo_id):
    for t in TABLAS_POR_ARCHIVO:
        con.execute(delete(t).where(t.c.archivo_id == archivo_id))


def _insertar(con, tabla, filas, **extra):
    if not filas:
        return
    lote = [{**f, **extra} for f in filas]
    con.execute(insert(tabla), lote)


def _cargar(con, archivo_id, tipo, datos):
    """Inserta las filas parseadas. Devuelve la cantidad de filas."""
    if tipo == "DIARIO":
        _insertar(con, db.sap_transportes, datos["fact"], archivo_id=archivo_id)
        _insertar(con, db.cambios_cabecera, datos["cab"], archivo_id=archivo_id)
        _insertar(con, db.validacion_diaria, datos["val"], archivo_id=archivo_id)
        return len(datos["fact"])
    if tipo == "EXCEPCIONES":
        _insertar(con, db.excepciones, datos["rows"], archivo_id=archivo_id)
        return len(datos["rows"])
    if tipo == "AUSENTISMOS":
        _insertar(con, db.ausentismos, datos["rows"], archivo_id=archivo_id)
        return len(datos["rows"])
    if tipo == "CARGAS_AJ":
        _insertar(con, db.cargas_aj, datos["cargas"], archivo_id=archivo_id)
        _insertar(con, db.recargas_aj, datos["recargas"], archivo_id=archivo_id)
        _insertar(con, db.recolecciones_aj, datos["recolec"], archivo_id=archivo_id)
        return len(datos["cargas"])
    if tipo == "RUTAS_FIJAS":
        n = 0
        for periodo, filas in datos["por_periodo"].items():
            lote = [dict(periodo=periodo, **{k: v for k, v in f.items() if k not in ("anio", "mes")})
                    for f in filas]
            # una ruta repetida dentro del mismo mes/archivo: se queda la primera
            vistos, unicas = set(), []
            for r in lote:
                if r["ruta"] not in vistos:
                    vistos.add(r["ruta"])
                    unicas.append(r)
            _insertar(con, db.rutas_oficiales, unicas, archivo_id=archivo_id)
            n += len(unicas)
        _insertar(con, db.rutas_desactivadas,
                  [dict(periodo=sorted(datos["por_periodo"])[-1], **d) for d in datos["desactivadas"]],
                  archivo_id=archivo_id)
        return n
    return 0


def _parsear(tipo, path):
    return {"DIARIO": P.parse_diario, "EXCEPCIONES": P.parse_excepciones,
            "AUSENTISMOS": P.parse_ausentismos, "RUTAS_FIJAS": P.parse_rutas_fijas,
            "CARGAS_AJ": P.parse_cargas_aj}[tipo](path)


def _mes_carpeta(path: Path):
    for padre in path.parents:
        mm = re.match(r"M(\d+)", padre.name)
        if mm:
            return int(mm[1])
    return None


def _meta_y_claves(tipo, datos, path: Path):
    """(periodo, fecha, meta) detectados por CONTENIDO."""
    meta = {"mes_carpeta": _mes_carpeta(path)}
    if tipo == "DIARIO":
        fecha = datos["fecha"]
        m = re.match(r"(\d\d)-(\d\d)-(\d{4})", path.name)
        if m:
            nombre = f"{m[3]}-{m[2]}-{m[1]}"
            if nombre != fecha.isoformat():
                meta["aviso_nombre"] = f"El nombre dice {nombre} pero el contenido es del {fecha.isoformat()}"
        meta["avisos"] = datos["avisos"]
        mm = re.match(r"M(\d+)\b", path.parent.name)
        meta["mes_carpeta"] = int(mm[1]) if mm else None
        return P.periodo_de(fecha), fecha, meta
    if tipo in ("EXCEPCIONES", "AUSENTISMOS"):
        meta["max_fecha"] = datos["max_fecha"].isoformat()
        mes = int(datos["periodo"][5:])
        if meta["mes_carpeta"] and meta["mes_carpeta"] != mes:
            meta["aviso_periodo"] = (f"Está en la carpeta del mes {meta['mes_carpeta']} pero sus datos son de "
                                     f"{datos['periodo']}: parece una copia sin actualizar")
        return datos["periodo"], None, meta
    if tipo == "CARGAS_AJ":
        meta["max_fecha"] = datos["max_fecha"].isoformat()
        meta["min_fecha"] = datos["min_fecha"].isoformat()
        meta["periodos"] = datos["periodos"]
        return datos["periodos"][-1], None, meta
    if tipo == "RUTAS_FIJAS":
        periodos = sorted(datos["por_periodo"])
        meta["periodos"] = periodos
        return periodos[-1], None, meta
    return None, None, meta


def procesar(con, carpeta: Path, path: Path, forzar: bool, log, solo=None):
    rel = path.relative_to(carpeta).as_posix()
    st = path.stat()
    h = _hash(path)
    previo = con.execute(select(db.archivos).where(db.archivos.c.ruta == rel)).mappings().first()
    if previo and previo["hash"] == h and previo["estado"] in ("OK", "DUPLICADO", "OMITIDO") and not forzar:
        return "sin cambios"

    tipo = clasificar(path)
    if solo and tipo not in solo:
        return "sin cambios"
    base = dict(ruta=rel, tipo="OTRO" if tipo == "OMITIDO" else tipo, hash=h, tam=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime), importado_en=datetime.now())
    if previo:
        archivo_id = previo["id"]
        _borrar_filas(con, archivo_id)
    else:
        archivo_id = con.execute(insert(db.archivos).values(**base, estado="OK")).inserted_primary_key[0]

    if tipo in ("OMITIDO", "OTRO"):
        motivo = ("Copia/respaldo: no se importa" if tipo == "OMITIDO"
                  else "No es un archivo de la operación (sin hojas reconocibles)")
        con.execute(update(db.archivos).where(db.archivos.c.id == archivo_id).values(
            **base, estado="OMITIDO", mensaje=motivo, filas=0, periodo=None, fecha=None, meta=None))
        return "omitido"

    try:
        datos = _parsear(tipo, path)
        periodo, fecha, meta = _meta_y_claves(tipo, datos, path)
        filas = _cargar(con, archivo_id, tipo, datos)
        msg = meta.get("aviso_nombre") or meta.get("aviso_periodo")
        con.execute(update(db.archivos).where(db.archivos.c.id == archivo_id).values(
            **base, periodo=periodo, fecha=fecha, meta=json.dumps(meta), filas=filas, estado="OK", mensaje=msg))
        return f"{tipo} {filas} filas"
    except Exception as e:  # un archivo malo no debe frenar al resto
        _borrar_filas(con, archivo_id)
        con.execute(update(db.archivos).where(db.archivos.c.id == archivo_id).values(
            **base, estado="ERROR", mensaje=f"{type(e).__name__}: {e}"[:500], filas=0))
        return f"ERROR {e}"


def seleccionar_vigentes(con, log):
    """Entre archivos que cubren lo mismo, deja uno y marca el resto DUPLICADO (sin filas)."""
    arch = con.execute(select(db.archivos).where(
        db.archivos.c.tipo.in_(TIPOS_CON_FILAS), db.archivos.c.estado.in_(("OK", "DUPLICADO")))).mappings().all()
    grupos = {}
    for a in arch:
        meta = json.loads(a["meta"] or "{}")
        if a["tipo"] == "DIARIO":
            # gana el archivo cuyo NOMBRE coincide con su contenido y que está en la carpeta de su mes
            clave = ("DIARIO", a["fecha"])
            carpeta_ok = 1 if meta.get("mes_carpeta") == (a["fecha"].month if a["fecha"] else None) else 0
            rango = (0 if meta.get("aviso_nombre") else 1, carpeta_ok, a["filas"] or 0, a["ruta"])
        elif a["tipo"] == "CARGAS_AJ":
            clave = ("CARGAS_AJ", "unico")   # un solo control de AJ vigente: el que llega más lejos
            rango = (1, meta.get("max_fecha", ""), a["filas"] or 0, a["ruta"])
        elif a["tipo"] in ("EXCEPCIONES", "AUSENTISMOS"):
            clave = (a["tipo"], a["periodo"])
            carpeta_ok = 0 if meta.get("aviso_periodo") else 1
            rango = (carpeta_ok, meta.get("max_fecha", ""), a["filas"] or 0, a["ruta"])
        else:
            continue  # RUTAS_FIJAS se resuelve por periodo más abajo
        grupos.setdefault(clave, []).append((rango, a))

    por_reparsear = []
    for clave, lista in grupos.items():
        lista.sort(key=lambda x: x[0], reverse=True)
        ganador = lista[0][1]
        for _, a in lista[1:]:
            if a["estado"] != "DUPLICADO" or a["filas"]:
                _borrar_filas(con, a["id"])
            con.execute(update(db.archivos).where(db.archivos.c.id == a["id"]).values(
                estado="DUPLICADO", filas=0,
                mensaje=" · ".join(x for x in (f"Duplicado de {ganador['ruta']}",
                                               json.loads(a["meta"] or "{}").get("aviso_nombre") or
                                               json.loads(a["meta"] or "{}").get("aviso_periodo")) if x)))
        if ganador["estado"] == "DUPLICADO":
            por_reparsear.append(ganador)

    # Rutas fijas: por periodo gana la hoja visible y, en empate, el archivo más reciente (mayor id)
    ro = db.rutas_oficiales
    filas = con.execute(select(ro.c.periodo, ro.c.archivo_id, ro.c.fuente).distinct()).all()
    mejor = {}
    for periodo, aid, fuente in filas:
        k = (1 if fuente == "VISIBLE" else 0, aid)
        if periodo not in mejor or k > mejor[periodo][0]:
            mejor[periodo] = (k, aid)
    for periodo, (_, aid) in mejor.items():
        con.execute(delete(ro).where(ro.c.periodo == periodo, ro.c.archivo_id != aid))
    return por_reparsear


def importar(carpeta, engine=None, forzar=False, log=print, origen="local", solo=None):
    carpeta = Path(carpeta).resolve()
    engine = engine or db.make_engine()
    db.init_db(engine)
    with engine.begin() as con:
        imp_id = con.execute(insert(db.importaciones).values(
            inicio=datetime.now(), estado="CORRIENDO", origen=origen)).inserted_primary_key[0]
    resumen = {"nuevos": 0, "sin_cambios": 0, "errores": []}
    try:
        archivos = list(descubrir(carpeta))
        log(f"{len(archivos)} archivos Excel encontrados en {carpeta}")
        for i, path in enumerate(archivos, 1):
            with engine.begin() as con:
                r = procesar(con, carpeta, path, forzar, log, solo)
            if r == "sin cambios":
                resumen["sin_cambios"] += 1
            else:
                resumen["nuevos"] += 1
                log(f"[{i}/{len(archivos)}] {path.name}: {r}")
                if r.startswith("ERROR"):
                    resumen["errores"].append(f"{path.name}: {r}")
        with engine.begin() as con:
            pend = seleccionar_vigentes(con, log)
        for a in pend:  # un duplicado pasó a ser el vigente: hay que volver a cargarlo
            with engine.begin() as con:
                procesar(con, carpeta, carpeta / a["ruta"], True, log)
        if pend:
            with engine.begin() as con:
                seleccionar_vigentes(con, log)
        log("Recalculando cruce de rutas…")
        with engine.begin() as con:
            resumen["analisis"] = recalcular(con)
        estado = "OK"
    except Exception:
        estado = "ERROR"
        resumen["traza"] = traceback.format_exc()
        log(resumen["traza"])
    with engine.begin() as con:
        con.execute(update(db.importaciones).where(db.importaciones.c.id == imp_id).values(
            fin=datetime.now(), estado=estado, resumen=json.dumps(resumen, ensure_ascii=False, default=str)))
    log(f"Importación {estado}: {resumen['nuevos']} procesados, {resumen['sin_cambios']} sin cambios, "
        f"{len(resumen['errores'])} con error")
    return estado == "OK"
