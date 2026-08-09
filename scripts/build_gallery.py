#!/usr/bin/env python3
"""
Build the gallery manifest and derived assets.

Reads everything in images/, writes:
  images.json                     manifest consumed by index.html
  images/thumbs/<name>-600.webp   tile source, 1x / small screens
  images/thumbs/<name>-1200.webp  tile source, 2x / large screens
  images/thumbs/<name>-poster.jpg poster frame for videos
  og-image.jpg                    1200x630 social preview, fixed filename

Nothing here needs a hand-maintained file list. Drop media into images/,
commit, and the workflow regenerates all of the above.

Idempotent: derived files are only rebuilt when the source is newer.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = ROOT / "images"
THUMBS_DIR = IMAGES_DIR / "thumbs"
MANIFEST = ROOT / "images.json"
OG_IMAGE = ROOT / "og-image.jpg"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}

THUMB_HEIGHTS = (600, 1200)
THUMB_QUALITY = 80
OG_SIZE = (1200, 630)

# Pillow refuses very large files by default as a decompression-bomb guard.
# These are holiday photos, not hostile input.
Image.MAX_IMAGE_PIXELS = None


def is_stale(source: Path, derived: Path) -> bool:
    """True if derived is missing or older than source."""
    return not derived.exists() or derived.stat().st_mtime < source.stat().st_mtime


def slug(path: Path) -> str:
    """Filesystem-safe stem. Source filenames contain spaces and dots."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in path.stem)


def probe_video(path: Path):
    """Return (width, height) honouring any rotation metadata, or None."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:stream_side_data=rotation",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
        stream = json.loads(out)["streams"][0]
        w, h = int(stream["width"]), int(stream["height"])
        rotation = 0
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = abs(int(side["rotation"]))
        if rotation in (90, 270):
            w, h = h, w
        return w, h
    except Exception as exc:  # noqa: BLE001 - a bad file shouldn't fail the build
        print(f"  ! could not probe {path.name}: {exc}", file=sys.stderr)
        return None


def make_poster(path: Path, dest: Path) -> bool:
    """Grab a representative frame from a video."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", "00:00:01", "-i", str(path),
                "-frames:v", "1", "-q:v", "4",
                "-vf", "scale=-2:1200",
                str(dest),
            ],
            check=True, capture_output=True,
        )
        return dest.exists()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not make poster for {path.name}: {exc}", file=sys.stderr)
        return False


def build_thumbs(path: Path, stem: str) -> dict:
    """Write WebP renditions at each target height. Returns {height: relpath}."""
    out = {}
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)  # bake in phone rotation
        im = im.convert("RGB")
        for h in THUMB_HEIGHTS:
            dest = THUMBS_DIR / f"{stem}-{h}.webp"
            out[h] = dest.relative_to(ROOT).as_posix()
            if not is_stale(path, dest):
                continue
            if im.height <= h:
                copy = im.copy()  # don't upscale
            else:
                w = round(im.width * h / im.height)
                copy = im.resize((w, h), Image.LANCZOS)
            copy.save(dest, "WEBP", quality=THUMB_QUALITY, method=5)
    return out


def build_og_image(source: Path):
    """1200x630 centre crop under a fixed name so the meta tag never changes."""
    if not is_stale(source, OG_IMAGE):
        return
    with Image.open(source) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        # Bias the crop upward: in a portrait dog photo the face is up top.
        ImageOps.fit(im, OG_SIZE, Image.LANCZOS, centering=(0.5, 0.35)).save(
            OG_IMAGE, "JPEG", quality=86, optimize=True
        )
    print(f"  og-image.jpg from {source.name}")


def main() -> int:
    if not IMAGES_DIR.is_dir():
        print("images/ not found", file=sys.stderr)
        return 1

    THUMBS_DIR.mkdir(exist_ok=True)
    media, skipped = [], []

    sources = sorted(
        p for p in IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
    )
    print(f"scanning {len(sources)} files in images/")

    for path in sources:
        ext = path.suffix.lower()
        stem = slug(path)
        entry = {"file": f"images/{path.name}"}

        if ext in VIDEO_EXTS:
            dims = probe_video(path)
            if not dims:
                skipped.append(path.name)
                continue
            poster = THUMBS_DIR / f"{stem}-poster.jpg"
            if is_stale(path, poster):
                if not make_poster(path, poster):
                    skipped.append(path.name)
                    continue
            entry.update(
                type="video",
                w=dims[0], h=dims[1],
                poster=poster.relative_to(ROOT).as_posix(),
            )
        else:
            try:
                with Image.open(path) as im:
                    w, h = ImageOps.exif_transpose(im).size
            except Exception as exc:  # noqa: BLE001
                print(f"  ! skipping {path.name}: {exc}", file=sys.stderr)
                skipped.append(path.name)
                continue
            thumbs = build_thumbs(path, stem)
            entry.update(
                type="image",
                w=w, h=h,
                small=thumbs[600],
                large=thumbs[1200],
            )

        media.append(entry)

    if not media:
        print("no usable media found", file=sys.stderr)
        return 1

    # Widest landscape shot makes the best 1200x630 crop; fall back to the first.
    landscape = [m for m in media if m["type"] == "image" and m["w"] > m["h"]]
    og_source = max(landscape, key=lambda m: m["w"] / m["h"], default=None)
    build_og_image(ROOT / (og_source or media[0])["file"])

    MANIFEST.write_text(
        json.dumps(
            {
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "count": len(media),
                "media": media,
            },
            indent=1,
        )
        + "\n"
    )

    portraits = sum(1 for m in media if m["h"] > m["w"])
    print(f"wrote images.json — {len(media)} items ({portraits} portrait)")
    if skipped:
        print(f"skipped {len(skipped)}: {', '.join(skipped[:5])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
