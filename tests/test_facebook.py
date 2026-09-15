"""Tests for Facebook post and comment collection.

Everything here is offline: `facebook._get` is replaced with a stub returning
fixture markup, so the parsers, the paging logic and the comment scoring are
exercised without touching the network. That matters because the real thing is
login-walled and rate-limited -- a test that needed it would be a test nobody
could run.

Run with:  python -m pytest tests/test_facebook.py -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PROFILER_DATABASE_URI"] = "sqlite:///:memory:"

from app.monitor import collectors, engine, facebook as fb, graphbuild  # noqa: E402
from app.monitor import keywords  # noqa: E402


TIMELINE = """
<html><body><div id="objects_container">
<div data-ft='{"top_level_post_id":"1234567890"}'>
  <h3><a href="/NAMFREL?refid=17">NAMFREL</a></h3>
  <div><span>Volunteers are reminded that canvassing for the BARMM
  parliamentary election begins at 8am. Report irregularities to the hotline.</span></div>
  <abbr>2 hrs</abbr>
  <footer><a href="/story.php?story_fbid=1234567890&amp;id=999&amp;refid=52">Full Story</a>
  <span>128 people reacted</span><span>45 comments</span><span>12 shares</span></footer>
</div>
<div data-ft='{"top_level_post_id":"222"}'>
  <h3><a href="/NAMFREL">NAMFREL</a></h3>
  <div>Statement on the circulating fake BARMM advisory bearing our letterhead.</div>
  <abbr>14 September at 09:12</abbr>
  <footer><a href="/permalink.php?story_fbid=222&amp;id=999">Full Story</a></footer>
</div>
</div></body></html>
"""

COMMENTS_P1 = """
<html><body>
 <div id="98765432101"><h3><a href="/juan.delacruz.9">Juan Dela Cruz</a></h3>
   <div>This BARMM advisory is fake, the real one is on the official site.</div>
   <abbr>1 hr</abbr><a href="/like.php?id=1">Like</a></div>
 <div id="98765432102"><h3><a href="/free.load.ph">Free Load PH</a></h3>
   <div>URGENT!! Claim your free 500 load now at bit.ly/xyzclaim limited slots!!</div>
   <abbr>30 mins</abbr></div>
 <a href="/story.php?story_fbid=222&amp;p=10">View more comments</a>
</body></html>
"""

COMMENTS_P2 = """
<html><body>
 <div id="98765432103"><h3><a href="/third.voice">Third Voice</a></h3>
   <div>We know where the BARMM canvassers live. They will pay for this.</div>
   <abbr>10 mins</abbr></div>
</body></html>
"""

LOGIN_WALL = '<html><body><form action="/login.php">' \
             '<input type="password" name="pass"></form></body></html>'


class _Resp:
    def __init__(self, text, url, status=200):
        self.text, self.url, self.status_code = text, url, status


def _stub(mapping, default=""):
    """A `_get` replacement that answers from a URL-substring mapping."""
    def get(url, session=None, timeout=20):
        for fragment, body in mapping.items():
            if fragment in url:
                return _Resp(body, url)
        return _Resp(default, url, 200 if default else 404)
    return get


class PageNameTests(unittest.TestCase):
    """Whatever the analyst pastes has to resolve to a Page, or to nothing."""

    def test_accepts_the_shapes_people_actually_paste(self):
        for value, expected in [
            ("NAMFREL", "NAMFREL"),
            ("@NAMFREL", "NAMFREL"),
            ("https://www.facebook.com/NAMFREL", "NAMFREL"),
            ("https://m.facebook.com/pg/NAMFREL/posts/", "NAMFREL"),
            ("https://mbasic.facebook.com/NAMFREL?ref=x", "NAMFREL"),
        ]:
            self.assertEqual(fb.page_name(value), expected, value)

    def test_numeric_profiles_keep_their_id(self):
        self.assertEqual(fb.page_name("https://www.facebook.com/profile.php?id=100064"),
                         "profile.php?id=100064")

    def test_rejects_plumbing_and_junk(self):
        # A reserved path is not a Page; fetching it would return the login
        # page and look like a scraping failure rather than bad input.
        for value in ["", "   ", "https://www.facebook.com/search/posts?q=x",
                      "https://www.facebook.com/", "not a page!!"]:
            self.assertEqual(fb.page_name(value), "", repr(value))


class TimestampTests(unittest.TestCase):
    """Without timestamps every post sorts as 'now' and the date filter lies."""

    NOW = datetime(2026, 9, 15, 12, 0, 0)

    def test_relative_forms(self):
        self.assertEqual(fb._parse_stamp("2 hrs", self.NOW), "2026-09-15T10:00:00")
        self.assertEqual(fb._parse_stamp("30 mins", self.NOW), "2026-09-15T11:30:00")
        self.assertEqual(fb._parse_stamp("3 d", self.NOW), "2026-09-12T12:00:00")
        self.assertEqual(fb._parse_stamp("Yesterday", self.NOW), "2026-09-14T12:00:00")
        self.assertEqual(fb._parse_stamp("Just now", self.NOW), "2026-09-15T12:00:00")

    def test_absolute_forms(self):
        self.assertEqual(fb._parse_stamp("14 September at 09:12", self.NOW),
                         "2026-09-14T09:12:00")
        self.assertEqual(fb._parse_stamp("2026-09-14 08:00", self.NOW),
                         "2026-09-14T08:00:00")

    def test_a_year_less_date_in_the_future_belongs_to_last_year(self):
        # "31 December" read on 15 September is last year's, not in 3 months.
        self.assertEqual(fb._parse_stamp("December 31 at 23:00", self.NOW),
                         "2025-12-31T23:00:00")

    def test_nonsense_is_none_not_now(self):
        self.assertIsNone(fb._parse_stamp("not a date"))
        self.assertIsNone(fb._parse_stamp(""))


class TimelineTests(unittest.TestCase):

    def test_extracts_posts_with_permalinks_and_engagement(self):
        posts = fb.parse_timeline(TIMELINE, page="NAMFREL", limit=10)
        self.assertEqual(len(posts), 2)

        first = posts[0]
        self.assertEqual(first["author"], "NAMFREL")
        self.assertIn("canvassing for the BARMM", first["text"])
        self.assertEqual(first["kind"], "post")
        self.assertEqual(first["post_ref"], "1234567890")
        self.assertEqual(first["engagement"],
                         {"reactions": 128, "comments": 45, "shares": 12})

    def test_tracking_parameters_are_stripped_from_permalinks(self):
        # `refid` changes per fetch; left in, the same post dedupes as new
        # every single run.
        first = fb.parse_timeline(TIMELINE, page="NAMFREL")[0]
        self.assertNotIn("refid", first["url"])
        self.assertIn("story_fbid=1234567890", first["url"])

    def test_interface_chrome_is_not_treated_as_post_text(self):
        for post in fb.parse_timeline(TIMELINE, page="NAMFREL"):
            self.assertNotIn("Full Story", post["text"])
            self.assertNotIn("people reacted", post["text"])


class RegexFallbackTests(unittest.TestCase):
    """Without BeautifulSoup the parser must degrade, not break.

    bs4 is in requirements.txt, but a trimmed deployment might not have it, and
    losing Facebook collection entirely over one optional import would be a bad
    trade.
    """

    def _without_bs4(self, fn):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **kw):
            if name == "bs4":
                raise ImportError("bs4 unavailable for this test")
            return real(name, *a, **kw)

        builtins.__import__ = fake
        try:
            return fn()
        finally:
            builtins.__import__ = real

    def test_soup_is_none_without_bs4(self):
        self.assertIsNone(self._without_bs4(lambda: fb._soup("<div>x</div>")))

    def test_the_fallback_recovers_text_url_and_counters(self):
        posts = self._without_bs4(
            lambda: fb.parse_timeline(TIMELINE, page="NAMFREL", limit=5))
        self.assertTrue(posts)
        first = posts[0]
        self.assertIn("canvassing for the BARMM", first["text"])
        self.assertIn("story_fbid=1234567890", first["url"])
        self.assertEqual(first["engagement"]["reactions"], 128)

    def test_the_fallback_does_not_leak_markup_into_the_text(self):
        # The split lands mid-attribute, so a naive fallback prefixes every
        # post with the rest of the tag.
        posts = self._without_bs4(
            lambda: fb.parse_timeline(TIMELINE, page="NAMFREL", limit=5))
        for p in posts:
            self.assertNotIn(">", p["text"])
            self.assertNotIn('"', p["text"][:4])
            self.assertNotIn("Full Story", p["text"])
            self.assertNotIn("people reacted", p["text"])

    def test_the_fallback_reads_comments(self):
        rows = self._without_bs4(
            lambda: fb.parse_comments(COMMENTS_P1,
                                      parent_url="https://www.facebook.com/x"))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "comment")


class CommentTests(unittest.TestCase):

    def test_extracts_author_body_and_handle(self):
        rows = fb.parse_comments(COMMENTS_P1,
                                 parent_url="https://www.facebook.com/permalink.php?story_fbid=222",
                                 parent_author="NAMFREL")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["author"], "Juan Dela Cruz")
        self.assertEqual(rows[0]["handle"], "juan.delacruz.9")
        self.assertEqual(rows[0]["kind"], "comment")
        self.assertEqual(rows[0]["parent_author"], "NAMFREL")
        self.assertIn("comment_id=98765432101", rows[0]["url"])
        # The action links are chrome, not what the person wrote.
        self.assertNotIn("Like", rows[0]["text"])

    def test_paging_follows_view_more_and_stops_without_repeating(self):
        original = fb._get
        fb._get = _stub({"p=10": COMMENTS_P2, "permalink.php": COMMENTS_P1})
        try:
            result = fb.collect_comments(
                "https://www.facebook.com/permalink.php?story_fbid=222",
                limit=50, max_pages=5)
        finally:
            fb._get = original

        self.assertTrue(result["ok"])
        refs = [c["comment_ref"] for c in result["posts"]]
        self.assertEqual(len(refs), len(set(refs)), "a comment was collected twice")
        self.assertEqual(len(refs), 3)

    def test_limit_is_respected(self):
        original = fb._get
        fb._get = _stub({"p=10": COMMENTS_P2, "permalink.php": COMMENTS_P1})
        try:
            result = fb.collect_comments(
                "https://www.facebook.com/permalink.php?story_fbid=222", limit=2)
        finally:
            fb._get = original
        self.assertEqual(len(result["posts"]), 2)

    def test_a_non_facebook_url_is_refused_rather_than_fetched(self):
        result = fb.collect_comments("https://example.com/thread")
        self.assertFalse(result["ok"])
        self.assertIn("not a Facebook post", result["note"])


class PageCollectionTests(unittest.TestCase):

    def test_collects_posts_and_optionally_their_comments(self):
        original = fb._get
        fb._get = _stub({"story.php": COMMENTS_P1, "permalink.php": COMMENTS_P1,
                         "NAMFREL": TIMELINE})
        try:
            plain = fb.collect_page("NAMFREL", limit=10)
            withc = fb.collect_page("NAMFREL", limit=10, with_comments=True)
        finally:
            fb._get = original

        self.assertTrue(plain["ok"])
        self.assertTrue(all(p["kind"] == "post" for p in plain["posts"]))

        kinds = [p["kind"] for p in withc["posts"]]
        self.assertIn("comment", kinds)
        # Comments must be extra, never a replacement for the posts.
        self.assertGreaterEqual(kinds.count("post"), plain["posts"].__len__())

    def test_a_login_wall_is_reported_not_silently_empty(self):
        original = fb._get
        fb._get = _stub({"NAMFREL": LOGIN_WALL})
        try:
            result = fb.collect_page("NAMFREL")
        finally:
            fb._get = original
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertIn("login wall", result["note"])

    def test_a_time_budget_returns_partial_results_not_a_timeout(self):
        # On a serverless host the request is killed at a fixed cap. Returning
        # the posts already read -- and saying how many threads were not --
        # beats losing the whole run to a gateway error.
        import time as _time

        story = ('<div data-ft="1"><h3><a href="/PageOne">PageOne</a></h3>'
                 '<div>a post with enough text in it to be collected</div>'
                 '<footer><a href="/story.php?story_fbid=%d">Full Story</a>'
                 '</footer></div>')
        timeline = "<html><body>%s</body></html>" % "".join(
            story % i for i in range(1, 9))
        thread = ('<html><body><div id="111111"><h3><a href="/c.user">C</a>'
                  '</h3><div>a reply here</div></div></body></html>')

        def slow(url, session=None, timeout=20):
            if "story.php" in url:
                _time.sleep(0.4)
                return _Resp(thread, url)
            return _Resp(timeline, url)

        original = fb._get
        fb._get = slow
        try:
            result = fb.collect_page("PageOne", limit=10, with_comments=True,
                                     budget=1)
        finally:
            fb._get = original

        self.assertTrue(result["ok"])
        self.assertIn("unread", result["note"])
        # The posts themselves must all survive the early stop.
        posts = [p for p in result["posts"] if p["kind"] == "post"]
        self.assertEqual(len(posts), 8)

    def test_since_keeps_undated_posts(self):
        # An undated post is not evidence that it is old; dropping it would
        # throw away everything mbasic declined to timestamp.
        posts = [{"posted_at": None}, {"posted_at": "2020-01-01T00:00:00"},
                 {"posted_at": datetime.utcnow().isoformat(timespec="seconds")}]
        kept = fb._apply_since(posts, datetime.utcnow() - timedelta(days=7))
        self.assertEqual(len(kept), 2)


class CommentScoringTests(unittest.TestCase):
    """Comments go through the same engine, with one deliberate difference."""

    def cfg(self):
        watch = {"subject": "NAMFREL", "kw_required": "BARMM",
                 "kw_optional": "", "kw_excluded": "", "kw_match_mode": "any",
                 "keywords": "", "weights": {},
                 "threshold_review": 30, "threshold_high": 60}
        return engine.build_config(watch)

    def test_a_scam_comment_scores_and_is_typed(self):
        cfg = self.cfg()
        result = engine.analyze({
            "author": "Free Load PH", "handle": "free.load.ph",
            "platform": "Facebook", "kind": "comment",
            "text": "URGENT!! Claim your free 500 load now at bit.ly/xyzclaim "
                    "limited slots only!!", "url": ""}, cfg)
        self.assertGreaterEqual(result["score"], 60)
        self.assertEqual(result["verdict"], "bad")
        self.assertIn("Scam", result["types"])

    def test_a_reply_inherits_its_parents_relevance(self):
        # "This is fake, don't share" names nothing the keyword rules look for,
        # yet it is exactly the reply worth reading. A post saying the same
        # thing is still judged on its own words.
        cfg = self.cfg()
        text = "This is fake, do not share it."
        as_comment = engine.analyze(
            {"author": "A", "text": text, "platform": "Facebook",
             "kind": "comment"}, cfg)
        as_post = engine.analyze(
            {"author": "A", "text": text, "platform": "Facebook",
             "kind": "post"}, cfg)
        self.assertTrue(as_comment["relevant"])
        self.assertFalse(as_post["relevant"])

    def test_threats_are_flagged_without_hunter_mode(self):
        # A comment saying "we know where they live" must surface whether or
        # not someone remembered to switch Hunter on. It used to be invisible
        # in a standard watch.
        cfg = self.cfg()
        self.assertFalse(cfg["mode_hunter"])
        result = engine.analyze({
            "author": "Angry", "platform": "Facebook", "kind": "comment",
            "text": "We know where the BARMM canvassers live. "
                    "They will pay for this."}, cfg)
        self.assertIn("Threat", result["types"])
        self.assertNotEqual(result["verdict"], "ok")

    def test_ordinary_election_reporting_is_not_a_threat(self):
        # The counterpart that keeps the rule above honest: crime and election
        # coverage uses violent vocabulary constantly.
        cfg = self.cfg()
        for text in [
            "BARMM poll body says canvassing will finish by Friday",
            "Historic BARMM vote opens under shadow of killings and tampering",
            "The BARMM candidate will pay the filing fee this week",
            "Police arrested a suspect in the BARMM shooting incident",
        ]:
            result = engine.analyze(
                {"author": "Newsroom", "platform": "News site",
                 "kind": "post", "text": text}, cfg)
            self.assertNotIn("Threat", result["types"], text)

    def test_an_excluded_term_still_rejects_a_comment(self):
        watch = {"subject": "NAMFREL", "kw_required": "BARMM",
                 "kw_optional": "", "kw_excluded": "basketball",
                 "kw_match_mode": "any", "keywords": "", "weights": {}}
        cfg = engine.build_config(watch)
        result = engine.analyze(
            {"author": "A", "text": "BARMM basketball league results",
             "platform": "Facebook", "kind": "comment"}, cfg)
        self.assertFalse(result["relevant"])


class CommentGraphTests(unittest.TestCase):
    """The reason to collect comments at all: the shape they make on a map."""

    def records(self):
        return [
            {"id": 1, "platform": "Facebook", "author": "Scam One",
             "handle": "scam.one", "text": "claim free load bit.ly/a",
             "url": "", "score": 80, "verdict": "bad", "types": ["Scam"],
             "kind": "comment", "parent_author": "NAMFREL",
             "parent_url": "https://www.facebook.com/permalink.php?story_fbid=222"},
            {"id": 2, "platform": "Facebook", "author": "Scam Two",
             "handle": "scam.two", "text": "claim free load bit.ly/a",
             "url": "", "score": 75, "verdict": "bad", "types": ["Scam"],
             "kind": "comment", "parent_author": "NAMFREL",
             "parent_url": "https://www.facebook.com/permalink.php?story_fbid=222"},
        ]

    def test_commenters_attach_to_the_thread_author(self):
        graph, stats = graphbuild.build_from_records(
            {"subject": "NAMFREL", "name": "Test"}, self.records(), min_score=30)

        self.assertEqual(stats["comments"], 2)
        labels = {n["label"]: n for n in graph["nodes"]}
        self.assertIn("NAMFREL", labels)

        # Both commenters must point at the one thread node -- that convergence
        # is the pattern the map exists to show.
        thread_id = labels["NAMFREL"]["id"]
        into_thread = [e for e in graph["edges"]
                       if e["to"] == thread_id and e["label"] == "commented on"]
        self.assertEqual(len(into_thread), 2)

    def test_the_subject_is_not_duplicated_as_its_own_thread(self):
        # Watching NAMFREL and reading NAMFREL's page is the common case. Drawn
        # naively that produces two "NAMFREL" nodes with the commenters hanging
        # off the wrong one.
        graph, _ = graphbuild.build_from_records(
            {"subject": "NAMFREL", "name": "Test"}, self.records(), min_score=30)
        labels = [n["label"] for n in graph["nodes"]]
        self.assertEqual(labels.count("NAMFREL"), 1, labels)

    def test_a_third_party_thread_still_gets_its_own_node(self):
        rows = self.records()
        for r in rows:
            r["parent_author"] = "Some Other Page"
        graph, _ = graphbuild.build_from_records(
            {"subject": "NAMFREL", "name": "Test"}, rows, min_score=30)
        labels = [n["label"] for n in graph["nodes"]]
        self.assertIn("Some Other Page", labels)
        self.assertIn("NAMFREL", labels)

    def test_a_shared_domain_becomes_one_shared_node(self):
        graph, _ = graphbuild.build_from_records(
            {"subject": "NAMFREL", "name": "Test"}, self.records(), min_score=30)
        shorteners = [n for n in graph["nodes"] if n["label"] == "bit.ly"]
        self.assertEqual(len(shorteners), 1, "the shared domain was duplicated")


class CollectorWiringTests(unittest.TestCase):
    """The dispatch table is what the UI and the scripts both go through."""

    def test_both_facebook_sources_are_registered(self):
        self.assertIn("facebook", collectors.COLLECTORS)
        self.assertIn("fb_comments", collectors.COLLECTORS)
        keys = {s["key"] for s in collectors.SOURCE_META}
        self.assertIn("fb_comments", keys)

    def test_comments_are_not_counted_against_the_post_limit(self):
        # "25 posts with comments" must not return 5 posts and 20 replies.
        original = fb._get
        fb._get = _stub({"story.php": COMMENTS_P1, "permalink.php": COMMENTS_P1,
                         "NAMFREL": TIMELINE})
        try:
            result = collectors.fetch_facebook(
                "BARMM", limit=2, page="NAMFREL", with_comments=True)
        finally:
            fb._get = original
        posts = [p for p in result["posts"] if p.get("kind") != "comment"]
        self.assertLessEqual(len(posts), 2)
        self.assertTrue(any(p.get("kind") == "comment" for p in result["posts"]))

    def test_comments_source_needs_a_url(self):
        result = collectors.fetch_fb_comments("BARMM", url=None)
        self.assertFalse(result["ok"])
        self.assertIn("URL", result["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
