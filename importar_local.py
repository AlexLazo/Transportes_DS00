"""Importa los Excel de una carpeta a la base de datos.

  python importar_local.py                      # carpeta de este proyecto → db/transportes.sqlite
  python importar_local.py --carpeta "D:/OneDrive/Coordinación"
  python importar_local.py --db "postgresql://usuario:clave@host:puerto/railway"   # sube directo a Railway
  python importar_local.py --forzar             # reprocesa todo aunque no haya cambiado
  python importar_local.py --forzar --solo EXCEPCIONES AUSENTISMOS   # solo esos tipos

Los Excel se leen AQUÍ (en tu PC) y a Railway solo viajan los datos ya normalizados.
"""
import argparse
import sys
from pathlib import Path

from transportes import db
from transportes.importador.importar import importar

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--carpeta", default=str(Path(__file__).parent), help="carpeta con los Excel (recursivo)")
    ap.add_argument("--db", default=None, help="URL de la base (por defecto DATABASE_URL o SQLite local)")
    ap.add_argument("--forzar", action="store_true", help="reprocesar todos los archivos")
    ap.add_argument("--solo", nargs="+", choices=["DIARIO", "RUTAS_FIJAS", "EXCEPCIONES", "AUSENTISMOS", "CARGAS_AJ"],
                    help="limitar a estos tipos de archivo")
    a = ap.parse_args()
    url = a.db
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    ok = importar(a.carpeta, db.make_engine(url), forzar=a.forzar, solo=set(a.solo) if a.solo else None, origen="local")
    sys.exit(0 if ok else 1)
