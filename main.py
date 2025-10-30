import re
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import csv
import gdown
import os
import argparse
from pathlib import Path


def detect_google_link(link):
    """Checks if a link is a google drive/sheets/slides/docs link"""

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

        if parsed_url.netloc in hostnames:

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
    """checks if current_url is on the same domain as main_url"""

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
    """Remove fragment (#section) and query parameters for comparison"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def recursive_crawl(url, main_url, max_depth, seen_urls, glinks, crawl_path="", current_depth=1):
    """Crawl recursively and track the path taken to reach each page"""

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
    """writes the glinks dictionary to a local csv file"""

    if not data:
        print("No data to write, maybe the url has no google hosted content")
        return

    fieldnames = ['href_abs', 'type', 'source_page', 'crawl_path', 'text']

    with open(filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)
    
    print(f"Successfully wrote {len(data)} links to {filename}")


def gdown_download(csv_filename, target_location):
    """Downloads all files/folders from CSV to target location, preserving crawl hierarchy"""
    try:
        # read csv file
        with open(csv_filename, 'r', encoding='utf-8') as f:
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
    
    for item in data:
        url = item['href_abs']
        link_type = item['type']
        crawl_path = item.get('crawl_path', '')

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
        
        try:
            if link_type == 'folder':
                # Download entire folder
                gdown.download_folder(url=url, output=output_dir, quiet=False, use_cookies=True, resume=True)
            else:
                # Download single file with fuzzy=True for better compatibility
                gdown.download(url=url, output=output_dir + '/', fuzzy=True, quiet=False, resume=True)
            
            print(f"   Success")
        
        except Exception as e:
            print(f"   Failed: {e}")
            continue
    
    print(f"\n{'='*60}")
    print(f"Download complete! Files saved to: {target_location}")
    print(f"{'='*60}")


def main():
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
        gdown_download(args.csv, args.output)
    else:
        print(f"\nCrawl complete. Use 'python crawler.py --help' to see download options.")


if __name__ == "__main__":
    main()
