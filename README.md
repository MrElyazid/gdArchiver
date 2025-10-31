# gdArchiver :

A simple python script to Archive / mass download Google drive/docs/sheets content from a website, by using [gdown](https://github.com/wkentaro/gdown).

## what it does :

Recursively crawl a url and download the content of any Google drive link found on it, preserving the file heirarchy when downloading.

- `python3 main.py https://example.com` will recursively crawl `example.com` infinitely (can also specify a depth) and saves any link it encounters that is a google drive ( or sheets or docs ) to a csv with its href, exact source page it was found on .. etc, and then after no more links can be crawled or reaching the depth specified, it downloads all the files/folders from the csv with respect to the way they were found with on the website.

## Installation :

### with uv (Recommended) :

- install `uv` on your machine (takes 10 secondes) : [install guide](https://docs.astral.sh/uv/getting-started/installation/)

- clone with git :
```sh
git clone https://github.com/MrElyazid/gdArchiver.git && cd gdArchiver/

```
- run `uv sync`. thats it, now just run `uv run main.py` to run the program.

### with pip :
- clone with git : 
```sh
git clone https://github.com/MrElyazid/gdArchiver.git && cd gdArchiver/

```
- install requirements :

```sh
pip install -r requirements.txt
```
- run main.py : `python3 main.py` ( can also be `py main.py` or `python main.py` )

_Note_ : prefferably use a new venv. 


## Usage :

```txt
usage: main.py [-h] [-d DEPTH] [-o OUTPUT] [--csv CSV] [--crawl-only] url

positional arguments:
  url                  Starting URL to crawl

options:
  -h, --help           show this help message and exit
  -d, --depth DEPTH    Maximum crawl depth (default: unlimited - crawl entire site)
  -o, --output OUTPUT  Output directory for downloaded files (default: ./{domain_name})
  --csv CSV            CSV file to store found links (default: {domain_name}.csv)
  --crawl-only         Only crawl and generate CSV, skip downloading
```

### Examples :

- `python3 main.py https://example.com` crawls infinitely saving links to `gdrive_links.csv` and then downloading the content in the current folder under the name `{domain_name}/` ( in this case a folder `example/`) 
- `python3 main.py https://example.com -d 10 -o /tmp/test --csv save_here.csv` crawls up to a max depth of 10, daving the urls to a file save_here.csv and downloading the content to /tmp/test .
- `python3 main.py https://example.com -d 10 --crawl-only` only crawls the site up to a depth of 10 getting just the links and generating a csv, no downloading.

_Note_ : if using uv then just replace `python3` with `uv run`.
