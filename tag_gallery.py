"""Sigal plugin: build per-keyword "tag" gallery pages.

For each keyword found across every album, render one page aggregating every
image carrying that keyword (e.g. ``/<base_path>/<slug>.html``), plus a
tag-list index page. Pages reuse the active theme's templates so they match the
rest of the site, and normal album pages get the same filename-token caption
links via the ``before_render`` signal.

Enable it from ``sigal.conf.py`` (never edit the installed ``sigal`` package)::

    plugin_paths = ["."]                 # dir containing this file
    plugins = ["tag_gallery"]            # registered by module name (picklable)
    tag_gallery = {"backend": "iptc", "base_path": "tag", "min_count": 1}

Keyword backend
---------------
Many photo libraries store identical keywords in XMP ``dc:subject`` *and* legacy
IPTC. Pillow's ``getxmp()`` needs ``defusedxml``, which is often missing from
sigal's environment, so XMP reads come back empty there. IPTC (record 2,
dataset 25) is read by Pillow with no extra dependency, so it is the default
backend. ``xmp`` and ``exiftool`` backends are also available for libraries
tagged differently.
"""

import calendar
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from natsort import natsort_keygen, ns
from PIL import Image, IptcImagePlugin
from sigal import signals
from sigal.writer import THEMES_PATH, AbstractWriter

PLUGIN_TEMPLATES = Path(__file__).resolve().parent / "templates"

logger = logging.getLogger(__name__)

#: XMP/exiftool keys (namespace-stripped) that hold keywords.
KEYWORD_KEYS = {"subject", "Keywords", "hierarchicalSubject"}

#: Valid 1-based month numbers, for turning a "MM" album component into a name.
_MONTHS = range(1, 13)

DEFAULTS: dict[str, Any] = {
    "backend": "iptc",  # "iptc" | "xmp" | "exiftool"
    "base_path": "tag",  # output dir under the gallery root
    "min_count": 1,  # skip tags with fewer than this many images
    "title": "Tags",  # title of the tag-list index page
    "month_names": True,  # caption crumb months as names ("June") vs numbers ("06")
    # Thumbnail order on tag pages. None inherits sigal's gallery-wide
    # medias_sort_attr / medias_sort_reverse; set either to override just here.
    "sort_attr": None,  # "filename" | "date" | "meta.<key>" | None (inherit)
    "sort_reverse": None,  # True | False | None (inherit)
}

#: Per-build shared state, populated once on gallery_initialized and reused by
#: both the album-caption linker (before_render) and the tag-page builder
#: (gallery_build), so the keyword scan runs only once and both agree on the
#: set of tags that have pages.
_STATE: dict[str, Any] = {}


def slugify(text: str) -> str:
    """Convert a keyword to a URL-safe slug.

    >>> slugify("Landscape")
    'landscape'
    >>> slugify("Black & White")
    'black-white'
    >>> slugify("  Cafe  Mornings ")
    'cafe-mornings'
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _coerce_str_list(value: object) -> list[str]:
    """Flatten an XMP/IPTC value (string, bytes, list, or nested dict) to strings."""
    match value:
        case str():
            return [value]
        case bytes():
            return [value.decode("utf-8", "replace")]
        case list():
            return [s for item in value for s in _coerce_str_list(item)]
        case dict():
            return [s for item in value.values() for s in _coerce_str_list(item)]
        case _:
            return []


def _walk_for_keys(obj: object, wanted: set[str]) -> list[str]:
    """Recursively collect values stored under any namespace-suffixed wanted key."""
    found: list[str] = []
    match obj:
        case dict():
            for key, value in obj.items():
                if key.split(":")[-1] in wanted:
                    found.extend(_coerce_str_list(value))
                else:
                    found.extend(_walk_for_keys(value, wanted))
        case list():
            for item in obj:
                found.extend(_walk_for_keys(item, wanted))
    return found


def _keywords_iptc(src_path: str) -> list[str]:
    """Read keywords from legacy IPTC-IIM (record 2, dataset 25) via Pillow."""
    try:
        with Image.open(src_path) as im:
            info = IptcImagePlugin.getiptcinfo(im)
    except (OSError, SyntaxError):
        return []
    if not info:
        return []
    return _coerce_str_list(info.get((2, 25), []))


def _keywords_xmp(src_path: str) -> list[str]:
    """Read keywords from XMP dc:subject / hierarchicalSubject via Pillow.

    Returns nothing unless ``defusedxml`` is installed in sigal's environment.
    """
    try:
        with Image.open(src_path) as im:
            xmp = im.getxmp()
    except (OSError, AttributeError):
        return []
    return _walk_for_keys(xmp, KEYWORD_KEYS)


def _keywords_exiftool(src_path: str) -> list[str]:
    """Read keywords via the exiftool CLI (authoritative, if installed)."""
    if shutil.which("exiftool") is None:
        return []
    # `--` ends option parsing, so a filename beginning with "-" can't be
    # mistaken for an exiftool option.
    cmd = ["exiftool", "-j", "-Keywords", "-Subject", "--", str(src_path)]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    records = json.loads(out.stdout or "[]")
    if not records:
        return []
    return [
        s
        for k, v in records[0].items()
        if k != "SourceFile"
        for s in _coerce_str_list(v)
    ]


BACKENDS = {
    "iptc": _keywords_iptc,
    "xmp": _keywords_xmp,
    "exiftool": _keywords_exiftool,
}


def extract_keywords(src_path: str, backend: str) -> list[str]:
    """Return de-duplicated keywords for an image using the chosen backend."""
    reader = BACKENDS.get(backend, _keywords_iptc)
    return list(dict.fromkeys(kw.strip() for kw in reader(src_path) if kw.strip()))


@dataclass
class _LinkContext:
    """Shared inputs for resolving a media's links from a page directory."""

    tags: set[str]  # slugs that have a tag page (so tokens can link to them)
    base_dir: Path  # directory the tag pages live in
    destination: Path  # gallery output root
    output_file: str  # index filename (e.g. "index.html")
    month_names: bool = True  # show crumb months as names vs numbers


class _MediaProxy:
    """Wrap a sigal media so its links resolve from an arbitrary page directory.

    ``url``/``thumbnail``/``big_url`` are recomputed relative to ``page_dir``
    (the directory of the page being rendered) with ``os.path.relpath``; on a
    tag page ``page_dir`` is the tag dir, on a normal album page it is that
    album's own dir (so the links come out identical to sigal's). Tag-page
    links in ``name_parts`` are computed relative to ``page_dir`` from
    ``ctx.base_dir``. Everything else (``type``, ``size``, ``exif``, ``mime``,
    ...) delegates to the real media.
    """

    def __init__(self, media: Any, page_dir: str | Path, ctx: _LinkContext) -> None:
        self._media = media
        self._page_dir = page_dir
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._media, name)

    @property
    def name_parts(self) -> list[tuple[str, str | None]]:
        """Filename tokens, each linked to its tag page when one exists."""
        from sigal.utils import url_from_path

        parts: list[tuple[str, str | None]] = []
        for token in re.split(r"[-_]+", self._media.basename):
            if not token:
                continue
            slug = slugify(token)
            if slug in self._ctx.tags:
                target = self._ctx.base_dir / f"{slug}.html"
                href = url_from_path(os.path.relpath(target, self._page_dir))
            else:
                href = None
            parts.append((token, href))
        return parts

    @property
    def album_crumbs(self) -> list[tuple[str, str | None]]:
        """Source album path (year, month, ...) linked to each album page.

        Year stays numeric; a 1-12 month component is shown as a month name.
        """
        from sigal.utils import url_from_path

        comps = [c for c in self._media.path.split("/") if c]
        crumbs: list[tuple[str, str | None]] = []
        for i, comp in enumerate(comps):
            target = self._ctx.destination.joinpath(
                *comps[: i + 1], self._ctx.output_file
            )
            href = url_from_path(os.path.relpath(target, self._page_dir))
            label = comp
            is_month = i > 0 and comp.isdigit() and int(comp) in _MONTHS
            if self._ctx.month_names and is_month:
                label = calendar.month_name[int(comp)]
            crumbs.append((label, href))
        return crumbs

    @property
    def url(self) -> str:
        from sigal.utils import url_from_path

        return url_from_path(os.path.relpath(self._media.dst_path, self._page_dir))

    @property
    def thumbnail(self) -> str:
        from sigal.utils import url_from_path

        return url_from_path(os.path.relpath(self._media.thumb_path, self._page_dir))

    @property
    def big_url(self) -> str | None:
        from sigal.utils import url_from_path

        big = self._media.big
        if big is None:
            return None
        big_abs = Path(self._media.dst_path).parent / big
        return url_from_path(os.path.relpath(big_abs, self._page_dir))


class _PseudoAlbum:
    """A minimal object that quacks like a sigal Album for the theme templates."""

    def __init__(  # noqa: PLR0913 -- mirrors the theme's Album context fields
        self,
        *,
        title: str,
        dst_path: str,
        output_file: str,
        index_url: str,
        author: str | None,
        description: str = "",
        summary: str = "",
        medias: list[Any] | None = None,
        albums: list[Any] | None = None,
        breadcrumb: list[tuple[str, str]] | None = None,
    ) -> None:
        self.title = title
        self.dst_path = dst_path
        self.output_file = output_file
        self.index_url = index_url
        self.author = author
        self.description = description
        self.summary = summary
        self.medias = medias or []
        self.albums = albums or []
        self.breadcrumb = breadcrumb or []
        self.zip = None


class _TagLink:
    """A clickable tag entry on the index page (mimics a sub-album)."""

    def __init__(self, *, title: str, url: str, thumbnail: str) -> None:
        self.title = title
        self.url = url
        self.thumbnail = thumbnail


class _AlbumCaptionWrapper:
    """Wrap a real sigal Album so its medias get linked captions.

    Delegates every attribute to the real album except ``medias``, which it
    re-exposes as ``_MediaProxy`` objects whose ``page_dir`` is the album's own
    directory (so ``url``/``thumbnail`` come out identical to sigal's). Each
    proxy adds ``name_parts``, which the theme's caption renders as filename
    tokens linked to tag pages. It also exposes ``summary`` (count + sort order)
    so the theme can show the same header tag pages have.
    """

    def __init__(self, album: Any, ctx: _LinkContext, summary: str = "") -> None:
        self._album = album
        self._ctx = ctx
        self.summary = summary

    def __getattr__(self, name: str) -> Any:
        return getattr(self._album, name)

    @property
    def medias(self) -> list[Any]:
        page_dir = self._album.dst_path
        return [_MediaProxy(m, page_dir, self._ctx) for m in self._album.medias]


class _DirMakingWriter(AbstractWriter):
    """AbstractWriter that creates the output directory before writing."""

    def write(self, album: Any) -> None:
        Path(album.dst_path).mkdir(parents=True, exist_ok=True)
        super().write(album)


class _TagPageWriter(_DirMakingWriter):
    """Render a tag page via the plugin's tag_album.html (extends the theme)."""

    template_file = "album.html"  # placeholder so AbstractWriter.__init__ succeeds

    def __init__(self, settings: Any, index_title: str = "") -> None:
        super().__init__(settings, index_title=index_title)
        # Swap in the plugin template, with the theme + default dirs available
        # so its `{% extends "album.html" %}` and includes still resolve.
        loaders = [
            FileSystemLoader(PLUGIN_TEMPLATES),
            FileSystemLoader(Path(self.theme) / "templates"),
            FileSystemLoader(Path(THEMES_PATH) / "default" / "templates"),
        ]
        env = Environment(
            loader=ChoiceLoader(loaders),
            trim_blocks=True,
            autoescape=True,
            lstrip_blocks=True,
        )
        self.template = env.get_template("tag_album.html")


class _TagIndexWriter(_DirMakingWriter):
    template_file = "album_list.html"


def _collect(gallery: Any, backend: str) -> dict[str, tuple[str, list[Any]]]:
    """Map slug -> (display keyword, media list) across every album."""
    groups: dict[str, list[Any]] = defaultdict(list)
    display: dict[str, str] = {}
    for album in gallery.albums.values():
        for media in album.medias:
            for keyword in extract_keywords(media.src_path, backend):
                slug = slugify(keyword)
                if not slug:
                    continue
                groups[slug].append(media)
                display.setdefault(slug, keyword)
    return {slug: (display[slug], medias) for slug, medias in groups.items()}


def _signature(medias: Iterable[Any]) -> str:
    """Hash of member identities + source mtimes, for idempotent rebuilds."""
    parts = []
    for media in sorted(medias, key=lambda m: m.dst_path):
        try:
            mtime = Path(media.src_path).stat().st_mtime
        except OSError:
            mtime = 0.0
        parts.append(f"{media.dst_path}:{mtime}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _resolve_sort(
    settings: dict[str, Any], options: dict[str, Any]
) -> tuple[str, bool]:
    """Resolve the tag-page media sort, falling back to sigal's gallery-wide one.

    The plugin's ``sort_attr`` / ``sort_reverse`` win when set; ``None`` (the
    default) inherits ``medias_sort_attr`` / ``medias_sort_reverse``.
    """
    attr = options.get("sort_attr") or settings.get("medias_sort_attr", "filename")
    reverse = options.get("sort_reverse")
    if reverse is None:
        reverse = settings.get("medias_sort_reverse", False)
    return attr, bool(reverse)


def _sort_label(attr: str, reverse: bool) -> str:
    """Human-readable thumbnail order, for the page summary line.

    >>> _sort_label("date", True)
    'newest first'
    >>> _sort_label("date", False)
    'oldest first'
    >>> _sort_label("filename", False)
    'normal sort'
    """
    if attr == "date":
        return "newest first" if reverse else "oldest first"
    return "reverse sort" if reverse else "normal sort"


def _album_summary(album: Any, settings: dict[str, Any]) -> str:
    """One-line "<count> <unit> · <order>" header for a normal album page.

    Container albums (e.g. ``2025/``, listing sub-albums) report their
    sub-album count and the gallery's album sort; leaf albums (e.g.
    ``2025/12/``) report their photo count and the media sort. This mirrors how
    sigal renders an album with album_list.html vs album.html.
    """
    subdirs = getattr(album, "albums", None) or []
    if subdirs:
        attr = settings.get("albums_sort_attr", "name")
        if isinstance(attr, list):
            attr = attr[0] if attr else "name"
        label = _sort_label(attr, settings.get("albums_sort_reverse", False))
        n = len(subdirs)
        return f"{n} album{'' if n == 1 else 's'} · {label}"
    label = _sort_label(
        settings.get("medias_sort_attr", "filename"),
        settings.get("medias_sort_reverse", False),
    )
    n = len(getattr(album, "medias", None) or [])
    return f"{n} photo{'' if n == 1 else 's'} · {label}"


def _sort_medias(medias: list[Any], attr: str, reverse: bool) -> list[Any]:
    """Order medias the way sigal orders an album's own medias.

    Mirrors ``sigal.gallery.Album.sort_medias`` (``attr`` is ``"filename"``,
    ``"date"``, or ``"meta.<key>"``) so tag pages can sort like the rest of the
    gallery instead of always sorting ascending by destination path.
    """
    if attr == "filename":
        attr = "dst_filename"
    if attr == "date":
        return sorted(medias, key=lambda m: m.date or datetime.now(), reverse=reverse)
    if attr.startswith("meta."):
        meta_key = attr.split(".", 1)[1]
        keygen = natsort_keygen(
            key=lambda m: m.meta.get(meta_key, [""])[0], alg=ns.SIGNED | ns.LOCALE
        )
    else:
        keygen = natsort_keygen(
            key=lambda m: getattr(m, attr), alg=ns.SIGNED | ns.LOCALE
        )
    return sorted(medias, key=keygen, reverse=reverse)


def precompute(gallery: Any) -> None:
    """gallery_initialized receiver: scan keywords once into shared state.

    Runs before album pages are written, so before_render can link their
    captions; the result is also reused by gallery_build so the (slow) keyword
    scan happens only once and both phases agree on which tags have pages.
    """
    settings: dict[str, Any] = gallery.settings
    if not settings.get("write_html", True):
        return
    options = {**DEFAULTS, **settings.get("tag_gallery", {})}
    # Tags to suppress entirely (e.g. people's names). Accepts a top-level
    # `tag_gallery_exclude` set or an "exclude" key inside `tag_gallery`;
    # entries are slugified so "Joe" and "joe" both match.
    exclude = settings.get("tag_gallery_exclude") or options.get("exclude") or ()
    exclude_slugs = {slugify(str(x)) for x in exclude}
    min_count = options["min_count"]

    tags = _collect(gallery, options["backend"])
    tags = {
        s: v
        for s, v in tags.items()
        if len(v[1]) >= min_count and s not in exclude_slugs
    }
    destination = Path(settings["destination"])
    _STATE.update(
        settings=settings,
        options=options,
        tags=tags,
        tag_slugs=set(tags),  # slugs that actually have a page, for caption links
        destination=destination,
        output_file=settings["output_filename"],
        author=settings.get("author"),
        base_dir=destination / options["base_path"],
    )


def build_tag_pages(gallery: Any) -> None:
    """gallery_build receiver: render per-tag pages and a tag-list index."""
    from sigal.utils import url_from_path

    if not _STATE:
        precompute(gallery)  # fallback if gallery_initialized was not seen
    tags = _STATE.get("tags")
    if not tags:
        logger.info("tag_gallery: no keywords found; nothing to write")
        return
    settings = _STATE["settings"]
    options = _STATE["options"]
    destination: Path = _STATE["destination"]
    output_file: str = _STATE["output_file"]
    author = _STATE["author"]
    base_dir: Path = _STATE["base_dir"]
    tag_slugs: set[str] = _STATE["tag_slugs"]
    ctx = _LinkContext(
        tag_slugs, base_dir, destination, output_file, options["month_names"]
    )
    # Tag pages are flat files (tag/<slug>.html) living directly in base_dir,
    # alongside the tag-list index (tag/index.html). index_url is the link back
    # to the gallery's root index (used by the <h1> masthead); tag_index_url is
    # the sibling tag-list index, the parent in every tag page's breadcrumb.
    index_url = url_from_path(os.path.relpath(destination / output_file, base_dir))
    tag_index_url = url_from_path(output_file)

    cache_path = base_dir / ".tag_gallery_cache.json"
    try:
        with cache_path.open(encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}

    sort_attr, sort_reverse = _resolve_sort(settings, options)
    sort_label = _sort_label(sort_attr, sort_reverse)
    # A page is stale when the set of linkable tags changes (captions link to
    # other tag pages, e.g. after an edit to tag_gallery_exclude or min_count)
    # or when the media sort order changes (which reorders the thumbnails).
    sort_key = f"{sort_attr}:{sort_reverse}"
    universe_sig = hashlib.sha256(
        ("\n".join(sorted(tag_slugs)) + "\n" + sort_key).encode("utf-8")
    ).hexdigest()[:16]

    page_writer = _TagPageWriter(settings, index_title=settings["title"])
    new_cache: dict[str, str] = {}
    written = 0
    for slug, (keyword, medias) in sorted(tags.items()):
        sig = f"{_signature(medias)}:{universe_sig}"
        new_cache[slug] = sig
        page_path = base_dir / f"{slug}.html"
        if cache.get(slug) == sig and page_path.is_file():
            continue
        ordered = _sort_medias(medias, sort_attr, sort_reverse)
        proxies = [_MediaProxy(m, base_dir, ctx) for m in ordered]
        album = _PseudoAlbum(
            title=f"{keyword} ({len(ordered)})",
            dst_path=str(base_dir),
            output_file=f"{slug}.html",
            index_url=index_url,
            author=author,
            summary=f"{len(ordered)} photos · {sort_label}",
            medias=proxies,
            breadcrumb=[
                (tag_index_url, options["title"]),
                (url_from_path(f"{slug}.html"), keyword),
            ],
        )
        page_writer.write(album)
        written += 1

    # Tag-list index page.
    index_writer = _TagIndexWriter(settings, index_title=settings["title"])
    links = []
    for slug, (keyword, medias) in sorted(tags.items()):
        ordered = _sort_medias(medias, sort_attr, sort_reverse)
        thumb_src = ordered[0]
        links.append(
            _TagLink(
                title=f"{keyword} ({len(ordered)})",
                url=url_from_path(f"{slug}.html"),
                thumbnail=url_from_path(
                    os.path.relpath(thumb_src.thumb_path, base_dir)
                ),
            )
        )
    # Tags are listed alphabetically by slug (the sorted() above), so the index
    # order is always the normal (ascending) sort regardless of the per-tag
    # thumbnail sort.
    index_album = _PseudoAlbum(
        title=options["title"],
        dst_path=str(base_dir),
        output_file=output_file,
        index_url=index_url,
        author=author,
        summary=f"{len(links)} tag{'' if len(links) == 1 else 's'} · normal sort",
        albums=links,
        breadcrumb=[(tag_index_url, options["title"])],
    )
    index_writer.write(index_album)

    # Prune stale tag pages (excluded tags, or tags now below min_count).
    keep = {f"{slug}.html" for slug in tags} | {output_file}
    for item in base_dir.iterdir():
        if item.suffix == ".html" and item.name not in keep:
            item.unlink()
            logger.debug("tag_gallery: removed stale tag page %s", item.name)

    with cache_path.open("w", encoding="utf-8") as fh:
        json.dump(new_cache, fh, indent=2)
    logger.info(
        "tag_gallery: %d tags (%d pages rewritten) -> %s", len(tags), written, base_dir
    )


def link_album_captions(context: dict[str, Any]) -> None:
    """before_render receiver: give normal album pages linked captions too.

    sigal renders each album with a fixed ``{{ media.title }}`` caption; wrap
    the album so its medias' ``title`` becomes the filename-token caption linked
    to tag pages, and expose a ``summary`` (count + sort order) for the header.
    Our own tag/index pages (``_PseudoAlbum``) are skipped — they render their
    captions and summary via the plugin template instead.
    """
    album = context.get("album")
    if album is None or isinstance(album, _PseudoAlbum) or not _STATE:
        return
    ctx = _LinkContext(
        _STATE.get("tag_slugs") or set(),
        _STATE["base_dir"],
        _STATE["destination"],
        _STATE["output_file"],
        _STATE["options"]["month_names"],
    )
    summary = _album_summary(album, _STATE["settings"])
    context["album"] = _AlbumCaptionWrapper(album, ctx, summary)


def register(settings: dict[str, Any]) -> None:
    """Sigal entry point: connect the plugin's signal handlers."""
    signals.gallery_initialized.connect(precompute)
    signals.before_render.connect(link_album_captions)
    signals.gallery_build.connect(build_tag_pages)
