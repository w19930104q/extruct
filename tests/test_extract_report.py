# mypy: disallow_untyped_defs=False
"""Regression tests for :func:`extruct.extract_report`.

A single broken metadata block must be reported and skipped without discarding
the healthy blocks around it.
"""
import unittest
from unittest.mock import patch

import lxml.html

import extruct
import extruct._extruct as core
from extruct import w3cmicrodata
from extruct.utils import parse_html

MIXED_PAGE = """
<html><body>
  <script type="application/ld+json">
    {"@context": "http://schema.org/", "@type": "Product", "name": "good one"}
  </script>
  <script type="application/ld+json">{this is not valid json</script>
  <script type="application/ld+json">
    [{"@context": "http://schema.org/", "@type": "Offer", "price": "42"}]
  </script>

  <div itemscope itemtype="http://schema.org/Product" id="healthy">
    <span itemprop="name">healthy product</span>
  </div>
  <div itemscope itemtype="http://schema.org/Thing" id="broken">
    <span itemprop="failing-prop">boom</span>
  </div>
  <div itemscope itemtype="http://schema.org/Event" id="healthy-2">
    <span itemprop="name">healthy event</span>
  </div>
</body></html>
"""

ITEMREF_PAGE = """
<html><body>
  <div id="shared">
    <span itemprop="shared-prop">shared value</span>
  </div>
  <div itemscope itemtype="http://schema.org/Product" id="ref-a" itemref="shared">
    <span itemprop="name">product A</span>
  </div>
  <div itemscope itemtype="http://schema.org/Product" id="ref-b" itemref="shared">
    <span itemprop="name">product B</span>
    <div itemscope itemprop="nested" itemtype="http://schema.org/Thing" id="nested">
      <span itemprop="failing-nested">boom</span>
    </div>
  </div>
  <div itemscope itemtype="http://schema.org/Event" id="ref-c">
    <span itemprop="name">event C</span>
  </div>
</body></html>
"""


def _patch_failing_extraction():
    original = w3cmicrodata.LxmlMicrodataExtractor._extract_property_value

    def _fail_on_failing_prop(
        extractor, node, items_seen, base_url, itemids, force=False
    ):
        if node.get("itemprop") in ("failing-prop", "failing-nested"):
            raise RuntimeError("simulated extraction failure")
        # call the unpatched function captured before patching, never the
        # mock attribute (which would recurse into this side effect)
        return original(extractor, node, items_seen, base_url, itemids, force=force)

    return patch.object(
        w3cmicrodata.LxmlMicrodataExtractor,
        "_extract_property_value",
        autospec=True,
        side_effect=_fail_on_failing_prop,
    )


class TestExtractReportJsonLd(unittest.TestCase):
    def test_bad_script_does_not_discard_other_scripts(self):
        report = extruct.extract_report(MIXED_PAGE, syntaxes=["json-ld"])
        self.assertEqual(
            report["data"]["json-ld"],
            [
                {
                    "@context": "http://schema.org/",
                    "@type": "Product",
                    "name": "good one",
                },
                {
                    "@context": "http://schema.org/",
                    "@type": "Offer",
                    "price": "42",
                },
            ],
        )
        self.assertEqual(len(report["issues"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["syntax"], "json-ld")
        self.assertEqual(issue["stage"], "extraction")
        self.assertEqual(issue["xpath"], "/html/body/script[2]")
        self.assertEqual(issue["exception_type"], "JSONDecodeError")
        self.assertTrue(issue["message"])

    def test_bytes_and_parsed_tree_inputs(self):
        from_string = extruct.extract_report(MIXED_PAGE, syntaxes=["json-ld"])
        from_bytes = extruct.extract_report(
            MIXED_PAGE.encode("UTF-8"), syntaxes=["json-ld"]
        )
        tree = parse_html(MIXED_PAGE.encode("UTF-8"), encoding="UTF-8")
        from_tree = extruct.extract_report(tree, syntaxes=["json-ld"])
        self.assertEqual(from_string, from_bytes)
        self.assertEqual(from_string, from_tree)

    def test_passed_tree_is_not_modified(self):
        tree = parse_html(MIXED_PAGE.encode("UTF-8"), encoding="UTF-8")
        before = lxml.html.tostring(tree)
        extruct.extract_report(tree)
        self.assertEqual(lxml.html.tostring(tree), before)

    def test_repeated_calls_are_isolated(self):
        first = extruct.extract_report(MIXED_PAGE)
        second = extruct.extract_report(MIXED_PAGE)
        self.assertEqual(first, second)
        third = extruct.extract_report(
            parse_html(MIXED_PAGE.encode("UTF-8"), encoding="UTF-8")
        )
        self.assertEqual(first, third)


class TestExtractReportMicrodata(unittest.TestCase):
    def test_failed_top_level_entry_is_the_only_loss(self):
        with _patch_failing_extraction():
            report = extruct.extract_report(MIXED_PAGE, syntaxes=["microdata"])
        items = report["data"]["microdata"]
        self.assertEqual(
            [item["properties"]["name"] for item in items],
            ["healthy product", "healthy event"],
        )
        self.assertEqual(len(report["issues"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["syntax"], "microdata")
        self.assertEqual(issue["stage"], "extraction")
        self.assertEqual(issue["xpath"], "/html/body/div[2]")
        self.assertEqual(issue["exception_type"], "RuntimeError")
        self.assertIn("simulated extraction failure", issue["message"])

    def test_mixed_page_report_keeps_both_syntaxes_independent(self):
        with _patch_failing_extraction():
            report = extruct.extract_report(MIXED_PAGE)
        products = [
            item for item in report["data"]["json-ld"] if item["@type"] == "Product"
        ]
        self.assertEqual([item["name"] for item in products], ["good one"])
        self.assertEqual(
            [item["properties"]["name"] for item in report["data"]["microdata"]],
            ["healthy product", "healthy event"],
        )
        self.assertEqual(
            sorted(issue["syntax"] for issue in report["issues"]),
            ["json-ld", "microdata"],
        )
        stages = {(issue["syntax"], issue["stage"]) for issue in report["issues"]}
        self.assertEqual(
            stages, {("json-ld", "extraction"), ("microdata", "extraction")}
        )

    def test_nested_failure_is_reported_once_on_owning_top_level_entry(self):
        with _patch_failing_extraction():
            report = extruct.extract_report(ITEMREF_PAGE, syntaxes=["microdata"])
        # product B (with the failing nested item) is dropped; A and C survive,
        # the nested node never leaks as an orphan top-level entry
        self.assertEqual(
            [item["properties"]["name"] for item in report["data"]["microdata"]],
            ["product A", "event C"],
        )
        self.assertEqual(len(report["issues"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["xpath"], "/html/body/div[3]")
        self.assertEqual(issue["stage"], "extraction")
        self.assertEqual(issue["exception_type"], "RuntimeError")

    def test_shared_itemref_semantics_are_preserved(self):
        report = extruct.extract_report(
            ITEMREF_PAGE, syntaxes=["microdata"], uniform=False
        )
        self.assertEqual(report["issues"], [])
        names = [item["properties"]["name"] for item in report["data"]["microdata"]]
        self.assertEqual(names, ["product A", "product B", "event C"])
        for item in report["data"]["microdata"][:2]:
            self.assertEqual(item["properties"]["shared-prop"], "shared value")

    def test_uniform_failure_drops_only_owning_entry(self):
        original = core.flatten_dict

        def patched(item, schema_context, add_context):
            if item.get("type") == "http://schema.org/Thing":
                raise ValueError("simulated uniform failure")
            return original(item, schema_context, add_context)

        with patch.object(core, "flatten_dict", side_effect=patched):
            report = extruct.extract_report(
                MIXED_PAGE, uniform=True, syntaxes=["microdata"]
            )
        items = report["data"]["microdata"]
        self.assertEqual([item["@type"] for item in items], ["Product", "Event"])
        self.assertEqual(
            [item["@context"] for item in items],
            ["http://schema.org", "http://schema.org"],
        )
        self.assertEqual(len(report["issues"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["syntax"], "microdata")
        self.assertEqual(issue["stage"], "uniform")
        self.assertEqual(issue["xpath"], "/html/body/div[2]")
        self.assertEqual(issue["exception_type"], "ValueError")
        self.assertIn("simulated uniform failure", issue["message"])
        # only microdata was requested, so only its key is present
        self.assertNotIn("json-ld", report["data"])

    def test_uniform_and_raw_outputs_match_when_healthy(self):
        raw = extruct.extract_report(MIXED_PAGE, uniform=False)
        with _patch_failing_extraction():
            report = extruct.extract_report(MIXED_PAGE, uniform=False)
            uniform_report = extruct.extract_report(MIXED_PAGE, uniform=True)
        # the microdata extraction failure is reported identically at both
        # uniform settings, and only once (the nested failure stays merged in)
        micro_issues = [i for i in report["issues"] if i["syntax"] == "microdata"]
        micro_uniform_issues = [
            i for i in uniform_report["issues"] if i["syntax"] == "microdata"
        ]
        self.assertEqual([i["stage"] for i in micro_issues], ["extraction"])
        self.assertEqual(
            [i["xpath"] for i in micro_issues],
            [i["xpath"] for i in micro_uniform_issues],
        )
        # raw entries keep the microdata shape, uniform ones the flat shape
        self.assertIn("type", report["data"]["microdata"][0])
        self.assertIn("@type", uniform_report["data"]["microdata"][0])
        self.assertEqual(raw["data"]["json-ld"], report["data"]["json-ld"])


class TestExtractReportCompatibility(unittest.TestCase):
    def test_invalid_syntaxes_are_rejected(self):
        with self.assertRaises(ValueError):
            extruct.extract_report(MIXED_PAGE, syntaxes=["rdfa"])
        with self.assertRaises(ValueError):
            extruct.extract_report(MIXED_PAGE, syntaxes="json-ld")  # type: ignore[arg-type]

    def test_legacy_extract_strict_raises_on_bad_jsonld(self):
        with self.assertRaises(ValueError):
            extruct.extract(MIXED_PAGE, syntaxes=["json-ld"], errors="strict")

    def test_legacy_extract_ignore_and_log_keep_working(self):
        for mode in ("ignore", "log"):
            data = extruct.extract(MIXED_PAGE, syntaxes=["json-ld"], errors=mode)
            # whole syntax output is lost under the legacy all-or-nothing policy
            self.assertNotIn("json-ld", data)

    def test_legacy_extract_healthy_page_unchanged(self):
        page = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Product", "name": "x"}'
            "</script>"
            '<div itemscope itemtype="http://schema.org/Product">'
            '<span itemprop="name">x</span></div>'
            "</body></html>"
        )
        legacy = extruct.extract(page, syntaxes=["microdata", "json-ld"])
        report = extruct.extract_report(page)
        self.assertEqual(legacy, report["data"])
        self.assertEqual(report["issues"], [])

    def test_report_does_not_mutate_tree_across_calls(self):
        tree = parse_html(ITEMREF_PAGE.encode("UTF-8"), encoding="UTF-8")
        before = lxml.html.tostring(tree)
        with _patch_failing_extraction():
            extruct.extract_report(tree)
            extruct.extract_report(tree, uniform=True)
        self.assertEqual(lxml.html.tostring(tree), before)

    def test_return_html_node_points_at_the_uniform_xpath(self):
        with _patch_failing_extraction():
            report = extruct.extract_report(
                MIXED_PAGE, syntaxes=["microdata"], return_html_node=True
            )
        items = report["data"]["microdata"]
        self.assertEqual(len(items), 2)
        xpaths = [
            item["htmlNode"].getroottree().getpath(item["htmlNode"]) for item in items
        ]
        self.assertEqual(xpaths, ["/html/body/div[1]", "/html/body/div[3]"])
        # the dropped entry is still located by the same root XPath scheme
        self.assertEqual(report["issues"][0]["xpath"], "/html/body/div[2]")
