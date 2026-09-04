#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio, os, io, json, re, gzip, argparse, hashlib, time, random, tempfile, subprocess, sys
import shutil
import fcntl
from urllib.parse import urlparse
from urllib import robotparser
from warcio.archiveiterator import ArchiveIterator
# stealth import, tolerant of playwright-stealth 1.x and 2.x
# =========================
# Robust stealth import (handles v2.0.0+)
# =========================
try:
    # attempt 1: 1.x exposes the function directly
    from playwright_stealth import stealth_async
except ImportError:
    try:
        # attempt 2: 2.0.0 wraps it in a Stealth class
        from playwright_stealth import Stealth

        async def stealth_async(page):
            # adapter: go through page.context and call the new API
            # in v2.0.0 Stealth targets the context, not the page
            stealth = Stealth()
            if hasattr(page, "context"):
                await stealth.apply_stealth_async(page.context)
            else:
                # in case a context was passed in
                await stealth.apply_stealth_async(page)

    except ImportError:
        # attempt 3: last-resort no-op
        print("[WARNING] Could not import stealth_async or Stealth class. Stealth disabled.")
        async def stealth_async(page):
            pass
from warcio.warcwriter import WARCWriter
from warcio.statusandheaders import StatusAndHeaders
from playwright.async_api import async_playwright, Error as PlaywrightError
import html2text

# Optional language detection: used to pick localized cookie-consent keywords.
# Degrades gracefully to English when not installed.
try:
    from langdetect import detect as _ld_detect
    HAVE_LANGDETECT = True
except Exception:
    HAVE_LANGDETECT = False

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# =========================
# Config (writes only WARC + index; no other local artifacts)
# =========================
# ---- result reason codes ----
REASON_OK                    = "ok"                      # scraped OK (written to slice)
REASON_SKIP_ALREADY_DONE     = "skip_already_scraped"   # already scraped for this version (non-force)
REASON_ROBOTS_DISALLOW       = "robots_disallow"
REASON_CHALLENGE_TIMEOUT     = "challenge_timeout"      # timed out waiting on Cloudflare/Incapsula
REASON_STILL_CHALLENGE       = "still_challenge_page"   # still a challenge page
REASON_EMPTY_DOM             = "empty_dom"              # DOM HTML empty / far too short
REASON_EXCEPTION             = "exception"              # other exception
REASON_WATCHDOG_TIMEOUT      = "watchdog_timeout"
MAX_SUBPAGES = 6
CONCURRENCY = 5
MAIN_WAIT = "networkidle"   # ("domcontentloaded" | "load" | "networkidle")
SUB_WAIT  = "domcontentloaded"
HEADLESS  = True
# Identify the crawler. If you adapt this code, point the +URL at a page that
# describes your project and gives a contact address.
USER_AGENT = ("Mozilla/5.0 (compatible; AS2Biz-Crawler/1.0; "
              "+https://github.com/InetIntel/Dataset-AS2Biz) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ACCEPT_LANG = "en-US,en;q=0.9,pt-BR;q=0.8,es;q=0.7"
JSON_SAVING_LIMIT = 0  # we do not write JSON evidence to disk

CAPTURE_SCREENSHOTS = False  # set by CLI --screenshots
FORCE_RESCRAPE = False       # set by CLI --force-rescrape

KEYWORDS = [
    "plan","plans","tariff","tarifa","tarifas","internet","fiber","fibra",
    "business","empresa","for-business","residencial","residential",
    "services","servicos","products","produtos","solutions","solucoes",
    "about","about-us","sobre","network","tv","iptv","who-we-are","tv","television", "ix","telephone","telephony"
]

COOKIE_BUTTON_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button[aria-label*='accept' i]",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('Allow')",
    "button:has-text('Aceitar')",
    "button:has-text('Concordo')",
    "button:has-text('Permitir')",
    "div#onetrust-banner-sdk button",
    "button[aria-label='Close']",
    "button.mfp-close",
]

HIDE_COOKIE_CSS = """
[id*='cookie' i], [class*='cookie' i], [id*='consent' i], [class*='consent' i],
[id*='gdpr' i], [class*='gdpr' i], [id*='privacy' i], [class*='privacy' i],
.cc-window, .cc-banner, .cookie-banner, .cookie-consent, .cookie-overlay,
.consent-banner, .consent-overlay, .popup-overlay, .overlay {
  display:none !important; visibility:hidden !important; pointer-events:none !important;
}
"""

# resource trimming: on subpages block heavy assets, keep script/XHR
BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_EXTS  = (
    ".png",".jpg",".jpeg",".webp",".gif",".svg",".ico",
    ".mp4",".webm",".avi",".mov",".m4v",".mp3",".wav",".ogg",
    ".woff",".woff2",".ttf",".otf",".eot",".css",".less",".sass",".scss",
)

# =========================
# Utils
# =========================
SMART_IDLE_MS_MAIN = 3500
SMART_IDLE_MS_SUB  = 2000
SMART_SOFT_CAP_MS  = 20000  # allow the main site more time

# ---- constants shared by A + B ----
MAX_RETRIES = 3
PER_URL_HARD_TIMEOUT = 150      # hard total time limit per URL
BACKOFF_BASE = 2.0
CHUNK = 200                     # restart the browser every 200 URLs

async def smart_goto(page, url: str, is_main: bool, timeout_ms: int = 60000):
    from urllib.parse import urlparse
    base_host = urlparse(url).netloc.lower()

    in_flight = set()
    last_event_ts = page._loop.time()

    def _same_origin(u: str) -> bool:
        try:
            return urlparse(u).netloc.lower() == base_host
        except Exception:
            return False

    def _mark_event(_=None):
        nonlocal last_event_ts
        last_event_ts = page._loop.time()

    def on_request(req):
        if _same_origin(req.url) and req.resource_type in {"document","xhr","fetch"}:
            in_flight.add(req)
            _mark_event()

    def on_request_finished(req):
        if req in in_flight:
            in_flight.discard(req)
            _mark_event()

    def on_request_failed(req):
        if req in in_flight:
            in_flight.discard(req)
            _mark_event()

    page.on("request", on_request)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)

    idle_need = SMART_IDLE_MS_MAIN/1000 if is_main else SMART_IDLE_MS_SUB/1000
    soft_cap  = SMART_SOFT_CAP_MS/1000 if is_main else 12000/1000

    try:
        # await page.goto(url, wait_until="load", timeout=timeout_ms)
        # loose: wait for DOM ready, let the custom idle + stability loop below finish the job
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            # if a site is still stuck, give it one short networkidle chance
            try:
                await page.goto(url, wait_until="networkidle", timeout=min(30000, timeout_ms))
            except Exception:
                # otherwise carry on; the stability loop and is_challenge_page will decide
                pass
        try:
            await page.wait_for_selector("body", timeout=5000)
        except Exception:
            pass

        try:
            for _ in range(3):
                await page.mouse.wheel(0, 1000)
                await page.wait_for_timeout(300)
        except Exception:
            pass

        start = page._loop.time()
        stable_count = 0
        last_txt, last_html, last_kids = -1, -1, -1
        stable_count = 0
        while True:
            await page.wait_for_timeout(200)
            now = page._loop.time()
            if (now - start) >= soft_cap:
                break

            try:
                metrics = await page.evaluate("""
                () => {
                const b = document.body;
                if (!b) return {txt:0, html:0, kids:0};
                return {
                    txt:  (b.innerText || '').length,
                    html: (b.innerHTML || '').length,
                    kids: (b.childElementCount || 0)
                };
                }
                """)
                cur_txt  = int(metrics.get("txt", 0))
                cur_html = int(metrics.get("html", 0))
                cur_kids = int(metrics.get("kids", 0))
            except Exception:
                cur_txt, cur_html, cur_kids = 0, 0, 0

            # "still loading?" test: total delta across three dimensions
            delta = abs(cur_txt - last_txt) + abs(cur_html - last_html) + (0 if cur_kids == last_kids else 100)

            if delta < 300:
                stable_count += 1
            else:
                stable_count = 0

            last_txt, last_html, last_kids = cur_txt, cur_html, cur_kids

            # release only when: no in-flight requests + idle long enough + stable N times in a row
            if (not in_flight) and ((now - last_event_ts) >= idle_need) and (stable_count >= 5):
                break

        return page.url
    finally:
        try:
            page.off("request", on_request)
            page.off("requestfinished", on_request_finished)
            page.off("requestfailed", on_request_failed)
        except Exception:
            pass

# helper section
async def safe_outer_html(page, tries: int = 3):
    for i in range(tries):
        try:
            # optional chaining so undefined document/documentElement does not throw
            return await page.evaluate("() => document?.documentElement?.outerHTML ?? ''")
        except Exception as e:
            msg = str(e)
            # two typical transient errors: nodeType / execution context destroyed (during nav or reload)
            if "nodeType" in msg or "Execution context was destroyed" in msg:
                # brief wait + one more DOM-ready attempt
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                except Exception:
                    pass
                await page.wait_for_timeout(300 * (i + 1))
                continue
            # re-raise anything else
            raise
    # if evaluate keeps failing, fall back to page.content (also wrapped, it can fail too)
    try:
        return await page.content()
    except Exception:
        return ""

async def safe_eval(page, js, arg=None, tries: int = 3, wait_after_dom_ms: int = 200):
    """
    Safe wrapper around page.evaluate / locator.evaluate_all calls.
    Retries on transient errors like 'nodeType' / 'Execution context was destroyed'.
    """
    for i in range(tries):
        try:
            if arg is None:
                return await page.evaluate(js)
            else:
                return await page.evaluate(js, arg)
        except Exception as e:
            msg = str(e)
            if ("nodeType" in msg) or ("Execution context was destroyed" in msg) or ("Cannot find context" in msg):
                try:
                    # short DOM-ready wait + increasing backoff
                    await page.wait_for_load_state("domcontentloaded", timeout=1500)
                except Exception:
                    pass
                await page.wait_for_timeout(wait_after_dom_ms * (i + 1))
                continue
            raise
    # give up: raise the original exception
    return await page.evaluate(js, arg)  # let it raise; caller handles it


# helper section
CF_PATTERNS = (
    "Just a moment", "Enable JavaScript and cookies to continue",
    "cf-browser-verification", "__cf_chl", "cf-chl-", "challenge-form"
)

async def is_challenge_page(page) -> bool:
    try:
        html = await page.content()
        h = html[:5000]  # enough
        return any(pat in h for pat in CF_PATTERNS)
    except Exception:
        return False

async def wait_through_challenge(page, max_ms: int = 25000):
    """
    Wait for a Cloudflare/Incapsula-style challenge to finish (cookie issued +
    auto reload). Repeatedly checks cookies and page content; triggers one reload if needed.
    """
    start = time.monotonic()
    did_reload = False
    while (time.monotonic() - start)*1000 < max_ms:
        try:
            # already past the challenge page -> return
            if not await is_challenge_page(page):
                return True

            # wait for network idle to give the challenge script time to run
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                await page.wait_for_timeout(1500)

            # check key cookies (Cloudflare: cf_clearance/__cf_bm)
            try:
                ck = await page.context.cookies()
                has_cf = any(c.get("name") in ("cf_clearance", "__cf_bm") for c in ck)
            except Exception:
                has_cf = False

            # got the cookie but content is still the challenge page -> one reload
            if has_cf and not did_reload:
                try:
                    await page.reload(wait_until="load", timeout=15000)
                    did_reload = True
                    continue
                except Exception:
                    pass

            # wait a bit more to see if it navigates on its own
            await page.wait_for_timeout(1200)
        except Exception:
            await page.wait_for_timeout(800)

    # timed out, still on the challenge page
    return not await is_challenge_page(page)

async def remove_cookie_banners(page):
    selectors_to_remove = [
        "[id*='cookie' i]", "[class*='cookie' i]", "[id*='consent' i]", "[class*='consent' i]",
        "[id*='gdpr' i]", "[class*='gdpr' i]", "[id*='privacy' i]", "[class*='privacy' i]",
        ".cc-window", ".cc-banner", ".cookie-banner", ".cookie-consent",
        ".cookie-overlay", ".consent-banner", ".consent-overlay", ".popup-overlay", ".overlay"
    ]
    try:
        js = """
        (sels) => {
          for (const sel of sels) {
            try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
          }
        }
        """
        try:
            await safe_eval(page, js, selectors_to_remove)
        except Exception:
            pass

    except Exception:
        pass
# def remove_cookie_banners(page):
#     try:
#         selectors_to_remove = [
#             "[id*='cookie']", "[class*='cookie']", "[id*='consent']", "[class*='consent']",
#             "[id*='gdpr']", "[class*='gdpr']", "[id*='privacy']", "[class*='privacy']",
#             ".cc-window", ".cc-banner", ".cookie-banner", ".cookie-consent",
#             ".cookie-overlay", ".consent-banner", ".consent-overlay", ".popup-overlay", ".overlay"
#         ]
#         for selector in selectors_to_remove:
#             page.evaluate(f"document.querySelectorAll('{selector}').forEach(el => el.remove());")
#     except Exception:
#         pass

def ensure_url_with_protocol(u: str) -> str:
    u = u.strip()
    if not u.startswith(("http://","https://")):
        u = "https://" + u
    return u

def canonical_no_www(host_or_url: str) -> str:
    s = (host_or_url or "").strip().lower()
    if "://" in s:
        try:
            s = urlparse(s).netloc
        except Exception:
            pass
    h = s.split("/")[0]
    if h.startswith("www."): h = h[4:]
    return h.strip().strip(".")

def same_domain(u, base):
    try:
        return urlparse(u).netloc.lower() == urlparse(base).netloc.lower()
    except Exception:
        return False

def html_to_text(html: str) -> str:
    conv = html2text.HTML2Text()
    conv.ignore_links = True
    conv.ignore_images = True
    conv.body_width = 0
    try:
        return conv.handle(html)
    except Exception:
        return ""

RESPECT_ROBOTS = False  # off by default; set True to strictly honour robots.txt

async def robots_allows(url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(USER_AGENT, url) or rp.can_fetch("*", url)
    except Exception:
        return True

async def fetch_raw_with_context(context, url: str):
    resp = await context.request.get(
        url,
        headers={"Accept-Language": ACCEPT_LANG, "User-Agent": USER_AGENT},
        max_redirects=20,
        timeout=60_000
    )
    final_url = resp.url
    status = resp.status
    headers = dict(resp.headers)
    body = await resp.body()
    return final_url, status, headers, body

# =========================
# Translation cache (the only persistent cache)
# =========================
class TranslatorCache:
    def __init__(self, path: str | None):
        self.path = path
        self.cache = {}
        self._load()
    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
    def save(self):
        if not self.path: return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            _atomic_write_json(self.path, self.cache, indent=2)
        except Exception:
            pass
    def translate_keywords(self, keywords, target_language):
        return keywords # For now, disable translation in case it blocks the scraping process.

# =========================
# Existing index reader (for per-version seed de-dup & store de-dup)
# =========================
class ExistingIndex:
    def __init__(self, archives_dir: str, version_tag: str):
        self.slice_index_path = os.path.join(archives_dir, "versions", version_tag, "index.jsonl")
        self.store_index_path = os.path.join(archives_dir, "store", "index", "store_index.jsonl")
        self.seeds_done = set()      # (site, seed_url_norm)
        self.sha_to_store = {}       # sha256 -> {record_id, warc, mime, url}
        self.page_to_store = {}
        self._load()

    def _load(self):
        bad_slice_lines = 0
        bad_store_lines = 0
        if os.path.exists(self.slice_index_path):
            with open(self.slice_index_path, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        obj = json.loads(ln)
                        site = obj.get("site","")
                        seed = obj.get("seed","") or obj.get("url","")
                        key = (site, seed.strip())
                        self.seeds_done.add(key)
                        url = obj.get("url", "")
                        store_ref = obj.get("store_ref") or {}
                        store_id = store_ref.get("record_id")
                        if site and url and store_id:
                            self.page_to_store[(site, url.strip())] = store_id
                    except Exception:
                        bad_slice_lines += 1
                self._slice_hwm = f.tell()
        else:
            self._slice_hwm = 0
        if os.path.exists(self.store_index_path):
            with open(self.store_index_path, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        obj = json.loads(ln)
                        sha = obj.get("sha256")
                        if sha:
                            self.sha_to_store[sha] = obj
                    except Exception:
                        bad_store_lines += 1
                self._store_hwm = f.tell()
        else:
            self._store_hwm = 0
        if bad_slice_lines:
            print(f"[index-warn] skipped {bad_slice_lines} malformed lines in {self.slice_index_path}")
        if bad_store_lines:
            print(f"[index-warn] skipped {bad_store_lines} malformed lines in {self.store_index_path}")

    def already_scraped_seed(self, site: str, seed_url: str) -> bool:
        return (site, seed_url.strip()) in self.seeds_done

    def lookup_store_by_sha(self, sha: str):
        hit = self.sha_to_store.get(sha)
        if hit:
            return hit
        if not os.path.exists(self.store_index_path):
            return None
        # Cross-process safety: only scan lines appended by other processes
        # since our last read, instead of re-scanning the entire file.
        with open(self.store_index_path, "r", encoding="utf-8") as f:
            f.seek(self._store_hwm)
            for ln in f:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                s = obj.get("sha256")
                if s:
                    self.sha_to_store[s] = obj
            self._store_hwm = f.tell()
        return self.sha_to_store.get(sha)

    def add_store_sha(self, sha: str, entry: dict):
        self.sha_to_store[sha] = entry

    def add_seed_done(self, site: str, seed_url: str):
        self.seeds_done.add((site, seed_url.strip()))

    def already_scraped_page(self, site: str, url: str, store_record_id: str | None = None) -> bool:
        key = (site, url.strip())
        if key not in self.page_to_store:
            # Cross-process safety: only scan lines appended since our last read.
            if os.path.exists(self.slice_index_path):
                with open(self.slice_index_path, "r", encoding="utf-8") as f:
                    f.seek(self._slice_hwm)
                    for ln in f:
                        try:
                            obj = json.loads(ln)
                        except Exception:
                            continue
                        s = obj.get("site", "")
                        u = (obj.get("url", "") or "").strip()
                        store_ref = obj.get("store_ref") or {}
                        rid = store_ref.get("record_id")
                        if s and u and rid:
                            self.page_to_store[(s, u)] = rid
                    self._slice_hwm = f.tell()
            if key not in self.page_to_store:
                return False
        if store_record_id is None:
            return True
        return self.page_to_store[key] == store_record_id

    # update the (site, url) -> store_record_id map
    def update_page_store(self, site: str, url: str, store_record_id: str):
        self.page_to_store[(site, url.strip())] = store_record_id

# =========================
# WARC archivist (writes store and slice)
# =========================
WARC_ROTATE_BYTES = 256_000_000  # ≈1GB
WARC_ROTATE_EVERY_RECORDS = 5000
DURABILITY_LEVEL = "balanced"
FSYNC_EVERY_RECORDS = 50
HEALTH_CHECK_ON_START = True
POST_RUN_HEALTH_CHECK = True

def _ensure_dir(p): os.makedirs(p, exist_ok=True)

def _next_index_name(dirpath: str, prefix: str, suffix: str = ".warc.gz") -> tuple[int, str]:
    pat = re.compile(rf"^{re.escape(prefix)}_(\d{{5}}){re.escape(suffix)}$")
    try:
        names = [n for n in os.listdir(dirpath) if pat.match(n)]
    except FileNotFoundError:
        names = []
    if not names:
        return 0, f"{prefix}_00000{suffix}"
    idxs = sorted(int(pat.match(n).group(1)) for n in names)
    last = idxs[-1]
    return last, f"{prefix}_{last:05d}{suffix}"

def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

def _atomic_write_json(path: str, obj, indent: int | None = None):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

class WarcArchivist:
    def __init__(
        self,
        archives_dir: str,
        version_tag: str,
        rotate_bytes: int = WARC_ROTATE_BYTES,
        rotate_every_records: int = WARC_ROTATE_EVERY_RECORDS,
        fsync_every_records: int = FSYNC_EVERY_RECORDS,
        durability_level: str = DURABILITY_LEVEL,
        health_check_on_start: bool = HEALTH_CHECK_ON_START,
    ):
        self.archives_dir = archives_dir
        self.version_tag  = version_tag
        self.rotate_bytes = rotate_bytes
        self.rotate_every_records = max(1, int(rotate_every_records))
        self.health_check_on_start = bool(health_check_on_start)

        d = (durability_level or "balanced").strip().lower()
        if d not in {"strict", "balanced", "throughput"}:
            d = "balanced"
        self.durability_level = d
        if self.durability_level == "strict":
            self.fsync_every_records = 1
        elif self.durability_level == "throughput":
            self.fsync_every_records = max(200, int(fsync_every_records))
        else:
            self.fsync_every_records = max(1, int(fsync_every_records))

        self.store_warc_dir = os.path.join(archives_dir, "store", "warc")
        self.store_index    = os.path.join(archives_dir, "store", "index", "store_index.jsonl")
        self.slice_warc_dir = os.path.join(archives_dir, "versions", version_tag, "warc")
        self.slice_index    = os.path.join(archives_dir, "versions", version_tag, "index.jsonl")
        self.cache_dir      = os.path.join(archives_dir, "versions", version_tag, "cache")

        _ensure_dir(self.store_warc_dir)
        _ensure_dir(os.path.dirname(self.store_index))
        _ensure_dir(self.slice_warc_dir)
        _ensure_dir(os.path.dirname(self.slice_index))
        _ensure_dir(self.cache_dir)

        self.lock_dir = os.path.join(self.archives_dir, ".locks")
        _ensure_dir(self.lock_dir)
        self.ipc_lock_path = os.path.join(self.lock_dir, f"archivist_{self.version_tag}.lock")
        self._ipc_lock_fp = open(self.ipc_lock_path, "a+", encoding="utf-8")
        self.manifest_path = os.path.join(self.cache_dir, f"writer_manifest_{self.version_tag}.json")
        self._manifest = {
            "version_tag": self.version_tag,
            "run_id": f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}",
            "status": "initializing",
            "durability": {
                "level": self.durability_level,
                "fsync_every_records": self.fsync_every_records,
                "rotate_every_records": self.rotate_every_records,
                "rotate_bytes": self.rotate_bytes,
            },
            "open_files": {},
            "counters": {"store": {"records": 0}, "slice": {"records": 0}},
            "last_records": {"store": None, "slice": None},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._store_records_since_rotate = 0
        self._slice_records_since_rotate = 0
        self._store_records_since_fsync = 0
        self._slice_records_since_fsync = 0
        self._acquire_ipc_lock()
        try:
            if self.health_check_on_start:
                self._startup_tail_health_guard()
            self._store_idx, self._store_name = self._reserve_next_index_name_locked(self.store_warc_dir, "store")
            self._store_path = os.path.join(self.store_warc_dir, self._store_name)
            self._store_fp = open(self._store_path, "ab")
            self._store_wr = WARCWriter(self._store_fp, gzip=True)

            slice_prefix = f"slice_{self.version_tag}"
            self._slice_idx, self._slice_name = self._reserve_next_index_name_locked(self.slice_warc_dir, slice_prefix)
            self._slice_path = os.path.join(self.slice_warc_dir, self._slice_name)
            self._slice_fp = open(self._slice_path, "ab")
            self._slice_wr = WARCWriter(self._slice_fp, gzip=True)
        finally:
            self._release_ipc_lock()

        self._store_idx_f = open(self.store_index, "a", encoding="utf-8")
        self._slice_idx_f = open(self.slice_index, "a", encoding="utf-8")

        self._lock = asyncio.Lock()
        self._manifest["status"] = "running"
        self._manifest["open_files"] = {"store": self._store_name, "slice": self._slice_name}
        self._checkpoint_manifest(event="startup", force=True)

    def _acquire_ipc_lock(self):
        fcntl.flock(self._ipc_lock_fp.fileno(), fcntl.LOCK_EX)

    async def _acquire_ipc_lock_async(self):
        while True:
            try:
                fcntl.flock(self._ipc_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, OSError):
                import asyncio
                await asyncio.sleep(0.05)

    def _release_ipc_lock(self):
        try:
            fcntl.flock(self._ipc_lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    def _checkpoint_manifest(self, event: str, force: bool = False):
        self._manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._manifest["last_event"] = event
        self._manifest["open_files"] = {
            "store": getattr(self, "_store_name", None),
            "slice": getattr(self, "_slice_name", None),
        }
        if force or event in {"startup", "rotate", "fsync", "shutdown", "tail_health_startup"}:
            _atomic_write_json(self.manifest_path, self._manifest, indent=2)

    def _mark_record_written(self, kind: str, record_id: str | None, url: str):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if kind == "store":
            self._manifest["counters"]["store"]["records"] += 1
            self._store_records_since_rotate += 1
            self._store_records_since_fsync += 1
        else:
            self._manifest["counters"]["slice"]["records"] += 1
            self._slice_records_since_rotate += 1
            self._slice_records_since_fsync += 1
        self._manifest["last_records"][kind] = {"record_id": record_id, "url": url, "ts": now_iso}

    def _needs_rotate(self, kind: str) -> bool:
        if kind == "store":
            return (_size(self._store_path) >= self.rotate_bytes) or (self._store_records_since_rotate >= self.rotate_every_records)
        return (_size(self._slice_path) >= self.rotate_bytes) or (self._slice_records_since_rotate >= self.rotate_every_records)

    def _fsync_kind_locked(self, kind: str):
        if kind == "store":
            self._store_fp.flush()
            self._store_idx_f.flush()
            os.fsync(self._store_fp.fileno())
            os.fsync(self._store_idx_f.fileno())
            self._store_records_since_fsync = 0
        else:
            self._slice_fp.flush()
            self._slice_idx_f.flush()
            os.fsync(self._slice_fp.fileno())
            os.fsync(self._slice_idx_f.fileno())
            self._slice_records_since_fsync = 0

    async def _post_write_housekeeping(self, kind: str):
        if self._needs_rotate(kind):
            await self._maybe_rotate(kind)
            self._checkpoint_manifest(event="rotate", force=True)
        if (kind == "store" and self._store_records_since_fsync >= self.fsync_every_records) or (
            kind == "slice" and self._slice_records_since_fsync >= self.fsync_every_records
        ):
            self._fsync_kind_locked(kind)
            self._checkpoint_manifest(event="fsync", force=True)

    def _latest_warc_by_prefix(self, dirpath: str, prefix: str) -> str | None:
        pat = re.compile(rf"^{re.escape(prefix)}_(\d{{5}})\.warc\.gz$")
        try:
            names = [n for n in os.listdir(dirpath) if pat.match(n)]
        except FileNotFoundError:
            return None
        if not names:
            return None
        names.sort(key=lambda n: int(pat.match(n).group(1)))
        return os.path.join(dirpath, names[-1])

    def _warc_is_readable(self, path: str) -> tuple[bool, str]:
        stream = None
        try:
            stream = gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")
            for rec in ArchiveIterator(stream, verify_http=False):
                _ = rec.content_stream().read(1)
            return True, ""
        except Exception as e:
            return False, str(e)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _try_repair_warc(self, path: str) -> tuple[bool, str]:
        repaired = f"{path}.repairing"
        try:
            src = gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")
            with src, open(repaired, "wb") as dst:
                writer = WARCWriter(dst, gzip=True)
                count = 0
                for rec in ArchiveIterator(src, verify_http=False):
                    writer.write_record(rec)
                    count += 1
            if count == 0:
                raise RuntimeError("repair produced zero records")
            os.replace(repaired, path)
            return True, f"repaired_records={count}"
        except Exception as e:
            try:
                if os.path.exists(repaired):
                    os.remove(repaired)
            except Exception:
                pass
            return False, str(e)

    def _quarantine_warc(self, path: str):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        qpath = f"{path}.broken.{ts}"
        os.replace(path, qpath)
        return qpath

    def _startup_tail_health_guard(self):
        checks = [
            self._latest_warc_by_prefix(self.store_warc_dir, "store"),
            self._latest_warc_by_prefix(self.slice_warc_dir, f"slice_{self.version_tag}"),
        ]
        summary = []
        for path in checks:
            if not path:
                continue
            ok, err = self._warc_is_readable(path)
            if ok:
                summary.append({"path": path, "status": "ok"})
                continue
            repaired_ok, repaired_msg = self._try_repair_warc(path)
            if repaired_ok:
                summary.append({"path": path, "status": "repaired", "details": repaired_msg})
                continue
            quarantined_to = self._quarantine_warc(path)
            summary.append({"path": path, "status": "quarantined", "details": err, "quarantined_to": quarantined_to})
        if summary:
            self._manifest["tail_health_startup"] = summary
            self._checkpoint_manifest(event="tail_health_startup", force=True)

    def _reserve_next_index_name_locked(self, dirpath: str, prefix: str, suffix: str = ".warc.gz") -> tuple[int, str]:
        pat = re.compile(rf"^{re.escape(prefix)}_(\d{{5}}){re.escape(suffix)}$")
        try:
            names = [n for n in os.listdir(dirpath) if pat.match(n)]
        except FileNotFoundError:
            names = []
        if not names:
            idx = 0
        else:
            idx = max(int(pat.match(n).group(1)) for n in names) + 1
        name = f"{prefix}_{idx:05d}{suffix}"
        path = os.path.join(dirpath, name)
        open(path, "ab").close()
        return idx, name

    async def write_store_binary(self, url: str, mime: str, data: bytes):
        async with self._lock:
            await self._acquire_ipc_lock_async()
            try:
                rec = self._store_wr.create_warc_record(url, 'resource', payload=io.BytesIO(data or b""))
                self._store_wr.write_record(rec)
                record_id = rec.rec_headers.get_header('WARC-Record-ID')

                mime = (mime or "application/octet-stream").lower()
                sha  = self._sha256(data)
                row = {
                    "record_id": record_id,
                    "warc": self._store_name,
                    "url": url,
                    "mime": mime,
                    "sha256": sha,
                    "length": len(data or b""),
                }
                self._store_idx_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._store_idx_f.flush()

                self._mark_record_written("store", record_id, url)
                await self._post_write_housekeeping("store")
                return record_id, self._store_name, mime, sha
            finally:
                self._release_ipc_lock()

    async def _maybe_rotate(self, kind: str):
        if kind == "store":
            if self._needs_rotate("store"):
                self._store_fp.close()
                self._store_idx, self._store_name = self._reserve_next_index_name_locked(self.store_warc_dir, "store")
                self._store_path = os.path.join(self.store_warc_dir, self._store_name)
                self._store_fp = open(self._store_path, "ab")
                self._store_wr = WARCWriter(self._store_fp, gzip=True)
                self._store_records_since_rotate = 0
        else:
            if self._needs_rotate("slice"):
                self._slice_fp.close()
                prefix = f"slice_{self.version_tag}"
                self._slice_idx, self._slice_name = self._reserve_next_index_name_locked(self.slice_warc_dir, prefix)
                self._slice_path = os.path.join(self.slice_warc_dir, self._slice_name)
                self._slice_fp = open(self._slice_path, "ab")
                self._slice_wr = WARCWriter(self._slice_fp, gzip=True)
                self._slice_records_since_rotate = 0

    def _sha256(self, b: bytes) -> str:
        return hashlib.sha256(b or b"").hexdigest()

    async def write_store_response(self, url: str, status_code: int, headers: dict, body: bytes, mime_hint: str | None):
        async with self._lock:
            await self._acquire_ipc_lock_async()
            try:
                # 1) determine MIME
                mime = None
                for k, v in (headers or {}).items():
                    if k.lower() == "content-type":
                        mime = v.split(";")[0].strip().lower()
                        break
                if not mime:
                    mime = (mime_hint or "text/html").split(";")[0].strip().lower()

                # 2) assemble HTTP headers (warcio needs StatusAndHeaders)
                statusline = f"{status_code} OK"
                http_headers = []
                for k, v in (headers or {}).items():
                    try:
                        http_headers.append((k, str(v)))
                    except Exception:
                        pass
                http_header_obj = StatusAndHeaders(statusline=statusline, headers=http_headers, protocol='HTTP/1.1')

                # 3) write the store WARC record (type 'response')
                rec = self._store_wr.create_warc_record(
                    url, 'response',
                    payload=io.BytesIO(body or b""),
                    http_headers=http_header_obj
                )
                self._store_wr.write_record(rec)
                record_id = rec.rec_headers.get_header('WARC-Record-ID')

                # 4) write the store index line
                sha = hashlib.sha256(body or b"").hexdigest()
                row = {
                    "record_id": record_id,
                    "warc": self._store_name,
                    "url": url,
                    "mime": mime,
                    "sha256": sha,
                    "length": len(body or b""),
                }
                self._store_idx_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._store_idx_f.flush()

                self._mark_record_written("store", record_id, url)
                await self._post_write_housekeeping("store")
                return record_id, self._store_name, mime, sha
            finally:
                self._release_ipc_lock()


    async def write_slice_revisit(
        self,
        site: str,
        seed: str,
        url: str,
        kind: str,
        mime: str,
        store_record_id: str,
        store_warc_name: str,
        store_origin: str | None = None,     # e.g. 'newly_scraped' / 'reused_from_store'
        captured_at_utc: str | None = None,  # ISO8601 UTC, optional
        **extra_meta,                        # extra metadata, written to both index and WARC header
    ):
        import time, json

        async with self._lock:
            await self._acquire_ipc_lock_async()
            try:
                # 1) normalize the timestamp (used by both index and WARC header)
                if not captured_at_utc:
                    captured_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                # 2) clean extra_meta, drop None
                meta_clean = {k: v for k, v in extra_meta.items() if v is not None} if extra_meta else {}

                # 3) build WARC headers: standard fields + custom fields that can rebuild the index
                headers = [
                    # standard / semi-standard fields
                    ("WARC-Refers-To",            store_record_id),
                    ("WARC-Refers-To-Target-URI", url),
                    ("WARC-Refers-To-Date",       captured_at_utc),

                    # custom extension headers: AS2Web metadata
                    ("X-AS2Web-Version-Tag",      self.version_tag),
                    ("X-AS2Web-Site",             site),
                    ("X-AS2Web-Seed",             seed),
                    ("X-AS2Web-Kind",             kind),           # 'html' or 'screenshot'
                    ("X-AS2Web-Mime",             mime),
                    ("X-AS2Web-Store-Warc",       store_warc_name),
                    ("X-AS2Web-Store-Record-ID",  store_record_id),
                ]
                if store_origin:
                    headers.append(("X-AS2Web-Store-Origin", store_origin))
                if meta_clean:
                    # stash all extra metadata as JSON in one header for later parsing
                    headers.append(("X-AS2Web-Meta", json.dumps(meta_clean, ensure_ascii=False)))

                # 4) write the slice WARC record (revisit)
                rec = self._slice_wr.create_warc_record(
                    url,
                    "revisit",
                    warc_headers_dict=dict(headers),
                )
                self._slice_wr.write_record(rec)
                slice_rid = rec.rec_headers.get_header("WARC-Record-ID")

                # 5) write one index.jsonl line
                row = {
                    "site": site,
                    "seed": seed,
                    "url": url,
                    "kind": kind,   # 'html' or 'screenshot'
                    "mime": mime,
                    "warc": self._slice_name,
                    "record_id": slice_rid,
                    "store_ref": {"record_id": store_record_id, "warc": store_warc_name},
                    "store_origin": store_origin,
                    "captured_at": captured_at_utc,
                }
                if meta_clean:
                    row.update(meta_clean)

                self._slice_idx_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._slice_idx_f.flush()

                self._mark_record_written("slice", slice_rid, url)
                await self._post_write_housekeeping("slice")
                return slice_rid
            finally:
                self._release_ipc_lock()

    def close(self):
        try:
            self._acquire_ipc_lock()
            try:
                self._fsync_kind_locked("store")
            except Exception:
                pass
            try:
                self._fsync_kind_locked("slice")
            except Exception:
                pass
            self._manifest["status"] = "completed"
            self._checkpoint_manifest(event="shutdown", force=True)
        finally:
            try:
                self._release_ipc_lock()
            except Exception:
                pass
        try: self._store_idx_f.close()
        except: pass
        try: self._slice_idx_f.close()
        except: pass
        try: self._store_fp.close()
        except: pass
        try: self._slice_fp.close()
        except: pass
        try:
            tail_summary = []
            for path in [self._store_path, self._slice_path]:
                ok, err = self._warc_is_readable(path)
                tail_summary.append({"path": path, "status": "ok" if ok else "broken", "error": err if not ok else ""})
            _atomic_write_json(os.path.join(self.cache_dir, "tail_health_summary.json"), {"tail_checks": tail_summary}, indent=2)
        except Exception:
            pass
        try: self._ipc_lock_fp.close()
        except: pass

# =========================
# Playwright helpers (no local artifacts)
# =========================
async def detect_page_language(page) -> str:
    """Prefer DOM lang; then common <meta>; then langdetect; fall back to 'en'."""
    async def _safe_eval(js, arg=None, tries: int = 3, wait_ms: int = 200):
        """Light wrapper around page.evaluate for nodeType / destroyed-context errors."""
        for i in range(tries):
            try:
                if arg is None:
                    return await page.evaluate(js)
                else:
                    return await page.evaluate(js, arg)
            except Exception as e:
                msg = str(e)
                if ("nodeType" in msg) or ("Execution context was destroyed" in msg) or ("Cannot find context" in msg):
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=1500)
                    except Exception:
                        pass
                    await page.wait_for_timeout(wait_ms * (i + 1))
                    continue
                # re-raise anything else
                raise
        # still failing: let it raise, outer try/except handles it
        return await page.evaluate(js, arg)

    def _norm_lang(s: str) -> str:
        """
        Normalize a language tag to its first two letters.
        e.g. 'en', 'en-US', 'pt_BR', 'PL_pl' -> 'en'/'pt'/'pl'.
        """
        if not s:
            return ""
        s = s.strip()
        # split on '-'/'_', take the first part
        for sep in ("-", "_"):
            if sep in s:
                s = s.split(sep, 1)[0]
                break
        s = s.strip().lower()
        # keep letters only, common two-letter codes
        if len(s) >= 2:
            return s[:2]
        return s

    # 1) <html lang="...">
    try:
        dom_lang = await _safe_eval("() => (document?.documentElement?.lang || '').trim()")
        dl = _norm_lang(dom_lang)
        if dl:
            return dl
    except Exception:
        pass

    # 2) common <meta> hints: og:locale / name=language / http-equiv=content-language
    try:
        meta_val = await _safe_eval("""
            () => {
              const m1 = document.querySelector("meta[property='og:locale']");
              const m2 = document.querySelector("meta[name='language']");
              const m3 = document.querySelector("meta[http-equiv='content-language']");
              return (m1?.content || m2?.content || m3?.content || '').trim();
            }
        """)
        ml = _norm_lang(meta_val)
        if ml:
            return ml
    except Exception:
        pass

    # 3) langdetect fallback (HTML -> text -> detect)
    if HAVE_LANGDETECT:
        try:
            html = await _safe_eval("() => document?.documentElement?.outerHTML || ''")
            if html:
                def _do_html2text(h):
                    conv = html2text.HTML2Text()
                    conv.ignore_links = True
                    conv.ignore_images = True
                    conv.body_width = 0
                    try:
                        return conv.handle(h)
                    except Exception:
                        return ""
                import asyncio
                txt = await asyncio.to_thread(_do_html2text, html[:500000])
                if txt and txt.strip():
                    try:
                        return _norm_lang(_ld_detect(txt))
                    except Exception:
                        pass
        except Exception:
            pass

    # 4) still unknown -> default to English
    return "en"

async def accept_cookies(page, detected_language: str = "en", translator_cache: TranslatorCache | None = None):
    """Multilingual keywords + common selectors; if that fails, hide the overlay."""
    try:
        # scan common buttons first
        for sel in COOKIE_BUTTON_SELECTORS:
            loc = page.locator(sel)
            if await loc.count():
                try:
                    await loc.first.click(timeout=800)
                    await page.wait_for_timeout(150)
                    return True
                except Exception:
                    pass

        # multilingual keyword buttons
        base_keywords = [
            "Allow all cookies","Accept All Cookies","Accept cookies","Accept additional cookies",
            "Accept All","Accept all","Accept","Agree","Allow All","Allow all",
            "Permit All","Agree To All","Allow","OK","Close","Accept everything",
            "Accept and Continue"
        ]
        kws = list(base_keywords)
        if translator_cache:
            try:
                kws = list({*base_keywords, *translator_cache.translate_keywords(base_keywords, detected_language)})
            except Exception:
                pass

        for kw in kws:
            sel = f"button:has-text('{kw}')"
            loc = page.locator(sel)
            if await loc.count():
                try:
                    await loc.first.click(timeout=800)
                    await page.wait_for_timeout(150)
                    return True
                except Exception:
                    pass

        # fallback: inject hiding CSS
        try:
            await page.add_style_tag(content=HIDE_COOKIE_CSS)
        except Exception:
            pass
    except Exception:
        pass
    return False

async def gentle_scroll(page):
    try:
        for _ in range(3):
            await page.mouse.wheel(0, 1000)
            await page.wait_for_timeout(250 + random.randint(0, 200))
    except Exception:
        pass

async def handle_city_gate(page):
    try:
        sels = page.locator("select")
        if await sels.count():
            try:
                await sels.first.select_option(index=0)
                await page.wait_for_load_state("networkidle")
                return True
            except Exception:
                pass
    except Exception:
        pass
    try:
        if await page.get_by_role("combobox").count():
            cb = page.get_by_role("combobox").first
            await cb.click()
            opts = page.get_by_role("option")
            if await opts.count():
                await opts.nth(0).click()
                await page.wait_for_load_state("networkidle")
                return True
    except Exception:
        pass
    try:
        changed = await page.evaluate("""
        let changed=false;
        try{
          for (const k of ['city','cidade','municipio','region','localidade']){
            localStorage.setItem(k,'default');
          }
          document.cookie='city=default; path=/';
          changed=true;
        }catch(e){}
        changed;
        """)
        if changed:
            await page.reload(wait_until="networkidle")
            return True
    except Exception:
        pass
    return False

import unicodedata
def _norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()

def norm_host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def score_link(href: str, text: str, keywords: list) -> int:
    s = _norm((href or "") + " " + (text or ""))
    # normalize the keywords too
    return sum(1 for k in keywords if _norm(k) and _norm(k) in s)

async def collect_candidate_links(page, base_url, keywords):
    anchors = page.locator("a[href]")
    pairs = None
    for i in range(3):
        try:
            pairs = await anchors.evaluate_all(
                "els => els.map(e => [e?.href || '', (e?.textContent||'').trim()])"
            )
            break
        except Exception as e:
            msg = str(e)
            if ("Execution context was destroyed" in msg) or ("nodeType" in msg):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=1500)
                except Exception:
                    pass
                await page.wait_for_timeout(200 * (i + 1))
                continue
            raise
    if pairs is None:
        pairs = []

    base_host = norm_host(base_url)
    scored = []
    for href, txt in pairs:
        if not href:
            continue
        # same-site test: keep same host or subdomain (avoid false drops)
        h = norm_host(href)
        if h != base_host and not h.endswith("." + base_host):
            continue
        s = score_link(href, txt, keywords)
        if s:
            scored.append((s, href))

    seen = set()
    chosen = []
    for _score, href in sorted(scored, key=lambda x: -x[0]):
        key = urlparse(href).path.rstrip("/")
        if key not in seen:
            seen.add(key)
            chosen.append(href)
        if len(chosen) >= MAX_SUBPAGES:
            break
    return chosen

# =========================
# Scraper (no local artifacts)
# =========================
class Scraper:
    def __init__(self, archivist: WarcArchivist, existing: ExistingIndex, force_rescrape: bool = False):
        self.archivist = archivist
        self.existing  = existing
        self.force_rescrape = force_rescrape
        self.trans_cache = TranslatorCache(os.path.join(archivist.cache_dir, "translation_cache.json"))
        self.report = []  # list of per-URL dict
        self.report_path = os.path.join(self.archivist.cache_dir, "scrape_report.json")

        # --- append-only JSONL (prefer reading this; appends are safer) ---
        self.report_jsonl_path = os.path.join(self.archivist.cache_dir, "scrape_report.jsonl")
        os.makedirs(os.path.dirname(self.report_jsonl_path), exist_ok=True)
        self._report_lock = asyncio.Lock()
        self._dedupe_lock = asyncio.Lock()

        import uuid, time as _t
        self.run_id = f"{_t.strftime('%Y%m%dT%H%M%SZ', _t.gmtime())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    async def scrape(self, urls):
        # 1) pre-normalize + pre-filter
        urls = list(urls)
        todo = []
        skipped_now = 0

        # --- step 1: clean and de-dup ---
        for u in urls:
            try:
                u_norm = ensure_url_with_protocol((u or "").strip())
            except Exception:
                u_norm = str(u)
            site = canonical_no_www(u_norm)

            if (not self.force_rescrape) and self.existing.already_scraped_seed(site, u_norm):
                skipped_now += 1
            else:
                todo.append(u_norm)

        print(f"[prefilter] total={len(urls)}  skipped={skipped_now}  to_scrape={len(todo)}")

        if not todo:
            # nothing to scrape: save the report and exit
            self.trans_cache.save()
            try:
                os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
                _atomic_write_json(self.report_path, self.report, indent=2)
                print(f"[report] wrote {self.report_path}")
            except Exception as e:
                print(f"[report-error] {type(e).__name__}: {e}")
            print("[done] nothing to scrape; all skipped.")
            return

        # 2) dynamic queue strategy
        todo_queue = asyncio.Queue()
        for u in todo:
            todo_queue.put_nowait((u, 0))   # (url, requeue_round)

        print(f"[strategy] Enqueued {len(todo)} URLs for {CONCURRENCY} dynamic workers.")

        async def run_worker(worker_id, playwright_instance):
            browser = None
            processed_since_restart = 0
            MAX_REQUEUE_ROUNDS = 2

            async def launch_browser():
                for _launch_attempt in range(3):
                    try:
                        b = await asyncio.wait_for(
                            playwright_instance.chromium.launch(
                                headless=False,
                                args=[
                                    "--headless=new",
                                    "--ignore-certificate-errors",
                                    "--disable-blink-features=AutomationControlled",
                                    "--no-sandbox",
                                    "--disable-gpu",
                                ],
                            ),
                            timeout=90,
                        )
                        return b
                    except Exception as e:
                        print(f"[worker-{worker_id}-fatal] launch failed {type(e).__name__}: {e}, retrying...")
                        await asyncio.sleep(5)
                return None

            async def close_browser_safely(b):
                if b is not None:
                    try:
                        await asyncio.wait_for(b.close(), timeout=15)
                    except Exception:
                        pass

            browser = await launch_browser()
            if not browser:
                print(f"[worker-{worker_id}-fatal] could not launch browser after 3 attempts.")
                return

            while True:
                try:
                    u, round_no = todo_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    print(f"[attempt-w{worker_id}] {u} round={round_no}", flush=True)

                    completed_this_url = False

                    for attempt in range(MAX_RETRIES):
                        task = asyncio.create_task(self._scrape_one(browser, u, attempt_no=attempt + 1))
                        try:
                            await asyncio.wait_for(task, timeout=PER_URL_HARD_TIMEOUT)
                            completed_this_url = True
                            break

                        except asyncio.TimeoutError:
                            print(f"[watchdog-timeout-w{worker_id}] {u} (attempt {attempt+1}/{MAX_RETRIES})", flush=True)
                            task.cancel()
                            try:
                                await asyncio.wait_for(task, timeout=10)
                            except (asyncio.CancelledError, PlaywrightError):
                                pass
                            except asyncio.TimeoutError:
                                pass
                            except Exception as e:
                                print(f"[worker-{worker_id}-cleanup] {u}: {type(e).__name__}: {e}")

                            try:
                                site = canonical_no_www(u)
                                if self.existing.already_scraped_seed(site, u):
                                    completed_this_url = True
                                    break
                            except Exception:
                                pass

                        except Exception as e:
                            print(f"[worker-{worker_id}-crash] {u}: {type(e).__name__}: {e}")

                        await asyncio.sleep((BACKOFF_BASE ** attempt) + random.random())

                    if not completed_this_url:
                        if round_no < MAX_REQUEUE_ROUNDS:
                            print(f"[requeue-w{worker_id}] {u} round={round_no + 1}", flush=True)
                            todo_queue.put_nowait((u, round_no + 1))
                        else:
                            print(f"[drop-w{worker_id}] {u} exceeded requeue limit", flush=True)


                    processed_since_restart += 1

                    if processed_since_restart >= CHUNK:
                        await close_browser_safely(browser)
                        browser = await launch_browser()
                        if not browser:
                            print(f"[worker-{worker_id}-fatal] browser relaunch failed.")
                            return
                        processed_since_restart = 0
                        print(f"[worker-{worker_id}] browser restarted after {CHUNK} URLs", flush=True)

                finally:
                    todo_queue.task_done()

            await close_browser_safely(browser)
            print(f"[worker-{worker_id}] finished", flush=True)

        # 3) start concurrent execution
        async with async_playwright() as p:
            tasks = [asyncio.create_task(run_worker(i + 1, p)) for i in range(CONCURRENCY)]

            if tasks:
                print(f"[start] Launching {len(tasks)} dynamic workers...")
                await todo_queue.join()

                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        # 4) wrap-up after everything finishes
        try:
            attempted = 0
            success = 0
            failed = 0
            skipped = 0
            for row in self.report:
                attempted += 1
                st = row.get("status")
                if st == "success":
                    success += 1
                elif st == "failed":
                    failed += 1
                elif st == "skipped":
                    skipped += 1
            print(f"[summary] attempted={attempted} success={success} failed={failed} skipped={skipped} input_total={len(urls)}")
        except Exception as e:
            print(f"[summary-error] {type(e).__name__}: {e}")

        self.trans_cache.save()
        try:
            os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
            _atomic_write_json(self.report_path, self.report, indent=2)
            print(f"[report] wrote {self.report_path}")
        except Exception as e:
            print(f"[report-error] {type(e).__name__}: {e}")

        print("[done] all scraping tasks finished.")

    async def _route_blocker(self, route):
        try:
            req = route.request
            url = (req.url or "").lower()
            rtype = req.resource_type or ""

            # block only large static assets; keep document / script / xhr / fetch
            if rtype in BLOCKED_TYPES:
                # extension-based fallback (some sites serve fonts as other types)
                return await route.abort()

            # block some extensions even when typed as "other"
            if any(url.endswith(ext) for ext in BLOCKED_EXTS):
                return await route.abort()

            # do not block challenge-related scripts
            # (routing is usually enabled only on subpages, but be safe here too)
            if ("cf-chl" in url) or ("challenge" in url and "cf" in url):
                return await route.continue_()

            return await route.continue_()
        except Exception:
            # fallback: allow, so requests do not hang
            try:
                await route.continue_()
            except Exception:
                pass

    async def _scrape_one_subpage(self, context, link, site, seed_url):
        seed_url = ensure_url_with_protocol((seed_url or "").strip())
        subp = None
        try:
            if not await robots_allows(link):
                print(f"[robots-sub] disallow: {link}")
                return

            subp = await context.new_page()
            await smart_goto(subp, link, is_main=False, timeout_ms=30000)

            # ensure basic DOM first
            try:
                await subp.wait_for_selector("html", timeout=2000)
            except Exception:
                pass
            try:
                await subp.wait_for_load_state("networkidle", timeout=1000)
            except Exception:
                pass

            # cookies / overlays
            d2 = await detect_page_language(subp)
            await accept_cookies(subp, detected_language=d2, translator_cache=self.trans_cache)
            await remove_cookie_banners(subp)

            # challenge-page handling
            waited = await wait_through_challenge(subp, max_ms=18000)
            if not waited or await is_challenge_page(subp):
                print(f"[sub-challenge-skip] {link}")
                return

            final_link = subp.url

            # DOM (safe fetch)
            try:
                sub_html = await safe_outer_html(subp)   # subp is correct here
            except Exception:
                sub_html = await subp.content()
            sbody = (sub_html or "").encode("utf-8", "ignore")
            sheaders = {"content-type": "text/html; charset=utf-8"}
            sstatus = 200

            import hashlib
            s_sha = hashlib.sha256(sbody).hexdigest()
            async with self._dedupe_lock:
                s_hit = self.existing.lookup_store_by_sha(s_sha)

                if s_hit:
                    s_rid = s_hit["record_id"]; s_warc = s_hit["warc"]
                    s_mime = (s_hit.get("mime") or "text/html").split(";")[0].lower()
                    s_origin = "reused_from_store"
                else:
                    s_rid, s_warc, s_mime, s_sha = await self.archivist.write_store_response(
                        final_link, sstatus, sheaders, sbody, "text/html"
                    )
                    self.existing.add_store_sha(s_sha, {
                        "record_id": s_rid, "warc": s_warc,
                        "mime": s_mime, "url": final_link, "sha256": s_sha
                    })
                    s_origin = "newly_scraped"

                # subpage: per-page + store_record de-dup check/write in one lock.
                if self.existing.already_scraped_page(site, final_link, s_rid):
                    print(f"[subpage-skip] slice already exists for {final_link} with same store record; no new slice.")
                else:
                    await self.archivist.write_slice_revisit(
                        site=site, seed=seed_url, url=final_link, kind="html",
                        mime=s_mime, store_record_id=s_rid, store_warc_name=s_warc,
                        store_origin=s_origin,
                    )
                    self.existing.update_page_store(site, final_link, s_rid)
                    print(f"[subpage] {final_link} ({s_origin})")
        finally:
            # make sure closing the subpage cannot hang forever
            if subp:
                try:
                    await asyncio.wait_for(subp.close(), timeout=5)
                except Exception:
                    pass

    async def _scrape_one(self, browser, seed_url, attempt_no: int = 1):
        # -------- 1) normalize & init the record --------
        try:
            seed_url_norm = ensure_url_with_protocol((seed_url or "").strip())
        except Exception:
            seed_url_norm = str(seed_url)
        site = canonical_no_www(seed_url_norm)

        entry = {
            "seed": seed_url_norm,
            "site": site,
            "status": "pending",
            "reason": None,
            "details": None,
            "final_url": None,
            "attempt_no": attempt_no,
        }
        async def _append_jsonl(row: dict):
            row = dict(row)  # defensive copy
            row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            row["run_id"] = self.run_id
            # write JSONL line by line (append-only)
            async with self._report_lock:
                with open(self.report_jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        # _finish is async so exceptions are caught in the current context and the write completes
        async def _finish(status: str, reason: str | None = None,
                    details: str | None = None, final_url: str | None = None):
            entry["status"] = status
            entry["reason"] = reason
            entry["details"] = details
            entry["final_url"] = final_url
            self.report.append(dict(entry))

            # await directly so exceptions are caught here and the write finishes
            try:
                await _append_jsonl(entry)
            except Exception as e:
                print(f"[report-write-error] {e}")

            print(f"[end] {seed_url_norm} status={status} reason={reason or '-'}", flush=True)

        # -------- 2) early-exit branches (always use the normalized URL) --------
        if (not self.force_rescrape) and self.existing.already_scraped_seed(site, seed_url_norm):
            print(f"[skip] {seed_url_norm} already scraped for this version.")
            await _finish(status="skipped", reason=REASON_SKIP_ALREADY_DONE)
            return

        if not await robots_allows(seed_url_norm):
            print(f"[robots] disallow: {seed_url_norm} — skip")
            await _finish(status="skipped", reason=REASON_ROBOTS_DISALLOW)
            return

        print(f"[start] {seed_url_norm} (attempt {attempt_no}/{MAX_RETRIES})", flush=True)

        # -------- 3) open context and scrape --------
        success_main = False
        context = None
        page = None
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                extra_http_headers={"Accept-Language": ACCEPT_LANG},
                ignore_https_errors=True,
            )
            context.set_default_timeout(20000)              # 20s
            context.set_default_navigation_timeout(20000)   # 20s
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = await context.new_page()
            await stealth_async(page)

            # duplicate robots check removed (it used the un-normalized seed_url)
            # if not await robots_allows(seed_url): ...  # <-- removed

            # ===== Landing =====
            # use the normalized seed_url_norm everywhere
            _ = await smart_goto(page, seed_url_norm, is_main=True, timeout_ms=60000)

            detected_lang = await detect_page_language(page)
            await accept_cookies(page, detected_language=detected_lang, translator_cache=self.trans_cache)
            await remove_cookie_banners(page)

            # wait through the challenge
            waited = await wait_through_challenge(page, max_ms=25000)
            if not waited:
                print(f"[challenge-timeout] {seed_url_norm} still shows challenge after waiting; will not mark done.")
                await _finish(status="failed", reason=REASON_CHALLENGE_TIMEOUT)
                return
            try:
                await page.wait_for_selector("html", timeout=2000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=1000)
            except Exception:
                pass
            if await is_challenge_page(page):
                await _finish(status="failed", reason=REASON_STILL_CHALLENGE)
                return
            # await page.evaluate("window.dispatchEvent(new Event('resize'));")
            try:
                await safe_eval(page, "window.dispatchEvent(new Event('resize'));")
            except Exception:
                pass

            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(300)

            final_main_url = page.url

                        # store the DOM-rendered HTML (not the raw "Just a moment..." server HTML)
            raw_html = await safe_outer_html(page)

            async def _wait_client_render_extra(page, budget_ms=8000):
                # 1) scroll a few times to trigger lazy-load
                try:
                    for _ in range(4):
                        await page.mouse.wheel(0, 1200)
                        await page.wait_for_timeout(250)
                except:
                    pass
                # 2) if body is empty, wait once for a child node to appear
                try:
                    await page.wait_for_function(
                        "document.body && document.body.childElementCount > 0",
                        timeout=2000,
                    )
                except:
                    pass
                # 3) attach a short-lived MutationObserver to collect changes over a brief window
                try:
                    await page.evaluate(
                        """
                        () => new Promise(resolve => {
                            const b = document.body; if (!b) return resolve();
                            const obs = new MutationObserver(() => {});
                            obs.observe(b, {subtree:true, childList:true});
                            setTimeout(() => { obs.disconnect(); resolve(); }, 1500);
                        })
                        """
                    )
                except:
                    pass
                # 4) one more networkidle wait
                try:
                    await page.wait_for_load_state("networkidle", timeout=1500)
                except:
                    pass
                await page.wait_for_timeout(300)

            # measure "visible text" length (strip script/style/head tags)
            def _visible_text_len(html: str) -> int:
                if not html:
                    return 0
                # drop script/style/noscript
                tmp = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\\1>", " ", html)
                # drop comments
                tmp = re.sub(r"(?is)<!--.*?-->", " ", tmp)
                # drop the head section
                tmp = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", tmp)
                # drop remaining tags
                tmp = re.sub(r"(?is)<[^>]+>", " ", tmp)
                # collapse whitespace
                tmp = re.sub(r"\s+", " ", tmp).strip()
                return len(tmp)

            # ===== structural check: use visible-text length to judge if load finished =====
            vis_len = await asyncio.to_thread(_visible_text_len, raw_html)

            # thresholds can be tuned
            MIN_VIS_FOR_LOAD = 120   # below this: not fully loaded, keep waiting / try another fetch
            MIN_VIS_FOR_ACCEPT = 60  # minimum to accept the page as non-empty

            # little visible text -> not fully loaded, wait one more round
            if vis_len < MIN_VIS_FOR_LOAD:
                await _wait_client_render_extra(page)
                raw_html = await safe_outer_html(page)
                vis_len = await asyncio.to_thread(_visible_text_len, raw_html)

            # still little? fall back to the raw HTTP response for more complete HTML
            if vis_len < MIN_VIS_FOR_LOAD:
                try:
                    final_url, s, h, body = await fetch_raw_with_context(context, final_main_url)
                    if s and body:
                        candidate = body.decode("utf-8", "ignore")
                        cand_vis = await asyncio.to_thread(_visible_text_len, candidate)
                        # replace only if the candidate looks more like a real page
                        if cand_vis >= vis_len and cand_vis >= MIN_VIS_FOR_ACCEPT:
                            raw_html = candidate
                            vis_len = cand_vis
                except Exception:
                    pass

            # final fallback: still too short -> treat as empty_dom / nothing really loaded
            if vis_len < MIN_VIS_FOR_ACCEPT:
                await _finish(
                    status="failed",
                    reason=REASON_EMPTY_DOM,
                    details=f"landing html visible text too short (len={vis_len}) after extra waits/raw-fetch",
                )
                return

            body = (raw_html or "").encode("utf-8", "ignore")
            headers = {"content-type": "text/html; charset=utf-8"}
            status = 200

            # reuse-or-write decision + write to store
            sha = hashlib.sha256(body).hexdigest()
            async with self._dedupe_lock:
                hit = self.existing.lookup_store_by_sha(sha)
                if hit:
                    store_rid = hit["record_id"]
                    store_warc = hit["warc"]
                    mime = (hit.get("mime") or "text/html").split(";")[0].lower()
                    store_origin = "reused_from_store"
                else:
                    store_rid, store_warc, mime, sha = await self.archivist.write_store_response(
                        final_main_url, status, headers, body, "text/html"
                    )
                    self.existing.add_store_sha(sha, {
                        "record_id": store_rid, "warc": store_warc,
                        "mime": mime, "url": final_main_url, "sha256": sha
                    })
                    store_origin = "newly_scraped"

                # Check/write/update in one lock to avoid in-process races.
                if self.existing.already_scraped_page(site, final_main_url, store_rid):
                    print(f"[landing-skip] slice already exists for {final_main_url} with same store record; no new slice.")
                else:
                    await self.archivist.write_slice_revisit(
                        site=site, seed=seed_url_norm, url=final_main_url, kind="html",
                        mime=mime, store_record_id=store_rid, store_warc_name=store_warc,
                        store_origin=store_origin,
                    )
                    # update the in-memory page -> store map for later checks in this run
                    self.existing.update_page_store(site, final_main_url, store_rid)
                    print(f"[landing] {seed_url_norm} -> {final_main_url} ({store_origin})")
            success_main = True
            self.existing.add_seed_done(site, seed_url_norm)
            await _finish(status="success", reason=REASON_OK, final_url=final_main_url)

            # ===== Subpages =====
            # language and keyword expansion
            kwords = list(KEYWORDS)
            try:
                tk = self.trans_cache.translate_keywords(KEYWORDS, detected_lang)
                if tk:
                    kwords = list({*KEYWORDS, *tk})
            except Exception:
                pass

            # subs = await collect_candidate_links(page, seed_url, kwords)
            subs = await collect_candidate_links(page, final_main_url, kwords)
            subs = subs[:MAX_SUBPAGES]
            # print(f"[subs] {len(subs)} candidates")
            await context.route("**/*", self._route_blocker)

            # iterate subpage links:
            for i, link in enumerate(subs, 1):
                # check if the main task was cancelled (fail fast, avoid noisy TargetClosedError)
                if asyncio.current_task().cancelled():
                    break

                try:
                    await asyncio.wait_for(
                        self._scrape_one_subpage(context, link, site, seed_url_norm), timeout=40
                    )
                except asyncio.TimeoutError:
                    print(f"[subpage-timeout] {i}/{len(subs)} {link}")
                except (PlaywrightError, asyncio.CancelledError):
                    # browser closed or task cancelled: break without a traceback
                    # (usually means the main task is already cleaning up)
                    pass
                except Exception as e:
                    print(f"[subpage-error] {i}/{len(subs)} {link}: {type(e).__name__}: {e}")
            print(f"[done-one] {seed_url}")
        except asyncio.CancelledError:
            # interrupted by the outer watchdog (wait_for + task.cancel);
            # record a failed entry that was previously missed
            print(f"[cancelled] {seed_url_norm} by watchdog (attempt {attempt_no})")
            await _finish(
                status="failed",
                # a dedicated reason constant here helps the check script's stats
                reason=REASON_WATCHDOG_TIMEOUT,
                details=f"hard timeout after {PER_URL_HARD_TIMEOUT}s"
            )
            # do not re-raise CancelledError; let `await task` see a normal finish
            # so the outer except (CancelledError, PlaywrightError) does not fire
            return
        except Exception as e:
            print(f"[fatal] {seed_url}: {type(e).__name__}: {e}")
            await _finish(
                status="failed",
                reason=REASON_EXCEPTION,
                details=f"{type(e).__name__}: {e}"
            )
        finally:
            if context is not None:
                try:
                    await context.unroute("**/*", self._route_blocker)
                except Exception:
                    pass
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=5)
                except Exception:
                    pass
            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=5)
                except Exception:
                    pass

        # mark done only when the main page returned a non-challenge page
        if success_main:
            self.existing.add_seed_done(site, seed_url_norm)


# =========================
# IO
# =========================
def load_urls(path: str):
    with open(path, "r", encoding="utf-8") as f:
        if path.endswith(".json"):
            obj = json.load(f)
            if isinstance(obj, dict) and "urls" in obj:
                return [ensure_url_with_protocol(x) for x in obj["urls"]]
            elif isinstance(obj, list):
                return [ensure_url_with_protocol(x) for x in obj]
            else:
                raise SystemExit("JSON must be array or {'urls':[...]}!")
        else:
            return [ensure_url_with_protocol(ln.strip()) for ln in f if ln.strip()]

def load_urls_from_as2web(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    data = obj.get("data")
    if not isinstance(data, dict):
        raise SystemExit(f"as2web.json at {path} has no valid 'data' dict")
    seen = set()
    urls = []
    for asn_entry in data.values():
        if not isinstance(asn_entry, dict):
            continue
        raw = (asn_entry.get("url") or "").strip()
        if not raw:
            continue
        normed = ensure_url_with_protocol(raw)
        if normed not in seen:
            seen.add(normed)
            urls.append(normed)
    urls.sort()
    return urls

def _resolve_as2web_path(version_tag: str) -> str | None:
    tag_compact = version_tag.replace("-", "")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, "as2web", tag_compact, "as2web.json")
    return path if os.path.isfile(path) else None

def _parse_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {v}")

def _run_incremental_health_monitor(archives_dir: str, version_tag: str, cache_dir: str):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warc_health_monitor.py")
    if not os.path.exists(script):
        print(f"[health-monitor] script missing: {script}")
        return
    state_json = os.path.join(cache_dir, f"warc_health_state_{version_tag}.json")
    summary_json = os.path.join(cache_dir, f"warc_health_summary_{version_tag}.json")
    cmd = [
        sys.executable,
        script,
        "--archives-dir", archives_dir,
        "--version-tag", version_tag,
        "--state-json", state_json,
        "--summary-json", summary_json,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0:
            print(f"[health-monitor] completed, summary={summary_json}")
        else:
            print(f"[health-monitor] failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    except Exception as e:
        print(f"[health-monitor] error: {e}")

# =========================
# CLI
# =========================
def _backup_store_index(archives_dir: str, version_tag: str, max_backups: int = 5):
    src = os.path.join(archives_dir, "store", "index", "store_index.jsonl")
    if not os.path.exists(src):
        print("[backup] store_index.jsonl does not exist yet, nothing to back up.")
        return
    backup_dir = os.path.join(archives_dir, "store", "index", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    dst = os.path.join(backup_dir, f"store_index_{version_tag}_{ts}.jsonl")
    shutil.copy2(src, dst)
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    print(f"[backup] store_index.jsonl -> {dst}  ({size_mb:.1f} MB)")
    backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith("store_index_") and f.endswith(".jsonl")],
    )
    while len(backups) > max_backups:
        old = os.path.join(backup_dir, backups.pop(0))
        os.remove(old)
        print(f"[backup] pruned old backup: {os.path.basename(old)}")

async def amain(args):
    if args.input:
        urls = load_urls(args.input)
        print(f"[input] loaded {len(urls)} URLs from {args.input}")
    else:
        as2web_path = _resolve_as2web_path(args.version_tag)
        if as2web_path:
            urls = load_urls_from_as2web(as2web_path)
            print(f"[input] loaded {len(urls)} distinct URLs from {as2web_path}")
        else:
            tag_compact = args.version_tag.replace("-", "")
            raise SystemExit(
                f"No --input provided and no default as2web.json found at "
                f"as2web/{tag_compact}/as2web.json. "
                f"Please provide --input <path> to a URL list."
            )
    print("#URLs:", len(urls))
    _backup_store_index(args.archives_dir, args.version_tag)
    archivist = WarcArchivist(
        args.archives_dir,
        args.version_tag,
        rotate_every_records=args.rotate_every_records,
        fsync_every_records=args.fsync_every_records,
        durability_level=args.durability_level,
        health_check_on_start=args.health_check_on_start,
    )
    existing  = ExistingIndex(args.archives_dir, args.version_tag)
    try:
        scraper = Scraper(archivist=archivist, existing=existing, force_rescrape=args.force_rescrape)
        await scraper.scrape(urls)
    finally:
        archivist.close()
        if args.post_run_health_check:
            _run_incremental_health_monitor(args.archives_dir, args.version_tag, archivist.cache_dir)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="Path to a JSON list or text file containing one URL per line.")
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--version-tag", required=True, help="like 2025-01-01")
    ap.add_argument("--max_retries", type=int, default=MAX_RETRIES)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--screenshots", action="store_true", help="Capture full-page screenshots into WARC")
    ap.add_argument("--force-rescrape", action="store_true", help="Ignore 'already scraped' check and re-scrape all seeds")
    ap.add_argument("--durability-level", choices=["strict", "balanced", "throughput"], default=DURABILITY_LEVEL)
    ap.add_argument("--fsync-every-records", type=int, default=FSYNC_EVERY_RECORDS)
    ap.add_argument("--rotate-every-records", type=int, default=WARC_ROTATE_EVERY_RECORDS)
    ap.add_argument("--health-check-on-start", type=_parse_bool, default=HEALTH_CHECK_ON_START)
    ap.add_argument("--post-run-health-check", type=_parse_bool, default=POST_RUN_HEALTH_CHECK)
    args = ap.parse_args()
    CONCURRENCY = args.concurrency
    CAPTURE_SCREENSHOTS = args.screenshots
    FORCE_RESCRAPE = args.force_rescrape
    MAX_RETRIES = args.max_retries
    asyncio.run(amain(args))
