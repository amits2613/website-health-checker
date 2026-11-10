"""
Website health checks. Standard library only.

The main entry point is check_site(url). It runs these groups of checks:
  1. Reachability   - the site answers, with a success code, in reasonable time
  2. Redirects      - not too many hops, and http:// goes to https://
  3. TLS            - the certificate is valid and not close to expiry
  4. Security headers - common protective headers are present
  5. Compression and caching - HTML is compressed and has cache rules
  6. Page basics    - title, description, language, one h1
  7. Internal links - links on the homepage do not return errors
  8. Crawl files    - robots.txt and sitemap.xml exist
  9. Mixed content  - no http:// resources on an https page

Each check returns a dict: {"name", "status", "detail"} where status is
"pass", "warn" or "fail". Only "fail" makes the overall result unhealthy.
"""
import datetime
import gzip
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

USER_AGENT = "website-health-checker/1.0"
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024       # ignore anything bigger than 2 MB
SLOW_SECONDS = 1.0                      # warn if the homepage takes longer
TLS_WARN_DAYS = 30                      # warn if the certificate expires sooner
TLS_FAIL_DAYS = 7                       # fail if the certificate expires sooner


def result(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


# ---------- fetching ----------

class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from following redirects, so we can count and report them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoAutoRedirect)


def _gunzip(data):
    """Unpack gzip data. If it is not valid gzip, return it unchanged."""
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return data


def fetch(url, timeout=10):
    """
    GET one URL without following redirects.
    Returns a dict with: status, headers, body, seconds, error.
    On a network error, status is None and error holds the message.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Encoding": "gzip"})
    start = time.monotonic()
    try:
        response = _opener.open(request, timeout=timeout)
        status = response.status
        headers = response.headers
        body = response.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as error:       # 3xx and 4xx/5xx end up here
        status = error.code
        headers = error.headers
        body = error.read(MAX_BODY_BYTES) if error.fp else b""
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as error:
        return {"status": None, "headers": {}, "body": b"",
                "seconds": time.monotonic() - start, "error": str(error)}

    if headers.get("Content-Encoding", "").lower() == "gzip":
        body = _gunzip(body)
    return {"status": status, "headers": headers, "body": body,
            "seconds": time.monotonic() - start, "error": None}


def follow_redirects(url, timeout=10):
    """
    Follow redirects by hand. Returns (final_response, hops, final_url).
    final_response is the dict from fetch(); hops is the list of URLs visited.
    """
    hops = [url]
    current = url
    response = fetch(current, timeout)
    while response["status"] in (301, 302, 303, 307, 308) and len(hops) <= MAX_REDIRECTS:
        location = response["headers"].get("Location")
        if not location:
            break
        current = urllib.parse.urljoin(current, location)
        hops.append(current)
        response = fetch(current, timeout)
    return response, hops, current


# ---------- HTML parsing ----------

class PageParser(HTMLParser):
    """Reads one HTML page and collects the things the checks need."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.lang = ""
        self.h1_count = 0
        self.links = []          # href values from <a>
        self.resources = []      # src and stylesheet URLs (images, scripts, css)
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.description = a.get("content") or ""
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag in ("img", "script", "iframe") and a.get("src"):
            self.resources.append(a["src"])
        elif tag == "link" and "stylesheet" in (a.get("rel") or "").lower() and a.get("href"):
            self.resources.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_page(body_bytes):
    parser = PageParser()
    parser.feed(body_bytes.decode("utf-8", errors="replace"))
    return parser


def same_site(url, base_host):
    return urllib.parse.urlsplit(url).hostname == base_host


# ---------- pure rules (easy to test) ----------

def tls_status(days_left):
    if days_left < TLS_FAIL_DAYS:
        return "fail"
    if days_left < TLS_WARN_DAYS:
        return "warn"
    return "pass"


def timing_status(seconds):
    return "warn" if seconds > SLOW_SECONDS else "pass"


# ---------- individual checks ----------

def check_reachable(final, final_url):
    if final["error"]:
        return result("Reachable", "fail", f"could not connect: {final['error']}")
    if final["status"] and 200 <= final["status"] < 300:
        return result("Reachable", "pass", f"{final['status']} from {final_url}")
    return result("Reachable", "fail", f"returned HTTP {final['status']}")


def check_redirects(hops, original_url, final_status):
    """Fail on loops or too many hops. Warn if an http:// site never moves to https://."""
    redirects = len(hops) - 1
    if final_status in (301, 302, 303, 307, 308):
        return result("Redirects", "fail", f"redirect loop or more than {MAX_REDIRECTS} redirects")
    if original_url.startswith("http://") and not hops[-1].startswith("https://"):
        return result("Redirects", "warn", f"{redirects} redirect(s), but the site stays on http")
    if original_url.startswith("http://"):
        return result("Redirects", "pass", f"{redirects} redirect(s), http moved to https")
    return result("Redirects", "pass", f"{redirects} redirect(s)")


def check_timing(final):
    detail = f"{final['seconds']:.2f}s"
    return result("Response time", timing_status(final["seconds"]), detail)


def check_tls(hostname, port=443, timeout=10):
    """Read the certificate and see how many days are left."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
    except (ssl.SSLError, OSError, ValueError) as error:
        return result("TLS certificate", "fail", f"TLS check failed: {error}")
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    days_left = int((expires - time.time()) / 86400)
    status = tls_status(days_left)
    return result("TLS certificate", status, f"expires in {days_left} days")


SECURITY_HEADERS = {
    "Strict-Transport-Security": "tells browsers to use https only",
    "Content-Security-Policy": "limits where scripts and other content can load from",
    "X-Content-Type-Options": "stops browsers guessing content types",
    "X-Frame-Options": "stops other sites from framing this one (or use CSP frame-ancestors)",
}


def check_security_headers(headers, is_https):
    missing = []
    for name in SECURITY_HEADERS:
        if name == "X-Frame-Options" and headers.get("Content-Security-Policy", "").find("frame-ancestors") >= 0:
            continue
        if name == "Strict-Transport-Security" and not is_https:
            continue
        if headers.get(name) is None:
            missing.append(name)
    if not missing:
        return result("Security headers", "pass", "all expected headers present")
    return result("Security headers", "warn", "missing: " + ", ".join(missing))


def check_compression_and_cache(headers):
    problems = []
    if "gzip" not in (headers.get("Content-Encoding") or "") and "br" not in (headers.get("Content-Encoding") or ""):
        problems.append("HTML is not compressed (no gzip or br)")
    if not headers.get("Cache-Control"):
        problems.append("no Cache-Control header")
    if not problems:
        return result("Compression and caching", "pass", "compressed and cache rules set")
    return result("Compression and caching", "warn", "; ".join(problems))


def check_page_basics(page):
    problems = []
    if not page.title.strip():
        problems.append("no <title>")
    if not page.description.strip():
        problems.append("no meta description")
    if not page.lang:
        problems.append("no lang attribute on <html>")
    if page.h1_count != 1:
        problems.append(f"{page.h1_count} <h1> tags (expected 1)")
    if not problems:
        return result("Page basics", "pass", "title, description, language and h1 look good")
    return result("Page basics", "warn", "; ".join(problems))


def check_mixed_content(page, is_https):
    if not is_https:
        return result("Mixed content", "pass", "not an https page")
    insecure = [r for r in page.resources if r.startswith("http://")]
    if not insecure:
        return result("Mixed content", "pass", "no http:// resources")
    return result("Mixed content", "warn", f"{len(insecure)} http:// resource(s) on an https page")


def check_crawl_file(name, url, timeout):
    response = fetch(url, timeout)
    if response["status"] == 200:
        return result(name, "pass", f"{url} found")
    return result(name, "warn", f"{url} returned {response['status'] or 'no response'}")


def internal_links(page, base_url, base_host):
    """Unique absolute same-site links from a page, without #fragments."""
    found = []
    for href in page.links:
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not same_site(absolute, base_host):
            continue
        clean = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        if clean not in found:
            found.append(clean)
    return found


def check_links(links, timeout, workers):
    """Fetch every link at once and report the ones that fail."""
    if not links:
        return result("Internal links", "pass", "no internal links on the homepage")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(lambda u: fetch(u, timeout), links))
    broken = [(u, r["status"] or r["error"]) for u, r in zip(links, responses)
              if r["error"] or r["status"] is None or r["status"] >= 400]
    if not broken:
        return result("Internal links", "pass", f"{len(links)} checked, none broken")
    sample = "; ".join(f"{u} ({code})" for u, code in broken[:5])
    return result("Internal links", "fail", f"{len(broken)} of {len(links)} broken: {sample}")


# ---------- the whole check ----------

def check_site(url, max_links=30, timeout=10, workers=8):
    """Run every check for one site. Returns a dict with a summary and the results."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL must start with http:// or https:// and include a host")
    is_https = parts.scheme == "https"
    origin = f"{parts.scheme}://{parts.netloc}"

    checks = []
    final, hops, final_url = follow_redirects(url, timeout)
    checks.append(check_reachable(final, final_url))
    if final["error"]:
        # Nothing else can be measured if we could not connect at all.
        return _summary(url, checks)

    checks.append(check_redirects(hops, url, final["status"]))
    checks.append(check_timing(final))

    if is_https:
        checks.append(check_tls(parts.hostname, timeout=timeout))

    if final["status"] and 200 <= final["status"] < 300:
        checks.append(check_security_headers(final["headers"], is_https))
        checks.append(check_compression_and_cache(final["headers"]))
        content_type = final["headers"].get("Content-Type", "")
        if "html" in content_type.lower():
            page = parse_page(final["body"])
            checks.append(check_page_basics(page))
            checks.append(check_mixed_content(page, is_https))
            links = internal_links(page, final_url, parts.hostname)[:max_links]
            checks.append(check_links(links, timeout, workers))
        else:
            checks.append(result("Page basics", "warn", f"homepage is not HTML ({content_type or 'no type'})"))

    checks.append(check_crawl_file("robots.txt", origin + "/robots.txt", timeout))
    checks.append(check_crawl_file("sitemap.xml", origin + "/sitemap.xml", timeout))

    return _summary(url, checks)


def _summary(url, checks):
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        counts[check["status"]] += 1
    return {
        "url": url,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "healthy": counts["fail"] == 0,
        "counts": counts,
        "checks": checks,
    }
