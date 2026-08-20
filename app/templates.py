from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import i18n, js_strings, surfaces


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
    from app.auth import is_admin
    from app.billing import billing_enabled

    lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
    # The path with its language prefix already stripped by the middleware, so
    # the alternates below can be built for every language from one place.
    bare_path = i18n.split_path(request.url.path)[1]

    _install_language(lang)
    return {
        "surface": getattr(request.state, "surface", surfaces.DEFAULT),
        "billing_enabled": billing_enabled(),
        # A predicate rather than a flag: the context processor has the request
        # but not whichever user the route resolved, so the template applies it
        # to the user it already has.
        "is_admin": is_admin,
        "lang": lang,
        "lang_dir": "rtl" if lang in i18n.RTL_LANGUAGES else "ltr",
        "hreflang": i18n.HREFLANG.get(lang, lang),
        "languages": i18n.selectable_languages(),
        "language_names": i18n.LANGUAGE_NAMES,
        # Every internal href goes through this, so a link written once works in
        # all eleven. Static assets pass through unprefixed — they aren't
        # translated, and prefixing them would make each language miss the
        # browser cache for a file they all share.
        "url": lambda path: i18n.localized_path(path, lang),
        "alternates": [
            (i18n.HREFLANG.get(code, code), i18n.localized_path(bare_path, code))
            for code in i18n.LANGUAGES
        ],
        "num": lambda value: i18n.format_number(value or 0, lang),
        "js_strings": js_strings.catalogue(lang),
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
# Jinja's own i18n extension, so {% trans %} and _() work in templates and
# pybabel can extract from them without a custom parser.
templates.env.add_extension("jinja2.ext.i18n")


def _install_language(lang: str) -> None:
    templates.env.install_gettext_callables(
        gettext=i18n.gettext_for(lang), ngettext=lambda s, p, n: i18n.gettext_for(lang)(s if n == 1 else p), newstyle=True
    )


# Re-bound per request in the context processor below, because one Jinja
# environment is shared by every language.
_install_language(i18n.DEFAULT_LANG)
