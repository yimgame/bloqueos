# Bloqueo de choferes · documentación crítica

App web (Flask + SQLite) para controlar el bloqueo de choferes por
documentación crítica vencida: cruza el exportado de GCG con el Maestro de
proveedores y `dni.xlsx`, clasifica choferes vencidos / próximos a vencer por
documento, gestiona las solicitudes de desbloqueo (con auto-aprobación por
rol y chequeo automático contra la API de GCG) y arma los mails por
transporte con el detalle en Excel.

Este repo solo tiene el código. La base de datos y los Excel con datos reales
(choferes, DNI, proveedores) **no se suben** — quedan afuera por `.gitignore`
y se generan/cargan localmente.

## Instalación

```bash
pip install -r requirements.txt
python app.py
```

Sirve en `http://0.0.0.0:5000`. Al arrancar, `db.init_db()` crea
`bloqueos.db` desde cero si no existe: arma el esquema, carga el catálogo de
documentos críticos por defecto y crea los usuarios iniciales.

## Primer login

Usuario admin inicial: **admin / admin123**. Cambiá la contraseña (y creá o
ajustá el resto de los usuarios JRT/analista) desde Configuración → Usuarios
apenas entres.

## Carga de datos inicial

Con la app corriendo, como admin, en **Configuración → Datos de referencia**
subí en este orden:

1. **Maestro de proveedores** (excel con NRO DE PROVEEDOR, Razón social, JRT, mail, etc.)
2. **dni.xlsx** (ID vehículo / DNI → Nº de proveedor)
3. **Estado de bloqueos actual** (foto real de quién está bloqueado hoy)
4. **Exportado de GCG** (genera la primera "corrida" y ya clasifica vencidos/próximos)

En **Configuración → Documentos** revisá que cada documento crítico tenga
mapeado su número de GCG (Nº GCG), si no el chequeo automático no va a poder
correr.

## API de GCG

Para que el chequeo automático de desbloqueos funcione hace falta una API
key de GCG. Se puede cargar de dos formas:

- **Configuración → GCG** (queda guardada cifrada en la base).
- Un archivo `gcg/.env` local con una línea `api_key=...` (bootstrap: se migra
  solo a la base la primera vez que arranca la app). Este archivo no se sube
  al repo.

## Mail (SMTP)

Configurá el servidor SMTP y las plantillas de mail desde
**Configuración → SMTP** para poder enviar los avisos de bloqueo/desbloqueo
por transporte.

## Estructura

- `app.py` — rutas Flask
- `pipeline.py` — lectura/cruce de los excels y clasificación vencido/próximo
- `db.py` — esquema SQLite y acceso a datos
- `auth.py` — login y roles (admin / jrt / analista)
- `gcg_api.py` — consulta a la API de GCG
- `mailer.py` — envío de mails
- `templates/`, `static/` — vistas y assets
