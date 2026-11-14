"""Read-only research UI: static files served by FastAPI at /ui.

Same origin, local assets only, GET/HEAD only (Starlette's StaticFiles answers 405 to every
other method). No data endpoint and no template engine: in this phase every value shown comes
from `static/demo/*.json`, each marked `"source": "DEMO"`. A Content-Security-Policy confines
the browser to this origin, so the UI cannot load or call anything external, and
`form-action 'none'` rules out form submissions.
"""

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"
UI_PATH = "/ui"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}

# On Windows `mimetypes` reads the registry, which can map .js/.css to text/plain; browsers
# refuse module scripts and stylesheets served with the wrong type.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class ReadOnlyStaticFiles(StaticFiles):
    """StaticFiles plus the security headers above on every served file."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(SECURITY_HEADERS)
        return response


def mount_ui(app: FastAPI) -> None:
    app.mount(UI_PATH, ReadOnlyStaticFiles(directory=STATIC_DIR, html=True), name="ui")
