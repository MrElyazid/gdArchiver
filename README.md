# gdArchiver :

A simple python script to Archive / mass download Google drive/docs/sheets content from a website, by using [gdown](https://github.com/wkentaro/gdown).

## what it does :

Recursively crawl a url and download the content of any Google drive link found on it, preserving the file heirarchy when downloading.

- `uv run main.py https://example.com` will recursively crawl `example.com` infinitely (can also specify a depth) and saves any link it encounters that is a google drive ( or sheets or docs ) to a csv with its href, exact source page it was found on .. etc, and then after no more links can be crawled or reaching the depth specified, it downloads all the files/folders from the csv with respect to the way they were found with on the website.

## Installation :

### with uv :

- install `uv` on your machine (takes 10 secondes) : [install guide](https://docs.astral.sh/uv/getting-started/installation/)

- clone with git :
```sh
git clone https://github.com/MrElyazid/gdArchiver.git && cd gdArchiver/

```
- run `uv sync`. thats it, now just run `uv run main.py` to run the program. `uv sync --extra archive-tools` adds the optional dependencies used by the external one-off archive scripts; the core downloader does not need them.
- install the browser used for view-only pdf fallback (one time, ~115 MB) :
```sh
uv run playwright install chromium
```

## Usage :

```txt
usage: main.py [-h] [-d DEPTH] [-o OUTPUT] [--csv CSV] [--crawl-only] [--no-viewonly]
               [--no-forum] [--exclude REGEX] [--skip-dir REGEX] [--delay DELAY]
               [--drop-param NAME] [--keep-param NAME] [--download-only] [--pdf-only]
               [--only-ext EXT[,EXT...]] [--exclude-ext EXT[,EXT...]] [--reuse DIR]
               [--retry-failed] [--workers WORKERS] [--user-agent UA] [--log-file PATH] url

positional arguments:
  url                  Starting URL to crawl

options:
  -h, --help           show this help message and exit
  -d, --depth DEPTH    Maximum crawl depth (default: unlimited - crawl entire site)
  -o, --output OUTPUT  Output directory for downloaded files (default: ./{domain_name})
  --csv CSV            CSV file to store found links (default: {domain_name}.csv)
  --crawl-only         Only crawl and generate CSV, skip downloading
  --no-viewonly        Disable browser capture fallback for view-only pdfs
  --no-forum           Do not crawl forum pages (they are crawled by default)
  --exclude REGEX      Skip urls whose path and query match this regex (repeatable)
  --skip-dir REGEX     Skip drive subfolders whose relative path matches this regex (repeatable)
  --delay DELAY        Seconds to wait between page requests (default: 0.3)
  --drop-param NAME    Extra query parameter to ignore when comparing page urls (repeatable)
  --keep-param NAME    Query parameter to keep even when it is on the default deny list (repeatable)
  --download-only      Skip crawling and download from the existing CSV
  --pdf-only           Only download pdf files (default: all file types)
  --only-ext LIST      Only download these file extensions, e.g. pdf,docx
  --exclude-ext LIST   Never download these file extensions, e.g. rar,exe
  --reuse DIR          Move matching files from an older download tree into the new layout
  --retry-failed       Only retry entries marked failed or missing in manifest.csv
  --workers WORKERS    Parallel workers used to resolve file names from drive (default: 16)
  --user-agent UA      User-Agent sent to the site and to drive (default: a chrome string)
  --log-file PATH      Also append run output to this file
```

### Examples :

- `uv run main.py https://example.com` crawls infinitely saving links to `gdrive_links.csv` and then downloading the content in the current folder under the name `{domain_name}/` ( in this case a folder `example/`) 
- `uv run main.py https://example.com -d 10 -o /tmp/test --csv save_here.csv` crawls up to a max depth of 10, daving the urls to a file save_here.csv and downloading the content to /tmp/test .
- `uv run main.py https://example.com -d 10 --crawl-only` only crawls the site up to a depth of 10 getting just the links and generating a csv, no downloading.
- `uv run main.py https://example.com --download-only` re-runs the download step from an existing csv, without crawling again.
- `uv run main.py https://example.com --download-only --pdf-only` re-runs the download step, keeping only pdfs.
- `uv run main.py https://example.com --reuse ./old_archive --retry-failed` moves already downloaded files out of `./old_archive` into the new layout and retries only what is still missing.
- `uv run main.py https://example.com --download-only --skip-dir '(^|/)vendor(/|$)' --skip-dir 'node_modules'` re-runs downloads while leaving code/dependency trees out of folder downloads.

## archive layout :

- files are saved in a folder mirroring the page they were found on (`1inter/`, `prof/exprof1/`), forum topics go to `forum/topic-<id>/`, and pages identified only by query parameters get a slug (`p-123`): no deep crawl-history nesting.
- `manifest.csv` in the output root tracks every file id and its status (`ok`, `skipped-type`, `failed`), so re-running resumes and only fills gaps.
- failed downloads are retried once automatically and listed in `failures.txt`; `--retry-failed` reruns only those.
- all file types are downloaded by default (pdf, docx, pptx, rar, ...). the file type is read from the original upload name in the download headers, without downloading the file first. `--pdf-only`, `--only-ext` and `--exclude-ext` narrow it down.
- google-native docs/sheets/slides have no extension in their name; gdown exports them (docx/xlsx/pptx) and the exported name is kept.
- file-type filters apply to drive files; folder contents are walked from drive's own folder listing, so `--skip-dir` can leave whole subfolder trees (e.g. `vendor`, `node_modules`) out before anything inside them is downloaded.

## url handling for different sites :

- pages are compared ignoring scheme, `www.`, fragments, percent-encoding and query order, so `http://`, `https://` and `www.` variants are crawled once.
- all query parameters are kept (pagination like `?page=2`, ids like `?p=123`) except known session/tracking noise (`sid`, `PHPSESSID`, `utm_*`, `fbclid`, ...). use `--drop-param` / `--keep-param` when a site needs different rules.
- links to non-page files (`.zip`, `.jpg`, `.exe`, ...) are not crawled as pages; the file extensions that count as pages are built in.
- forum presets are built in for phpbb-style sites (`viewtopic.php`, `viewforum.php`): boards and topics get readable folder names, post permalinks are folded into their topic.
- requests use a chrome user agent by default, since some hosts answer 403 to script user agents. `--user-agent` overrides it everywhere (site crawl, drive name lookups, gdown transfers) for hosts that whitelist a specific agent, or to identify the tool honestly where nothing is blocked.
- `--log-file PATH` appends the terminal output to a file, handy for long background runs.

## view-only pdfs :

Some google drive links are shared with downloads disabled ("Sorry, the owner hasn't given you permission to download this file"), gdown fails on those. When that happens, gdArchiver falls back to a headless chromium capture of the drive preview : pages are fetched one by one from the viewer's image endpoint (by rewriting the page number of an observed url), rendered by google at up to 3200px wide (~388 DPI) and assembled into a lossless A4 pdf (via img2pdf).

- downloadable pdfs are still fetched by gdown as originals, the fallback is only used when gdown fails (including when google rate-limits the download endpoint after many accesses).
- the fallback only applies to pdfs; view-only non-pdf files are reported as failed in `failures.txt`.
- view-only folders are listed from the public folder page, so pdfs inside them are found even when they cannot be downloaded.
- folders are traversed recursively (up to 15 levels deep) through drive's own folder listing (one listing per folder, not per recursion level), and pdfs in nested subfolders are captured too. existing subfolders are reused even when their name differs only by unicode direction marks.
- folders with more than 50 items are listed completely through drive's internal paginated `items:list` api, so pdfs past the first html page are not missed.
- files that google itself cannot preview (corrupted or unsupported) are reported and skipped instead of hanging.
- existing valid pdfs are skipped, so the archive is resumable. a capture takes roughly 10-30 seconds per pdf (the viewer's lazy loading is bypassed, wheel scrolling is only a fallback for stubborn pages), `--no-viewonly` disables the fallback entirely.
- pages are cached in a hidden `.parts/<file_id>/` directory next to the target while a file is incomplete, so a retried capture only fetches the pages still missing; the directory is removed once the pdf is complete.

## archive janitor :

`helpers/janitor.py` sweeps a downloaded tree for google upload junk and duplicate files. it only reports by default; `--apply` moves anything it removes into a quarantine directory next to the archive (never inside it), so every action is reversible.

- `uv run python -m helpers.janitor saborpcmath_com` writes `<root>.janitor_report.csv` and changes nothing.
- `uv run python -m helpers.janitor saborpcmath_com --apply` quarantines `.tmp.driveupload` scratch files and the numbered copies that duplicate a real file next to them.
- `--purge-dirs vendor,node_modules --purge-parts --purge-empty --purge-locks` additionally quarantines whole dependency directories, `.parts` capture caches, zero byte files and microsoft office lock files (`~$name.docx`).
- duplicate files beyond the first copy are reported by default; `--trash-duplicates` quarantines them, `--hardlink` replaces them with hardlinks to the kept copy. `--prune-empty-dirs` removes directories left empty afterwards.
- the quarantine (`<root>.janitor_trash`) is kept until you delete it; the report lists every action with its sha256 and the path of the kept copy.

## ocr :

`helpers/ocr.py` adds a searchable text layer to the image-only pdfs of an archive with ocrmypdf/tesseract. it is resumable: every result is recorded in `<root>.ocr_state.sqlite3`, files that already have text are skipped, and killing the run never loses progress (ocrmypdf writes a temp file and only replaces the pdf after success).

- `uv run python -m helpers.ocr saborpcmath_com --workers 3 --nice 10` scans for pdfs without a text layer, OCRs them with `fra+ara`, writes a `.txt` sidecar per file under `<root>.ocr_text/` and records the state.
- page parallelism is disabled (`--jobs 1`) and `--nice` defaults to 10, so a few workers leave the desktop usable. `--workers`, `--nice`, `--limit`, `--dry-run`, `--retry-failed` and `--all` adjust the run.
- missing tesseract languages are downloaded on first use to `~/.local/share/gdarchiver/tessdata` (tessdata_best); `--lang` selects the languages.
- failures keep the original pdf untouched and are recorded with the last ocrmypdf error line; `--retry-failed` retries them. `--no-sidecar` skips the text files.
- the state db uses WAL with batched commits, so scanning thousands of files for a text layer stays fast and prints progress every 500 files.
- rough speed on the 4-core reference machine: ~3.3 s/page for french, ~5.4 s/page for french+arabic, so 180k pages takes a few days with 3 workers.

## compress :

`helpers/compress.py` shrinks the pdfs of an archive in place. the default engine is ghostscript: it downsamples page images above `--dpi` (default 200) and re-encodes them as jpeg, keeping any existing text layer and doing no OCR. it is resumable: each result is stored in `<root>.compress_state.sqlite3`, files that would not shrink are left untouched, and a kill never loses finished work.

- `uv run python -m helpers.compress saborpcmath_com --workers 4 --nice 10` compresses the whole tree; `--dry-run` counts the queue and changes nothing.
- `--engine ocrmypdf` switches to `ocrmypdf --optimize 3` for maximum compression (and OCR of image-only pages), at roughly 10-15x the cpu time.
- `--dpi`, `--jpeg-quality`, `--optimize`, `--min-gain` (default 5%), `--limit`, `--retry-failed` and `--state` adjust the pass.
- measured: gs at 200 dpi takes 1.1 s for a 4.6 MB image pdf and 13 s for a 38 MB capture (9.6 MB out, readable); ocrmypdf took 17-21 s for the same files and reached 75% only on text-bearing pages.


