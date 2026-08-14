from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import surfaces


def _surface(request: Request) -> dict:
    """Puts the request's surface into every template.

    A context processor rather than an argument at each call site: there is no
    shared context here — every route builds its own dict — so adding this by
    hand would mean touching nine TemplateResponse calls and remembering it on
    the tenth. The middleware in main.py resolves the surface; the getattr
    fallback covers templates rendered outside a request, such as the email body.
    """
    return {"surface": getattr(request.state, "surface", surfaces.DEFAULT)}


templates = Jinja2Templates(directory="app/templates", context_processors=[_surface])
