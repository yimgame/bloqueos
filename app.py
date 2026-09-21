# -*- coding: utf-8 -*-
"""App web para controlar el bloqueo de choferes por documentación crítica.

Uso:
    python app.py
    (se sirve en http://0.0.0.0:5000, accesible desde la red)
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file

import auth
import db
import gcg_api
import mailer
import pipeline

app = Flask(__name__)
app.secret_key = "bloqueo-choferes-doc-critica"
app.jinja_env.filters["fecha"] = db.fmt_fecha

db.init_db()

CHEQUEO_GCG_INTERVALO_SEG = 30 * 60


def _notificar_solicitud(nombre, dni, jrt, razon_social, documentos, fecha_solicitud,
                          autor_user=None, ya_autorizada=False):
    """Avisa por mail a los administradores y al JRT correspondiente de una
    solicitud de desbloqueo (nueva o recién auto-aprobada). El que la generó no
    va en copia: si fue un humano, ya le queda su registro en el sistema; si fue
    el chequeo automático de GCG, no hay a quién excluir."""
    excluir_mail = autor_user.get("mail") if autor_user else None
    destinatarios = set(db.get_mails_admin(excluir_mail=excluir_mail))
    jrt_usuario = db.get_usuario_by_jrt(jrt)
    if jrt_usuario and jrt_usuario.get("mail") and jrt_usuario["mail"] != excluir_mail:
        destinatarios.add(jrt_usuario["mail"])
    if not destinatarios:
        return

    quien = (autor_user["nombre_completo"] or autor_user["username"]) if autor_user else "El sistema (chequeo automático GCG)"
    estado_txt = (
        "Ya quedó autorizada: pendiente de liberar en GCG."
        if ya_autorizada else
        "Queda pendiente de autorización."
    )
    asunto = f"{'Desbloqueo pendiente de liberación' if ya_autorizada else 'Solicitud de desbloqueo'} - {nombre} (DNI {dni})"
    cuerpo = (
        f"{quien} generó el desbloqueo de:\n\n"
        f"Chofer: {nombre}\n"
        f"DNI: {dni}\n"
        f"JRT: {jrt or '—'}\n"
        f"Proveedor: {razon_social or '—'}\n"
        f"Documentos: {documentos or '—'}\n"
        f"Fecha: {db.fmt_fecha(fecha_solicitud, con_hora=True)}\n\n"
        f"{estado_txt} Revisalo en la sección Solicitudes del sistema."
    )
    try:
        mailer.enviar_mail(sorted(destinatarios), [], asunto, cuerpo)
        db.log_evento("mail", f"Aviso de desbloqueo ({nombre}, DNI {dni}) enviado a {', '.join(sorted(destinatarios))}", usuario=autor_user)
    except Exception as e:
        db.log_evento("mail", f"Error enviando aviso de desbloqueo ({nombre}, DNI {dni}): {e}", usuario=autor_user)
        if autor_user:
            flash(f"No se pudo enviar el mail de aviso de la solicitud: {e}", "error")


def _consultar_gcg_para_solicitud(dni):
    """Consulta a GCG en el momento de generar (o auto-generar) una solicitud de
    desbloqueo, para dejar guardado el JSON completo que se vio en ese momento
    (prueba de auditoría) y saber si con eso alcanza para auto-aprobar. Si la
    consulta falla no corta el flujo (puede pasar que GCG esté caído): la
    solicitud se genera igual, pero queda constancia del error en vez del JSON."""
    documentos_activos = db.get_documentos_control(solo_activos=True)
    numeros_criticos = [d["gcg_doc_numero"] for d in documentos_activos if d.get("gcg_doc_numero")]
    # Si falta mapear el Nº GCG de algún documento crítico activo, no se puede
    # confiar en el resultado (habría uno que ni se llegó a mirar): no se
    # auto-aprueba por "todos verdes" en ese caso.
    todos_mapeados = bool(documentos_activos) and len(numeros_criticos) == len(documentos_activos)
    try:
        data = gcg_api.consultar_trabajador(dni)
    except Exception as e:
        return {"gcg_json": None, "gcg_consultado_en": db.now_iso(), "gcg_error": str(e), "todos_verdes": False}
    todos_verdes = todos_mapeados and gcg_api.documentos_criticos_verdes(data, numeros_criticos)
    return {
        "gcg_json": json.dumps(data, ensure_ascii=False),
        "gcg_consultado_en": db.now_iso(),
        "gcg_error": None,
        "todos_verdes": todos_verdes,
    }


def _ejecutar_chequeo_gcg():
    """Recorre los choferes vencidos de la última corrida y le pregunta a GCG si
    ya tienen todos sus documentos críticos vigentes. Si es así, genera una
    solicitud de desbloqueo ya auto-aprobada, pendiente de liberación."""
    run = db.get_last_run()
    if not run:
        return
    if not gcg_api.api_key_configurada():
        db.log_evento("gcg_auto", "Chequeo automático GCG omitido: no hay API key configurada (Configuración → GCG).")
        return

    documentos_activos = db.get_documentos_control(solo_activos=True)
    sin_mapear = [d["nombre"] for d in documentos_activos if not d.get("gcg_doc_numero")]
    if sin_mapear:
        db.log_evento(
            "gcg_auto",
            "Chequeo automático GCG omitido: faltan mapear a su Nº GCG estos documentos críticos "
            f"(Configuración → Documentos): {', '.join(sin_mapear)}.",
        )
        return
    numeros_criticos = [d["gcg_doc_numero"] for d in documentos_activos]

    if db.count_estado_bloqueos() == 0:
        db.log_evento(
            "gcg_auto",
            "Chequeo automático GCG omitido: falta cargar el Excel de estado de bloqueos actual "
            "(Configuración → Datos de referencia). Sin esa foto no sabemos a quién le vale la pena "
            "consultarle a GCG.",
        )
        return

    resultado = pipeline.clasificar_run(run["id"], dias_alerta=0)
    revisados = ya_liberados = generados = errores = 0
    actor = "Sistema (chequeo automático GCG)"
    estado_bloqueos_en = db.get_config().get("estado_bloqueos_actualizado_en") or ""

    for c in resultado["vencidos"]:
        if db.solicitud_pendiente_existente(c["dni"]):
            continue
        # Si ya se le ejecutó una liberación y todavía no se subió un estado de
        # bloqueos más nuevo que esa ejecución, la foto real puede seguir
        # arrastrándolo como bloqueado por inercia: no generarle otra de más.
        if estado_bloqueos_en and db.solicitud_ejecutada_sin_refrescar(c["dni"], estado_bloqueos_en):
            continue
        # Si no figura en la foto real de bloqueos, ya está liberado (por lo que
        # sea) y no hace falta gastar una consulta a la API ni generar nada.
        if not db.esta_bloqueado_actual(c["dni"]):
            ya_liberados += 1
            continue
        revisados += 1
        try:
            data = gcg_api.consultar_trabajador(c["dni"])
        except Exception:
            errores += 1
            time.sleep(0.3)
            continue

        if gcg_api.documentos_criticos_verdes(data, numeros_criticos):
            fecha = db.now_iso()
            documentos_txt = ", ".join(d["documento"] for d in c["docs_vencidos"])
            solicitud_id = db.crear_solicitud({
                "run_id": run["id"],
                "dni": c["dni"],
                "nombre": c["nombre"],
                "jrt": c["jrt"],
                "nro_proveedor": c["nro_proveedor"],
                "razon_social": c["razon_social"],
                "documentos": documentos_txt,
                "solicitado_por_id": None,
                "solicitado_por_nombre": actor,
                "fecha_solicitud": fecha,
                "origen": "gcg_auto",
                "gcg_json": json.dumps(data, ensure_ascii=False),
                "gcg_consultado_en": fecha,
            })
            db.autorizar_solicitud(
                solicitud_id, None, actor,
                comentario="Todos los documentos críticos figuran vigentes en la consulta a GCG.",
            )
            db.log_evento("solicitud", f"Desbloqueo auto-generado por GCG para {c['nombre']} (DNI {c['dni']})")
            _notificar_solicitud(c["nombre"], c["dni"], c["jrt"], c["razon_social"], documentos_txt, fecha, ya_autorizada=True)
            generados += 1
        time.sleep(0.3)

    db.log_evento(
        "gcg_auto",
        f"Chequeo automático GCG: {revisados} chofer(es) consultado(s) a la API, "
        f"{ya_liberados} ya no figuraban bloqueados en la foto real (se saltearon), "
        f"{generados} desbloqueo(s) generado(s), {errores} con error de consulta.",
    )


def _bucle_chequeo_gcg():
    while True:
        time.sleep(CHEQUEO_GCG_INTERVALO_SEG)
        try:
            _ejecutar_chequeo_gcg()
        except Exception as e:
            db.log_evento("gcg_auto", f"Error en el chequeo automático de GCG: {e}")


# Con el reloader de Flask (debug=True) el script se importa dos veces: una en el
# proceso "vigía" y otra en el proceso que realmente sirve. WERKZEUG_RUN_MAIN sólo
# está seteado en este último, así evitamos arrancar el hilo dos veces. Ojo: acá
# abajo se evalúa "app.debug" antes de que app.run(debug=True) lo haya seteado, por
# lo que hay que fijarlo explícitamente primero (si no, el proceso "vigía" también
# arranca el hilo y quedan dos chequeos automáticos corriendo en paralelo).
app.debug = True
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    threading.Thread(target=_bucle_chequeo_gcg, daemon=True).start()


@app.before_request
def requerir_login():
    if request.endpoint in ("login", "static") or request.endpoint is None:
        return None
    if not auth.current_user():
        return redirect(url_for("login", next=request.path))


@app.context_processor
def inject_user():
    return {"current_user": auth.current_user()}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.verificar_login(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            db.log_evento("login", f"Inicio de sesión: {user['username']}", usuario=user)
            return redirect(request.args.get("next") or url_for("dashboard"))
        db.log_evento("login", f"Intento de login fallido: {username}")
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _dias_alerta_default():
    try:
        return int(db.get_config().get("dias_alerta_default") or 7)
    except (TypeError, ValueError):
        return 7


def _resolver_jrt_filtro(user, jrts):
    """Lista de JRT a mostrar. Los roles jrt/analista quedan fijos a su propio
    JRT. Para el resto: si se envió el filtro (form del dashboard, con
    jrt_submitted), se respeta lo tildado; si no, por defecto van todos los
    JRT activados salvo "Sin JRT / sin proveedor" (arranca sin tildar)."""
    if "jrt_submitted" in request.args:
        return request.args.getlist("jrt")
    if user["rol"] in ("jrt", "analista"):
        return [user["jrt"]] if user["jrt"] else []
    return [j for j in jrts if j != pipeline.SIN_JRT_LABEL]


def _cc_para_transporte(transporte, cfg, excluir=()):
    """CC de un mail a transporte: el JRT del transporte, los analistas a su cargo,
    los administradores (siempre en copia de todo) y la lista manual de Configuración."""
    cc = set()
    jrt_usuario = db.get_usuario_by_jrt(transporte["jrt"])
    if jrt_usuario and jrt_usuario.get("mail"):
        cc.add(jrt_usuario["mail"])
    cc.update(db.get_mails_analistas_por_jrt(transporte["jrt"]))
    cc.update(db.get_mails_admin())
    cc.update(m.strip() for m in re.split(r"[;,]", cfg.get("mail_cc_adicional") or "") if m.strip())
    cc -= set(excluir)
    return sorted(cc)


@app.route("/")
def dashboard():
    user = auth.current_user()
    run_id = request.args.get("run_id", type=int)
    dias_alerta = request.args.get("dias", default=_dias_alerta_default(), type=int)
    orden = request.args.get("sort") or None
    direccion = request.args.get("dir", default="asc")

    run = db.get_run(run_id) if run_id else db.get_last_run()
    if not run:
        return render_template("dashboard.html", run=None)

    jrts = pipeline.jrt_list_for_run(run["id"])
    jrt_filtro = _resolver_jrt_filtro(user, jrts)

    resultado = pipeline.clasificar_run(
        run["id"], dias_alerta=dias_alerta, jrt_filtro=jrt_filtro, orden=orden, direccion=direccion
    )
    solicitudes_por_dni = {
        s["dni"]: s for s in db.list_solicitudes() if s["run_id"] == run["id"]
    }
    documentos_activos = [d["nombre"] for d in db.get_documentos_control(solo_activos=True)]
    transportes = pipeline.resumen_por_transporte(run["id"], dias_alerta=dias_alerta, jrt_filtro=jrt_filtro)
    cfg = db.get_config()
    for t in transportes:
        destinatarios_to = [m.strip() for m in re.split(r"[;,]", t.get("mail_proveedor") or "") if m.strip()]
        t["cc_preview"] = _cc_para_transporte(t, cfg, excluir=destinatarios_to)

    return render_template(
        "dashboard.html",
        run=run,
        resultado=resultado,
        dias_alerta=dias_alerta,
        jrt_filtro=jrt_filtro,
        jrts=jrts,
        counts=db.counts(),
        orden=orden,
        direccion=direccion,
        solicitudes_por_dni=solicitudes_por_dni,
        documentos_activos=documentos_activos,
        transportes=transportes,
    )


@app.route("/export/<tipo>.xlsx")
def exportar(tipo):
    if tipo not in ("vencidos", "proximos"):
        return redirect(url_for("dashboard"))
    user = auth.current_user()
    run_id = request.args.get("run_id", type=int)
    dias_alerta = request.args.get("dias", default=_dias_alerta_default(), type=int)
    orden = request.args.get("sort") or None
    direccion = request.args.get("dir", default="asc")

    run = db.get_run(run_id) if run_id else db.get_last_run()
    if not run:
        return redirect(url_for("dashboard"))

    jrts = pipeline.jrt_list_for_run(run["id"])
    jrt_filtro = _resolver_jrt_filtro(user, jrts)

    resultado = pipeline.clasificar_run(
        run["id"], dias_alerta=dias_alerta, jrt_filtro=jrt_filtro, orden=orden, direccion=direccion
    )
    buf = pipeline.exportar_excel(resultado[tipo])
    nombre = f"{tipo}_{run['fecha_run'][:10]}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=nombre,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/subir-export", methods=["POST"])
@auth.admin_required
def subir_export():
    user = auth.current_user()
    file = request.files.get("archivo")
    if not file or file.filename == "":
        flash("Elegí un archivo del exportado de GCG.", "error")
        return redirect(url_for("admin", tab="datos"))
    if db.counts()["proveedores"] == 0 or db.counts()["dni_proveedor"] == 0:
        flash("Primero cargá el Maestro de proveedores y el dni.xlsx.", "error")
        return redirect(url_for("admin", tab="datos"))
    try:
        resumen = pipeline.procesar_gcg_export(file, archivo_origen=file.filename)
    except Exception as e:
        flash(f"Error procesando el archivo: {e}", "error")
        db.log_evento("corrida", f"Error procesando '{file.filename}': {e}", usuario=user)
        return redirect(url_for("admin", tab="datos"))

    msg = (
        f"Procesados {resumen['total_evaluados']} choferes "
        f"({resumen['total_excluidos']} excluidos por extranjero/independiente, "
        f"{resumen['total_sin_proveedor']} sin número de proveedor)."
    )
    if resumen["missing_doc_cols"]:
        msg += " No se encontraron columnas para: " + ", ".join(resumen["missing_doc_cols"])
    flash(msg, "ok")
    db.log_evento(
        "corrida",
        f"Nueva corrida #{resumen['run_id']} desde '{file.filename}': {msg}",
        usuario=user,
    )

    # Dispara el chequeo de GCG al toque, sin esperar el ciclo de 30 min, para que
    # esta corrida ya genere sus solicitudes de desbloqueo si corresponde.
    db.log_evento("gcg_auto", "Chequeo de GCG disparado automáticamente al procesar la corrida", usuario=user)
    threading.Thread(target=_ejecutar_chequeo_gcg, daemon=True).start()

    return redirect(url_for("dashboard", run_id=resumen["run_id"]))


@app.route("/enviar-mail-transporte", methods=["POST"])
def enviar_mail_transporte():
    user = auth.current_user()
    run_id = request.form.get("run_id", type=int)
    dias_alerta = request.form.get("dias", default=_dias_alerta_default(), type=int)
    nro_proveedor = request.form.get("nro_proveedor", type=int)
    razon_social = request.form.get("razon_social")

    run = db.get_run(run_id)
    if not run:
        flash("No se encontró la corrida.", "error")
        return redirect(url_for("dashboard"))

    transportes = pipeline.resumen_por_transporte(run_id, dias_alerta=dias_alerta)
    transporte = next(
        (t for t in transportes if (
            t["nro_proveedor"] == nro_proveedor if nro_proveedor else t["razon_social"] == razon_social
        )),
        None,
    )
    if not transporte:
        flash("No se encontró información para ese transporte.", "error")
        return redirect(url_for("dashboard", run_id=run_id, dias=dias_alerta))

    destinatarios_to = [m.strip() for m in re.split(r"[;,]", transporte.get("mail_proveedor") or "") if m.strip()]
    if not destinatarios_to:
        flash(f"El proveedor {transporte['razon_social']} no tiene mail cargado en el maestro.", "error")
        return redirect(url_for("dashboard", run_id=run_id, dias=dias_alerta))

    cfg = db.get_config()
    cc = _cc_para_transporte(transporte, cfg, excluir=destinatarios_to)

    hoy = datetime.now().date()
    contexto = {
        "proveedor": transporte["razon_social"] or "",
        "jrt": transporte["jrt"] or "",
        "fecha": hoy.strftime("%d/%m/%Y"),
        "bloqueados": len(transporte["vencidos"]),
        "proximos": len(transporte["proximos"]),
    }
    try:
        asunto = (cfg.get("mail_asunto") or "").format(**contexto)
        cuerpo = (cfg.get("mail_cuerpo") or "").format(**contexto)
    except (KeyError, IndexError):
        asunto = cfg.get("mail_asunto") or ""
        cuerpo = cfg.get("mail_cuerpo") or ""

    buf = pipeline.exportar_excel_transporte(transporte)
    nombre_slug = re.sub(r"[^A-Za-z0-9]+", "_", transporte["razon_social"] or "transporte")
    nombre_archivo = f"bloqueos_{nombre_slug}_{hoy.isoformat()}.xlsx"

    try:
        mailer.enviar_mail(destinatarios_to, cc, asunto, cuerpo, buf.getvalue(), nombre_archivo)
    except Exception as e:
        flash(f"Error enviando el mail: {e}", "error")
        db.log_evento(
            "mail",
            f"Error enviando mail a {transporte['razon_social']} ({', '.join(destinatarios_to)}): {e}",
            usuario=user,
        )
        return redirect(url_for("dashboard", run_id=run_id, dias=dias_alerta))

    destinos = ", ".join(destinatarios_to + cc)
    flash(f"Mail enviado a {transporte['razon_social']} ({destinos}).", "ok")
    db.log_evento(
        "mail",
        f"Mail de bloqueados/próximos enviado a {transporte['razon_social']} "
        f"(To: {', '.join(destinatarios_to)}; CC: {', '.join(cc) or '—'}; "
        f"{len(transporte['vencidos'])} bloqueados, {len(transporte['proximos'])} próximos)",
        usuario=user,
    )
    return redirect(url_for("dashboard", run_id=run_id, dias=dias_alerta))


# ---------------------------------------------------------------------------
# Consulta en vivo a GCG (buscador por DNI)
# ---------------------------------------------------------------------------

@app.route("/gcg/consultar")
def gcg_consultar():
    dni = (request.args.get("dni") or "").strip()
    dni = re.sub(r"\D", "", dni)
    contexto = {"dni_buscado": dni, "trabajador": None, "error": None}

    if dni:
        try:
            data = gcg_api.consultar_trabajador(dni)
        except Exception as e:
            contexto["error"] = f"No se pudo consultar GCG: {e}"
        else:
            documentos_activos = db.get_documentos_control(solo_activos=True)
            criticos = gcg_api.evaluar_criticos(data, documentos_activos)
            todos_verdes = bool(criticos) and all(d["estado"] for d in criticos)
            catalogo = db.get_catalogo_documentos()
            proveedor = pipeline.buscar_proveedor_por_dni(dni)
            run = db.get_last_run()
            solicitud_activa = db.solicitud_pendiente_existente(dni)

            documentos_txt = "; ".join(
                f"{d['nombre']}: {'OK' if d['estado'] else ('VENCIDO' if d['encontrado'] else 'no encontrado en GCG')}"
                + (f" (vto {d['fecha']})" if d["fecha"] else "")
                for d in criticos
            )

            contexto.update({
                "trabajador": {
                    "nombre": f"{data.get('apellido', '')}, {data.get('nombre', '')}".strip(", "),
                    "dni": data.get("dni") or dni,
                    "habilitado_gcg": data.get("habilitado"),
                },
                "criticos": criticos,
                "todos_verdes": todos_verdes,
                "documentos_todos": gcg_api.listar_todos_los_documentos(data, catalogo),
                "proveedor": proveedor,
                "run": run,
                "documentos_txt": documentos_txt,
                "solicitud_activa": solicitud_activa,
            })

    return render_template("gcg_consulta.html", **contexto)


@app.route("/historial")
def historial():
    runs = db.list_runs()
    return render_template("historial.html", runs=runs)


# ---------------------------------------------------------------------------
# Solicitudes de desbloqueo
# ---------------------------------------------------------------------------

@app.route("/solicitar-desbloqueo", methods=["POST"])
def solicitar_desbloqueo():
    user = auth.current_user()
    run_id = request.form.get("run_id", type=int)
    dni = request.form.get("dni")
    nombre = request.form.get("nombre")
    jrt = request.form.get("jrt")
    razon_social = request.form.get("razon_social")
    documentos = request.form.get("documentos")
    existente = db.solicitud_pendiente_existente(dni)
    if existente:
        flash(
            f"Ya hay una solicitud de desbloqueo activa para ese chofer: la pidió "
            f"{existente['solicitado_por_nombre']} el {db.fmt_fecha(existente['fecha_solicitud'], con_hora=True)}.",
            "error",
        )
        return redirect(request.referrer or url_for("dashboard"))
    fecha_solicitud = db.now_iso()

    # Se consulta a GCG en el momento de pedir el desbloqueo (no los datos de la
    # corrida cargada, que pueden ser viejos): eso queda guardado como prueba de
    # auditoría y también sirve para decidir el auto-aprobado por críticos verdes.
    consulta = _consultar_gcg_para_solicitud(dni)
    if consulta["gcg_error"]:
        flash(f"No se pudo consultar GCG al generar la solicitud: {consulta['gcg_error']}. Se registra igual.", "error")

    # Admin y JRT pueden liberar directamente, sin importar cómo estén los
    # documentos. Para cualquier otro rol, si en la consulta a GCG los críticos
    # ya figuran todos vigentes, también se auto-aprueba (misma regla que el
    # chequeo automático); si no, queda pendiente de que el JRT la autorice.
    if user["rol"] == "jrt":
        origen = "jrt_auto"
    elif user["rol"] == "admin":
        origen = "admin_auto"
    elif consulta["todos_verdes"]:
        origen = "criticos_verdes"
    else:
        origen = "manual"
    auto_aprueba = origen != "manual"

    solicitud_id = db.crear_solicitud({
        "run_id": run_id,
        "dni": dni,
        "nombre": nombre,
        "jrt": jrt,
        "nro_proveedor": request.form.get("nro_proveedor") or None,
        "razon_social": razon_social,
        "documentos": documentos,
        "solicitado_por_id": user["id"],
        "solicitado_por_nombre": user["nombre_completo"] or user["username"],
        "fecha_solicitud": fecha_solicitud,
        "origen": origen,
        "gcg_json": consulta["gcg_json"],
        "gcg_consultado_en": consulta["gcg_consultado_en"],
        "gcg_error": consulta["gcg_error"],
    })
    flash(f"Solicitud de desbloqueo registrada para {nombre}.", "ok")
    db.log_evento(
        "solicitud",
        f"{user['nombre_completo'] or user['username']} solicitó el desbloqueo de {nombre} "
        f"(DNI {dni}, JRT {jrt or '—'}, {razon_social or 'sin proveedor'})",
        usuario=user,
    )

    if auto_aprueba:
        if origen == "criticos_verdes":
            comentario = "Todos los documentos críticos figuran vigentes en la consulta a GCG al momento de la solicitud."
        else:
            comentario = f"Auto-aprobado: lo pidió {user['rol']}, no importa cómo estén los documentos."
        db.autorizar_solicitud(
            solicitud_id, user["id"], user["nombre_completo"] or user["username"],
            comentario=comentario,
        )
        db.log_evento("solicitud", f"Solicitud de {nombre} auto-aprobada ({comentario})", usuario=user)

    _notificar_solicitud(nombre, dni, jrt, razon_social, documentos, fecha_solicitud,
                          autor_user=user, ya_autorizada=auto_aprueba)

    return redirect(request.referrer or url_for("dashboard"))


@app.route("/solicitudes")
def solicitudes():
    catalogo = db.get_catalogo_documentos()
    lista = db.list_solicitudes()
    for s in lista:
        s["gcg_documentos"] = None
        s["gcg_trabajador"] = None
        if s.get("gcg_json"):
            try:
                data = json.loads(s["gcg_json"])
            except (ValueError, TypeError):
                data = None
            if data:
                s["gcg_documentos"] = gcg_api.listar_todos_los_documentos(data, catalogo)
                s["gcg_trabajador"] = {
                    "nombre": f"{data.get('apellido', '')}, {data.get('nombre', '')}".strip(", "),
                    "dni": data.get("dni"),
                    "habilitado": data.get("habilitado"),
                }
    return render_template("solicitudes.html", solicitudes=lista)


@app.route("/solicitudes/export.xlsx")
def solicitudes_exportar():
    buf = pipeline.exportar_excel_solicitudes(db.list_solicitudes())
    nombre = f"solicitudes_desbloqueo_{datetime.now().date().isoformat()}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=nombre,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/solicitudes/<int:solicitud_id>/autorizar", methods=["POST"])
def solicitud_autorizar(solicitud_id):
    user = auth.current_user()
    s = db.get_solicitud(solicitud_id)
    if s and s["solicitado_por_id"] == user["id"] and user["rol"] != "admin":
        flash("No podés autorizar tu propia solicitud.", "error")
        return redirect(url_for("solicitudes"))
    db.autorizar_solicitud(solicitud_id, user["id"], user["nombre_completo"] or user["username"])
    flash("Solicitud autorizada.", "ok")
    db.log_evento("solicitud", f"Solicitud #{solicitud_id} autorizada ({s['nombre'] if s else ''})", usuario=user)
    return redirect(url_for("solicitudes"))


@app.route("/solicitudes/<int:solicitud_id>/rechazar", methods=["POST"])
def solicitud_rechazar(solicitud_id):
    user = auth.current_user()
    comentario = request.form.get("comentario") or None
    db.rechazar_solicitud(solicitud_id, user["id"], user["nombre_completo"] or user["username"], comentario)
    flash("Solicitud rechazada.", "ok")
    db.log_evento("solicitud", f"Solicitud #{solicitud_id} rechazada" + (f" ({comentario})" if comentario else ""), usuario=user)
    return redirect(url_for("solicitudes"))


@app.route("/solicitudes/<int:solicitud_id>/ejecutar", methods=["POST"])
@auth.admin_required
def solicitud_ejecutar(solicitud_id):
    user = auth.current_user()
    db.ejecutar_solicitud(solicitud_id, user["id"], user["nombre_completo"] or user["username"])
    flash("Desbloqueo marcado como ejecutado.", "ok")
    db.log_evento("solicitud", f"Solicitud #{solicitud_id} marcada como ejecutada", usuario=user)
    return redirect(url_for("solicitudes"))


# ---------------------------------------------------------------------------
# Configuración (maestro, dni, documentos controlados)
# ---------------------------------------------------------------------------

ADMIN_TABS = ("general", "smtp", "documentos", "datos", "usuarios", "gcg", "log")


@app.route("/admin", methods=["GET"])
@auth.admin_required
def admin():
    active_tab = request.args.get("tab", "general")
    if active_tab not in ADMIN_TABS:
        active_tab = "general"
    filtro_tipo = request.args.get("tipo_log") or None
    documentos = db.get_documentos_control()
    return render_template(
        "admin.html",
        counts=db.counts(),
        documentos=documentos,
        config=db.get_config(),
        usuarios=db.list_usuarios(),
        active_tab=active_tab,
        eventos=db.list_eventos(limit=300, tipo=filtro_tipo),
        tipos_log=db.tipos_eventos(),
        filtro_tipo=filtro_tipo,
        gcg_api_key_configurada=gcg_api.api_key_configurada(),
        gcg_intervalo_min=CHEQUEO_GCG_INTERVALO_SEG // 60,
        gcg_sin_mapear=[d for d in documentos if d["activo"] and not d.get("gcg_doc_numero")],
        gcg_eventos=db.list_eventos(limit=20, tipo="gcg_auto"),
        run=db.get_last_run(),
    )


@app.route("/admin/config", methods=["POST"])
@auth.admin_required
def admin_config():
    valores = {
        "dias_alerta_default": request.form.get("dias_alerta_default", "").strip() or "7",
        "smtp_host": request.form.get("smtp_host", "").strip(),
        "smtp_port": request.form.get("smtp_port", "").strip() or "587",
        "smtp_user": request.form.get("smtp_user", "").strip(),
        "smtp_from": request.form.get("smtp_from", "").strip(),
        "smtp_use_tls": "1" if request.form.get("smtp_use_tls") == "1" else "0",
        "mail_cc_adicional": request.form.get("mail_cc_adicional", "").strip(),
        "mail_asunto": request.form.get("mail_asunto", "").strip(),
        "mail_cuerpo": request.form.get("mail_cuerpo", ""),
    }
    nueva_password = request.form.get("smtp_password", "")
    if nueva_password:
        valores["smtp_password"] = nueva_password
    db.set_config(valores)
    flash("Configuración guardada.", "ok")
    tab = request.form.get("tab", "general")
    db.log_evento("config", f"Configuración actualizada (sección: {tab})", usuario=auth.current_user())
    return redirect(url_for("admin", tab=tab))


@app.route("/admin/gcg/api-key", methods=["POST"])
@auth.admin_required
def admin_gcg_api_key():
    nueva = request.form.get("gcg_api_key", "").strip()
    if nueva:
        db.set_gcg_api_key(nueva)
        flash("API key de GCG guardada.", "ok")
        db.log_evento("config", "API key de GCG actualizada", usuario=auth.current_user())
    else:
        flash("Ingresá una API key para guardar.", "error")
    return redirect(url_for("admin", tab="gcg"))


@app.route("/admin/gcg/chequear-ahora", methods=["POST"])
@auth.admin_required
def admin_gcg_chequear_ahora():
    user = auth.current_user()
    db.log_evento("gcg_auto", "Chequeo manual de GCG iniciado desde Configuración", usuario=user)
    threading.Thread(target=_ejecutar_chequeo_gcg, daemon=True).start()
    flash("Chequeo de GCG iniciado en segundo plano. El resultado va a aparecer en la Actividad en unos minutos.", "ok")
    return redirect(url_for("admin", tab="gcg"))


@app.route("/admin/gcg/eventos.json")
@auth.admin_required
def admin_gcg_eventos_json():
    eventos = db.list_eventos(limit=20, tipo="gcg_auto")
    return {
        "eventos": [
            {"id": e["id"], "fecha": db.fmt_fecha(e["fecha"], con_hora=True), "mensaje": e["mensaje"]}
            for e in eventos
        ]
    }


@app.route("/admin/maestro", methods=["POST"])
@auth.admin_required
def admin_maestro():
    file = request.files.get("archivo")
    if not file or file.filename == "":
        flash("Elegí el archivo del Maestro de proveedores.", "error")
        return redirect(url_for("admin", tab="datos"))
    try:
        n = pipeline.load_maestro_file(file)
        flash(f"Maestro actualizado: {n} proveedores cargados.", "ok")
        db.log_evento("datos", f"Maestro de proveedores actualizado: {n} proveedores ({file.filename})", usuario=auth.current_user())
    except Exception as e:
        flash(f"Error cargando el maestro: {e}", "error")
        db.log_evento("datos", f"Error cargando maestro de proveedores ({file.filename}): {e}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="datos"))


@app.route("/admin/dni", methods=["POST"])
@auth.admin_required
def admin_dni():
    file = request.files.get("archivo")
    if not file or file.filename == "":
        flash("Elegí el archivo dni.xlsx.", "error")
        return redirect(url_for("admin", tab="datos"))
    try:
        n = pipeline.load_dni_file(file)
        flash(f"DNI/proveedor actualizado: {n} choferes cargados.", "ok")
        db.log_evento("datos", f"DNI/proveedor actualizado: {n} choferes ({file.filename})", usuario=auth.current_user())
    except Exception as e:
        flash(f"Error cargando dni.xlsx: {e}", "error")
        db.log_evento("datos", f"Error cargando dni.xlsx ({file.filename}): {e}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="datos"))


@app.route("/admin/estado-bloqueos", methods=["POST"])
@auth.admin_required
def admin_estado_bloqueos():
    file = request.files.get("archivo")
    if not file or file.filename == "":
        flash("Elegí el archivo de estado de bloqueos.", "error")
        return redirect(url_for("admin", tab="datos"))
    try:
        n = pipeline.load_estado_bloqueos_file(file)
        flash(f"Estado de bloqueos actualizado: {n} choferes bloqueados hoy.", "ok")
        db.log_evento("datos", f"Estado de bloqueos actualizado: {n} bloqueados ({file.filename})", usuario=auth.current_user())
    except Exception as e:
        flash(f"Error cargando el estado de bloqueos: {e}", "error")
        db.log_evento("datos", f"Error cargando estado de bloqueos ({file.filename}): {e}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="datos"))


@app.route("/admin/documentos/agregar", methods=["POST"])
@auth.admin_required
def admin_doc_agregar():
    nombre = request.form.get("nombre", "").strip()
    if nombre:
        db.add_documento(nombre)
        flash(f"Documento agregado: {nombre}", "ok")
        db.log_evento("documento", f"Documento agregado: {nombre}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="documentos"))


@app.route("/admin/documentos/<int:doc_id>/renombrar", methods=["POST"])
@auth.admin_required
def admin_doc_renombrar(doc_id):
    nombre = request.form.get("nombre", "").strip()
    if nombre:
        anterior = db.get_documentos_control()
        nombre_anterior = next((d["nombre"] for d in anterior if d["id"] == doc_id), None)
        db.rename_documento(doc_id, nombre)
        flash("Documento actualizado.", "ok")
        db.log_evento("documento", f"Documento renombrado: '{nombre_anterior}' → '{nombre}'", usuario=auth.current_user())
    return redirect(url_for("admin", tab="documentos"))


@app.route("/admin/documentos/<int:doc_id>/gcg-numero", methods=["POST"])
@auth.admin_required
def admin_doc_gcg_numero(doc_id):
    valor = request.form.get("gcg_doc_numero", "").strip()
    numero = int(valor) if valor.isdigit() else None
    db.set_documento_gcg_numero(doc_id, numero)
    flash("Número de GCG actualizado.", "ok")
    db.log_evento("documento", f"Documento #{doc_id}: número de GCG actualizado a {numero if numero is not None else '(vacío)'}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="documentos"))


@app.route("/admin/documentos/<int:doc_id>/toggle", methods=["POST"])
@auth.admin_required
def admin_doc_toggle(doc_id):
    activo = request.form.get("activo") == "1"
    db.set_documento_activo(doc_id, activo)
    db.log_evento("documento", f"Documento #{doc_id} marcado como {'activo' if activo else 'inactivo'}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="documentos"))


@app.route("/admin/documentos/<int:doc_id>/borrar", methods=["POST"])
@auth.admin_required
def admin_doc_borrar(doc_id):
    db.delete_documento(doc_id)
    flash("Documento eliminado del control.", "ok")
    db.log_evento("documento", f"Documento #{doc_id} eliminado del control", usuario=auth.current_user())
    return redirect(url_for("admin", tab="documentos"))


# ---------------------------------------------------------------------------
# Usuarios (solo admin)
# ---------------------------------------------------------------------------

@app.route("/admin/usuarios")
@auth.admin_required
def admin_usuarios():
    return redirect(url_for("admin", tab="usuarios"))


@app.route("/admin/usuarios/agregar", methods=["POST"])
@auth.admin_required
def admin_usuarios_agregar():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    nombre = request.form.get("nombre_completo", "").strip()
    jrt = request.form.get("jrt", "").strip() or None
    mail = request.form.get("mail", "").strip() or None
    rol = request.form.get("rol", "jrt")
    if not username or not password:
        flash("Usuario y contraseña son obligatorios.", "error")
        return redirect(url_for("admin", tab="usuarios"))
    if db.get_usuario_by_username(username):
        flash("Ya existe un usuario con ese nombre.", "error")
        return redirect(url_for("admin", tab="usuarios"))
    db.create_usuario(username, password, nombre, jrt, rol, mail)
    flash(f"Usuario {username} creado.", "ok")
    db.log_evento("usuario", f"Usuario creado: {username} (rol {rol}, JRT {jrt or '—'})", usuario=auth.current_user())
    return redirect(url_for("admin", tab="usuarios"))


@app.route("/admin/usuarios/<int:user_id>/toggle", methods=["POST"])
@auth.admin_required
def admin_usuarios_toggle(user_id):
    activo = request.form.get("activo") == "1"
    db.set_usuario_activo(user_id, activo)
    objetivo = db.get_usuario(user_id)
    nombre = objetivo["username"] if objetivo else user_id
    db.log_evento("usuario", f"Usuario {nombre} marcado como {'activo' if activo else 'inactivo'}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="usuarios"))


@app.route("/admin/usuarios/<int:user_id>/mail", methods=["POST"])
@auth.admin_required
def admin_usuarios_mail(user_id):
    mail = request.form.get("mail", "").strip() or None
    db.set_usuario_mail(user_id, mail)
    flash("Mail actualizado.", "ok")
    objetivo = db.get_usuario(user_id)
    nombre = objetivo["username"] if objetivo else user_id
    db.log_evento("usuario", f"Mail de {nombre} actualizado a {mail or '(vacío)'}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="usuarios"))


@app.route("/admin/usuarios/<int:user_id>/resetear-password", methods=["POST"])
@auth.admin_required
def admin_usuarios_resetear(user_id):
    nueva = request.form.get("password", "").strip()
    if not nueva:
        flash("Ingresá una contraseña nueva.", "error")
        return redirect(url_for("admin", tab="usuarios"))
    db.set_usuario_password(user_id, nueva)
    flash("Contraseña actualizada.", "ok")
    objetivo = db.get_usuario(user_id)
    nombre = objetivo["username"] if objetivo else user_id
    db.log_evento("usuario", f"Contraseña restablecida para {nombre}", usuario=auth.current_user())
    return redirect(url_for("admin", tab="usuarios"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
