# gdArchiver

Crawl a website, find Google Drive links, and download the linked files and folders with [gdown](https://github.com/wkentaro/gdown). The archive mirrors the page hierarchy and resumes from a manifest. View-only PDFs are captured through a headless Chromium fallback.

## Install

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/MrElyazid/gdArchiver.git
cd gdArchiver
uv sync
uv run playwright install chromium
```

`uv sync --extra archive-tools` installs the dependencies used by the external one-off scripts in `../gdArchiver-tools`. The core downloader does not need them.

## Usage

```sh
uv run main.py https://example.com
uv run main.py https://example.com -d 2 --crawl-only
uv run main.py https://example.com --download-only
uv run main.py https://example.com --pdf-only --reuse ./old_archive
```

The default output is `./example_com/`, and the default link CSV is `example_com.csv`. Run `uv run main.py --help` for every option.

## What it does

gdArchiver recursively crawls a site and records Google Drive links in a CSV with their source pages. It then downloads the linked files and folders, preserving the page layout. Downloads resume from `manifest.csv`, and failed files are listed in `failures.txt`.

- Google-native docs, sheets, and slides are exported to Office formats.
- File type filters apply to Drive files. `--skip-dir` can leave whole folder trees out before download.
- Forum presets are built in for phpBB-style sites.
- URLs are deduplicated ignoring scheme, `www.`, fragments, percent-encoding, and query order. Query parameters are kept except known tracking and session noise; use `--drop-param` and `--keep-param` when a site needs different rules.
- The default User-Agent is a Chrome string, since some hosts reject script agents. `--user-agent` overrides it.

## Archive layout

- Files mirror the page path. Forum topics go to `forum/topic-<id>/`.
- `manifest.csv` in the output root stores per-file status and drives resume.
- `failures.txt` lists failed downloads. `--retry-failed` retries only those.
- `--pdf-only`, `--only-ext`, and `--exclude-ext` narrow the download set.

## View-only PDFs

Some Drive PDFs have downloads disabled, so gdown fails. In that case gdArchiver captures the Drive preview with headless Chromium: it fetches the viewer page images at up to 3200 px wide, assembles a lossless A4 PDF with `img2pdf`, and caches incomplete pages in `.parts/<file_id>/` so a retry resumes. Downloadable PDFs are still fetched as originals. The fallback only handles PDFs; view-only non-PDF files are reported in `failures.txt`. Use `--no-viewonly` to disable it.

## Helpers

The optional `helpers/` modules post-process an archive. OCR and compression are resumable and keep state next to the archive.

| Command | Purpose |
| --- | --- |
| `uv run python -m helpers.janitor <root>` | Report or quarantine Drive upload junk and duplicate files. |
| `uv run python -m helpers.ocr <root>` | Add a searchable text layer to image-only PDFs. |
| `uv run python -m helpers.compress <root>` | Shrink PDFs with Ghostscript or ocrmypdf. |

`janitor` reports by default; add `--apply` to act on its findings. Run any helper with `--help` for its options.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

## License

MIT. See [LICENSE](LICENSE).
