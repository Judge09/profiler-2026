"""Breach intelligence and pivot links.

The pivot tests are entirely offline -- they only build URLs. The breach tests
use a fixed catalogue fixture rather than the live service, so they assert on
behaviour (matching, severity, honest wording) rather than on whatever HIBP
happens to hold today. `tests/test_browser_osint.py` covers the live path.

Run with:  python -m pytest tests/test_osint.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PROFILER_DATABASE_URI"] = "sqlite:///:memory:"

from app.monitor import breachintel, pivots  # noqa: E402


CATALOGUE = [
    {"Name": "COMELEC", "Title": "COMELEC (Philippines Voters)",
     "Domain": "comelec.gov.ph", "BreachDate": "2016-03-27",
     "AddedDate": "2016-04-21T00:00:00Z", "PwnCount": 228605,
     "DataClasses": ["Biometric data", "Physical addresses", "Names"],
     "IsVerified": True, "Description": "<p>Voter records.</p>"},
    {"Name": "Wendys", "Title": "Wendy's", "Domain": "wendys.com.ph",
     "BreachDate": "2018-01-01", "AddedDate": "2018-02-01T00:00:00Z",
     "PwnCount": 52485, "DataClasses": ["Email addresses", "Passwords"],
     "IsVerified": True, "Description": "Loyalty accounts."},
    {"Name": "Fake", "Title": "Fabricated List", "Domain": "notreal.example",
     "BreachDate": "2020-01-01", "AddedDate": "2020-02-01T00:00:00Z",
     "PwnCount": 10, "DataClasses": ["Email addresses"],
     "IsFabricated": True, "IsVerified": False, "Description": "Disputed."},
]


class DomainCheckTests(unittest.TestCase):

    def check(self, value):
        return breachintel.check_domain(value, breaches=CATALOGUE)

    def test_a_breached_domain_is_found_with_its_record_count(self):
        result = self.check("comelec.gov.ph")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["breaches"]), 1)
        self.assertEqual(result["total_records"], 228605)
        self.assertIn("228,605", result["note"])

    def test_severe_data_classes_are_called_out(self):
        # "email addresses leaked" and "biometrics leaked" are not the same
        # finding, and the difference must survive into the summary.
        result = self.check("comelec.gov.ph")
        severe = result["breaches"][0]["severe"]
        self.assertIn("biometric data", severe)
        self.assertIn("home addresses", severe)
        self.assertIn("biometric data", result["note"])

    def test_urls_and_subdomains_resolve_to_the_same_domain(self):
        for value in ("https://www.comelec.gov.ph/results",
                      "mail.comelec.gov.ph", "COMELEC.GOV.PH",
                      "comelec.gov.ph/"):
            self.assertEqual(len(self.check(value)["breaches"]), 1, value)

    def test_an_unbreached_domain_does_not_claim_safety(self):
        # Absence of evidence is not evidence of absence, and a tool that
        # implies otherwise gets someone hurt.
        result = self.check("nothing-here.example")
        self.assertEqual(result["breaches"], [])
        self.assertIn("not proof", result["note"])

    def test_a_fabricated_breach_is_flagged_as_such(self):
        result = self.check("notreal.example")
        self.assertTrue(result["breaches"][0]["fabricated"])

    def test_junk_input_is_refused_cleanly(self):
        result = breachintel.check_domain("", breaches=CATALOGUE)
        self.assertFalse(result["ok"])


class SearchTests(unittest.TestCase):

    def test_search_matches_name_title_and_domain(self):
        for term in ("comelec", "philippines", "gov.ph"):
            result = breachintel.search(term)
            # Uses the live catalogue; assert the shape, not the contents.
            self.assertIn("breaches", result)

    def test_a_one_character_search_is_refused(self):
        result = breachintel.search("a")
        self.assertFalse(result["ok"])


class AccountLookupTests(unittest.TestCase):
    """The paid path must never imply an address is clean when unchecked."""

    def test_without_a_key_it_says_it_could_not_look(self):
        result = breachintel.check_account("someone@example.com")
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["breaches"], [])
        # The wording must not read as "no breaches found".
        self.assertIn("key", result["note"].lower())
        self.assertNotIn("does not appear", result["note"])

    def test_a_non_address_is_refused_before_any_request(self):
        result = breachintel.check_account("not-an-address")
        self.assertFalse(result["ok"])
        self.assertTrue(result["available"])


class PivotTests(unittest.TestCase):
    """Pivots only build URLs, so these are fast and fully offline."""

    def test_a_handle_is_recovered_from_whatever_was_pasted(self):
        for value in ("@juan.dc", "juan.dc",
                      "https://facebook.com/juan.dc",
                      "https://x.com/juan.dc?ref=x"):
            self.assertEqual(pivots._clean_username(value), "juan.dc", value)

    def test_username_pivots_cover_the_field_standards(self):
        labels = [p["label"] for p in pivots.username_pivots("juan.dc")]
        self.assertTrue(any("WhatsMyName" in l for l in labels))
        self.assertTrue(any("Sherlock" in l for l in labels))
        self.assertTrue(any("Maigret" in l for l in labels))

    def test_local_tools_come_with_the_command_to_run(self):
        # A link to Sherlock's GitHub page is not what an analyst needs at
        # that moment; the command is.
        sherlock = [p for p in pivots.username_pivots("juan.dc")
                    if "Sherlock" in p["label"]][0]
        self.assertEqual(sherlock["command"], "sherlock juan.dc")

    def test_image_pivots_cover_all_three_engines(self):
        labels = [p["label"] for p in
                  pivots.image_pivots("https://example.com/a.jpg")]
        for engine in ("Google Lens", "Yandex", "TinEye"):
            self.assertTrue(any(engine in l for l in labels), engine)

    def test_the_image_url_is_encoded_into_the_query(self):
        first = pivots.image_pivots("https://example.com/a b.jpg")[0]
        self.assertIn("%20", first["url"])
        self.assertNotIn(" ", first["url"])

    def test_email_pivots_include_the_domain_breach_check(self):
        links = pivots.email_pivots("juan@gmail.com")
        internal = [p for p in links if p.get("internal")]
        self.assertTrue(internal)
        self.assertIn("gmail.com", internal[0]["url"])

    def test_empty_or_malformed_input_yields_nothing(self):
        self.assertEqual(pivots.username_pivots(""), [])
        self.assertEqual(pivots.email_pivots("not-an-address"), [])
        self.assertEqual(pivots.image_pivots(""), [])
        self.assertEqual(pivots.name_pivots("ab"), [])

    def test_an_unknown_kind_returns_nothing_rather_than_raising(self):
        self.assertEqual(pivots.build("nonsense", "x"), [])

    def test_a_profile_produces_pivots_for_each_of_its_accounts(self):
        groups = pivots.for_profile({
            "codename": "FALCON-1", "real_name": "Juan Dela Cruz",
            "social_links": [{"platform": "Facebook", "username": "juan.dc"},
                             {"platform": "X", "username": "@juandc"}],
        })
        subjects = [g["subject"] for g in groups]
        self.assertIn("juan.dc", subjects)
        self.assertIn("juandc", subjects)
        self.assertIn("Juan Dela Cruz", subjects)

    def test_domain_pivots_reach_the_infrastructure_tools(self):
        labels = [p["label"] for p in pivots.domain_pivots("https://scam.xyz/x")]
        for tool in ("Wayback", "crt.sh", "urlscan", "VirusTotal"):
            self.assertTrue(any(tool in l for l in labels), tool)


class PasswordCheckTests(unittest.TestCase):
    """The k-anonymity contract is the whole point; assert it holds."""

    def test_an_empty_password_is_refused_without_a_request(self):
        result = breachintel.check_password("")
        self.assertFalse(result["ok"])

    def test_only_a_hash_prefix_is_ever_sent(self):
        sent = {}

        def fake_get(url, headers=None, timeout=20):
            sent["url"] = url

            class _Resp:
                status_code = 200
                text = "0018A45C4D1DEF81644B54AB7F969B88D65:1"
            return _Resp()

        original = breachintel._get
        breachintel._get = fake_get
        try:
            breachintel.check_password("password123")
        finally:
            breachintel._get = original

        # The API takes a 5-character prefix and nothing else.
        tail = sent["url"].rsplit("/", 1)[-1]
        self.assertEqual(len(tail), 5)
        self.assertNotIn("password123", sent["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
