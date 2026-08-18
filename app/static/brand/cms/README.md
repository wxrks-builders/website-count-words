# Platform logos for the translation offer

Drop an SVG here named after the platform's slug and the offer picks it up on
the next restart — `webflow.svg`, `contentful.svg`, `wordpress.svg`,
`drupal.svg`. See `_logo_url` in `app/promos.py`.

Nothing is shipped here on purpose. These are other companies' trademarks, and
an approximation drawn from memory would be both worse than no logo and a
misuse of someone's mark. Use the official asset from each platform's brand
page, and check their brand guidelines — most permit "works with X" usage, some
require a specific lockup or minimum clear space.

Keep them square-ish and legible at 18px: that is the size they render at, on
the near-black the offer sits on, so a dark monochrome mark will disappear.
A white or single-colour-light version is usually the right one.

The offer works without them — the platform is named in the copy either way,
and the accent colour already carries the cue. Email never shows a logo at all,
because SVG support across mail clients is too unreliable to depend on.
