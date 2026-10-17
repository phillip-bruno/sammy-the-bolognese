#!/usr/bin/env python3
"""
Build the gallery manifest and derived assets.

Reads everything in images/, writes:
  images.json                     manifest consumed by index.html
  images/thumbs/<name>-600.webp   tile source, 1x / small screens
  images/thumbs/<name>-1200.webp  tile source, 2x / large screens
  images/thumbs/<name>-poster.jpg poster frame for videos
  images/thumbs/<name>-web.mp4    H.264 copy of each video that browsers can play
  og-image.jpg                    1200x630 social preview, fixed filename

Nothing here needs a hand-maintained file list. Drop media into images/,
commit, and the workflow regenerates all of the above. Derived files whose
source has been deleted are removed.

Idempotent: derived files are only rebuilt when the source is newer.
"""

import json
import shutil
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

# Phones record HEVC, which Firefox and many Chrome installs can't play, so
# every video gets an H.264 copy. Short side capped at 1080, frame rate at 60
# (Pixel slow-motion clips are 120fps), HDR tone-mapped down to SDR.
VIDEO_SHORT_SIDE = 1080
VIDEO_CRF = 24
VIDEO_MAX_FPS = 60
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}   # HLG, PQ

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
    """Return {w, h, hdr, duration} honouring any rotation metadata, or None."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,color_transfer:stream_side_data=rotation"
                ":format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
        stream = data["streams"][0]
        w, h = int(stream["width"]), int(stream["height"])
        rotation = 0
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = abs(int(side["rotation"]))
        if rotation in (90, 270):
            w, h = h, w
        return {
            "w": w, "h": h,
            "hdr": stream.get("color_transfer") in HDR_TRANSFERS,
            "duration": float(data.get("format", {}).get("duration") or 0),
        }
    except Exception as exc:  # noqa: BLE001 - a bad file shouldn't fail the build
        print(f"  ! could not probe {path.name}: {exc}", file=sys.stderr)
        return None


def has_zscale() -> bool:
    """zscale (libzimg) does the HDR tone-mapping; not every ffmpeg has it."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True
    ).stdout
    return " zscale " in out


def make_web_video(path: Path, dest: Path, hdr: bool, tonemap: bool) -> bool:
    """Transcode to H.264/AAC MP4 that plays in every browser."""
    # ffmpeg applies rotation metadata before these filters, so iw/ih are the
    # upright dimensions here.
    short = VIDEO_SHORT_SIDE
    filters = [
        f"scale='if(gt(iw,ih),-2,min(iw,{short}))':'if(gt(iw,ih),min(ih,{short}),-2)'"
    ]
    if hdr and tonemap:
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    elif hdr:
        print(f"  ! {path.name} is HDR but this ffmpeg has no zscale; "
              "colours will look washed out", file=sys.stderr)
    filters.append("format=yuv420p")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", ",".join(filters),
                "-fpsmax", str(VIDEO_MAX_FPS),
                "-c:v", "libx264", "-preset", "slow", "-crf", str(VIDEO_CRF),
                "-profile:v", "high",
                "-c:a", "aac", "-b:a", "128k",
                "-map_metadata", "-1",       # no location or device tags
                "-movflags", "+faststart",   # start playing before fully loaded
                str(dest),
            ],
            check=True, capture_output=True, text=True,
        )
        return dest.exists()
    except subprocess.CalledProcessError as exc:
        print(f"  ! could not transcode {path.name}: {exc.stderr.strip()}",
              file=sys.stderr)
        dest.unlink(missing_ok=True)   # don't leave a half-written file behind
        return False


def make_poster(video: Path, dest: Path, duration: float) -> bool:
    """Grab a representative frame. Expects the web copy, which is already
    upright and SDR, so the poster matches what plays."""
    seek = f"{min(1.0, duration / 2):.2f}"   # some clips are barely a second
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", seek, "-i", str(video),
                "-frames:v", "1", "-q:v", "4",
                "-vf", "scale=-2:min(ih\\,1200)",
                str(dest),
            ],
            check=True, capture_output=True,
        )
        return dest.exists()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not make poster for {video.name}: {exc}", file=sys.stderr)
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

    # Without ffmpeg every video would be skipped, which used to happen
    # silently in CI. Fail instead so it gets noticed.
    has_videos = any(p.suffix.lower() in VIDEO_EXTS for p in sources)
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if has_videos and missing:
        print(f"videos found but {' and '.join(missing)} not installed",
              file=sys.stderr)
        return 1
    tonemap = has_videos and has_zscale()

    for path in sources:
        ext = path.suffix.lower()
        stem = slug(path)
        entry = {"file": f"images/{path.name}"}

        if ext in VIDEO_EXTS:
            info = probe_video(path)
            if not info:
                skipped.append(path.name)
                continue
            web = THUMBS_DIR / f"{stem}-web.mp4"
            if is_stale(path, web):
                print(f"  transcoding {path.name}" + (" (HDR)" if info["hdr"] else ""))
                if not make_web_video(path, web, info["hdr"], tonemap):
                    skipped.append(path.name)
                    continue
            poster = THUMBS_DIR / f"{stem}-poster.jpg"
            if is_stale(web, poster):
                if not make_poster(web, poster, info["duration"]):
                    skipped.append(path.name)
                    continue
            entry.update(
                type="video",
                w=info["w"], h=info["h"],
                src=web.relative_to(ROOT).as_posix(),
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

    # Anything in thumbs/ the manifest doesn't point at belongs to a source
    # that was deleted (or failed this run, and will be rebuilt next time).
    keep = {ROOT / m[k] for m in media for k in ("small", "large", "src", "poster") if k in m}
    pruned = [p for p in THUMBS_DIR.iterdir() if p.is_file() and p not in keep]
    for p in pruned:
        p.unlink()
    if pruned:
        print(f"  removed {len(pruned)} derived files with no source")

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
