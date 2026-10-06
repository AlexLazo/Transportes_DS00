"""Respaldo y restauración de la base de datos (funciona igual con SQLite local y PostgreSQL en Railway).

  python respaldo.py exportar                        # respaldo completo de la base local → respaldos/
  python respaldo.py exportar --solo-app             # solo lo que no se puede regenerar desde los Excel
  python respaldo.py exportar --db "<URL pública de PostgreSQL>"     # respalda la base de Railway a tu PC
  python respaldo.py verificar respaldos/archivo.zip                 # comprueba que no esté dañado
  python respaldo.py restaurar respaldos/archivo.zip --db "<URL pública de PostgreSQL>"   # sube lo local a Railway
  python respaldo.py restaurar archivo.zip --db "<URL>" --reemplazar # si el destino ya tiene datos (los borra)

Un respaldo hecho en SQLite se puede restaurar en PostgreSQL y viceversa.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

from transportes import db
from transportes.respaldo import ErrorRespaldo, exportar, restaurar, verificar


def _engine(url):
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return db.make_engine(url)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("exportar")
    e.add_argument("--db", default=None, help="URL de la base (por defecto DATABASE_URL o SQLite local)")
    e.add_argument("--solo-app", action="store_true")
    e.add_argument("--salida", default=None, help="archivo .zip de salida (por defecto respaldos/…)")
    v = sub.add_parser("verificar")
    v.add_argument("archivo")
    r = sub.add_parser("restaurar")
    r.add_argument("archivo")
    r.add_argument("--db", default=None)
    r.add_argument("--reemplazar", action="store_true", help="borrar los datos del destino antes de restaurar")
    a = ap.parse_args()
    try:
        if a.cmd == "exportar":
            eng = _engine(a.db)
            db.init_db(eng)
            destino = Path(a.salida) if a.salida else (
                Path(__file__).parent / "respaldos" /
                f"respaldo_transportes_{datetime.now():%Y%m%d_%H%M}{'_app' if a.solo_app else ''}.zip")
            destino.parent.mkdir(parents=True, exist_ok=True)
            m = exportar(eng, str(destino), solo_app=a.solo_app)
            verificar(str(destino))
            filas = sum(t["filas"] for t in m["tablas"].values())
            print(f"Respaldo listo y verificado: {destino}  ({destino.stat().st_size / 1e6:.1f} MB, {filas} filas, "
                  f"{len(m['tablas'])} tablas)")
        elif a.cmd == "verificar":
            m = verificar(a.archivo)
            print(f"Respaldo íntegro · {m['alcance']} · creado {m['creado_en']} en {m['motor']}")
            for t, i in m["tablas"].items():
                print(f"  {t}: {i['filas']} filas")
        else:
            eng = _engine(a.db)
            print(f"Restaurando en {eng.url.render_as_string(hide_password=True)} …")
            restaurar(eng, a.archivo, reemplazar=a.reemplazar)
            print("Restauración completa y verificada.")
    except ErrorRespaldo as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
