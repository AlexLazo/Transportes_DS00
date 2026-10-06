"""Base de datos portable: SQLite en local, PostgreSQL en Railway (misma definición).

Capa 1 (importada desde Excel): la reescribe el importador.
Capa 2 (propia de la app): justificaciones, usuarios, bitácora, cierres. El importador NUNCA la toca.
"""
import os
from pathlib import Path

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Index,
    Integer, LargeBinary, MetaData, String, Table, Text, UniqueConstraint,
    create_engine, event, insert, select,
)

RAIZ = Path(__file__).resolve().parent.parent
metadata = MetaData()


def en_railway() -> bool:
    return any(k.startswith("RAILWAY_") for k in os.environ)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        if en_railway():
            # El disco del servicio web en Railway es efímero: un SQLite ahí se borraría en cada redeploy.
            raise RuntimeError(
                "Falta DATABASE_URL. En Railway agrega el plugin PostgreSQL y define "
                "DATABASE_URL = ${{Postgres.DATABASE_URL}} en las variables del servicio web. "
                "La app no arranca sin eso para no guardar datos en un disco que se borra.")
        return "sqlite:///" + str(RAIZ / "db" / "transportes.sqlite").replace("\\", "/")
    # Railway/Heroku entregan postgres://, SQLAlchemy 2 necesita postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def make_engine(url: str | None = None):
    url = url or database_url()
    kwargs = {"future": True}
    if url.startswith("sqlite"):
        Path(RAIZ / "db").mkdir(exist_ok=True)
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


# ───────────── Capa 1: importados ─────────────

archivos = Table(
    "archivos", metadata,
    Column("id", Integer, primary_key=True),
    Column("ruta", String(500), nullable=False, unique=True),
    Column("tipo", String(20), nullable=False),     # DIARIO | RUTAS_FIJAS | EXCEPCIONES | AUSENTISMOS | OTRO
    Column("periodo", String(7)),                   # YYYY-MM detectado por contenido
    Column("fecha", Date),                          # solo DIARIO, por contenido
    Column("hash", String(64)),
    Column("tam", BigInteger),
    Column("mtime", DateTime),
    Column("meta", Text),                           # JSON: max_fecha, avisos…
    Column("importado_en", DateTime),
    Column("filas", Integer, default=0),
    Column("estado", String(40), nullable=False),   # OK | DUPLICADO | OMITIDO | ERROR
    Column("mensaje", Text),
)

rutas_oficiales = Table(
    "rutas_oficiales", metadata,
    Column("archivo_id", Integer, primary_key=True),
    Column("periodo", String(7), primary_key=True),
    Column("ruta", String(40), primary_key=True),
    Column("canal", String(60)),
    Column("cod_sap", String(20)),
    Column("personas", Integer),
    Column("categoria", String(10)),                # GC | AJ
    Column("estatus", String(30)),
    Column("tipo", String(30)),
    Column("transportista", String(120)),
    Column("supervisor", String(120)),
    Column("vehiculo", String(20)),
    Column("placa", String(20)),
    Column("fuente", String(10)),                   # VISIBLE | OCULTA
)
Index("ix_ro_periodo", rutas_oficiales.c.periodo)

rutas_desactivadas = Table(
    "rutas_desactivadas", metadata,
    Column("archivo_id", Integer, primary_key=True),
    Column("ruta", String(40), primary_key=True),
    Column("periodo", String(7)),
    Column("estatus", String(30)),
    Column("tripulacion", Integer),
    Column("comentario", Text),
)

sap_transportes = Table(
    "sap_transportes", metadata,
    Column("archivo_id", Integer, primary_key=True),
    Column("transporte", BigInteger, primary_key=True),
    Column("fecha", Date, nullable=False),
    Column("ruta", String(40)),
    Column("viaje", Integer),
    Column("st", Integer),
    Column("cantidad", Float),
)
Index("ix_sap_fecha_transp", sap_transportes.c.fecha, sap_transportes.c.transporte)
Index("ix_sap_fecha_ruta", sap_transportes.c.fecha, sap_transportes.c.ruta)

cambios_cabecera = Table(
    "cambios_cabecera", metadata,
    Column("archivo_id", Integer, primary_key=True),
    Column("transporte", BigInteger, primary_key=True),
    Column("fecha", Date, nullable=False),
    Column("ruta_original", String(40)),
    Column("cod_transportista", String(20)),
    Column("nueva_ruta", String(40)),
    Column("conductor_sap", String(20)),
    Column("tractor", String(20)),
    Column("viaje", Integer),
    Column("cajas", Float),
    Column("contratista", String(120)),
    Column("comentario", Text),
)
Index("ix_cab_fecha_transp", cambios_cabecera.c.fecha, cambios_cabecera.c.transporte)
Index("ix_cab_fecha_nueva", cambios_cabecera.c.fecha, cambios_cabecera.c.nueva_ruta)

validacion_diaria = Table(
    "validacion_diaria", metadata,
    Column("archivo_id", Integer, primary_key=True),
    Column("ruta", String(40), primary_key=True),
    Column("fecha", Date, nullable=False),
    Column("estatus", String(200)),                 # nota manual del día
    Column("tipo", String(30)),
    Column("contratista", String(120)),
    Column("supervisor", String(120)),
)
Index("ix_val_fecha_ruta", validacion_diaria.c.fecha, validacion_diaria.c.ruta)

excepciones = Table(
    "excepciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("archivo_id", Integer, nullable=False),
    Column("fila", Integer),
    Column("periodo", String(7)),
    Column("fecha", Date),
    Column("transporte", BigInteger),
    Column("ruta", String(40)),
    Column("ruta_liquidada", String(40)),
    Column("dummy", String(2)),
    Column("recarga", String(2)),
    Column("excepcion", String(2)),
    Column("ruta_fija", String(2)),
    Column("spot1", String(2)),
    Column("spot_inc", String(2)),
    Column("envases", String(2)),
    Column("contratista", String(120)),
    Column("contratista_n", String(120)),
    Column("cod_od", String(20)),
    Column("firmada", String(40)),
    Column("cod", String(40)),
    Column("comentario", Text),
    Column("quincena", Integer),
)
Index("ix_exc_periodo", excepciones.c.periodo, excepciones.c.quincena)
Index("ix_exc_transp", excepciones.c.transporte)
Index("ix_exc_archivo", excepciones.c.archivo_id)

ausentismos = Table(
    "ausentismos", metadata,
    Column("id", Integer, primary_key=True),
    Column("archivo_id", Integer, nullable=False),
    Column("fila", Integer),
    Column("periodo", String(7)),
    Column("fecha", Date),
    Column("ruta", String(40)),
    Column("contratista", String(120)),
    Column("contratista_n", String(120)),
    Column("motivo", String(120)),
    Column("tripulacion", Float),
    Column("costo_ruta", Float),
    Column("dias_trab", Float),
    Column("costo_dia", Float),
    Column("comentario", Text),
)
Index("ix_aus_fecha_ruta", ausentismos.c.fecha, ausentismos.c.ruta)
Index("ix_aus_archivo", ausentismos.c.archivo_id)

# ── Control de AJ (agua y jugos) que lleva la supervisora ──
cargas_aj = Table(
    "cargas_aj", metadata,
    Column("id", Integer, primary_key=True),
    Column("archivo_id", Integer, nullable=False),
    Column("fila", Integer),
    Column("periodo", String(7)),
    Column("fecha", Date),
    Column("transporte", BigInteger),
    Column("ruta", String(40)),
    Column("carga_dia", String(2)),
    Column("dummy", String(2)),
    Column("recarga", String(2)),
    Column("reposicion", String(2)),
    Column("recoleccion", String(2)),
    Column("viaje", Integer),
    Column("cantidad", Float),
    Column("transportista", String(20)),          # código SAP (W00205 = PAE...)
    Column("aviso", String(200)),                 # correccion aplicada al leer (p. ej. "N0" -> "NO")
)
Index("ix_caj_periodo", cargas_aj.c.periodo)
Index("ix_caj_transp", cargas_aj.c.transporte)
Index("ix_caj_archivo", cargas_aj.c.archivo_id)

recargas_aj = Table(
    "recargas_aj", metadata,
    Column("id", Integer, primary_key=True),
    Column("archivo_id", Integer, nullable=False),
    Column("fila", Integer),
    Column("periodo", String(7)),
    Column("fecha", Date),
    Column("transporte", BigInteger),
    Column("ruta", String(40)),
    Column("cantidad", Float),
    Column("cliente", String(120)),
)
Index("ix_raj_periodo", recargas_aj.c.periodo)
Index("ix_raj_archivo", recargas_aj.c.archivo_id)

recolecciones_aj = Table(
    "recolecciones_aj", metadata,
    Column("id", Integer, primary_key=True),
    Column("archivo_id", Integer, nullable=False),
    Column("fila", Integer),
    Column("periodo", String(7)),
    Column("fecha", Date),
    Column("ruta", String(40)),
    Column("personal", String(120)),
    Column("cliente", String(120)),
    Column("envases", Float),
    Column("racks", Float),
)
Index("ix_reaj_periodo", recolecciones_aj.c.periodo)
Index("ix_reaj_archivo", recolecciones_aj.c.archivo_id)

dias = Table(
    "dias", metadata,
    Column("fecha", Date, primary_key=True),
    Column("transportes", Integer),                 # transportes SAP en el archivo diario
    Column("auto_operativo", Boolean, nullable=False, default=True),
)

uso_rutas = Table(
    "uso_rutas", metadata,
    Column("fecha", Date, primary_key=True),
    Column("ruta", String(40), primary_key=True),
    Column("periodo", String(7), nullable=False),
    Column("lista", String(10), nullable=False),    # OFICIAL | DIARIO
    Column("categoria", String(10)),
    Column("contratista", String(120)),
    Column("supervisor", String(120)),
    Column("estado", String(10), nullable=False),   # USADA | USADA_CAB | NO_USADA
    Column("transporte", BigInteger),
    Column("cajas", Float),
    Column("nota_excel", String(200)),
)
Index("ix_uso_periodo_estado", uso_rutas.c.periodo, uso_rutas.c.estado)

importaciones = Table(
    "importaciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("inicio", DateTime),
    Column("fin", DateTime),
    Column("estado", String(10)),                   # CORRIENDO | OK | ERROR
    Column("origen", String(40)),
    Column("resumen", Text),
)

# ───────────── Capa 2: propios de la app ─────────────

usuarios = Table(
    "usuarios", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario", String(60), nullable=False, unique=True),
    Column("nombre", String(120), nullable=False),
    Column("rol", String(10), nullable=False),      # admin | captura | consulta
    Column("hash", String(300), nullable=False),
    Column("activo", Boolean, nullable=False, default=True),
    Column("creado_en", DateTime, nullable=False),
)

motivos = Table(
    "motivos", metadata,
    Column("id", Integer, primary_key=True),
    Column("nombre", String(120), nullable=False, unique=True),
    Column("requiere_evidencia", Boolean, nullable=False, default=False),
    Column("activo", Boolean, nullable=False, default=True),
)

justificaciones = Table(
    "justificaciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("fecha", Date, nullable=False),
    Column("ruta", String(40), nullable=False),
    Column("motivo_id", Integer, ForeignKey("motivos.id"), nullable=False),
    Column("detalle", Text),
    Column("origen", String(10), nullable=False, default="MANUAL"),   # MANUAL | EXCEL
    Column("creado_por", String(60), nullable=False),
    Column("creado_en", DateTime, nullable=False),
    Column("actualizado_por", String(60)),
    Column("actualizado_en", DateTime),
    UniqueConstraint("fecha", "ruta", name="uq_just_fecha_ruta"),
)

evidencias = Table(
    "evidencias", metadata,
    Column("id", Integer, primary_key=True),
    Column("justificacion_id", Integer, ForeignKey("justificaciones.id", ondelete="CASCADE"), nullable=False),
    Column("nombre_original", String(255), nullable=False),
    Column("mime", String(100)),
    Column("tam", Integer),
    Column("sha256", String(64)),
    Column("contenido", LargeBinary, nullable=False),   # en BD: portable y respaldado junto con todo
    Column("subido_por", String(60), nullable=False),
    Column("subido_en", DateTime, nullable=False),
)

dias_config = Table(
    "dias_config", metadata,
    Column("fecha", Date, primary_key=True),
    Column("operativo", Boolean, nullable=False),
    Column("nota", String(200)),
    Column("por", String(60)),
    Column("en", DateTime),
)

cierres = Table(
    "cierres", metadata,
    Column("periodo", String(7), primary_key=True),
    Column("cerrado_por", String(60), nullable=False),
    Column("cerrado_en", DateTime, nullable=False),
    Column("nota", Text),
    Column("huellas", Text, nullable=False),        # JSON {archivo: hash} al cerrar
)

bitacora = Table(
    "bitacora", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", DateTime, nullable=False),
    Column("usuario", String(60)),
    Column("accion", String(60), nullable=False),
    Column("entidad", String(40)),
    Column("entidad_id", String(60)),
    Column("detalle", Text),
    Column("ip", String(60)),
)
Index("ix_bitacora_ts", bitacora.c.ts)

MOTIVOS_INICIALES = [
    ("Ausentismo del operador", False),
    ("Vacaciones", False),
    ("Incapacidad / permiso", True),
    ("Unidad en taller / sin unidad", False),
    ("Sin personal disponible", False),
    ("Ruta sin volumen / sin pedidos", False),
    ("Cliente cerrado / feriado", False),
    ("Cubierta por otra ruta (dummy / spot)", False),
    ("Ruta desactivada o en reestructura", False),
    ("Ruta de reserva (disponible)", False),
    ("Error de captura en el Excel", False),
    ("Otro (explicar en detalle)", False),
]


def sincronizar_columnas(engine) -> list[str]:
    """Agrega a las tablas existentes las columnas que el código ya conoce y la base aún no tiene.

    `create_all` solo crea tablas nuevas: si una versión futura agrega una columna, una base de Railway
    creada con la versión anterior fallaría al desplegar. Esto la agrega (siempre como NULL) sin tocar datos.
    Nunca borra ni modifica columnas existentes."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    cambios = []
    with engine.begin() as con:
        for tabla in metadata.sorted_tables:
            if not insp.has_table(tabla.name):
                continue
            existentes = {c["name"] for c in insp.get_columns(tabla.name)}
            for col in tabla.columns:
                if col.name not in existentes:
                    tipo = col.type.compile(dialect=engine.dialect)
                    con.execute(text(f'ALTER TABLE "{tabla.name}" ADD COLUMN "{col.name}" {tipo}'))
                    cambios.append(f"{tabla.name}.{col.name}")
    return cambios


def esperar_base(engine, intentos: int = 15, pausa: float = 3.0) -> None:
    """En un redeploy la base puede tardar unos segundos en aceptar conexiones: reintenta antes de rendirse."""
    import time
    from sqlalchemy import text
    for i in range(1, intentos + 1):
        try:
            with engine.connect() as con:
                con.execute(text("SELECT 1"))
            return
        except Exception:
            if i == intentos:
                raise
            time.sleep(pausa)


def init_db(engine) -> None:
    """Crea las tablas si no existen, agrega columnas nuevas y siembra el catálogo de motivos.
    Es seguro ejecutarlo en cada arranque: nunca borra ni reescribe datos."""
    esperar_base(engine)
    metadata.create_all(engine)
    sincronizar_columnas(engine)
    with engine.begin() as con:
        existentes = {r[0] for r in con.execute(select(motivos.c.nombre))}
        nuevos = [{"nombre": n, "requiere_evidencia": ev, "activo": True}
                  for n, ev in MOTIVOS_INICIALES if n not in existentes]
        if nuevos:
            con.execute(insert(motivos), nuevos)
