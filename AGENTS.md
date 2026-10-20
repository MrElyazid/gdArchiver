# gdArchiver

Two flat modules, no package/entrypoint: `main.py` is the CLI + crawler + drive downloader, `viewonly.py` holds the headless-chromium capture fallback and the filename sanitizers/normalizers that `main.py` reuses. `README.md` is the authoritative usage doc; keep it in sync when adding flags.

## Commands

- Run: `uv run main.py <url> [flags]`; `uv sync` populates `.venv` from `pyproject.toml`/`uv.lock`.
- All tests (fast, fully mocked, no network): `.venv/bin/python -m unittest discover -s tests -t .`
- Single test: `.venv/bin/python -m unittest tests.test_main.CrawlTests.test_links_are_deduped_by_file_id`
- View-only pdf fallback needs a one-time `uv run playwright install chromium`; unit tests do not.
- No linter, formatter, typechecker, or CI config exists. The unittest suite is the only gate; do not introduce a toolchain unless asked.

## Hard-won context

- `pc1_ma/` (~17 GB) and `pc1_ma.csv` are a local regression archive and are ignored by `.gitignore`, as are `saborpcmath_com/`, `ia/`, `chunks/` and generated reports/state. Never `git add -A` or stage them, nor any output tree you create.
- `manifest.csv` in the output root is the resume source of truth: per file id `status` is `ok` / `skipped-type` / `failed`, with legacy `skipped-nonpdf` normalized to `skipped-type` on load. Re-runs skip `ok` rows and fill gaps; tests encode this, so change `Manifest` semantics only with tests.
- The browser User-Agent default is deliberate: some hosts (pc1.ma) answer 403 to plain `python-requests`. Crawl headers come from `main.browser_headers()`/`main.DEFAULT_USER_AGENT`; the view-only paths read the module global `viewonly.UA`, mutated via `viewonly.set_user_agent()`. `--user-agent` wires both, and is forwarded to gdown only when set (otherwise gdown keeps its own agent).
- Tests monkeypatch the network seams (`main.fetch_page`, `main.gdown.download`, `main.process_pdf`, `main.process_file`, `main.download_folder_pdfs`), and `crawl_site` calls `fetch_page(url, delay, user_agent)` positionally. Keep those signatures mock-friendly when threading new args.
- Real runs are slow and stateful: depth is unlimited by default, drive folders recurse up to `MAX_FOLDER_DEPTH` (15), and a view-only capture takes ~1-2 min per pdf through chromium. Smoke-check with `--crawl-only -d 1`/`-d 2`; do not start an unbounded crawl/download without asking.
- Filename and file-validity handling is intentionally strict: `viewonly.sanitize*` strip illegal characters/unicode marks and truncate by utf-8 bytes, `main._reuse_key` decodes html entities so `--reuse` matches trees written by older versions (`&#39;` vs `'`), and `main.archived_ok`/`viewonly.valid_pdf` reject zero-byte and non-`%PDF-` files (which is how drive sign-in/error pages are filtered). Do not weaken these to `exists()`.
