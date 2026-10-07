# Control de Transportes · CD Soyapango (DS00)

App de auditoría sobre los Excel de la operación (asignación diaria, dummys/excepciones, ausentismos y rutas fijas).

**Cómo está pensada:** los Excel de SharePoint siguen siendo donde se captura la operación. Un importador los lee
(en tu PC) y carga datos normalizados a una base. La app agrega lo que el Excel no tiene: justificaciones con evidencia,
controles automáticos, cierre de mes con bitácora y usuarios por rol.

```
Excel (SharePoint/OneDrive) ──► importar_local.py ──► Base de datos ──► App web (Flask)
                                 (en tu PC)           SQLite local o        local o Railway
                                                      PostgreSQL (Railway)
```

## Uso local

```bash
pip install -r requirements.txt        # una sola vez
python importar_local.py               # lee todos los Excel de esta carpeta (solo procesa lo nuevo o modificado)
python wsgi.py                         # abre http://localhost:5000
```
O con doble clic: `importar_excels.bat` y `iniciar_app.bat`.

La primera vez que abras la app pide crear el administrador. Después crea los demás usuarios en **Usuarios**:
`admin` (todo), `captura` (justifica y adjunta evidencias), `consulta` (solo ve; ideal para el auditor).

Pantallas principales: **Tablero**, **Para explicar** (todo lo que un auditor puede preguntar, de lo más grave a lo informativo), **Matriz ruta × día** (con la letra de la causa en cada cuadro: ausentismo, vacaciones, reserva…), **AJ** (mismo formato que GC más el cruce contra el control de la supervisora) y **Reporte imprimible** (matrices GC y AJ, ausentismos, dummys, hallazgos y detalle).

Para leer otra carpeta (por ejemplo la que sincroniza OneDrive): `python importar_local.py --carpeta "D:/OneDrive/.../Coordinación"`.
Para reprocesar todo: `--forzar`. Solo algunos tipos: `--forzar --solo EXCEPCIONES AUSENTISMOS`.

### Qué lee el importador
| Archivo | Hoja | Para qué |
|---|---|---|
| `dd-mm-aaaa BASE DE DETALLE DE CLIENTES*.xlsx` | `Asignacion` (SAP, validación de rutas, cambios de cabecera) | Qué transportes salieron por ruta cada día |
| `Copia de Rutas fijas <Mes>.xlsx` | `RUTAS X SUP` | Rutas activas del mes (lista oficial) |
| `* Excepciones.xlsx` / `DUMMYS RECARGAS Y EXCEPCIONES.xlsx` | `DDBB` | Pago de dummys, recargas y excepciones |
| `Ausentismos - <Mes>.xlsx` | `REGISTROS` | Ausentismos |
| `Cargas - AJ.xlsx` (supervisora de AJ) | `Cargas ...`, `Recargas`, `Recolección` | Control de agua y jugos, cruzado contra SAP |

Si dos archivos dicen lo mismo (copias, "respaldo", un archivo que trae el contenido de otro día) conserva el correcto
y marca el otro como DUPLICADO, con el motivo, en **Archivos importados**.

Para los meses sin archivo oficial de rutas (enero–julio) la app usa las rutas marcadas FIJA en el Excel diario.
Cuando subas el archivo oficial de un mes, el siguiente `importar_local.py` lo adopta solo.

## Cargar datos desde la app (sin terminal)
Con sesión de administrador, en **Cargar archivos**:
- **Subir Excel**: elige uno o varios (hasta 60 por vez) y la app reconoce cada tipo por su contenido (base diaria, Excepciones,
  Ausentismos, Rutas fijas, Cargas AJ), los procesa y muestra qué hizo con cada uno. Reenviar un archivo que ya estaba no duplica
  nada y una versión corregida reemplaza a la anterior. Las justificaciones y evidencias nunca se tocan.
- **Restaurar respaldo (.zip)**: reemplaza los datos actuales por los del respaldo (pide escribir REEMPLAZAR). Después hay que volver a
  entrar con los usuarios del respaldo.
- **Primer uso en una instalación nueva** (por ejemplo Railway): la pantalla `/setup?token=…` permite subir el respaldo directamente y
  entrar con el usuario de siempre, sin crear uno nuevo.

La terminal (`importar_local.py`, `respaldo.py`) sigue disponible, pero ya no es necesaria.

## Despliegue en Railway

1. Sube este proyecto a un repositorio **privado** de GitHub. El `.gitignore` ya excluye los Excel, la base y claves:
   **verifica con `git status` que no aparezca ningún `.xlsx`** antes del primer push.
2. En Railway: *New Project → Deploy from GitHub repo* y elige el repositorio. Agrega el plugin **PostgreSQL**.
3. En las variables del servicio web define:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `SECRET_KEY` = una cadena larga y aleatoria (`python -c "import secrets; print(secrets.token_hex(32))"`)
   - `SETUP_TOKEN` = otra cadena aleatoria (protege la creación del primer administrador, porque la URL es pública)
4. Cuando termine el despliegue abre `https://<tu-dominio>/setup?token=<SETUP_TOKEN>` y crea el administrador.
5. Sube lo que ya tienes, sin terminal: abre `https://<tu-dominio>/setup?token=<SETUP_TOKEN>` y en «Opción 1» sube el respaldo `.zip`
   que generaste en tu PC (**Respaldos → Descargar respaldo completo**, o `respaldar.bat`). Entras con tu usuario de siempre.
6. Cuando haya Excel nuevos: **Cargar archivos → Subir Excel**.

## Que los datos no se pierdan en cada redeploy
- **La base vive en PostgreSQL, no en el servicio web.** Railway guarda PostgreSQL en un volumen persistente: un redeploy del servicio web
  no lo toca. Lo que SÍ se borra en cada redeploy es el disco del servicio web; por eso la app no guarda nada ahí (las evidencias
  están dentro de la base).
- **Protección contra el error clásico:** si en Railway falta `DATABASE_URL`, la app **se niega a arrancar** en vez de crear un SQLite
  que desaparecería en el siguiente despliegue.
- **El arranque nunca borra datos:** solo crea tablas que faltan y agrega columnas nuevas (siempre vacías). Reimportar Excel
  recalcula únicamente las tablas derivadas; justificaciones, evidencias y usuarios no se tocan.
- **Si la base tarda en estar lista** durante un redeploy, la app reintenta unos segundos antes de rendirse.
- **Respaldos en tres niveles** (el mejor es combinarlos):
  1. *Backups de volumen de Railway*: se programan desde el servicio de PostgreSQL (diario/semanal/mensual) y se restauran con
     un clic. Revisa en tu plan qué retención y funciones incluye: <https://docs.railway.com/guides/postgres-backups-restores>
  2. *Respaldo desde la app* (**Respaldos**, solo admin) o `python respaldo.py exportar --db "<URL de Railway>"` desde tu PC:
     un .zip portable, verificado con huellas SHA-256, que se restaura en cualquier otra base.
  3. *Copia fuera de Railway*: guarda esos .zip en tu PC o SharePoint. Es la única que sobrevive si se borra el proyecto.
  `respaldar.bat` hace los dos respaldos (completo y solo-app) de tu base local con un doble clic.
- Recomendación: respaldo **solo-app** (pocos KB) al terminar cada jornada, **completo** al cerrar cada mes y antes de actualizar la app.

## Seguridad y datos
- Los archivos traen nombres de contratistas y conductores; antes de subirlos a un servicio en la nube confirma con TI/Compliance.
- Contraseñas con hash, protección CSRF, bloqueo tras 5 intentos fallidos, cookie segura en Railway, evidencias servidas con `Content-Security-Policy: sandbox`.
- Cada acción de captura queda en la **Bitácora** (usuario, fecha/hora, IP). Un mes cerrado no admite cambios y la app avisa si algún Excel de respaldo cambia después del cierre.

## Estructura
```
wsgi.py                         punto de entrada (gunicorn en Railway)
importar_local.py               importador por línea de comandos
respaldo.py                     respaldar / verificar / restaurar la base (SQLite o PostgreSQL)
transportes/db.py               tablas (SQLite local / PostgreSQL en Railway)
transportes/importador/         lectura de Excel, detección de duplicados, cruce de rutas
transportes/web/                vistas, plantillas y estilos
Procfile · railway.json         arranque y healthcheck para Railway
```


