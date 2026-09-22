import re
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import csv
import gdown
import os
import argparse
from pathlib import Path

import viewonly


def detect_google_link(link):
    """Checks if a link is a google drive/sheets/slides/docs link.

    Returns:
        "file" for file links, "folder" for folder links, "unkown" otherwise.
    """

    # patterns for files
    # thanks to https://github.com/wkentaro/gdown/blob/main/gdown/parse_url.py#L31
    file_patterns = [
            r"^/file/d/(.*?)/(edit|view)$",
            r"^/file/u/[0-9]+/d/(.*?)/(edit|view)$",
            r"^/document/d/(.*?)/(edit|htmlview|view)$",
            r"^/document/u/[0-9]+/d/(.*?)/(edit|htmlview|view)$",
            r"^/presentation/d/(.*?)/(edit|htmlview|view)$",
            r"^/presentation/u/[0-9]+/d/(.*?)/(edit|htmlview|view)$",
            r"^/spreadsheets/d/(.*?)/(edit|htmlview|view)$",
            r"^/spreadsheets/u/[0-9]+/d/(.*?)/(edit|htmlview|view)$",
        ]

    # pattern for folder
    folder_patterns = [r"^/drive/folders/([a-zA-Z0-9_-]+)"]

    # google sheets, slides, docs share the same hostname docs.google.com
    hostnames = ["drive.google.com", "docs.google.com"]

    try:
        parsed_url = urlparse(link)

        if parsed_url.netloc.lower() in hostnames:

            for pattern in file_patterns:
                if re.match(pattern, parsed_url.path):
                    return "file"
            
            for pattern in folder_patterns:
                if re.match(pattern, parsed_url.path):
                    return "folder"

    except Exception:
        return "unkown"

    return "unkown"


def is_sub_url(current_url, main_url):
    """Checks if current_url is on the same domain as main_url, ignoring the www. prefix."""

    try:
        main_parts = urlparse(main_url)
        current_parts = urlparse(current_url)

        # normalize the hostname, www.example.com is the same as example.com
        main_host = main_parts.hostname.replace('www.', '')
        current_host = current_parts.hostname.replace('www.', '')

        return main_host == current_host

    except Exception:
        return False


def normalize_url(url):
    """Removes fragment (#section) and query parameters, used for url comparison."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def recursive_crawl(url, main_url, max_depth, seen_urls, glinks, crawl_path="", current_depth=1):
    """Crawls a site recursively and records every google drive link found.

    Args:
        url: page to crawl
        main_url: starting url, used to stay on the same domain
        max_depth: maximum crawl depth, None for unlimited
        seen_urls: set of already visited (normalized) urls
        glinks: list collecting the google links found
        crawl_path: folder path built from the pages followed to reach this page
        current_depth: current recursion depth
    """

    # normalize URL for comparison (strip #fragments and ?query)
    normalized_url = normalize_url(url)
    
    # base case: stop if already visited or exceeded max depth (if set)
    if normalized_url in seen_urls:
        return
    
    if max_depth is not None and current_depth > max_depth:
        return

    print(f"crawling {url} at depth: {current_depth}/{max_depth if max_depth else 'infinity'}")
    seen_urls.add(normalized_url)

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch {url}, error : {e}")
        return

    soup = BeautifulSoup(response.content, 'lxml')

    for a_tag in soup.find_all('a', href=True):
        link_href = a_tag['href']

        # convert relative urls to absolute links
        absolute_link = urljoin(url, link_href)

        link_type = detect_google_link(absolute_link)

        if link_type != "unkown":
            print(f"Found google drive link {link_type}: {absolute_link}")
            glinks.append({
                'text': a_tag.get_text(strip=True),
                'href_abs': absolute_link,
                'type': link_type,
                'source_page': url,
                'crawl_path': crawl_path  # path to get to this link, used to reconstruct the same folder heirarchy
            })

        # check if we need to crawl the link BEFORE entering the recursive_crawl function
        if is_sub_url(absolute_link, main_url) and normalize_url(absolute_link) not in seen_urls:
            # page name from URL (remove extension)
            page_path = urlparse(absolute_link).path.strip('/')
            if page_path:
                page_name = Path(page_path).stem.replace('/', '_')
                #crawl path: current_path/new_page
                new_crawl_path = f"{crawl_path}/{page_name}" if crawl_path else page_name
            else:
                new_crawl_path = crawl_path
            
            recursive_crawl(absolute_link, main_url, max_depth, seen_urls, glinks, new_crawl_path, current_depth + 1)


def write_csv(filename, data):
    """Writes the collected google links to a csv file, with a fixed column order."""

    if not data:
        print("No data to write, maybe the url has no google hosted content")
        return

    fieldnames = ['href_abs', 'type', 'source_page', 'crawl_path', 'text']

    with open(filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)
    
    print(f"Successfully wrote {len(data)} links to {filename}")


def extract_google_id(url, link_type):
    """Extracts the drive file/folder id from a google url, or None if it is not found."""

    if link_type == 'folder':
        match = re.search(r"/drive/folders/([a-zA-Z0-9_-]+)", url)
    else:
        match = re.search(r"/(?:file|document|presentation|spreadsheets)(?:/u/[0-9]+)?/d/([a-zA-Z0-9_-]+)", url)

    return match.group(1) if match else None


MAX_FOLDER_DEPTH = 15


def process_pdf(url, file_id, target, capturer):
    """Downloads one pdf with gdown, falling back to browser capture for view-only files.

    Args:
        url: share url of the file, used by gdown
        file_id: drive file id, used by the browser capturer
        target: destination pdf path
        capturer: ViewOnlyCapturer instance, or None to disable the fallback
    """

    if viewonly.valid_pdf(target):
        print(f"   Already downloaded: {target.name}")
        return

    try:
        result = gdown.download(url=url, output=str(target), fuzzy=True, quiet=False, resume=True)
        if result and viewonly.valid_pdf(result):
            print(f"   Downloaded: {target.name}")
            return
    except Exception as e:
        print(f"   gdown failed: {e}")

    if target.exists() and not viewonly.valid_pdf(target):
        target.unlink()

    if capturer is None:
        print(f"   View-only fallback disabled, skipping: {target.name}")
        return

    print(f"   View-only fallback: capturing {target.name} ...")
    info = capturer.capture_to_pdf(file_id, target)
    print(f"   Captured: {target.name} ({info['pages']} pages, {info['bytes']/1e6:.1f} MB)")


def download_pdf_file(url, file_id, output_dir, capturer):
    """Downloads a single pdf row from the csv, resolving its real name from drive first."""

    name = viewonly.get_file_name(file_id) or f"{file_id}.pdf"

    if not name.lower().endswith('.pdf'):
        print(f"   Skipping non-pdf: {name}")
        return

    target = Path(output_dir) / viewonly.sanitize(name)
    process_pdf(url, file_id, target, capturer)


def list_folder_entries(url, capturer):
    """Lists (pdfs, folders) of a folder.

    Uses the fast html parse and switches to the browser items:list api when the
    payload hits the 50 item cap, so that nothing is missed. The fast results are
    kept whenever the browser listing fails or returns fewer items.
    """

    pdfs, folders = viewonly.parse_folder_html(url)
    fast_count = len(pdfs) + len(folders)

    if fast_count < 50:
        return pdfs, folders

    if capturer is None:
        print("   Warning: folder may have more than 50 items, some could be missed")
        return pdfs, folders

    print(f"   Folder has 50+ items, listing complete contents via browser ...")
    try:
        full = capturer.list_folder_all(url)
    except Exception as e:
        print(f"   Browser listing failed, keeping the first 50 items: {e}")
        return pdfs, folders

    if full is not None and len(full[0]) + len(full[1]) >= fast_count:
        pdfs, folders = full
    else:
        print("   Warning: browser listing incomplete, keeping the first 50 items")

    return pdfs, folders


def subfolder_dir(output_dir, name):
    """Returns the subfolder path, reusing an existing dir whose name differs only by unicode direction marks."""

    clean = viewonly.sanitize_dir(name)
    target = Path(output_dir) / clean
    if not target.exists() and Path(output_dir).exists():
        norm = viewonly.normalize_name(clean)
        for p in Path(output_dir).iterdir():
            if p.is_dir() and viewonly.normalize_name(p.name) == norm:
                return p
    return target


def download_folder_pdfs(url, output_dir, capturer, visited=None, depth=0):
    """Downloads a folder and its subfolders recursively, filling in view-only pdfs via browser capture.

    Args:
        url: drive folder url
        output_dir: local directory matching this folder
        capturer: ViewOnlyCapturer instance, or None to disable the fallback
        visited: set of folder ids already processed, guards against loops
        depth: current recursion depth, capped by MAX_FOLDER_DEPTH
    """

    if visited is None:
        visited = set()

    folder_id = extract_google_id(url, 'folder')
    if folder_id and folder_id in visited:
        return
    if folder_id:
        visited.add(folder_id)

    try:
        gdown.download_folder(url=url, output=output_dir, quiet=False, use_cookies=True, resume=True)
    except Exception as e:
        print(f"   gdown folder failed: {e}")

    try:
        pdfs, folders = list_folder_entries(url, capturer)
    except Exception as e:
        print(f"   Could not list folder contents: {e}")
        return

    print(f"   Found {len(pdfs)} pdfs, {len(folders)} subfolders")

    for file_id, name in pdfs:
        target = Path(output_dir) / viewonly.sanitize(name)
        try:
            process_pdf(f"https://drive.google.com/file/d/{file_id}/view", file_id, target, capturer)
        except Exception as e:
            print(f"   Failed to capture {name}: {e}")

    if depth >= MAX_FOLDER_DEPTH:
        if folders:
            print(f"   Max folder depth ({MAX_FOLDER_DEPTH}) reached, skipping {len(folders)} subfolders")
        return

    for sub_id, sub_name in folders:
        print(f"   -> subfolder: {sub_name}")
        try:
            download_folder_pdfs(
                f"https://drive.google.com/drive/folders/{sub_id}",
                str(subfolder_dir(output_dir, sub_name)),
                capturer,
                visited,
                depth + 1,
            )
        except Exception as e:
            print(f"   Failed subfolder {sub_name}: {e}")


def gdown_download(csv_filename, target_location, viewonly_fallback=True):
    """Downloads all files/folders from the csv to the target location, preserving the crawl hierarchy.

    Args:
        csv_filename: csv produced by the crawler
        target_location: root output directory
        viewonly_fallback: enable the browser capture fallback for view-only pdfs
    """
    try:
        # read csv file (utf-8-sig also strips a possible BOM)
        with open(csv_filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            data = list(reader)
    except FileNotFoundError as e:
        print(f"the csv file doesnt exist, the website probably has no google content: {e} ")
        return
        
    if not data:
        print("No data in CSV to download")
        return
   

    # create target location if it doesnt exist
    Path(target_location).mkdir(parents=True, exist_ok=True)
    
    downloaded = set()
    capturer = viewonly.ViewOnlyCapturer() if viewonly_fallback else None

    try:
        for item in data:
            url = (item.get('href_abs') or '').strip()
            link_type = (item.get('type') or '').strip()
            crawl_path = item.get('crawl_path', '')

            if not url or link_type not in ('file', 'folder'):
                print(f"\nSkipping malformed csv row: {item}")
                continue

            if url in downloaded:
                continue
            downloaded.add(url)
            
            # Build output directory based on crawl path
            if crawl_path:
                output_dir = os.path.join(target_location, crawl_path)
                Path(output_dir).mkdir(parents=True, exist_ok=True)
            else:
                # File found on homepage, download directly to target_location
                output_dir = target_location
            
            print(f"\nDownloading {link_type}: {url}")
            print(f"  -> Saving to: {output_dir}")

            file_id = extract_google_id(url, link_type)
            if not file_id:
                print("   Could not extract google id, skipping")
                continue

            try:
                if link_type == 'folder':
                    download_folder_pdfs(url, output_dir, capturer)
                else:
                    download_pdf_file(url, file_id, output_dir, capturer)

            except Exception as e:
                print(f"   Failed: {e}")
                continue
    finally:
        if capturer is not None:
            capturer.close()
    
    print(f"\n{'='*60}")
    print(f"Download complete! Files saved to: {target_location}")
    print(f"{'='*60}")


def main():
    """Parses cli arguments, crawls the given url and downloads the google links found."""

    parser = argparse.ArgumentParser(
        description='Crawl a website and download Google Drive files/folders while preserving hierarchy'
    )
    
    parser.add_argument(
        'url',
        help='Starting URL to crawl'
    )
    
    parser.add_argument(
        '-d', '--depth',
        type=int,
        default=None,
        help='Maximum crawl depth (default: unlimited - crawl entire site)'
    )
    
    parser.add_argument(
        '-o', '--output',
        default=None,
        help='Output directory for downloaded files (default: ./{domain_name})'
    )
    
    parser.add_argument(
        '--csv',
        default=None,
        help='CSV file to store found links (default: {domain_name}.csv)'
    )
    
    parser.add_argument(
        '--crawl-only',
        action='store_true',
        help='Only crawl and generate CSV, skip downloading'
    )
    
    parser.add_argument(
        '--no-viewonly',
        action='store_true',
        help='Disable browser capture fallback for view-only pdfs'
    )
    
    args = parser.parse_args()
    
    # add https:// if missing
    if not args.url.startswith(('http://', 'https://')):
        args.url = 'https://' + args.url
    
    # default output directory based on netloc if not provided
    if args.output is None:
        netloc = urlparse(args.url).netloc or "downloads"
        netloc = netloc.replace('.', '_')
        args.output = f"./{netloc}"
 

    if args.csv is None:
        
        csv_name = urlparse(args.url).netloc or "gdrive_links.csv"
        csv_name = csv_name.replace('.', '_')
        args.csv = f"{csv_name}.csv"
        

    seen_urls = set()
    glinks = []
    
    # crawl start
    print(f"Starting crawler on {args.url} with max depth {args.depth}...")
    recursive_crawl(args.url, args.url, args.depth, seen_urls, glinks)
    
    # writing to csv
    write_csv(args.csv, glinks)
    
    # download start
    if not args.crawl_only:
        print(f"\nStarting download to {args.output}...")
        gdown_download(args.csv, args.output, viewonly_fallback=not args.no_viewonly)
    else:
        print(f"\nCrawl complete. Use 'python crawler.py --help' to see download options.")


if __name__ == "__main__":
    main()
