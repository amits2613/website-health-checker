"""
Tests run against a small local web server, so no internet access is needed.
Run with:  python3 -m unittest discover -s src/tests -v
"""
import gzip
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import checker  # noqa: E402

GOOD_HOME = b"""<!doctype html><html lang="en"><head>
<title>Home</title><meta name="description" content="A test site">
</head><body><h1>Welcome</h1>
<a href="/about">About</a> <a href="/about#team">Team</a>
<a href="/broken">Broken</a> <a href="https://other.example.org/">External</a>
<img src="/logo.png"></body></html>"""

PAGES = {
    "/": (200, "text/html", GOOD_HOME, {"Content-Encoding": "gzip", "Cache-Control": "max-age=60",
                                         "Strict-Transport-Security": "max-age=1",
                                         "Content-Security-Policy": "default-src 'self'",
                                         "X-Content-Type-Options": "nosniff",
                                         "X-Frame-Options": "DENY"}),
    "/about": (200, "text/html", b"<html><head><title>About</title></head><body><h1>x</h1></body></html>", {}),
    "/old": (301, "text/html", b"", {"Location": "/about"}),
    "/loop": (302, "text/html", b"", {"Location": "/loop"}),
    "/robots.txt": (200, "text/plain", b"User-agent: *", {}),
    "/broken": (404, "text/html", b"not found", {}),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, ctype, body, headers = PAGES.get(self.path, (404, "text/html", b"", {}))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for name, value in headers.items():
            self.send_header(name, value)
        if headers.get("Content-Encoding") == "gzip":
            body = gzip.compress(body)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def by_name(self, report, name):
        return next(c for c in report["checks"] if c["name"] == name)


class PureRuleTests(unittest.TestCase):
    def test_tls_thresholds(self):
        self.assertEqual(checker.tls_status(100), "pass")
        self.assertEqual(checker.tls_status(20), "warn")
        self.assertEqual(checker.tls_status(3), "fail")

    def test_slow_response_warns(self):
        self.assertEqual(checker.timing_status(0.2), "pass")
        self.assertEqual(checker.timing_status(2.5), "warn")

    def test_parser_reads_title_description_and_links(self):
        page = checker.parse_page(GOOD_HOME)
        self.assertEqual(page.title, "Home")
        self.assertEqual(page.description, "A test site")
        self.assertEqual(page.lang, "en")
        self.assertEqual(page.h1_count, 1)
        self.assertIn("/about", page.links)


class PageRuleTests(unittest.TestCase):
    def test_page_basics_warns_about_missing_parts(self):
        page = checker.parse_page(b"<html><body><p>no head</p></body></html>")
        check = checker.check_page_basics(page)
        self.assertEqual(check["status"], "warn")
        self.assertIn("no <title>", check["detail"])
        self.assertIn("0 <h1> tags", check["detail"])

    def test_page_basics_passes_for_complete_page(self):
        page = checker.parse_page(GOOD_HOME)
        self.assertEqual(checker.check_page_basics(page)["status"], "pass")

    def test_mixed_content_warns_on_https_page(self):
        page = checker.parse_page(b'<html><body><img src="http://cdn.example.com/a.png"></body></html>')
        self.assertEqual(checker.check_mixed_content(page, is_https=True)["status"], "warn")

    def test_mixed_content_ignored_on_http_page(self):
        page = checker.parse_page(b'<html><body><img src="http://cdn.example.com/a.png"></body></html>')
        self.assertEqual(checker.check_mixed_content(page, is_https=False)["status"], "pass")


class SiteChecks(LocalSite):
    def test_reachable_site_passes_reachability_check(self):
        report = checker.check_site(self.base + "/about")
        self.assertEqual(self.by_name(report, "Reachable")["status"], "pass")

    def test_redirect_is_followed_and_counted(self):
        report = checker.check_site(self.base + "/old")
        self.assertIn("1 redirect", self.by_name(report, "Redirects")["detail"])

    def test_redirect_loop_fails(self):
        report = checker.check_site(self.base + "/loop")
        self.assertEqual(self.by_name(report, "Redirects")["status"], "fail")
        self.assertFalse(report["healthy"])

    def test_broken_internal_link_is_reported(self):
        report = checker.check_site(self.base + "/")
        links = self.by_name(report, "Internal links")
        self.assertEqual(links["status"], "fail")
        self.assertIn("/broken", links["detail"])

    def test_security_headers_and_caching_pass_when_present(self):
        report = checker.check_site(self.base + "/")
        self.assertEqual(self.by_name(report, "Security headers")["status"], "pass")
        self.assertEqual(self.by_name(report, "Compression and caching")["status"], "pass")

    def test_missing_sitemap_is_a_warning_not_a_failure(self):
        report = checker.check_site(self.base + "/")
        self.assertEqual(self.by_name(report, "sitemap.xml")["status"], "warn")
        self.assertEqual(self.by_name(report, "robots.txt")["status"], "pass")

    def test_unreachable_site_fails(self):
        report = checker.check_site("http://127.0.0.1:1/", timeout=2)
        self.assertEqual(self.by_name(report, "Reachable")["status"], "fail")
        self.assertFalse(report["healthy"])
        names = [c["name"] for c in report["checks"]]
        self.assertEqual(names, ["Reachable"])

    def test_compressed_page_passes_compression_check(self):
        report = checker.check_site(self.base + "/")
        self.assertEqual(self.by_name(report, "Compression and caching")["status"], "pass")

    def test_bad_url_is_rejected(self):
        with self.assertRaises(ValueError):
            checker.check_site("example.com")


if __name__ == "__main__":
    unittest.main()
