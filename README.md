# Sammy the Bolognese 🐾

A photo gallery for Sammy, the world's fluffiest Bolognese dog.

Live at [phillip-bruno.github.io/sammy-the-bolognese](https://phillip-bruno.github.io/sammy-the-bolognese).

## Adding photos

Drop image or video files into `images/` and commit. That's the whole job —
there is no list to update anywhere.

On push, the **Build gallery** workflow regenerates:

| File | What it's for |
| --- | --- |
| `images.json` | The manifest the page reads: filenames, dimensions, media type |
| `images/thumbs/*-600.webp`, `*-1200.webp` | Tile-sized renditions, picked per screen |
| `images/thumbs/*-poster.jpg` | First-frame posters for videos |
| `og-image.jpg` | 1200×630 preview shown when the link is shared |

Supported: `.jpg` `.jpeg` `.png` `.webp` `.gif` `.avif` `.mp4` `.webm` `.mov`.
Phone rotation (EXIF / video rotation metadata) is baked in, so sideways
photos come out the right way up.

## Running the build locally

```sh
pip install -r requirements.txt   # ffmpeg is also needed, for videos only
python scripts/build_gallery.py
```

It only rebuilds files whose source is newer, so re-running is cheap.

## How the page works

`index.html` is the whole site — no build step, no dependencies, no framework.

- Photos are laid out in **justified rows**: each tile grows in proportion to
  its aspect ratio, so every photo appears at its true shape and nothing is
  cropped. Roughly 95% of this collection is portrait, which the old fixed
  grid handled badly.
- Tile count follows the viewport — 2 on a phone, up to 8 on a large display.
- The next set is preloaded during the idle window, so shuffles are instant.
- Videos show a poster frame in the grid and only download when opened. The
  largest is 19 MB.
- If `images.json` is missing, the page falls back to the GitHub Contents API
  so it still works before the first build runs.

## Keyboard

| Key | Action |
| --- | --- |
| `Tab` / `Enter` | Move between photos, open one |
| `←` `→` | Previous / next photo in the viewer |
| `Esc` | Close the viewer |

The slideshow starts paused for anyone whose system asks for reduced motion,
and can always be stopped with the pause button.
