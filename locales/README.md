# Translation catalogues

Ten languages, the same ones wxrks.com publishes: **ar, de, es, fr, it, ja, ko,
pt, sv, zh**. English is the source and lives at the root of the site, so it has
no catalogue of its own.

## The loop with wxrks

    python scripts/translations.py export        # -> dist/wordcounter-translations-<date>.zip
    #   upload to wxrks, translate the ten languages, export .po back
    python scripts/translations.py import <zip>  # validates, writes, compiles, reports coverage
    #   run pytest, commit the .po diffs, push — the deploy compiles them

The export refuses a stale catalogue; the import refuses a batch with broken
placeholders before writing anything. Under the hood it is still plain gettext:

    pybabel extract -F babel.cfg -o locales/messages.pot --no-location --sort-output .
    pybabel update  -i locales/messages.pot -d locales
    pybabel compile -d locales

`.po` files are committed. `.mo` files are **not** — they are build output, and
`pybabel compile` runs in the Dockerfile.

Admin pages are deliberately not extracted; see the comment in `babel.cfg`.

## Translating

These are gettext `.po` files, which every translation system reads. Send them
through wxrks like any other content: `msgid` is the English source, `msgstr` is
where the translation goes, and an empty `msgstr` falls back to English rather
than rendering blank — so a half-translated language is safe to deploy.

Two things worth telling a translator:

- **`%(name)s` placeholders must survive.** They are substituted at runtime; a
  translation that drops one, or renames it, raises instead of rendering.
- **Length matters here.** Several strings sit in a fixed-width chip or a
  single-line banner. German and French run long; the layout wraps, but a
  50-character label in a pill will look wrong.

## Adding a language

Add it to `LANGUAGES` and `HREFLANG` in `app/i18n.py`, add its own-language name
to `LANGUAGE_NAMES`, then:

    pybabel init -i locales/messages.pot -d locales -l xx

A language whose catalogue does not exist yet renders English rather than
failing, so the code change can land before the translations do.

## Finding strings that were never extracted

    PSEUDO_LOCALE=1 uvicorn app.main:app

Then browse `/zz/`. Every translated string comes back wrapped in brackets;
anything still bare is hard-coded English that needs marking up. This finds the
strings you did not think to look for, which grepping does not.
