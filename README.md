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
- run `uv sync`. thats it, now just run `uv run main.py` to run the program.
- install the browser used for view-only pdf fallback (one time, ~115 MB) :
```sh
uv run playwright install chromium
```

## Usage :

```txt
usage: main.py [-h] [-d DEPTH] [-o OUTPUT] [--csv CSV] [--crawl-only] [--no-viewonly] url

positional arguments:
  url                  Starting URL to crawl

options:
  -h, --help           show this help message and exit
  -d, --depth DEPTH    Maximum crawl depth (default: unlimited - crawl entire site)
  -o, --output OUTPUT  Output directory for downloaded files (default: ./{domain_name})
  --csv CSV            CSV file to store found links (default: {domain_name}.csv)
  --crawl-only         Only crawl and generate CSV, skip downloading
  --no-viewonly        Disable browser capture fallback for view-only pdfs
```

### Examples :

- `uv run main.py https://example.com` crawls infinitely saving links to `gdrive_links.csv` and then downloading the content in the current folder under the name `{domain_name}/` ( in this case a folder `example/`) 
- `uv run main.py https://example.com -d 10 -o /tmp/test --csv save_here.csv` crawls up to a max depth of 10, daving the urls to a file save_here.csv and downloading the content to /tmp/test .
- `uv run main.py https://example.com -d 10 --crawl-only` only crawls the site up to a depth of 10 getting just the links and generating a csv, no downloading.

## view-only pdfs :

Some google drive links are shared with downloads disabled ("Sorry, the owner hasn't given you permission to download this file"), gdown fails on those. When that happens, gdArchiver falls back to a headless chromium capture of the drive preview : each page is rendered by google at up to 3200px wide (~388 DPI) and assembled into a lossless A4 pdf (via img2pdf).

- downloadable pdfs are still fetched by gdown as originals, the fallback is only used when gdown fails (including when google rate-limits the download endpoint after many accesses).
- non-pdf files (docx, xlsx, ...) are skipped.
- view-only folders are listed from the public folder page, so pdfs inside them are found even if `gdown.download_folder` fails.
- folders are traversed recursively (up to 15 levels deep) and pdfs in nested subfolders are captured too. existing subfolders are reused even when their name differs only by unicode direction marks.
- folders with more than 50 items are listed completely through drive's internal paginated `items:list` api, so pdfs past the first html page are not missed.
- files that google itself cannot preview (corrupted or unsupported) are reported and skipped instead of hanging.
- existing valid pdfs are skipped, so the archive is resumable. a capture takes roughly 1-2 minutes per pdf (scrolling through every page), `--no-viewonly` disables the fallback entirely.
