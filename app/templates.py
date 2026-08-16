from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import surfaces


def _globals(request: Request) -> dict:
    """Puts the request's surface, and whether billing exists at all, into every
    template.

    A context processor rather than an argument at each call site: there is no
    shared context here — every route builds its own dict — so adding this by
    hand would mean touching nine TemplateResponse calls and remembering it on
    the tenth. The middleware in main.py resolves the surface; the getattr
    fallback covers templates rendered outside a request, such as the email body.

    billing_enabled is imported lazily to keep the import graph one-way:
    app.billing already imports this module for `templates`.
    """
    from app.billing import billing_enabled

    return {
        "surface": getattr(request.state, "surface", surfaces.DEFAULT),
        "billing_enabled": billing_enabled(),
    }


def format_duration(total_seconds) -> str:
    """"~1h 8m" / "~25 min". Mirrors formatDuration() in app/static/app.js so a
    duration reads the same whether the page rendered it or the browser did."""
    seconds = int(total_seconds or 0)
    if seconds < 60:
        return "less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"~{hours}h {remainder}m" if remainder else f"~{hours}h"


templates = Jinja2Templates(directory="app/templates", context_processors=[_globals])
templates.env.filters["duration"] = format_duration
