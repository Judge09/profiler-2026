import hmac
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
import config

auth = Blueprint("auth", __name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


@auth.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("profiles.list_profiles"))

    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw, config.PASSWORD):
            session["authed"] = True
            session.permanent = False
            return redirect(url_for("profiles.list_profiles"))
        else:
            error = "Invalid password."

    return render_template("auth/login.html", error=error)


@auth.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth.route("/")
def index():
    if session.get("authed"):
        return redirect(url_for("profiles.list_profiles"))
    return redirect(url_for("auth.login"))
