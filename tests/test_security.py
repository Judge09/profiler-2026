"""Security regressions: SSRF and login brute-force.

Both of these were confirmed by working exploit before they were fixed, so
these tests exist to prove the exploits stay dead:

  * `fetch_page("http://127.0.0.1:8477/admin")` returned the contents of a
    local service as collected post text. On a cloud host the same input
    reaches 169.254.169.254 and returns the instance's credentials.
  * The login form accepted about 2,400 guesses a second with no delay or
    lockout, against a single shared password whose default is in the README.

Run with:  python -m pytest tests/test_security.py -v
"""

import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PROFILER_DATABASE_URI"] = "sqlite:///:memory:"
os.environ.setdefault("PROFILER_PASSWORD", "correct-horse-battery")
# The guard has an escape hatch for lab use; it must be off for these tests.
os.environ.pop("ALLOW_PRIVATE_FETCH", None)

from app import create_app  # noqa: E402
from app.monitor import collectors, safefetch  # noqa: E402

SECRET = "sk-live-must-never-leak"


class _Handler(BaseHTTPRequestHandler):
    """Stands in for an internal service that should be unreachable."""

    def do_GET(self):
        if self.path.startswith("/bounce"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:8080/secret")
            self.end_headers()
            return
        body = ("<html><body><p>INTERNAL: %s and more text to pass the "
                "minimum block length used by the page reader.</p></body>"
                "</html>" % SECRET).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SSRFTests(unittest.TestCase):
    """A user-supplied URL must not reach the server's own network."""

    @classmethod
    def setUpClass(cls):
        # Port 8080 is in ALLOWED_PORTS, so only the address check can stop
        # this -- which is exactly what is being tested.
        cls.server = HTTPServer(("127.0.0.1", 8080), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_the_original_exploit_no_longer_leaks(self):
        posts, report = collectors.collect(
            ["page"], "http://127.0.0.1:8080/admin",
            {"page": {"url": "http://127.0.0.1:8080/admin"}})
        self.assertEqual(posts, [])
        self.assertFalse(any(SECRET in p.get("text", "") for p in posts))
        self.assertIn("loopback", report["sources"][0]["note"])

    def test_cloud_metadata_is_refused(self):
        with self.assertRaises(safefetch.BlockedURL) as caught:
            safefetch.check_url("http://169.254.169.254/latest/meta-data/")
        self.assertIn("link-local", str(caught.exception))

    def test_private_ranges_are_refused(self):
        for url in ("http://10.0.0.5/", "http://192.168.1.1/",
                    "http://172.16.0.1/", "http://127.0.0.1/",
                    "http://localhost/", "http://[::1]/",
                    "http://0.0.0.0/"):
            with self.assertRaises(safefetch.BlockedURL, msg=url):
                safefetch.check_url(url)

    def test_an_ipv4_address_wrapped_in_ipv6_is_still_loopback(self):
        # ::ffff:127.0.0.1 is 127.0.0.1 wearing a hat.
        with self.assertRaises(safefetch.BlockedURL):
            safefetch.check_url("http://[::ffff:127.0.0.1]/")

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "gopher://evil/", "ftp://host/x",
                    "data:text/html,hi"):
            with self.assertRaises(safefetch.BlockedURL, msg=url):
                safefetch.check_url(url)

    def test_credentials_in_the_url_are_refused(self):
        with self.assertRaises(safefetch.BlockedURL):
            safefetch.check_url("http://user:pass@example.com/")

    def test_non_web_ports_are_refused(self):
        # 6379 is Redis, 9200 Elasticsearch: a probe, not a page.
        for url in ("https://example.com:6379/", "https://example.com:9200/"):
            with self.assertRaises(safefetch.BlockedURL, msg=url):
                safefetch.check_url(url)

    def test_a_redirect_into_the_private_network_is_refused(self):
        # The standard bypass: an allowed public host redirects to loopback.
        # The client must not follow it silently.
        hops = []

        class _Resp:
            def __init__(self, code, location=None):
                self.status_code = code
                self.headers = {"Location": location} if location else {}
                self.text = "ok"

        def fake(url, **kwargs):
            hops.append(url)
            if "example.com" in url:
                return _Resp(302, "http://127.0.0.1:8080/secret")
            return _Resp(200)

        with self.assertRaises(safefetch.BlockedURL):
            safefetch.safe_get("https://example.com/bounce", fake)
        # The public hop is fetched; the internal one never is.
        self.assertEqual(hops, ["https://example.com/bounce"])

    def test_ordinary_public_urls_still_pass(self):
        for url in ("https://example.com/feed",
                    "https://news.google.com/rss/search?q=x",
                    "http://example.com:8080/page"):
            self.assertTrue(safefetch.is_safe(url), url)

    def test_the_escape_hatch_is_opt_in(self):
        os.environ["ALLOW_PRIVATE_FETCH"] = "1"
        try:
            self.assertTrue(safefetch.is_safe("http://127.0.0.1:8080/admin"))
        finally:
            os.environ.pop("ALLOW_PRIVATE_FETCH", None)
        self.assertFalse(safefetch.is_safe("http://127.0.0.1:8080/admin"))


class LoginThrottleTests(unittest.TestCase):
    """The login form must not answer thousands of guesses a second."""

    def setUp(self):
        from app.auth import routes as auth_routes
        auth_routes._attempts.clear()
        self.app = create_app()
        self.app.config["TESTING"] = True

    def test_repeated_failures_lock_the_client_out(self):
        client = self.app.test_client()
        codes = [client.post("/login", data={"password": "guess%d" % i})
                 .status_code for i in range(12)]
        self.assertIn(429, codes, "an attacker was never locked out")
        # Locked well before a dictionary gets anywhere.
        self.assertLessEqual(codes.index(429), 10)

    def test_a_locked_client_is_refused_even_with_the_right_password(self):
        client = self.app.test_client()
        for i in range(10):
            client.post("/login", data={"password": "guess%d" % i})
        resp = client.post("/login",
                           data={"password": os.environ["PROFILER_PASSWORD"]})
        self.assertEqual(resp.status_code, 429)

    def test_the_lockout_is_per_address(self):
        # One attacker must not be able to lock the owner out.
        client = self.app.test_client()
        for i in range(10):
            client.post("/login", data={"password": "guess%d" % i})
        resp = client.post("/login",
                           data={"password": os.environ["PROFILER_PASSWORD"]},
                           environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
        self.assertEqual(resp.status_code, 302)

    def test_a_correct_password_is_answered_immediately(self):
        client = self.app.test_client()
        started = time.time()
        resp = client.post("/login",
                           data={"password": os.environ["PROFILER_PASSWORD"]})
        self.assertEqual(resp.status_code, 302)
        self.assertLess(time.time() - started, 0.4,
                        "a legitimate login should not be delayed")

    def test_success_clears_the_failure_count(self):
        from app.auth import routes as auth_routes
        client = self.app.test_client()
        client.post("/login", data={"password": "wrong"})
        client.post("/login", data={"password": os.environ["PROFILER_PASSWORD"]})
        self.assertEqual(auth_routes._attempts, {},
                         "a typo should not count against you afterwards")

    def test_a_forwarded_header_is_ignored_unless_a_proxy_is_declared(self):
        # Otherwise every request could claim a fresh identity and the
        # throttle would count nothing.
        from app.auth import routes as auth_routes
        os.environ.pop("TRUST_PROXY", None)
        client = self.app.test_client()
        for i in range(10):
            client.post("/login", data={"password": "g%d" % i},
                        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
                        headers={"X-Forwarded-For": "10.0.0.%d" % i})
        resp = client.post("/login", data={"password": "again"},
                           environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
                           headers={"X-Forwarded-For": "10.0.0.250"})
        self.assertEqual(resp.status_code, 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)
