"""Tests for the Signal Monitor.

Run with:  python -m pytest tests/ -v
       or:  python tests/test_monitor.py

These use an in-memory database and never touch the network, so they are safe
to run anywhere. The network-dependent pieces (collectors, phishing feeds,
geolocation) are exercised separately in test_live.py, which is opt-in.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PROFILER_DATABASE_URI"] = "sqlite:///:memory:"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import MonitorPost, MonitorWatch, Profile  # noqa: E402
from app.monitor import engine, graphbuild, keywords, netintel, scoring  # noqa: E402


class KeywordTests(unittest.TestCase):
    """The relevance rules -- the part that decides what gets collected."""

    def spec(self, **kw):
        base = {"subject": "NAMFREL", "kw_required": "", "kw_optional": "",
                "kw_excluded": "", "kw_match_mode": "all", "keywords": ""}
        base.update(kw)
        return keywords.spec_for(base)

    def test_required_all_mode_needs_every_term(self):
        s = self.spec(kw_required="BARMM\nNAMFREL", kw_match_mode="all")
        self.assertTrue(s.evaluate("NAMFREL observes the BARMM vote")["relevant"])
        self.assertFalse(s.evaluate("NAMFREL observes the vote")["relevant"])

    def test_required_any_mode_needs_one(self):
        s = self.spec(kw_required="BARMM\nNAMFREL", kw_match_mode="any")
        self.assertTrue(s.evaluate("NAMFREL observes the vote")["relevant"])
        self.assertFalse(s.evaluate("A vote happened somewhere")["relevant"])

    def test_optional_alone_is_not_enough(self):
        """The original bug: generic words pulling in unrelated coverage."""
        s = self.spec(kw_required="BARMM", kw_optional="election")
        self.assertFalse(s.evaluate("Election results in Missouri")["relevant"])
        self.assertTrue(s.evaluate("BARMM election results")["relevant"])

    def test_excluded_rejects_outright(self):
        s = self.spec(kw_required="BARMM", kw_excluded="cricket")
        r = s.evaluate("BARMM cricket league final")
        self.assertFalse(r["relevant"])
        self.assertIn("cricket", r["reason"])

    def test_exclusion_beats_required(self):
        s = self.spec(kw_required="BARMM\nelection", kw_excluded="satire",
                      kw_match_mode="any")
        self.assertFalse(s.evaluate("BARMM election satire piece")["relevant"])

    def test_quoted_phrase_matches_as_a_unit(self):
        s = self.spec(kw_required='"media release"')
        self.assertTrue(s.evaluate("Read the media release today")["relevant"])
        self.assertFalse(s.evaluate("Media and release are separate")["relevant"])

    def test_or_group(self):
        s = self.spec(kw_required="comelec|election commission")
        self.assertTrue(s.evaluate("The election commission said")["relevant"])
        self.assertTrue(s.evaluate("COMELEC said")["relevant"])
        self.assertFalse(s.evaluate("Someone else said")["relevant"])

    def test_regex_term(self):
        s = self.spec(kw_required=r"/barmm\s+parl\w*/")
        self.assertTrue(s.evaluate("the BARMM parliament met")["relevant"])
        self.assertFalse(s.evaluate("the BARMM met")["relevant"])

    def test_word_boundaries(self):
        """'poll' must not match 'pollution'."""
        s = self.spec(kw_required="poll")
        self.assertFalse(s.evaluate("air pollution levels")["relevant"])
        self.assertTrue(s.evaluate("the poll closed")["relevant"])

    def test_accents_and_smart_quotes_do_not_break_matching(self):
        s = self.spec(kw_required="comelec")
        self.assertTrue(s.evaluate("COMELEC’s statement")["relevant"])

    def test_generic_required_is_demoted(self):
        """A purely generic anchor cannot scope a watch, so it moves."""
        s = self.spec(kw_required="election\nBARMM")
        self.assertIn("BARMM", [t.raw for t in s.required])
        self.assertNotIn("election", [t.raw for t in s.required])

    def test_query_building(self):
        s = self.spec(kw_required="BARMM\nNAMFREL", kw_optional="election",
                      kw_excluded="cricket", kw_match_mode="any")
        q = s.build_query()
        self.assertIn("BARMM OR NAMFREL", q)
        self.assertIn("-cricket", q)

    def test_query_all_mode_uses_and(self):
        s = self.spec(kw_required="BARMM\nNAMFREL", kw_match_mode="all")
        self.assertIn(" AND ", s.build_query())

    def test_legacy_keywords_still_work(self):
        """Watches created before the structured fields must keep working."""
        s = keywords.spec_for({"subject": "NAMFREL", "keywords": "BARMM, election",
                               "kw_required": "", "kw_optional": "",
                               "kw_excluded": ""})
        self.assertTrue(s.has_anchor)
        self.assertTrue(s.evaluate("BARMM news")["relevant"])
        self.assertFalse(s.evaluate("Election in Kansas")["relevant"])

    def test_rules_hash_changes_with_rules(self):
        a = {"subject": "X", "kw_required": "a", "weights": {}}
        b = {"subject": "X", "kw_required": "b", "weights": {}}
        self.assertNotEqual(keywords.rules_hash(a), keywords.rules_hash(b))
        self.assertEqual(keywords.rules_hash(a), keywords.rules_hash(dict(a)))


class NetIntelTests(unittest.TestCase):
    """Indicator extraction and phishing heuristics (no network)."""

    def test_extracts_urls_and_ips(self):
        """URLs and bare IPs are collected separately, each deduplicated."""
        ind = netintel.extract_indicators(
            "See http://evil.xyz/login and 8.8.4.4 plus bit.ly/abc")
        self.assertEqual(sorted(u["host"] for u in ind["urls"]),
                         ["bit.ly", "evil.xyz"])
        self.assertTrue(any(u["shortened"] for u in ind["urls"]))
        self.assertIn("8.8.4.4", [i["ip"] for i in ind["ips"]])

    def test_url_host_that_is_an_ip_is_also_recorded_as_an_ip(self):
        ind = netintel.extract_indicators("go to http://8.8.4.4/login")
        self.assertEqual([u["host"] for u in ind["urls"]], ["8.8.4.4"])
        self.assertIn("8.8.4.4", [i["ip"] for i in ind["ips"]])

    def test_documentation_range_is_not_routable(self):
        """TEST-NET addresses are reserved, so they carry no real location."""
        self.assertFalse(netintel.classify_ip("203.0.113.9")["routable"])

    def test_private_ip_classified(self):
        self.assertFalse(netintel.classify_ip("192.168.1.1")["routable"])
        self.assertTrue(netintel.classify_ip("8.8.8.8")["routable"])

    def test_version_numbers_are_not_ips(self):
        ind = netintel.extract_indicators("upgraded to version 1.2.3.4beta")
        self.assertEqual(ind["ips"], [])

    def test_credential_path_flagged(self):
        r = netintel.check_phishing("http://barmm-verify.xyz/account/login",
                                    use_feeds=False, do_expand=False)
        kinds = {x["kind"] for x in r["reasons"]}
        self.assertIn("credential", kinds)
        self.assertIn("transport", kinds)
        self.assertNotEqual(r["verdict"], "clean")

    def test_clean_url_stays_clean(self):
        r = netintel.check_phishing("https://www.bbc.com/news",
                                    use_feeds=False, do_expand=False)
        self.assertEqual(r["verdict"], "clean")
        self.assertEqual(r["reasons"], [])

    def test_raw_ip_url_flagged(self):
        r = netintel.check_phishing("http://203.0.113.9/signin",
                                    use_feeds=False, do_expand=False)
        self.assertIn("ip", {x["kind"] for x in r["reasons"]})

    def test_punycode_flagged(self):
        r = netintel.check_phishing("https://xn--comlec-9za.com/verify",
                                    use_feeds=False, do_expand=False)
        self.assertIn("homograph", {x["kind"] for x in r["reasons"]})

    def test_registrable_domain(self):
        self.assertEqual(netintel.registrable("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(netintel.registrable("news.example.com"), "example.com")


class GraphTests(unittest.TestCase):
    def test_merge_deduplicates_entities(self):
        base = {"nodes": [{"id": 1, "label": "ACME", "type": "organization"},
                          {"id": 2, "label": "@bob", "type": "username"}],
                "edges": [{"from": 1, "to": 2, "label": "posts"}]}
        add = {"nodes": [{"id": 9, "label": "@bob", "type": "username"},
                         {"id": 8, "label": "evil.xyz", "type": "website"}],
               "edges": [{"from": 9, "to": 8, "label": "links to"}]}
        merged, stats = graphbuild.merge(__import__("json").dumps(base), add)
        self.assertEqual(stats["nodes_added"], 1)
        self.assertEqual(stats["nodes_total"], 3)

    def test_merge_drops_duplicate_edges(self):
        base = {"nodes": [{"id": 1, "label": "A", "type": "person"},
                          {"id": 2, "label": "B", "type": "person"}],
                "edges": [{"from": 1, "to": 2, "label": "knows"}]}
        add = {"nodes": [{"id": 5, "label": "A", "type": "person"},
                         {"id": 6, "label": "B", "type": "person"}],
               "edges": [{"from": 5, "to": 6, "label": "knows"}]}
        _, stats = graphbuild.merge(__import__("json").dumps(base), add)
        self.assertEqual(stats["edges_added"], 0)

    def test_merge_survives_corrupt_json(self):
        merged, stats = graphbuild.merge("not json at all",
                                         {"nodes": [], "edges": []})
        self.assertEqual(merged["nodes"], [])


class AppTests(unittest.TestCase):
    """Route-level tests against a real app and database."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["authed"] = True

        self.watch = MonitorWatch(name="Test", subject="ACME",
                                  kw_required="ACME", kw_optional="election",
                                  kw_excluded="cricket", kw_match_mode="any")
        db.session.add(self.watch)
        db.session.commit()

        for i in range(30):
            db.session.add(MonitorPost(
                watch_id=self.watch.id, platform="X" if i % 2 else "News site",
                author="user%d" % i, handle="u%d" % i,
                text="ACME statement number %d about the election" % i,
                posted_ts=datetime.utcnow() - timedelta(days=i),
                dedupe_key="k%d" % i))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_pagination_splits_and_does_not_overlap(self):
        p1 = self.client.get("/monitor/%d/results?page=1&per_page=10"
                             % self.watch.id).get_json()
        p2 = self.client.get("/monitor/%d/results?page=2&per_page=10"
                             % self.watch.id).get_json()
        self.assertEqual(p1["pages"], 3)
        self.assertEqual(len(p1["posts"]), 10)
        self.assertFalse({p["id"] for p in p1["posts"]} &
                         {p["id"] for p in p2["posts"]})

    def test_pagination_bounds(self):
        r = self.client.get("/monitor/%d/results?page=999&per_page=10"
                            % self.watch.id).get_json()
        self.assertEqual(r["posts"], [])
        self.assertTrue(r["has_prev"])

    def test_platform_filter(self):
        r = self.client.get("/monitor/%d/results?platform=X&per_page=100"
                            % self.watch.id).get_json()
        self.assertTrue(all(p["platform"] == "X" for p in r["posts"]))

    def test_search_filter(self):
        r = self.client.get("/monitor/%d/results?q=user7&per_page=100"
                            % self.watch.id).get_json()
        self.assertTrue(r["matching"] >= 1)
        self.assertTrue(all("user7" in p["author"] for p in r["posts"]))

    def test_sorts_do_not_error(self):
        for sort in ("risk", "recent", "oldest", "platform", "status", "relevance"):
            r = self.client.get("/monitor/%d/results?sort=%s"
                                % (self.watch.id, sort))
            self.assertEqual(r.status_code, 200, sort)

    def test_scores_are_cached_and_reused(self):
        w = MonitorWatch.query.get(self.watch.id)
        _, first = scoring.ensure_fresh(w)
        _, second = scoring.ensure_fresh(w)
        self.assertEqual(first, 30)   # all scored on the first pass
        self.assertEqual(second, 0)   # nothing rescored on the second

    def test_editing_rules_invalidates_the_cache(self):
        w = MonitorWatch.query.get(self.watch.id)
        scoring.ensure_fresh(w)
        w.kw_required = "DIFFERENT"
        db.session.commit()
        _, n = scoring.ensure_fresh(w)
        self.assertEqual(n, 30)

    def test_counts_match_between_sql_and_engine(self):
        w = MonitorWatch.query.get(self.watch.id)
        sql_counts = scoring.counts_for(w)
        _, results = scoring.refresh_watch(w)
        engine_counts = {"bad": 0, "warn": 0, "ok": 0}
        for a in results.values():
            engine_counts[a["verdict"]] += 1
        self.assertEqual(sql_counts, engine_counts)

    def test_manual_override_changes_counts(self):
        post = MonitorPost.query.filter_by(watch_id=self.watch.id).first()
        r = self.client.patch("/monitor/posts/%d" % post.id,
                              json={"manual_verdict": "bad"})
        self.assertEqual(r.status_code, 200)
        w = MonitorWatch.query.get(self.watch.id)
        self.assertGreaterEqual(scoring.counts_for(w)["bad"], 1)

    def test_off_topic_posts_are_dropped_on_collection(self):
        from app.monitor.routes import _add_posts
        w = MonitorWatch.query.get(self.watch.id)
        stats = _add_posts(w, [
            {"author": "a", "text": "ACME announces results"},
            {"author": "b", "text": "Unrelated news from elsewhere"},
            {"author": "c", "text": "ACME cricket match"},
        ], drop_off_topic=True)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["off_topic"], 2)

    def test_topic_endpoint_previews_unsaved_edits(self):
        r = self.client.get("/monitor/%d/topic?kw_required=FOO&kw_excluded=BAR"
                            % self.watch.id).get_json()
        self.assertEqual(r["required"], ["FOO"])
        self.assertEqual(r["excluded"], ["BAR"])

    def test_dashboard_returns_totals(self):
        r = self.client.get("/monitor/dashboard/data").get_json()
        self.assertEqual(r["post_total"], 30)
        self.assertIn("storage", r)
        self.assertIn("capabilities", r)

    def test_linkmap_requires_something_to_draw(self):
        r = self.client.post("/monitor/%d/to-linkmap" % self.watch.id,
                             json={"min_score": 99})
        self.assertEqual(r.status_code, 400)

    def test_capabilities_endpoint(self):
        r = self.client.get("/monitor/capabilities").get_json()
        self.assertIn("groups", r)
        self.assertGreater(r["counts"]["total"], 0)

    def test_preview_scores_a_scam(self):
        r = self.client.post("/monitor/preview", json={
            "watch_id": self.watch.id,
            "post": {"author": "Fake ACME", "platform": "X",
                     "text": "FREE ACME voucher! Claim now at "
                             "http://acme-claim.xyz/login, send GCash"}}).get_json()
        self.assertGreater(r["score"], 50)
        self.assertIn(r["verdict"], ("bad", "warn"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
