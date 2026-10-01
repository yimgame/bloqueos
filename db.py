# -*- coding: utf-8 -*-
"""Acceso a la base SQLite: proveedores (maestro), dni->proveedor, documentos
controlados, y el historial de corridas (runs) con el detalle por chofer/documento."""
import base64
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from werkzeug.security import generate_password_hash

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - fallback si no está instalado el paquete
    Fernet = None
    InvalidToken = Exception

DB_PATH = "bloqueos.db"

# Passphrase local para cifrar secretos (API keys, etc.) guardados en app_config.
# No reemplaza un secret manager, pero evita tenerlos en texto plano en la DB.
_ENC_PASSPHRASE = "bloqueo-choferes-doc-critica-secretos-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proveedores (
    nro_proveedor INTEGER PRIMARY KEY,
    razon_social TEXT,
    jrt TEXT,
    estado_actual TEXT,
    tipo_servicio TEXT,
    mail TEXT,
    telefono TEXT,
    domicilio TEXT,
    provincia TEXT,
    cuit TEXT,
    pais TEXT,
    observaciones TEXT,
    actualizado_en TEXT
);

CREATE TABLE IF NOT EXISTS dni_proveedor (
    dni TEXT PRIMARY KEY,
    nro_proveedor INTEGER,
    fecha_actz TEXT,
    actualizado_en TEXT
);

CREATE TABLE IF NOT EXISTS documentos_control (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT UNIQUE NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    orden INTEGER,
    gcg_doc_numero INTEGER,
    paises TEXT NOT NULL DEFAULT 'AR'
);

CREATE TABLE IF NOT EXISTS gcg_catalogo_documentos (
    numero INTEGER PRIMARY KEY,
    descripcion TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado_bloqueos (
    dni TEXT PRIMARY KEY,
    tipo TEXT,
    descripcion TEXT,
    actualizado_en TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha_run TEXT NOT NULL,
    archivo_origen TEXT,
    total_evaluados INTEGER,
    total_choferes_sin_proveedor INTEGER,
    total_excluidos INTEGER
);

CREATE TABLE IF NOT EXISTS choferes_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    dni TEXT,
    nombre TEXT,
    condicion TEXT,
    nro_proveedor INTEGER,
    razon_social TEXT,
    jrt TEXT,
    mail_proveedor TEXT,
    pais TEXT,
    documento TEXT,
    fecha_vencimiento TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_choferes_docs_run ON choferes_docs(run_id);
CREATE INDEX IF NOT EXISTS idx_choferes_docs_jrt ON choferes_docs(jrt);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nombre_completo TEXT,
    jrt TEXT,
    mail TEXT,
    rol TEXT NOT NULL DEFAULT 'jrt',
    activo INTEGER NOT NULL DEFAULT 1,
    creado_en TEXT
);

CREATE TABLE IF NOT EXISTS solicitudes_desbloqueo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    dni TEXT,
    nombre TEXT,
    jrt TEXT,
    nro_proveedor INTEGER,
    razon_social TEXT,
    documentos TEXT,
    estado TEXT NOT NULL DEFAULT 'solicitado',
    comentario TEXT,
    solicitado_por_id INTEGER,
    solicitado_por_nombre TEXT,
    fecha_solicitud TEXT,
    autorizado_por_id INTEGER,
    autorizado_por_nombre TEXT,
    fecha_autorizacion TEXT,
    ejecutado_por_id INTEGER,
    ejecutado_por_nombre TEXT,
    fecha_ejecucion TEXT,
    origen TEXT NOT NULL DEFAULT 'manual',
    gcg_json TEXT,
    gcg_consultado_en TEXT,
    gcg_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_solicitudes_dni ON solicitudes_desbloqueo(dni);

CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS eventos_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    usuario_id INTEGER,
    usuario_nombre TEXT,
    tipo TEXT NOT NULL,
    mensaje TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eventos_log_fecha ON eventos_log(fecha);

-- En qué contratista (transporte) figuraba cada chofer cada vez que lo vimos: al
-- subir un exportado, al consultar la API (solicitudes, buscador, barrido
-- mensual). Sirve para saber por qué transportes pasó un chofer.
CREATE TABLE IF NOT EXISTS chofer_contratista_hist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dni TEXT NOT NULL,
    nombre TEXT,
    pais TEXT,
    situacion TEXT,
    contratista TEXT,
    contratista_cuit TEXT,
    fuente TEXT NOT NULL,
    fecha TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chofer_contratista_hist_dni ON chofer_contratista_hist(dni, fecha);
"""

DEFAULT_CONFIG = {
    "dias_alerta_default": "7",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "mail_cc_adicional": "",
    "mail_asunto": "Documentación crítica vencida/próxima a vencer - {proveedor} - {fecha}",
    "mail_cuerpo": (
        "Estimados,\n\n"
        "Adjuntamos el detalle de choferes de {proveedor} con documentación crítica "
        "vencida ({bloqueados}) o próxima a vencer ({proximos}) al {fecha}.\n\n"
        "Por favor regularizar la documentación a la brevedad para evitar el bloqueo "
        "(o levantar el bloqueo vigente).\n\n"
        "Saludos."
    ),
    "gcg_api_key": "",
    "estado_bloqueos_actualizado_en": "",
    "app_url": "",
    "mail_botones_admin": "0",
}

DOCUMENTOS_DEFAULT = [
    "Pago bancarizado haberes",
    "Pago Federación de Camioneros",
    "Pago Sindicato",
    "Pago, DJ F931 y Secuencia 0",
    "Planilla kilometraje / horarios y descansos",
    "Recibo de haberes",
]

# Mapeo al número de documento del catálogo de GCG (ver gcg/criticos.xlsx). La
# consulta a la API de GCG identifica cada documento del trabajador por este
# número (con 3 dígitos, ej. "010"), no por su nombre.
DOCUMENTOS_GCG_NUMERO_DEFAULT = {
    "Pago bancarizado haberes": 10,
    "Pago Federación de Camioneros": 41,
    "Pago Sindicato": 7,
    "Pago, DJ F931 y Secuencia 0": 2,
    "Planilla kilometraje / horarios y descansos": 9,
    "Recibo de haberes": 3,
}

# Catálogo completo de tipos de documento de GCG (ver gcg/criticos.xlsx), para
# traducir el código numérico que devuelve la API a un nombre legible.
GCG_CATALOGO_DEFAULT = {
    1: "Licencia de conducir",
    2: "Form. 931 y rec. de pago",
    3: "Constancia de remuneración",
    4: "LiNTI - Lic. Nac. Tpte. Inter.",
    5: "Libreta sanitaria",
    6: "Libre deuda sindical",
    7: "Pago sindicato camionero",
    8: "Libre deuda obra social",
    9: "Planilla de kilometraje",
    10: "Ticket dpto. bancario haberes",
    11: "Inspección técnica vehicular",
    12: "Póliza de seguro autom.",
    14: "R.U.T.A. - Reg. único tte. aut.",
    15: "Seguimiento satelital",
    16: "Formulario alta y medidas",
    17: "SENASA",
    18: "SEDRONAR",
    20: "Sustancias alimenticias",
    21: "Título del automotor",
    22: "Cuota de seguro autom.",
    23: "Tarjeta verde",
    24: "Dec. jurada de titu. dominial",
    25: "Doc legal impositiva tpte.",
    40: "Certificado cobertura ART",
    41: "Pago federación camioneros",
    42: "Póliza accid. personales",
    43: "Alta temprana del AFIP",
    45: "Cuota accid. personales",
    46: "Pago a subcontratista",
    47: "Curso CNRT",
    48: "Aguinaldo",
    49: "Curso cap. 1 seg. tpte. cargas",
    50: "Permiso internac. Uruguay",
    60: "Permiso internac. Brasil",
    70: "Permiso internac. Paraguay",
    80: "Permiso internac. Bolivia",
    90: "Permiso internac. Chile",
    91: "Curso cap. 2 accidente cero",
    92: "Curso cap. 3 prev. acc. viales",
    97: "Carta adhesión",
    98: "Carta oferta",
    99: "Activo / inactivo",
}

# Países (código ISO, como viene en el campo "iso" de la API de GCG) a los que se
# les controla un documento por defecto. Vacío = todos los países.
PAISES_DEFAULT = "AR"

USUARIOS_DEFAULT = [
    # (username, password_temporal, nombre_completo, jrt, rol)
    ("admin", "admin123", "Antonio Javier Cherin", None, "admin"),
    ("ailen", "ailen123", "Ailen Doorman", "Ailen Doorman", "jrt"),
    ("ariel", "ariel123", "Ariel Canavesio", "Ariel Canavesio", "jrt"),
    ("franco", "franco123", "Franco Auban", "Franco Auban", "jrt"),
    ("javier", "javier123", "Javier Perret", "Javier Perret", "jrt"),
    ("joel", "joel123", "Joel Dure", "Joel Dure", "jrt"),
]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrar_columnas(conn)
        existing = conn.execute("SELECT COUNT(*) c FROM documentos_control").fetchone()["c"]
        if existing == 0:
            for i, nombre in enumerate(DOCUMENTOS_DEFAULT):
                conn.execute(
                    "INSERT INTO documentos_control (nombre, activo, orden, gcg_doc_numero, paises) VALUES (?, 1, ?, ?, ?)",
                    (nombre, i, DOCUMENTOS_GCG_NUMERO_DEFAULT.get(nombre), PAISES_DEFAULT),
                )
        else:
            # Completa el número de GCG de los documentos por defecto que todavía no lo tengan
            # (bases creadas antes de este cambio).
            for nombre, numero in DOCUMENTOS_GCG_NUMERO_DEFAULT.items():
                conn.execute(
                    """UPDATE documentos_control SET gcg_doc_numero = ?
                       WHERE nombre = ? AND gcg_doc_numero IS NULL""",
                    (numero, nombre),
                )
        existing_users = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
        if existing_users == 0:
            ts = now_iso()
            for username, pw, nombre, jrt, rol in USUARIOS_DEFAULT:
                conn.execute(
                    """INSERT INTO usuarios (username, password_hash, nombre_completo, jrt, rol, activo, creado_en)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (username, generate_password_hash(pw), nombre, jrt, rol, ts),
                )
        _backfill_historial_contratista(conn)
        for numero, descripcion in GCG_CATALOGO_DEFAULT.items():
            conn.execute(
                """INSERT INTO gcg_catalogo_documentos (numero, descripcion) VALUES (?, ?)
                   ON CONFLICT(numero) DO NOTHING""",
                (numero, descripcion),
            )


def observacion_desde_gcg(data, fuente, fecha=None):
    """Arma una fila de chofer_contratista_hist a partir del JSON de la API de GCG
    (None si la respuesta no trae un trabajador)."""
    if not isinstance(data, dict) or not data.get("dni"):
        return None
    contratista = data.get("contratista") or {}
    return {
        "dni": str(data.get("dni")).strip(),
        "nombre": f"{data.get('apellido') or ''}, {data.get('nombre') or ''}".strip(", ") or None,
        "pais": data.get("iso"),
        "contratista": contratista.get("nombre"),
        "contratista_cuit": contratista.get("cuit"),
        "fuente": fuente,
        "fecha": fecha or now_iso(),
    }


def _backfill_historial_contratista(conn):
    """La primera vez, carga el historial con lo que ya teníamos guardado: el JSON
    de GCG de cada solicitud de desbloqueo."""
    if conn.execute("SELECT 1 FROM chofer_contratista_hist LIMIT 1").fetchone():
        return
    rows = []
    for r in conn.execute(
        "SELECT gcg_json, COALESCE(gcg_consultado_en, fecha_solicitud) fecha FROM solicitudes_desbloqueo WHERE gcg_json IS NOT NULL"
    ):
        try:
            obs = observacion_desde_gcg(json.loads(r["gcg_json"]), "solicitud", r["fecha"])
        except (ValueError, TypeError):
            obs = None
        if obs:
            rows.append(obs)
    _insert_historial(conn, rows)


def _insert_historial(conn, rows):
    conn.executemany(
        """INSERT INTO chofer_contratista_hist (dni, nombre, pais, situacion, contratista, contratista_cuit, fuente, fecha)
           VALUES (:dni, :nombre, :pais, :situacion, :contratista, :contratista_cuit, :fuente, :fecha)""",
        [{"nombre": None, "pais": None, "situacion": None, "contratista_cuit": None, **r} for r in rows],
    )


def insert_historial_contratista(rows):
    with get_conn() as conn:
        _insert_historial(conn, rows)


def get_historial_contratista(dni=None):
    """Observaciones ordenadas por DNI y fecha (de un DNI, o de todos)."""
    with get_conn() as conn:
        if dni:
            q = conn.execute("SELECT * FROM chofer_contratista_hist WHERE dni = ? ORDER BY fecha, id", (dni,))
        else:
            q = conn.execute("SELECT * FROM chofer_contratista_hist ORDER BY dni, fecha, id")
        return [dict(r) for r in q.fetchall()]


def dnis_vistos_desde(fecha_iso):
    """DNIs que figuraron en el historial desde esa fecha (para el barrido mensual)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT dni FROM chofer_contratista_hist WHERE fecha >= ? ORDER BY dni", (fecha_iso,)
        ).fetchall()
        return [r["dni"] for r in rows]


def ultimo_contratista_por_dni():
    """{dni: contratista} según la última vez que se vio a cada chofer."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT h.dni, h.contratista FROM chofer_contratista_hist h
               JOIN (SELECT dni, MAX(id) id FROM chofer_contratista_hist GROUP BY dni) u ON u.id = h.id"""
        ).fetchall()
        return {r["dni"]: r["contratista"] for r in rows}


def _migrar_columnas(conn):
    """Agrega columnas nuevas a tablas ya existentes (bases creadas antes de este cambio)."""
    cols_usuarios = {r["name"] for r in conn.execute("PRAGMA table_info(usuarios)")}
    if "mail" not in cols_usuarios:
        conn.execute("ALTER TABLE usuarios ADD COLUMN mail TEXT")
    # Vacaciones de un JRT: quién lo cubre y entre qué fechas (ambas opcionales:
    # sin "desde" rige ya, sin "hasta" rige hasta que se borre).
    if "cubierto_por_id" not in cols_usuarios:
        conn.execute("ALTER TABLE usuarios ADD COLUMN cubierto_por_id INTEGER")
    if "ausencia_desde" not in cols_usuarios:
        conn.execute("ALTER TABLE usuarios ADD COLUMN ausencia_desde TEXT")
    if "ausencia_hasta" not in cols_usuarios:
        conn.execute("ALTER TABLE usuarios ADD COLUMN ausencia_hasta TEXT")

    cols_solicitudes = {r["name"] for r in conn.execute("PRAGMA table_info(solicitudes_desbloqueo)")}
    if "origen" not in cols_solicitudes:
        conn.execute("ALTER TABLE solicitudes_desbloqueo ADD COLUMN origen TEXT NOT NULL DEFAULT 'manual'")
    if "gcg_json" not in cols_solicitudes:
        conn.execute("ALTER TABLE solicitudes_desbloqueo ADD COLUMN gcg_json TEXT")
    if "gcg_consultado_en" not in cols_solicitudes:
        conn.execute("ALTER TABLE solicitudes_desbloqueo ADD COLUMN gcg_consultado_en TEXT")
    if "gcg_error" not in cols_solicitudes:
        conn.execute("ALTER TABLE solicitudes_desbloqueo ADD COLUMN gcg_error TEXT")

    cols_documentos = {r["name"] for r in conn.execute("PRAGMA table_info(documentos_control)")}
    if "gcg_doc_numero" not in cols_documentos:
        conn.execute("ALTER TABLE documentos_control ADD COLUMN gcg_doc_numero INTEGER")
    # Países (ISO) a los que se les controla cada documento. Los choferes de otros
    # países (ej. extranjeros de Bolivia/Paraguay) no se bloquean por ese documento.
    if "paises" not in cols_documentos:
        conn.execute(f"ALTER TABLE documentos_control ADD COLUMN paises TEXT NOT NULL DEFAULT '{PAISES_DEFAULT}'")

    cols_hist = {r["name"] for r in conn.execute("PRAGMA table_info(chofer_contratista_hist)")}
    if "situacion" not in cols_hist:
        conn.execute("ALTER TABLE chofer_contratista_hist ADD COLUMN situacion TEXT")

    cols_choferes_docs = {r["name"] for r in conn.execute("PRAGMA table_info(choferes_docs)")}
    if "pais" not in cols_choferes_docs:
        conn.execute("ALTER TABLE choferes_docs ADD COLUMN pais TEXT")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def fmt_fecha(value, con_hora=False):
    """Convierte una fecha/datetime ISO ('YYYY-MM-DD' o 'YYYY-MM-DDTHH:MM:SS') a
    'DD/MM/AAAA' (o 'DD/MM/AAAA HH:MM' si con_hora). Si no se puede parsear, devuelve
    el valor tal cual vino."""
    if not value:
        return ""
    s = str(value)
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%d/%m/%Y %H:%M") if con_hora else dt.strftime("%d/%m/%Y")
        dt = datetime.fromisoformat(s[:10])
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return s


# ---------------------------------------------------------------------------
# Configuración general (clave/valor)
# ---------------------------------------------------------------------------

def get_config():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM app_config").fetchall()
        stored = {r["key"]: r["value"] for r in rows}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in stored.items() if v is not None})
    return cfg


def _fernet():
    if Fernet is None:
        return None
    clave = base64.urlsafe_b64encode(hashlib.sha256(_ENC_PASSPHRASE.encode()).digest())
    return Fernet(clave)


def _encriptar(valor):
    if not valor:
        return ""
    f = _fernet()
    if not f:
        return valor  # sin el paquete "cryptography" instalado: se guarda tal cual
    return "enc:" + f.encrypt(valor.encode()).decode()


def _desencriptar(valor):
    if not valor:
        return ""
    if not valor.startswith("enc:"):
        return valor  # compatibilidad con un valor viejo guardado sin cifrar
    f = _fernet()
    if not f:
        return ""
    try:
        return f.decrypt(valor[len("enc:"):].encode()).decode()
    except InvalidToken:
        return ""


def get_gcg_api_key():
    """Devuelve la API key de GCG ya desencriptada, o "" si no hay ninguna cargada."""
    return _desencriptar(get_config().get("gcg_api_key") or "")


def set_gcg_api_key(valor_plano):
    set_config({"gcg_api_key": _encriptar((valor_plano or "").strip())})


def set_config(values):
    with get_conn() as conn:
        for k, v in values.items():
            conn.execute(
                """INSERT INTO app_config (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (k, v),
            )


# ---------------------------------------------------------------------------
# Log de actividad
# ---------------------------------------------------------------------------

def log_evento(tipo, mensaje, usuario=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO eventos_log (fecha, usuario_id, usuario_nombre, tipo, mensaje)
               VALUES (?, ?, ?, ?, ?)""",
            (
                now_iso(),
                usuario["id"] if usuario else None,
                (usuario.get("nombre_completo") or usuario.get("username")) if usuario else None,
                tipo,
                mensaje,
            ),
        )


def list_eventos(limit=300, tipo=None):
    with get_conn() as conn:
        q = "SELECT * FROM eventos_log"
        params = []
        if tipo:
            q += " WHERE tipo = ?"
            params.append(tipo)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def tipos_eventos():
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT tipo FROM eventos_log ORDER BY tipo").fetchall()
        return [r["tipo"] for r in rows]


def replace_proveedores(rows):
    """rows: lista de dicts con las columnas de la tabla proveedores (sin actualizado_en)."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute("DELETE FROM proveedores")
        conn.executemany(
            """INSERT INTO proveedores
               (nro_proveedor, razon_social, jrt, estado_actual, tipo_servicio, mail,
                telefono, domicilio, provincia, cuit, pais, observaciones, actualizado_en)
               VALUES (:nro_proveedor, :razon_social, :jrt, :estado_actual, :tipo_servicio, :mail,
                       :telefono, :domicilio, :provincia, :cuit, :pais, :observaciones, :ts)""",
            [{**r, "ts": ts} for r in rows],
        )
    return len(rows)


def replace_dni_proveedor(rows):
    """rows: lista de dicts {dni, nro_proveedor, fecha_actz}. Ya deduplicados aguas arriba."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute("DELETE FROM dni_proveedor")
        conn.executemany(
            """INSERT INTO dni_proveedor (dni, nro_proveedor, fecha_actz, actualizado_en)
               VALUES (:dni, :nro_proveedor, :fecha_actz, :ts)""",
            [{**r, "ts": ts} for r in rows],
        )
    return len(rows)


def replace_estado_bloqueos(rows):
    """rows: lista de dicts {dni, tipo, descripcion}. Es la "foto" real de quién
    está bloqueado hoy en el sistema (fuera de GCG), se pisa entera en cada carga."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute("DELETE FROM estado_bloqueos")
        conn.executemany(
            """INSERT INTO estado_bloqueos (dni, tipo, descripcion, actualizado_en)
               VALUES (:dni, :tipo, :descripcion, :ts)""",
            [{**r, "ts": ts} for r in rows],
        )
    set_config({"estado_bloqueos_actualizado_en": ts})
    return len(rows)


def esta_bloqueado_actual(dni):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM estado_bloqueos WHERE dni = ?", (dni,)).fetchone()
        return row is not None


def get_estado_bloqueos_map():
    """dni -> {tipo, descripcion} con la foto real de bloqueos vigente."""
    with get_conn() as conn:
        rows = conn.execute("SELECT dni, tipo, descripcion FROM estado_bloqueos").fetchall()
        return {r["dni"]: {"tipo": r["tipo"], "descripcion": r["descripcion"]} for r in rows}


def count_estado_bloqueos():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM estado_bloqueos").fetchone()["c"]


def get_documentos_control(solo_activos=False):
    with get_conn() as conn:
        q = "SELECT * FROM documentos_control"
        if solo_activos:
            q += " WHERE activo = 1"
        q += " ORDER BY orden, id"
        return [dict(r) for r in conn.execute(q).fetchall()]


def get_catalogo_documentos():
    """{numero: descripcion} de todos los tipos de documento de GCG."""
    with get_conn() as conn:
        rows = conn.execute("SELECT numero, descripcion FROM gcg_catalogo_documentos").fetchall()
        return {r["numero"]: r["descripcion"] for r in rows}


def set_documento_activo(doc_id, activo):
    with get_conn() as conn:
        conn.execute("UPDATE documentos_control SET activo = ? WHERE id = ?", (int(activo), doc_id))


def add_documento(nombre):
    with get_conn() as conn:
        maxorden = conn.execute("SELECT COALESCE(MAX(orden), -1) m FROM documentos_control").fetchone()["m"]
        conn.execute(
            "INSERT OR IGNORE INTO documentos_control (nombre, activo, orden, paises) VALUES (?, 1, ?, ?)",
            (nombre.strip(), maxorden + 1, PAISES_DEFAULT),
        )


def delete_documento(doc_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM documentos_control WHERE id = ?", (doc_id,))


def rename_documento(doc_id, nombre):
    with get_conn() as conn:
        conn.execute("UPDATE documentos_control SET nombre = ? WHERE id = ?", (nombre.strip(), doc_id))


def set_documento_gcg_numero(doc_id, numero):
    with get_conn() as conn:
        conn.execute("UPDATE documentos_control SET gcg_doc_numero = ? WHERE id = ?", (numero, doc_id))


def normalizar_paises(texto):
    """'ar, py ' -> 'AR,PY' (sin repetidos, en el orden en que se cargaron)."""
    codigos = []
    for c in re.split(r"[\s,;]+", (texto or "").upper()):
        if c and c not in codigos:
            codigos.append(c)
    return ",".join(codigos)


def set_documento_paises(doc_id, texto):
    paises = normalizar_paises(texto)
    with get_conn() as conn:
        conn.execute("UPDATE documentos_control SET paises = ? WHERE id = ?", (paises, doc_id))
    return paises


def documento_aplica_a_pais(doc, iso):
    """True si el documento se le controla a un chofer de ese país (ISO). Sin
    países cargados en el documento aplica a todos; sin país conocido del chofer
    (corridas viejas, o país que no se pudo identificar) se controla igual, para
    no dejar pasar a nadie por falta de dato."""
    paises = normalizar_paises(doc.get("paises"))
    if not paises or not iso:
        return True
    return iso.upper() in paises.split(",")


def counts():
    with get_conn() as conn:
        p = conn.execute("SELECT COUNT(*) c FROM proveedores").fetchone()["c"]
        d = conn.execute("SELECT COUNT(*) c FROM dni_proveedor").fetchone()["c"]
        e = conn.execute("SELECT COUNT(*) c FROM estado_bloqueos").fetchone()["c"]
        return {"proveedores": p, "dni_proveedor": d, "estado_bloqueos": e}


def create_run(fecha_run, archivo_origen, total_evaluados, total_sin_proveedor, total_excluidos):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO runs (fecha_run, archivo_origen, total_evaluados,
                                  total_choferes_sin_proveedor, total_excluidos)
               VALUES (?, ?, ?, ?, ?)""",
            (fecha_run, archivo_origen, total_evaluados, total_sin_proveedor, total_excluidos),
        )
        return cur.lastrowid


def insert_choferes_docs(run_id, rows):
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO choferes_docs
               (run_id, dni, nombre, condicion, nro_proveedor, razon_social, jrt,
                mail_proveedor, pais, documento, fecha_vencimiento)
               VALUES (:run_id, :dni, :nombre, :condicion, :nro_proveedor, :razon_social, :jrt,
                       :mail_proveedor, :pais, :documento, :fecha_vencimiento)""",
            [{**r, "run_id": run_id} for r in rows],
        )


def get_last_run():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def get_run(run_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def get_choferes_docs(run_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM choferes_docs WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------

def get_usuario_by_username(username):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_usuario(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_usuarios():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM usuarios ORDER BY rol DESC, nombre_completo").fetchall()
        return [dict(r) for r in rows]


def create_usuario(username, password, nombre_completo, jrt, rol, mail=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO usuarios (username, password_hash, nombre_completo, jrt, mail, rol, activo, creado_en)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (username.strip().lower(), generate_password_hash(password), nombre_completo, jrt or None,
             mail or None, rol, now_iso()),
        )


def get_usuario_by_jrt(jrt):
    """El usuario con rol 'jrt' a cargo de ese JRT. Si hay varios (no debería), prioriza
    uno que tenga mail cargado."""
    if not jrt:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM usuarios WHERE jrt = ? AND rol = 'jrt' AND activo = 1
               ORDER BY (mail IS NULL OR mail = '') ASC LIMIT 1""",
            (jrt,),
        ).fetchone()
        return dict(row) if row else None


def get_reemplazo_vigente(titular):
    """El usuario que cubre a este JRT hoy (vacaciones), o None si no hay
    reemplazo cargado, está fuera de fechas o ya no está activo."""
    if not titular or not titular.get("cubierto_por_id"):
        return None
    hoy = date.today().isoformat()
    if titular.get("ausencia_desde") and hoy < titular["ausencia_desde"]:
        return None
    if titular.get("ausencia_hasta") and hoy > titular["ausencia_hasta"]:
        return None
    reemplazo = get_usuario(titular["cubierto_por_id"])
    if not reemplazo or not reemplazo["activo"]:
        return None
    return reemplazo


def set_usuario_reemplazo(user_id, cubierto_por_id, desde, hasta):
    with get_conn() as conn:
        conn.execute(
            """UPDATE usuarios SET cubierto_por_id = ?, ausencia_desde = ?, ausencia_hasta = ?
               WHERE id = ?""",
            (cubierto_por_id or None, desde or None, hasta or None, user_id),
        )


def es_responsable_jrt(user, solicitud):
    """True si el usuario es el JRT titular del chofer de esta solicitud o quien
    lo cubre hoy por vacaciones."""
    if not user or not solicitud:
        return False
    titular = get_usuario_by_jrt(solicitud.get("jrt"))
    if not titular:
        return False
    if titular["id"] == user["id"]:
        return True
    reemplazo = get_reemplazo_vigente(titular)
    return bool(reemplazo and reemplazo["id"] == user["id"])


def get_admins():
    """Administradores activos con mail cargado."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM usuarios WHERE rol = 'admin' AND activo = 1 AND mail IS NOT NULL AND mail != ''"
        ).fetchall()
        return [dict(r) for r in rows]


def get_estado_bloqueo(dni):
    """{tipo, descripcion, actualizado_en} de la foto real de bloqueos para ese DNI, o None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tipo, descripcion, actualizado_en FROM estado_bloqueos WHERE dni = ?", (dni,)
        ).fetchone()
        return dict(row) if row else None


def get_mails_admin(excluir_mail=None):
    """Mails de todos los administradores activos con mail cargado."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mail FROM usuarios WHERE rol = 'admin' AND activo = 1 AND mail IS NOT NULL AND mail != ''"
        ).fetchall()
    mails = {r["mail"] for r in rows}
    mails.discard(excluir_mail)
    return sorted(mails)


def get_mails_analistas_por_jrt(jrt, excluir_mail=None):
    """Mails de los analistas activos que dependen de ese JRT."""
    if not jrt:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT mail FROM usuarios
               WHERE jrt = ? AND rol = 'analista' AND activo = 1 AND mail IS NOT NULL AND mail != ''""",
            (jrt,),
        ).fetchall()
    mails = {r["mail"] for r in rows}
    mails.discard(excluir_mail)
    return sorted(mails)


def set_usuario_activo(user_id, activo):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET activo = ? WHERE id = ?", (int(activo), user_id))


def set_usuario_mail(user_id, mail):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET mail = ? WHERE id = ?", (mail or None, user_id))


def set_usuario_password(user_id, password):
    with get_conn() as conn:
        conn.execute(
            "UPDATE usuarios SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )


# ---------------------------------------------------------------------------
# Solicitudes de desbloqueo
# ---------------------------------------------------------------------------

def crear_solicitud(row):
    row = {"origen": "manual", "gcg_json": None, "gcg_consultado_en": None, "gcg_error": None, **row}
    obs = None
    if row["gcg_json"]:
        try:
            obs = observacion_desde_gcg(json.loads(row["gcg_json"]), "solicitud", row["gcg_consultado_en"])
        except (ValueError, TypeError):
            obs = None
    with get_conn() as conn:
        if obs:
            _insert_historial(conn, [obs])
        cur = conn.execute(
            """INSERT INTO solicitudes_desbloqueo
               (run_id, dni, nombre, jrt, nro_proveedor, razon_social, documentos,
                estado, solicitado_por_id, solicitado_por_nombre, fecha_solicitud, origen,
                gcg_json, gcg_consultado_en, gcg_error)
               VALUES (:run_id, :dni, :nombre, :jrt, :nro_proveedor, :razon_social, :documentos,
                       'solicitado', :solicitado_por_id, :solicitado_por_nombre, :fecha_solicitud, :origen,
                       :gcg_json, :gcg_consultado_en, :gcg_error)""",
            row,
        )
        return cur.lastrowid


def solicitud_pendiente_existente(dni):
    """Una solicitud queda "abierta" para un DNI mientras no se ejecutó (liberó en
    JDE) ni se rechazó, sin importar de qué corrida haya salido: el chofer se
    puede bloquear y liberar en cualquier momento, no atado a una corrida
    puntual. Lo que no puede pasar es tener dos solicitudes abiertas juntas."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM solicitudes_desbloqueo
               WHERE dni = ? AND estado IN ('solicitado', 'autorizado')
               ORDER BY id DESC LIMIT 1""",
            (dni,),
        ).fetchone()
        return dict(row) if row else None


def solicitud_rechazada_sin_refrescar(dni, desde):
    """True si a este DNI ya se le rechazó una solicitud en un momento igual o
    posterior a `desde` (la fecha de carga del estado de bloqueos actual
    vigente). Si alguien la rechazó a mano fue por algo que la foto de GCG no
    ve (ej. un chofer sancionado con los documentos viejos todavía vigentes):
    el chequeo automático no debe volver a generarle otra hasta que se suba un
    estado de bloqueos más nuevo que ese rechazo."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM solicitudes_desbloqueo
               WHERE dni = ? AND estado = 'rechazado' AND fecha_autorizacion >= ?
               LIMIT 1""",
            (dni, desde),
        ).fetchone()
        return row is not None


def solicitud_ejecutada_sin_refrescar(dni, desde):
    """True si a este DNI ya se le ejecutó (liberó) una solicitud en un momento
    igual o posterior a `desde` (la fecha de carga del estado de bloqueos actual
    vigente). Mientras no se suba un estado de bloqueos más nuevo que esa
    ejecución, la foto real puede seguir mostrando al chofer como bloqueado por
    inercia, y el chequeo automático no debe generarle otra solicitud de más
    hasta que esa foto se actualice."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM solicitudes_desbloqueo
               WHERE dni = ? AND estado = 'ejecutado' AND fecha_ejecucion >= ?
               LIMIT 1""",
            (dni, desde),
        ).fetchone()
        return row is not None


# Orígenes en los que una persona decidió el desbloqueo (el JRT/admin la pidió
# directo, o la autorizó a mano): al quedar autorizadas son prioridad 1 para
# liberar, por encima de las que se aprobaron solas (GCG / críticos verdes).
ORIGENES_HUMANOS = ("manual", "jrt_auto", "admin_auto")


def list_solicitudes():
    """Primero las autorizadas prioridad 1, después las autorizadas automáticas,
    después las pendientes de autorizar y al final las cerradas; dentro de cada
    grupo, las más nuevas arriba."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT *,
                   CASE WHEN estado = 'autorizado' AND origen IN {ORIGENES_HUMANOS} THEN 1 ELSE 0 END AS prio1
                FROM solicitudes_desbloqueo
                ORDER BY CASE
                    WHEN estado = 'autorizado' AND origen IN {ORIGENES_HUMANOS} THEN 0
                    WHEN estado = 'autorizado' THEN 1
                    WHEN estado = 'solicitado' THEN 2
                    ELSE 3 END,
                  id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_solicitud(solicitud_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM solicitudes_desbloqueo WHERE id = ?", (solicitud_id,)).fetchone()
        return dict(row) if row else None


def autorizar_solicitud(solicitud_id, usuario_id, nombre, comentario=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE solicitudes_desbloqueo
               SET estado = 'autorizado', autorizado_por_id = ?, autorizado_por_nombre = ?,
                   fecha_autorizacion = ?, comentario = COALESCE(?, comentario)
               WHERE id = ?""",
            (usuario_id, nombre, now_iso(), comentario, solicitud_id),
        )


def rechazar_solicitud(solicitud_id, usuario_id, nombre, comentario=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE solicitudes_desbloqueo
               SET estado = 'rechazado', autorizado_por_id = ?, autorizado_por_nombre = ?,
                   fecha_autorizacion = ?, comentario = COALESCE(?, comentario)
               WHERE id = ?""",
            (usuario_id, nombre, now_iso(), comentario, solicitud_id),
        )


def ejecutar_solicitud(solicitud_id, usuario_id, nombre, comentario=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE solicitudes_desbloqueo
               SET estado = 'ejecutado', ejecutado_por_id = ?, ejecutado_por_nombre = ?,
                   fecha_ejecucion = ?, comentario = COALESCE(?, comentario)
               WHERE id = ?""",
            (usuario_id, nombre, now_iso(), comentario, solicitud_id),
        )
