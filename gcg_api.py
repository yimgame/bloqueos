# -*- coding: utf-8 -*-
"""Consulta a la interfaz WS Trabajador de GCG Control (ver gcg/Trabajador.pdf).

GET https://api.gcgevolution.com/interfaces/api/{API_TOKEN}/trabajador/{DNI}
Devuelve, entre otras cosas, "documentos": [{nombre, fechaVto, estado}, ...] para
ese trabajador. Para sus propios documentos (no los del contratista), "nombre"
es el número de documento del catálogo de GCG con 3 dígitos (ej. "010"), no un
texto — por eso el mapeo se hace por número (ver gcg/criticos.xlsx y la columna
"Nº GCG" en Configuración → Documentos), no por nombre.
"""
import os

import requests

import db

_ENV_PATH = os.path.join(os.path.dirname(__file__), "gcg", ".env")
_CLAVES_API_KEY_ENV = ("api_key", "api_ke", "apikey", "token", "api_token")

BASE_URL = "https://api.gcgevolution.com/interfaces/api/{api_key}/trabajador/{dni}"


def _cargar_api_key_env():
    """Lee la API key de gcg/.env (bootstrap inicial, antes de migrarla a la DB)."""
    if not os.path.exists(_ENV_PATH):
        return None
    with open(_ENV_PATH, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, _, valor = linea.partition("=")
            if clave.strip().lower() in _CLAVES_API_KEY_ENV:
                return valor.strip().strip('"').strip("'")
    return None


def _api_key():
    """La API key vive cifrada en la DB. Si todavía no se cargó ahí pero existe en
    gcg/.env, se migra automáticamente (se guarda cifrada en la DB) la primera vez."""
    clave_db = db.get_gcg_api_key()
    if clave_db:
        return clave_db
    clave_env = _cargar_api_key_env()
    if clave_env:
        db.set_gcg_api_key(clave_env)
        return clave_env
    return None


def api_key_configurada():
    return bool(_api_key())


def consultar_trabajador(dni, timeout=15):
    """Devuelve el JSON de GCG para ese DNI. Lanza excepción si no se puede consultar."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("No hay API key de GCG cargada (Configuración → GCG).")
    url = BASE_URL.format(api_key=api_key, dni=dni)
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fecha(valor):
    """La API devuelve las fechas como 'DD-MM-AAAA'; las mostramos como 'DD/MM/AAAA'."""
    if not valor:
        return None
    return str(valor).replace("-", "/")


def _docs_por_numero(data):
    return {str(d.get("nombre") or "").strip().lstrip("0") or "0": d for d in (data.get("documentos") or [])}


def documentos_aplicables(data, documentos_control):
    """Los documentos críticos que se le controlan a este trabajador según su país
    (campo "iso" de la respuesta, ej. "AR", "BO")."""
    iso = (data.get("iso") or "").strip() or None
    return [d for d in documentos_control if db.documento_aplica_a_pais(d, iso)]


def evaluar_criticos(data, documentos_control):
    """Para cada documento crítico activo que aplica al país del trabajador (con su
    Nº GCG mapeado), busca su estado en la respuesta de GCG. Devuelve una lista de
    dicts: {nombre, numero, encontrado, estado (bool|None), fecha}."""
    docs_api = _docs_por_numero(data)
    detalle = []
    for doc in documentos_aplicables(data, documentos_control):
        numero = doc.get("gcg_doc_numero")
        if not numero:
            detalle.append({"nombre": doc["nombre"], "numero": None, "encontrado": False, "estado": None, "fecha": None})
            continue
        d = docs_api.get(str(int(numero)))
        detalle.append({
            "nombre": doc["nombre"],
            "numero": numero,
            "encontrado": d is not None,
            "estado": bool(d.get("estado")) if d else None,
            "fecha": _fecha(d.get("fechaVto")) if d else None,
        })
    return detalle


def documentos_criticos_verdes(data, numeros_documentos_criticos):
    """True si TODOS los documentos críticos (identificados por su número de GCG)
    aparecen en la respuesta con estado=true (no vencido). Si falta alguno o
    figura vencido, False."""
    if not numeros_documentos_criticos:
        return False
    docs_api = _docs_por_numero(data)
    for numero in numeros_documentos_criticos:
        d = docs_api.get(str(int(numero)))
        if not d or not d.get("estado"):
            return False
    return True


def listar_todos_los_documentos(data, catalogo):
    """Todos los documentos del trabajador (no sólo los críticos), con su nombre
    traducido usando el catálogo {numero: descripcion}. Si el código no está en
    el catálogo, se muestra tal cual vino."""
    filas = []
    for d in data.get("documentos") or []:
        codigo = str(d.get("nombre") or "").strip()
        numero = int(codigo) if codigo.isdigit() else None
        nombre = catalogo.get(numero, codigo) if numero is not None else codigo
        filas.append({
            "numero": numero,
            "nombre": nombre,
            "estado": bool(d.get("estado")),
            "fecha": _fecha(d.get("fechaVto")),
        })
    filas.sort(key=lambda f: f["nombre"])
    return filas
