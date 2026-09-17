# -*- coding: utf-8 -*-
"""Login por sesión (sin librerías externas): hash de contraseña con
werkzeug.security, usuario logueado guardado en la sesión de Flask."""
from functools import wraps

from flask import session, redirect, url_for, request
from werkzeug.security import check_password_hash

import db


def verificar_login(username, password):
    user = db.get_usuario_by_username(username)
    if not user or not user["activo"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db.get_usuario(user_id)
    if not user or not user["activo"]:
        return None
    return user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if user["rol"] != "admin":
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped
