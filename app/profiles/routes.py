"""Profile pages.

Profiles live in the browser (IndexedDB) alongside watches, posts and link
maps, so these routes render shells and the client fills them from the store.
Nothing here reads or writes user data.

Digital Hunter correlates watches against profiles, which is why they had to
move: leaving them server-side would have meant a browser-stored watch
depending on server-stored identities, and the whole point of the storage model
is that your data stays in one place.

The legacy `Profile` model is still defined so that the optional server sync
(`/monitor/api/sync/*`) has somewhere to put a pushed copy.
"""

from flask import Blueprint, render_template

from ..auth.routes import login_required

profiles = Blueprint("profiles", __name__, url_prefix="/profiles")


@profiles.route("/")
@login_required
def list_profiles():
    return render_template("profiles/list.html")


@profiles.route("/new")
@login_required
def new_profile():
    return render_template("profiles/form.html", mode="new")


@profiles.route("/<int:profile_id>")
@login_required
def detail(profile_id):
    """Profile detail. The id is a browser-side key, resolved client-side."""
    return render_template("profiles/detail.html", profile_id=profile_id)


@profiles.route("/<int:profile_id>/edit")
@login_required
def edit_profile(profile_id):
    return render_template("profiles/form.html", mode="edit",
                           profile_id=profile_id)
