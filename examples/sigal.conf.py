# Example sigal.conf.py fragment for enabling sigal-tag-gallery.
# Merge these lines into your own sigal.conf.py.

# Absolute path to the directory containing tag_gallery.py (sigal adds
# plugin_paths to sys.path verbatim, so a relative path only works when you
# run `sigal build` from that directory).
plugin_paths = ["/path/to/sigal-tag-gallery"]
plugins = ["tag_gallery"]

tag_gallery = {
    "backend": "iptc",   # "iptc" | "xmp" | "exiftool"
    "base_path": "tag",  # output dir under the gallery root -> /tag/<slug>.html
    "min_count": 1,      # skip tags with fewer than N images
    "title": "Tags",     # heading on the tag-list index page
    # Thumbnail order on tag pages; None inherits medias_sort_* below.
    "sort_attr": None,    # "filename" | "date" | "meta.<key>" | None
    "sort_reverse": None,  # True | False | None
}

# Optional: tags to suppress entirely (no page, no caption links).
# Slug-matched, so case/spacing is ignored.
tag_gallery_exclude = {
    # "alice",
    # "bob",
}
