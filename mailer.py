# -*- coding: utf-8 -*-
"""Envío de mails vía SMTP usando la configuración guardada en app_config."""
import smtplib
from email.message import EmailMessage

import db


def enviar_mail(destinatarios_to, destinatarios_cc, asunto, cuerpo, adjunto_bytes=None, adjunto_nombre=None,
                html=None):
    """cuerpo es el texto plano; si viene html, va como versión alternativa (la
    que muestran Outlook y cualquier cliente moderno)."""
    cfg = db.get_config()
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("El SMTP no está configurado. Cargá los datos en Configuración.")
    if not destinatarios_to:
        raise ValueError("No hay destinatario para este mail.")

    port = int(cfg.get("smtp_port") or 587)
    usuario = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    remitente = (cfg.get("smtp_from") or usuario).strip()
    usar_tls = (cfg.get("smtp_use_tls") or "1") == "1"

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = ", ".join(destinatarios_to)
    if destinatarios_cc:
        msg["Cc"] = ", ".join(destinatarios_cc)
    msg.set_content(cuerpo)
    if html:
        msg.add_alternative(html, subtype="html")

    if adjunto_bytes is not None:
        msg.add_attachment(
            adjunto_bytes,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=adjunto_nombre or "adjunto.xlsx",
        )

    todos = list(destinatarios_to) + list(destinatarios_cc or [])
    with smtplib.SMTP(host, port, timeout=30) as server:
        if usar_tls:
            server.starttls()
        if usuario:
            server.login(usuario, password)
        server.send_message(msg, to_addrs=todos)
