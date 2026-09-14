"""Link Mapper pages.

Graphs live in the browser (IndexedDB) with everything else the analyst
creates, so these routes serve shells and `linkmap.js` fills them from the
store. The editor is addressed as `/linkmap/edit?id=N`, where N is a
browser-side key.

The old server-addressed URLs are kept as redirects rather than deleted: they
used to render the editor with the id resolving to nothing, which drew every
control but bound none of them -- indistinguishable from a broken page.
"""

from flask import Blueprint, redirect, render_template, url_for

from ..auth.routes import login_required

linkmap = Blueprint("linkmap", __name__, url_prefix="/linkmap")


@linkmap.route("/")
@login_required
def list_maps():
    return render_template("linkmap/list.html", graphs=None)


@linkmap.route("/edit")
@login_required
def edit_page():
    """Editor shell for a browser-stored graph, addressed by ?id=."""
    return render_template("linkmap/editor.html", graph=None, profiles=[],
                           browser_store=True)


@linkmap.route("/new")
@login_required
def new_map():
    """A new map is created in the browser, so start the editor with no id."""
    return redirect(url_for("linkmap.edit_page"))


@linkmap.route("/<int:graph_id>")
@login_required
def edit_map(graph_id):
    """Legacy URL for a specific map."""
    return redirect(url_for("linkmap.edit_page", id=graph_id), code=301)


@linkmap.route("/profile/<int:pid>")
@login_required
def profile_map(pid):
    """Legacy per-profile map URL.

    The profile page now builds (or reuses) this map client-side, because both
    the profile and the graph live in the browser.
    """
    return redirect(url_for("profiles.detail", profile_id=pid), code=301)
