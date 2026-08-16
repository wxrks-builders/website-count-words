"""The frontier rules that keep one page from being counted hundreds of times.

The URL families below are real, taken from a clay.com crawl that came back
with 59,860 URLs and 253.8M words for a site of roughly 11,300 pages.
"""

from app.url_policy import (
    MAX_QUERY_SHAPES_PER_PATH,
    MAX_QUERY_VALUES_PER_PATH,
    ExclusionFilter,
    FrontierDedupeFilter,
    canonical_key,
    pagination_param_count,
    parse_exclusions,
)


class TestCanonicalKey:
    def test_www_trailing_slash_and_scheme_collapse(self):
        keys = {
            canonical_key("https://www.clay.com/about"),
            canonical_key("https://clay.com/about/"),
            canonical_key("http://www.clay.com/about"),
            canonical_key("https://WWW.Clay.com/about"),
        }
        assert len(keys) == 1

    def test_tracking_params_dropped(self):
        # The two app.clay.com/signup rows in the export differed by nothing else.
        a = "https://app.clay.com/signup?source=website&su=5c65512d-c520"
        b = "https://app.clay.com/signup?source=nav&su=5c65512d-c520"
        assert canonical_key(a) == canonical_key(b) == "https://app.clay.com/signup"

    def test_utm_family_dropped_by_prefix(self):
        assert canonical_key("https://clay.com/x?utm_content=a&utm_term=b") == "https://clay.com/x"

    def test_param_order_does_not_matter(self):
        assert canonical_key("https://clay.com/x?a=1&b=2") == canonical_key("https://clay.com/x?b=2&a=1")

    def test_post_auth_destinations_are_dropped(self):
        """?redirect_to= decides where you land after the page, never what the
        page says — 144 signup URLs that differed only by it are one page."""
        a = "https://app.clay.com/signup?redirect_to=https%3A%2F%2Fx.com%2Fa"
        b = "https://app.clay.com/signup?redirect_to=https%3A%2F%2Fx.com%2Fb"
        assert canonical_key(a) == canonical_key(b) == "https://app.clay.com/signup"

    def test_content_bearing_params_survive(self):
        """The rule that must not overreach: these genuinely select different
        content, and sweeping them up would silently lose real pages."""
        for url in (
            "https://www.clay.com/careers?ashby_jid=123",
            "https://www.clay.com/pricing?priceId=abc",
            "https://www.clay.com/x?enrichmentTab=data",
            "https://www.clay.com/x?provider-categories=email",
        ):
            assert "?" in canonical_key(url), url

    def test_pagination_params_are_kept_in_the_key(self):
        # Page 2 of a listing really is different content — it's the frontier
        # rules, not the key, that stop these multiplying.
        assert canonical_key("https://clay.com/blog?page-nrpb=2") != canonical_key("https://clay.com/blog")


class TestPaginationCounting:
    def test_recognizes_every_shape_seen_in_the_wild(self):
        assert pagination_param_count("https://clay.com/g/x") == 0
        assert pagination_param_count("https://clay.com/g/x?page-nrpb=2") == 1
        assert pagination_param_count("https://clay.com/g/x?645ae0eb_page=3") == 1
        assert pagination_param_count("https://clay.com/blog?page=2") == 1
        assert pagination_param_count("https://clay.com/g/x?page-nrpb=2&645ae0eb_page=3") == 2
        assert (
            pagination_param_count(
                "https://www.clay.com/glossary/site-retargeting"
                "?645ae0eb_page=3&e85c56e5_page=2&page-nrpb=2&page-nwzk=2"
            )
            == 4
        )

    def test_does_not_mistake_content_params_for_pagination(self):
        assert pagination_param_count("https://clay.com/x?priceId=abc&ashby_jid=1") == 0


class TestFrontierDedupeFilter:
    def test_single_paginator_admitted_combination_rejected(self):
        """The rule that actually contains the explosion. One paginator must
        still be walked, or the articles behind 'page 2' are never discovered;
        it's the combinations that multiply without bound."""
        f = FrontierDedupeFilter()
        assert f.apply("https://www.clay.com/glossary/x?page-nrpb=2") is True
        assert f.apply("https://www.clay.com/glossary/x?page-nrpb=2&645ae0eb_page=3") is False
        assert f.rejected_pagination == 1

    def test_second_variant_of_a_claimed_key_rejected(self):
        f = FrontierDedupeFilter()
        assert f.apply("https://www.clay.com/about") is True
        assert f.apply("https://clay.com/about/") is False
        assert f.rejected_duplicate == 1

    def test_many_values_of_one_parameter_are_all_kept(self):
        """The regression that cost a real crawl its job listings. clay.com
        identifies each posting as /jobs?ashby_jid=<id> — one shape, 71 values.
        Capping the values threw away 66 real pages."""
        f = FrontierDedupeFilter()
        kept = [f.apply(f"https://www.clay.com/jobs?ashby_jid=id-{i}") for i in range(71)]
        assert all(kept)
        assert f.rejected_shape_cap == 0

    def test_deep_pagination_of_a_single_paginator_survives(self):
        """The same regression seen from the other side: allowing one paginator
        through is pointless if a per-path cap then stops it at page 5, because
        everything linked only from pages 6+ becomes unreachable."""
        f = FrontierDedupeFilter()
        assert all(f.apply(f"https://clay.com/glossary/x?645ae0eb_page={n}") for n in range(2, 24))

    def test_combinations_of_parameters_are_still_capped(self):
        """What the cap is actually for: distinct *shapes*, which is how a
        combinatorial scheme shows up when values are ignored."""
        f = FrontierDedupeFilter()
        for i in range(MAX_QUERY_SHAPES_PER_PATH):
            assert f.apply("https://clay.com/x?" + "&".join(f"f{j}=1" for j in range(i + 1))) is True
        assert f.apply("https://clay.com/x?" + "&".join(f"g{j}=1" for j in range(9))) is False
        assert f.rejected_shape_cap == 1

    def test_a_runaway_value_scheme_is_eventually_stopped(self):
        """Shape-counting is blind to a unique value per link — a session id or
        cache buster would mint URLs forever. The guard is deliberately far
        above any real listing, so it only catches genuine runaway."""
        f = FrontierDedupeFilter()
        results = [f.apply(f"https://clay.com/s?sid=random-{i}") for i in range(MAX_QUERY_VALUES_PER_PATH + 50)]
        assert results[0] is True
        assert results[-1] is False
        assert f.rejected_runaway > 0
        assert MAX_QUERY_VALUES_PER_PATH > 362, "must clear clay.com's largest real listing"

    def test_query_free_urls_are_never_capped(self):
        """The cap is per-path and query-only — it must never stop a site's
        ordinary pages, however many share a prefix."""
        f = FrontierDedupeFilter()
        assert all(f.apply(f"https://clay.com/glossary/term-{i}") for i in range(50))

    def test_rejection_is_stable_under_re_testing(self):
        """BFSDeepCrawlStrategy re-tests a rejected URL from every other page
        that links to it, so a rejection must never flip to an admission —
        otherwise a duplicate slips in per inbound link."""
        f = FrontierDedupeFilter()
        f.apply("https://www.clay.com/about")
        for _ in range(5):
            assert f.apply("https://clay.com/about/") is False
        assert f.apply("https://clay.com/g/x?a_page=1&b_page=2") is False
        assert f.apply("https://clay.com/g/x?a_page=1&b_page=2") is False

    def test_resumed_crawl_reclaims_what_it_already_visited(self):
        """Without the seed, every canonical whose first variant was crawled
        before the pause is unclaimed on resume — one duplicate per page."""
        f = FrontierDedupeFilter(seed_urls=["https://www.clay.com/about"])
        assert f.apply("https://clay.com/about/") is False

    def test_seeded_shape_counts_carry_over(self):
        seeds = ["https://clay.com/x?" + "&".join(f"f{j}=1" for j in range(i + 1))
                 for i in range(MAX_QUERY_SHAPES_PER_PATH)]
        f = FrontierDedupeFilter(seed_urls=seeds)
        assert f.apply("https://clay.com/x?" + "&".join(f"g{j}=1" for j in range(9))) is False

    def test_contains_the_real_explosion(self):
        """End to end over the shape that produced 1,139 URLs for one page."""
        urls = [
            f"https://www.clay.com/glossary/site-retargeting?645ae0eb_page={a}&e85c56e5_page={b}&page-nrpb={c}"
            for a in range(1, 12)
            for b in range(1, 12)
            for c in range(1, 12)
        ]
        f = FrontierDedupeFilter()
        kept = [u for u in urls if f.apply(u)]
        assert len(urls) == 1331
        assert kept == []


class TestExclusions:
    def test_parse(self):
        assert parse_exclusions(" staging, web-staging ,/careers, ") == ["staging", "web-staging", "/careers"]
        assert parse_exclusions(None) == []
        assert parse_exclusions("") == []

    def test_bare_label_matches_hyphenated_subdomain(self):
        f = ExclusionFilter(["staging"])
        assert f.apply("https://web-staging.clay.com/x") is False
        assert f.apply("https://staging.clay.com/x") is False
        assert f.apply("https://www.clay.com/x") is True

    def test_full_hostname_and_its_subdomains(self):
        f = ExclusionFilter(["web-staging.clay.com"])
        assert f.apply("https://web-staging.clay.com/x") is False
        assert f.apply("https://www.web-staging.clay.com/x") is False
        assert f.apply("https://community.clay.com/x") is True

    def test_folder_prefix(self):
        f = ExclusionFilter(["/careers"])
        assert f.apply("https://clay.com/careers") is False
        assert f.apply("https://clay.com/careers/role-1") is False
        # Prefix must stop at a path boundary, not mid-segment.
        assert f.apply("https://clay.com/careers-at-clay") is True
        assert f.apply("https://clay.com/blog") is True
