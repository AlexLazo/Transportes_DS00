"""Cruce rutas activas del mes  vs  transportes que realmente salieron cada día."""
from collections import defaultdict
from statistics import median

from sqlalchemy import delete, insert, select

from .. import db
from .parsers import hnorm, periodo_de

TIPOS_FIJA = {"FIJA", "FIJA TEMPORADA", "FLETERA FIJA"}
UMBRAL_DIA_NORMAL = 0.4   # un día con menos del 40% de la mediana del mes se marca como "bajo volumen"


def recalcular(con):
    """Reconstruye `dias` y `uso_rutas` a partir de los datos vigentes. No toca justificaciones."""
    sap, cab, val, oficial = (defaultdict(list) for _ in range(4))
    control_aj = {}   # (fecha, ruta) -> (transporte, cajas) según el control de la supervisora de AJ
    for r in con.execute(select(db.cargas_aj)).mappings():
        if r["ruta"]:
            control_aj.setdefault((r["fecha"], r["ruta"]), (r["transporte"], r["cantidad"]))
    for r in con.execute(select(db.sap_transportes)).mappings():
        sap[r["fecha"]].append(r)
    for r in con.execute(select(db.cambios_cabecera)).mappings():
        cab[r["fecha"]].append(r)
    for r in con.execute(select(db.validacion_diaria)).mappings():
        val[r["fecha"]].append(r)
    for r in con.execute(select(db.rutas_oficiales)).mappings():
        if hnorm(r["estatus"]).startswith("ACTIV"):
            oficial[r["periodo"]].append(r)

    # ── días con archivo y si su volumen es normal ──
    por_mes = defaultdict(list)
    for f, filas in sap.items():
        por_mes[periodo_de(f)].append(len(filas))
    med = {p: median(v) for p, v in por_mes.items()}
    con.execute(delete(db.dias))
    dias_rows = [dict(fecha=f, transportes=len(filas),
                      auto_operativo=len(filas) >= UMBRAL_DIA_NORMAL * med[periodo_de(f)])
                 for f, filas in sorted(sap.items())]
    if dias_rows:
        con.execute(insert(db.dias), dias_rows)

    # ── uso de rutas por día ──
    con.execute(delete(db.uso_rutas))
    salida, sin_lista = [], 0
    for fecha in sorted(sap):
        periodo = periodo_de(fecha)
        # ruta efectiva de cada transporte: la "nueva ruta" si hubo cambio de cabecera
        efectiva = {}
        for r in sap[fecha]:
            efectiva[r["transporte"]] = [r["ruta"], False, r["cantidad"]]
        for c in cab[fecha]:
            if not c["nueva_ruta"]:
                continue
            if c["transporte"] in efectiva:
                e = efectiva[c["transporte"]]
                e[1] = c["nueva_ruta"] != e[0]
                e[0] = c["nueva_ruta"]
            else:
                efectiva[c["transporte"]] = [c["nueva_ruta"], True, c["cajas"]]
        usadas = defaultdict(list)
        for t, (ruta, via_cab, cajas) in efectiva.items():
            if ruta:
                usadas[ruta].append((t, via_cab, cajas))

        nota = {v["ruta"]: v["estatus"] for v in val[fecha]}
        if periodo in oficial:
            lista, esperadas = "OFICIAL", [
                (o["ruta"], o["categoria"], o["transportista"], o["supervisor"]) for o in oficial[periodo]]
        else:
            lista, esperadas = "DIARIO", [
                (v["ruta"], None, v["contratista"], v["supervisor"])
                for v in val[fecha] if hnorm(v["tipo"]) in TIPOS_FIJA]
            if not esperadas:
                sin_lista += 1
        for ruta, cat, contratista, sup in esperadas:
            u = usadas.get(ruta)
            if u:
                t, via_cab, _ = u[0]
                estado = "USADA_CAB" if all(x[1] for x in u) else "USADA"
                cajas = sum(x[2] or 0 for x in u)
            elif (fecha, ruta) in control_aj:
                # SAP no la muestra (p. ej. archivo diario desactualizado) pero la supervisora de AJ registró la carga
                (t, cajas), estado = control_aj[(fecha, ruta)], "USADA_AJ"
            else:
                t, estado, cajas = None, "NO_USADA", None
            salida.append(dict(
                fecha=fecha, ruta=ruta, periodo=periodo, lista=lista, categoria=cat, contratista=contratista,
                supervisor=sup, estado=estado, transporte=t, cajas=cajas, nota_excel=nota.get(ruta)))
    for i in range(0, len(salida), 5000):
        con.execute(insert(db.uso_rutas), salida[i:i + 5000])
    return dict(dias=len(dias_rows), uso_rutas=len(salida), dias_sin_lista=sin_lista)
