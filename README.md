# sigal-tag-gallery

A [sigal](https://github.com/saimn/sigal) plugin that builds **per-keyword "tag"
pages** for a static photo gallery. For every keyword in your photos, it
generates one page aggregating every image that carries that keyword across all
albums (e.g. `/tag/landscape.html`), plus a tag-list index — and it can turn the
filename captions on your normal album pages into links to those tag pages.

It is a drop-in plugin: nothing in the installed `sigal` package is modified.

## Features

- One flat page per keyword (`<base_path>/<slug>.html`) aggregating matching
  photos from every album, plus a tag-list index. Each page opens with a
  breadcrumb up to the tag-list index (`Tags » portland`) and a summary line
  naming the count and thumbnail order (e.g. `131 photos · newest first`).
- Pages render through your **active theme's templates**, so they match the
  rest of the site.
- Relative links are recomputed for the tag-page location, so thumbnails and
  full images resolve correctly.
- Optional **linked captions** on normal album pages: each filename token that
  is also a tag links to its tag page (needs a small, backwards-compatible theme
  tweak — see below).
- `min_count` threshold and a `tag_gallery_exclude` set (e.g. to keep people's
  names out of public tag pages).
- Idempotent and cache-friendly: only changed tag pages are rewritten, and
  stale pages are pruned.

## Requirements

- sigal 2.x (tested against 2.5)
- Pillow (already a sigal dependency) for the default IPTC backend
- Optional: `defusedxml` for the `xmp` backend; `exiftool` on `PATH` for the
  `exiftool` backend

## Install

This is a files-only plugin. Clone it somewhere and point sigal at it:

```sh
git clone https://github.com/reagle/sigal-tag-gallery.git
```

Then in your `sigal.conf.py`:

```python
plugin_paths = ["/path/to/sigal-tag-gallery"]  # dir containing tag_gallery.py
plugins = ["tag_gallery"]                       # registered by module name

tag_gallery = {
    "backend": "iptc",     # "iptc" | "xmp" | "exiftool"
    "base_path": "tag",    # output dir under the gallery root
    "min_count": 1,        # skip tags with fewer than N images
    "title": "Tags",       # heading on the tag-list index page
}
```

`plugin_paths` entries are added to `sys.path` as-is (sigal does **not** rewrite
them relative to the config file), so use an absolute path unless you always run
`sigal build` from the same directory.

If you already set `plugin_paths`/`plugins` for other plugins, append to the
existing lists rather than redefining them.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"iptc"` | Where to read keywords: `iptc`, `xmp`, or `exiftool` |
| `base_path` | `"tag"` | Output directory under the gallery root |
| `min_count` | `1` | Skip keywords with fewer than this many images |
| `title` | `"Tags"` | Heading/title of the tag-list index page |
| `month_names` | `True` | Caption breadcrumb months as names (`June`) vs numbers (`06`) |
| `sort_attr` | `None` | Thumbnail sort key; `None` inherits `medias_sort_attr` |
| `sort_reverse` | `None` | Reverse thumbnail order; `None` inherits `medias_sort_reverse` |

### Thumbnail order

Tag pages and the tag-list index get their own media sort, independent of the
album pages. Set `sort_attr` / `sort_reverse` inside the `tag_gallery` dict:

```python
tag_gallery = {
    "sort_attr": "date",      # "filename", "date", or "meta.<key>"
    "sort_reverse": True,     # newest-first
}
```

Both use the same values and natural-sort semantics as sigal's
`medias_sort_attr` / `medias_sort_reverse`. Leaving either at `None` (the
default) inherits the gallery-wide setting, so tag pages match your albums
unless you say otherwise. Changing either value re-renders every tag page (the
order is part of the cache signature).

### Excluding tags

A top-level `tag_gallery_exclude` set suppresses tags entirely — no page, no
index entry, and not linked in any caption (they render as plain text). Entries
are slug-matched, so case and spacing don't matter (`"New York"` == `"new-york"`):

```python
tag_gallery_exclude = {"alice", "bob", "private-trip"}
```

(You may also pass `"exclude"` inside the `tag_gallery` dict; the top-level set
takes precedence.) Editing it re-renders every page — captions depend on which
tags exist — and prunes the dropped pages.

## Keyword backends

Many libraries write the same keywords to both XMP `dc:subject` and legacy IPTC.

- **`iptc`** (default) — reads IPTC-IIM record 2, dataset 25 via Pillow, with no
  extra dependency. sigal's own IPTC reader only extracts title/description, so
  this plugin reads the keyword dataset itself from each photo's source file.
- **`xmp`** — reads XMP `dc:subject`/`hierarchicalSubject` via Pillow's
  `getxmp()`, which requires `defusedxml` in sigal's environment (otherwise it
  silently returns nothing).
- **`exiftool`** — shells out to the `exiftool` CLI; authoritative but slower.

## URL scheme

Pages are flat files under `base_path`:

```
<destination>/tag/index.html      # tag-list index  → /tag/
<destination>/tag/<slug>.html     # one page per keyword → /tag/<slug>.html
```

Keywords are slugified for the URL (`Black & White` → `black-white`). If
`site_url` is unset, links are relative and computed with `os.path.relpath`
from the tag-page directory to each asset.

## Linked captions on album pages (optional)

The plugin can also make the filename captions on your **normal album pages**
link to tag pages. This needs one backwards-compatible change to your theme's
`album.html` figcaption: render the plugin-supplied `name_parts` when present,
and fall back to the plain title otherwise (so the theme keeps working with the
plugin disabled):

```jinja
{% if media.name_parts is defined and media.name_parts %}
  {% for label, href in media.name_parts -%}
    {% if href %}<a href="{{ href }}">{{ label }}</a>{% else %}{{ label }}{% endif %}{{ "-" if not loop.last }}
  {%- endfor %} - {{ media.exif.datetime }}
{% else %}
  {{ media.title }} - {{ media.exif.datetime }}
{% endif %}
```

The tag pages themselves need **no** theme change — the plugin ships its own
`tag_album.html` (which extends your theme's `album.html`).

Note: when active, these captions show filename tokens, so a photo with a
distinct IPTC/Markdown title will show its filename instead. Underscores in
keywords render as hyphens.

## Summary header on album pages (optional)

The plugin also exposes `album.summary` on normal album pages — a one-line
`<count> <unit> · <sort order>` matching the tag-page header (e.g.
`31 photos · oldest first` on a leaf album, `12 albums · normal sort` on a
container album that lists sub-albums). The count and sort label track sigal's own
`medias_sort_*` / `albums_sort_*` settings. To show it, add this at the top of
your theme's `album.html` **and** `album_list.html` `{% block content %}`:

```jinja
{% if album.summary is defined and album.summary %}
<p class="gallery-summary">{{ album.summary }}</p>
{% endif %}
```

The `is defined` guard keeps the theme working with the plugin disabled. Style
`.gallery-summary` in your CSS as you like; tag pages emit the same element.

## How it works

The plugin connects three sigal signals:

- **`gallery_initialized`** — scans every photo's keywords once, applies
  `min_count` and `tag_gallery_exclude`, and caches the resulting tag set
  (so the scan runs once and both phases below agree on which tags have pages).
- **`before_render`** — wraps each normal album so its photos expose
  `name_parts` for the caption template above, and the album exposes `summary`
  for the header. Image/thumbnail links are untouched.
- **`gallery_build`** — renders the per-tag pages and tag index through the
  theme templates with relpath-corrected links, then prunes stale pages.

Idempotency: a per-tag hash of member paths + source mtimes (plus a hash of the
whole tag set) is cached in `<destination>/<base_path>/.tag_gallery_cache.json`;
unchanged tag pages are skipped on rebuild.

**Note:** the cache keys on the photos and the tag set, *not* on the template
content. After editing `tag_album.html` (or the plugin itself), clear the cache
so existing tag pages re-render:

```sh
rm <destination>/tag/.tag_gallery_cache.json   # then re-run `sigal build`
```

## License

MIT — see [LICENSE](LICENSE).
