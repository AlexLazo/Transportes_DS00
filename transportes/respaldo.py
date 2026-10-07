"""Respaldo y restauración portables entre motores (SQLite local ⇄ PostgreSQL en Railway).

El respaldo es un .zip con un archivo .jsonl por tabla (fechas en ISO, binarios en base64) y un manifest.json
con el conteo y el SHA-256 de cada tabla. Al restaurar se verifica la integridad ANTES de tocar la base.

Dos alcances:
  completo  → todas las tablas (datos importados + datos de la app)
  solo_app  → únicamente lo que no se puede regenerar desde los Excel: usuarios, motivos, justificaciones,
              evidencias, ajustes de días, cierres y bitácora (pesa poco; los datos importados se recrean con
              `python importar_local.py`)
"""
import base64
import hashlib
import io
import json
import zipfile
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, LargeBinary, delete, func, insert, select, text

from . import db

FORMATO = 1
TABLAS_APP = ("usuarios", "motivos", "justificaciones", "evidencias", "dias_config", "cierres", "bitacora")
LOTE = 1000
EXCLUIDAS = ("tareas",)   # estado operativo de procesos en curso: no se respalda ni se pisa al restaurar


class ErrorRespaldo(Exception):
    pass


def _tablas(solo_app: bool):
    """Tablas en orden de dependencias (las referenciadas primero)."""
    return [t for t in db.metadata.sorted_tables
            if t.name not in EXCLUIDAS and (not solo_app or t.name in TABLAS_APP)]


def _a_json(tabla, fila):
    out = {}
    for col in tabla.columns:
        v = fila[col.name]
        if v is None:
            out[col.name] = None
        elif isinstance(col.type, LargeBinary):
            out[col.name] = base64.b64encode(bytes(v)).decode("ascii")
        elif isinstance(v, (datetime, date)):
            out[col.name] = v.isoformat()
        else:
            out[col.name] = v
    return out


def _de_json(tabla, d):
    out = {}
    for col in tabla.columns:
        v = d.get(col.name)
        if v is None:
            out[col.name] = None
        elif isinstance(col.type, LargeBinary):
            out[col.name] = base64.b64decode(v)
        elif isinstance(col.type, DateTime):
            out[col.name] = datetime.fromisoformat(v)
        elif isinstance(col.type, Date):
            out[col.name] = date.fromisoformat(v)
        elif isinstance(col.type, Boolean):
            out[col.name] = bool(v)
        else:
            out[col.name] = v
    return out


def exportar(engine, destino, solo_app: bool = False) -> dict:
    """Escribe el respaldo en `destino` (ruta o archivo abierto en binario). Devuelve el manifest."""
    manifest = dict(formato=FORMATO, creado_en=datetime.now().isoformat(timespec="seconds"),
                    motor=engine.dialect.name, alcance="solo_app" if solo_app else "completo", tablas={})
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # lectura consistente: una sola transacción para todas las tablas
        with engine.connect() as con:
            for t in _tablas(solo_app):
                h, n, buf = hashlib.sha256(), 0, io.BytesIO()
                for fila in con.execute(select(t).execution_options(stream_results=True)).mappings():
                    linea = (json.dumps(_a_json(t, fila), ensure_ascii=False) + "\n").encode("utf-8")
                    h.update(linea)
                    buf.write(linea)
                    n += 1
                zf.writestr(f"{t.name}.jsonl", buf.getvalue())
                manifest["tablas"][t.name] = dict(filas=n, sha256=h.hexdigest())
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _leer(origen):
    try:
        zf = zipfile.ZipFile(origen)
        manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ErrorRespaldo(f"No es un respaldo válido ({e}).")
    if manifest.get("formato") != FORMATO:
        raise ErrorRespaldo(f"Formato de respaldo no soportado: {manifest.get('formato')}")
    return zf, manifest


def verificar(origen) -> dict:
    """Comprueba que el respaldo no esté dañado (conteos y huellas de cada tabla). No toca ninguna base."""
    zf, manifest = _leer(origen)
    with zf:
        for nombre, info in manifest["tablas"].items():
            datos = zf.read(f"{nombre}.jsonl")
            if hashlib.sha256(datos).hexdigest() != info["sha256"]:
                raise ErrorRespaldo(f"La tabla «{nombre}» está dañada (la huella no coincide).")
            if datos.count(b"\n") != info["filas"]:
                raise ErrorRespaldo(f"La tabla «{nombre}» no tiene el número de filas esperado.")
    return manifest


def _con_datos(con, tabla) -> bool:
    return con.execute(select(tabla).limit(1)).first() is not None


def restaurar(engine, origen, reemplazar: bool = False, log=print) -> dict:
    """Carga un respaldo en `engine`. Si la base ya tiene datos exige reemplazar=True (y los borra)."""
    manifest = verificar(origen)          # 1) integridad, antes de tocar nada
    db.init_db(engine)                    # 2) esquema al día
    solo_app = manifest["alcance"] == "solo_app"
    tablas = [t for t in _tablas(solo_app) if t.name in manifest["tablas"]]
    ignorar = {"motivos"}                 # el catálogo inicial lo siembra init_db; se reemplaza con el del respaldo
    avisos = []

    with engine.begin() as con:
        con_datos = [t.name for t in tablas if t.name not in ignorar and _con_datos(con, t)]
        if con_datos and not reemplazar:
            raise ErrorRespaldo(
                "La base de destino ya tiene datos (" + ", ".join(con_datos[:4]) + "…). Restaurar los reemplazaría: "
                "haz primero un respaldo de esa base y vuelve a correr con --reemplazar.")
        for t in reversed(tablas):        # 3) vaciar en orden inverso (respeta llaves foráneas)
            con.execute(delete(t))
        with zipfile.ZipFile(origen) as zf:
            for t in tablas:              # 4) cargar en orden de dependencias
                lote, n = [], 0
                for linea in zf.read(f"{t.name}.jsonl").decode("utf-8").splitlines():
                    lote.append(_de_json(t, json.loads(linea)))
                    if len(lote) >= LOTE:
                        con.execute(insert(t), lote)
                        n += len(lote)
                        lote = []
                if lote:
                    con.execute(insert(t), lote)
                    n += len(lote)
                # El aviso se emite al terminar: si `log` escribe en la base (como hace la pantalla de carga) mientras
                # esta transacción sigue abierta, SQLite —un solo escritor a la vez— se bloquea esperando.
                avisos.append(f"  {t.name}: {n} filas")
        if engine.dialect.name == "postgresql":   # 5) que los autoincrementales sigan después del último id
            for t in tablas:
                if "id" in t.c:
                    con.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{t.name}\"), 1), "
                        f"(SELECT MAX(id) IS NOT NULL FROM \"{t.name}\"))"))
    for a in avisos:
        log(a)
    with engine.connect() as con:         # 6) comprobación final contra el manifest
        for t in tablas:
            n = con.execute(select(func.count()).select_from(t)).scalar()
            if n != manifest["tablas"][t.name]["filas"]:
                raise ErrorRespaldo(f"Tras restaurar, «{t.name}» tiene {n} filas y se esperaban "
                                    f"{manifest['tablas'][t.name]['filas']}.")
    return manifest
