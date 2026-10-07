"""Tareas largas (importar Excel, restaurar un respaldo) que corren en un hilo y se consultan desde la pantalla.

El estado vive en la tabla `tareas` (no en memoria) para que cualquier proceso del servidor pueda responder
cuando la pantalla pregunta cómo va."""
import json
import shutil
import threading
import traceback
from datetime import datetime, timedelta

from sqlalchemy import insert, select, update

from .. import db

MAX_MINUTOS = 30   # una tarea "corriendo" más tiempo que esto se da por interrumpida (p. ej. reinicio del servicio)


def expirar_viejas(engine):
    limite = datetime.now() - timedelta(minutes=MAX_MINUTOS)
    with engine.begin() as con:
        con.execute(update(db.tareas).where(db.tareas.c.estado == "CORRIENDO", db.tareas.c.creado_en < limite).values(
            estado="ERROR", fin=datetime.now(),
            resumen=json.dumps({"error": "Se interrumpió (el servicio se reinició o tardó demasiado). Vuelve a intentarlo."})))


def en_curso(engine):
    expirar_viejas(engine)
    with engine.connect() as con:
        return con.execute(select(db.tareas.c.id, db.tareas.c.tipo).where(db.tareas.c.estado == "CORRIENDO")
                           .limit(1)).first()


def _escribir(engine, tid, **valores):
    with engine.begin() as con:
        con.execute(update(db.tareas).where(db.tareas.c.id == tid).values(**valores))


def lanzar(engine, tipo, usuario, descripcion, trabajo, limpiar=None):
    """Registra la tarea y ejecuta `trabajo(log)` en un hilo. `trabajo` devuelve un dict con el resultado
    (si trae "ok": False la tarea queda en ERROR). Devuelve el id de la tarea."""
    with engine.begin() as con:
        tid = con.execute(insert(db.tareas).values(
            tipo=tipo, estado="CORRIENDO", usuario=usuario, creado_en=datetime.now(),
            descripcion=descripcion[:200], log="")).inserted_primary_key[0]

    def correr():
        lineas = []

        def log(m):
            lineas.append(str(m))
            try:
                _escribir(engine, tid, log="\n".join(lineas[-300:]))
            except Exception:
                pass

        try:
            resultado = trabajo(log) or {}
            estado = "OK" if resultado.get("ok", True) else "ERROR"
        except Exception as e:
            estado = "ERROR"
            resultado = {"error": f"{type(e).__name__}: {e}"}
            lineas.append(traceback.format_exc())
        finally:
            if limpiar:
                try:
                    limpiar()
                except Exception:
                    pass
        _escribir(engine, tid, estado=estado, fin=datetime.now(), log="\n".join(lineas[-300:]),
                  resumen=json.dumps(resultado, ensure_ascii=False, default=str))

    threading.Thread(target=correr, name=f"tarea-{tid}", daemon=True).start()
    return tid


def borrar_carpeta(ruta):
    return lambda: shutil.rmtree(ruta, ignore_errors=True)
