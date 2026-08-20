#!/usr/bin/env python
"""The round trip with wxrks: catalogues out, translations back, nothing broken.

    python scripts/translations.py export
        -> dist/wordcounter-translations-<date>.zip
           (the ten .po files + TRANSLATING.md for whoever runs the batch)
           Refuses when the catalogue is stale against the code: a translator
           working on last week's strings is money spent twice.

    python scripts/translations.py import <zip-or-directory>
        -> validates every returned .po BEFORE anything lands in locales/,
           then writes, compiles, and prints per-language coverage.

The validation exists because of one specific failure: 38 strings carry
%(name)s placeholders, and a translation that drops or renames one raises at
RENDER time — one bad string and the page using it 500s, in that language
only. The import makes that class of bug impossible to commit; the matching
test in tests/test_i18n.py keeps it impossible for hand-edits too.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"
DIST = ROOT / "dist"
LANGS = ("ar", "de", "es", "fr", "it", "ja", "ko", "pt", "sv", "zh")

PLACEHOLDER_RE = re.compile(r"%\([a-zA-Z_]+\)s")

TRANSLATING_MD = """\
# Translating Word Counter

Ten gettext catalogues, one per language: ar de es fr it ja ko pt sv zh.
`msgid` is the English source — **never edit it, it is the key**. `msgstr` is
where the translation goes. An empty `msgstr` falls back to English, so a
partial batch is safe to return.

Rules that keep the app running:

1. **`%(name)s` placeholders must survive exactly** — same names, same count.
   They are substituted at runtime; a translation that drops or renames one
   crashes the page that uses it. "%(n)s pages at a time" -> the %(n)s moves
   wherever the language needs it, but it must still be %(n)s.
2. **Short strings live in small chips.** Anything under ~20 characters in
   English probably sits in a pill or button; German and French run long, and
   a 50-character label will look wrong even though nothing breaks.
3. Product names — Word Counter, Site to Markdown, wxrks, Pro — are names.
   Translate around them, not through them, unless local convention says
   otherwise.
4. Arrows and separators (→, ·, ⟶) are layout, keep them.

Return the same folder structure: <lang>/LC_MESSAGES/messages.po.
"""


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _pot_is_stale() -> bool:
    """Would `pybabel extract` change messages.pot? Compared with creation
    dates and location comments stripped, since those churn on every run."""
    with tempfile.NamedTemporaryFile(suffix=".pot", delete=False) as handle:
        fresh = Path(handle.name)
    try:
        subprocess.run(
            [sys.executable, "-m", "babel.messages.frontend", "extract",
             "-F", "babel.cfg", "-o", str(fresh), "--no-location", "--sort-output", "."],
            cwd=ROOT, check=True, capture_output=True,
        )

        def strings(path: Path) -> set[str]:
            with path.open("rb") as fh:
                return {m.id for m in read_po(fh) if m.id}

        return strings(fresh) != strings(LOCALES / "messages.pot")
    finally:
        fresh.unlink(missing_ok=True)


def cmd_export() -> None:
    if _pot_is_stale():
        fail("messages.pot is stale — run pybabel extract + update first, "
             "or a translator will work on last week's strings")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"wordcounter-translations-{datetime.date.today().isoformat()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("TRANSLATING.md", TRANSLATING_MD)
        for lang in LANGS:
            po = LOCALES / lang / "LC_MESSAGES" / "messages.po"
            if not po.exists():
                fail(f"{lang} has no catalogue at {po}")
            bundle.write(po, f"{lang}/LC_MESSAGES/messages.po")
    print(f"wrote {out.relative_to(ROOT)} — {len(LANGS)} catalogues + TRANSLATING.md")


def _validate_po(path: Path, lang: str) -> tuple[list[str], list[str], int, int]:
    """(errors, warnings, translated, total) for one returned catalogue."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with path.open("rb") as handle:
            catalog = read_po(handle)
    except Exception as exc:
        return [f"{lang}: cannot parse — {exc}"], [], 0, 0

    translated = total = 0
    for message in catalog:
        if not message.id or isinstance(message.id, tuple) is None:
            continue
        source = message.id if isinstance(message.id, str) else message.id[0]
        if not source:
            continue
        total += 1
        target = message.string if isinstance(message.string, str) else (message.string or [""])[0]
        if not target:
            continue
        translated += 1

        want = sorted(PLACEHOLDER_RE.findall(source))
        got = sorted(PLACEHOLDER_RE.findall(target))
        if want != got:
            errors.append(
                f"{lang}: placeholders changed in {source[:60]!r} — "
                f"source has {want or 'none'}, translation has {got or 'none'}"
            )
        if any(ord(ch) < 32 and ch not in "\n" for ch in target):
            errors.append(f"{lang}: control character in translation of {source[:60]!r}")
        if len(source) <= 20 and len(target) > 3 * max(len(source), 8):
            warnings.append(f"{lang}: {source[:40]!r} grew from {len(source)} to {len(target)} chars — check the chip it lives in")
    return errors, warnings, translated, total


def cmd_import(source: str) -> None:
    src = Path(source)
    workdir: Path
    if src.is_file() and src.suffix == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="translations-"))
        with zipfile.ZipFile(src) as bundle:
            bundle.extractall(tmp)
        workdir = tmp
    elif src.is_dir():
        workdir = src
    else:
        fail(f"{source} is neither a zip nor a directory")
        return

    incoming: dict[str, Path] = {}
    for lang in LANGS:
        for candidate in (workdir / lang / "LC_MESSAGES" / "messages.po",
                          workdir / f"{lang}.po", workdir / lang / "messages.po"):
            if candidate.exists():
                incoming[lang] = candidate
                break

    if not incoming:
        fail("no catalogues found — expected <lang>/LC_MESSAGES/messages.po")

    # Validate EVERYTHING before writing ANYTHING: a batch that is half good
    # should not leave locales/ half updated.
    all_errors: list[str] = []
    report: dict[str, tuple[int, int]] = {}
    for lang, path in sorted(incoming.items()):
        errors, warnings, translated, total = _validate_po(path, lang)
        all_errors.extend(errors)
        for warning in warnings:
            print(f"warning: {warning}")
        report[lang] = (translated, total)
    if all_errors:
        for line in all_errors:
            print(f"error: {line}", file=sys.stderr)
        fail(f"{len(all_errors)} problem(s) — nothing was imported")

    for lang, path in sorted(incoming.items()):
        dest = LOCALES / lang / "LC_MESSAGES" / "messages.po"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "compile", "-d", str(LOCALES)],
        cwd=ROOT, check=True, capture_output=True,
    )

    print("\ncoverage:")
    for lang, (translated, total) in sorted(report.items()):
        bar = "#" * int(20 * translated / total) if total else ""
        print(f"  {lang}  {translated:>4}/{total:<4} {bar}")
    missing = [lang for lang in LANGS if lang not in incoming]
    if missing:
        print(f"  not in this batch: {', '.join(missing)}")
    print("\nimported — run the tests, then commit the .po diffs. "
          "The deploy compiles them; no code change is needed.")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "export":
        cmd_export()
    elif len(sys.argv) >= 3 and sys.argv[1] == "import":
        cmd_import(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
