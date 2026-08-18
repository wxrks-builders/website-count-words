# Platform logos for the translation offer

Drop a file here named after the platform's slug and the offer picks it up on
the next restart: `webflow`, `contentful`, `wordpress`, `drupal`. See
`connector_for` in `app/promos.py`.

`.svg`, `.webp` and `.png` all work on the web, and vector wins when a platform
has more than one. **Email only ever uses `.png`** — SVG is unsupported in
Outlook and WebP fails there too, so a platform without a PNG simply gets no
logo in mail rather than a broken image. Webflow is currently in that position.

Each mark renders at 16px on a light rounded tile, so a dark logo on a
transparent background still reads against the near-black the offer sits on.
That also means there's no need to recolour anyone's mark, which most brand
guidelines forbid.

Nothing is committed here by the app itself — these are other companies'
trademarks. Take the official asset from each platform's brand page and check
their guidelines; most permit "works with X" usage, some require a specific
lockup or minimum clear space.

The offer works without any of them: the platform is named in the copy either
way, and the accent colour already carries the cue.
