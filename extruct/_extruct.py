from __future__ import annotations

import logging
import warnings
from typing import Any, Callable

from lxml.html import HtmlElement

from extruct.dublincore import DublinCoreExtractor
from extruct.jsonld import JsonLdExtractor
from extruct.microformat import MicroformatExtractor
from extruct.opengraph import OpenGraphExtractor
from extruct.rdfa import RDFaExtractor
from extruct.uniform import (
    _udublincore,
    _umicrodata_microformat,
    _uopengraph,
    flatten_dict,
)
from extruct.utils import parse_html, parse_xmldom_html
from extruct.w3cmicrodata import MicrodataExtractor

logger = logging.getLogger(__name__)
SYNTAXES = ["microdata", "opengraph", "json-ld", "microformat", "rdfa", "dublincore"]
REPORT_SYNTAXES = ["microdata", "json-ld"]


def extract(
    htmlstring_or_tree: str | bytes | HtmlElement,
    base_url: str | None = None,
    encoding: str = "UTF-8",
    syntaxes: list[str] = SYNTAXES,
    errors: str = "strict",
    uniform: bool = False,
    return_html_node: bool = False,
    schema_context: str = "http://schema.org",
    with_og_array: bool = False,
    url: str | None = None,  # deprecated
) -> dict[str, list[dict[str, Any]]]:
    """
    htmlstring: string with valid html document;
    base_url: base url of the html document
    encoding: encoding of the html document
    syntaxes: list of syntaxes to extract, default SYNTAXES
    errors: set to 'log' to log the exceptions, 'ignore' to ignore them
            or 'strict'(default) to raise them
    uniform: if True uniform output format of all syntaxes to a list of dicts.
             Returned dicts structure:
             {'@context': 'http://example.com',
              '@type': 'example_type',
              /* All other the properties in keys here */
              }
    return_html_node: if True, it includes into the result a HTML node of
                      respective embedded metadata under 'htmlNode' key.
                      The feature is supported only by microdata syntax.
                      Each node is of `lxml.etree.Element` type.
    schema_context: schema's context for current page"""
    if base_url is None and url is not None:
        warnings.warn(
            '"url" argument is deprecated, please use "base_url"',
            DeprecationWarning,
            stacklevel=2,
        )
        base_url = url
    if not (isinstance(syntaxes, list) and all(v in SYNTAXES for v in syntaxes)):
        raise ValueError(
            "syntaxes must be a list with any or all (default) of"
            "these values: {}".format(SYNTAXES)
        )
    if errors not in ["log", "ignore", "strict"]:
        raise ValueError(
            'Invalid error command, valid values are either "log"'
            ', "ignore" or "strict"'
        )
    if isinstance(htmlstring_or_tree, (str, bytes)):
        parser = parse_xmldom_html if "rdfa" in syntaxes else parse_html
        try:
            tree = parser(htmlstring_or_tree, encoding=encoding)
        except Exception as e:
            if errors == "ignore":
                return {}
            if errors == "log":
                logger.exception("Failed to parse html, raises {}".format(e))
                return {}
            if errors == "strict":
                raise
    else:
        if "microformat" in syntaxes:
            raise ValueError(
                "'microformat' syntax requires a string, not a parsed tree. "
                "Consider adjusting the 'syntaxes' argument to exclude it, "
                "or passing an HTML string or bytes."
            )
        tree = htmlstring_or_tree
    processors = []
    if "microdata" in syntaxes:
        processors.append(
            (
                "microdata",
                MicrodataExtractor(add_html_node=return_html_node).extract_items,
                tree,
            )
        )
    if "json-ld" in syntaxes:
        processors.append(
            (
                "json-ld",
                JsonLdExtractor().extract_items,
                tree,
            )
        )
    if "opengraph" in syntaxes:
        processors.append(("opengraph", OpenGraphExtractor().extract_items, tree))
    if "microformat" in syntaxes:
        processors.append(
            ("microformat", MicroformatExtractor().extract_items, htmlstring_or_tree)
        )
    if "rdfa" in syntaxes:
        processors.append(
            (
                "rdfa",
                RDFaExtractor().extract_items,
                tree,
            )
        )
    if "dublincore" in syntaxes:
        processors.append(
            (
                "dublincore",
                DublinCoreExtractor().extract_items,
                tree,
            )
        )
    output: dict[str, list[dict[str, Any]]] = {}
    for syntax, extract, document in processors:
        try:
            output[syntax] = list(extract(document, base_url=base_url))
        except Exception as e:
            if errors == "log":
                logger.exception("Failed to extract {}, raises {}".format(syntax, e))
            if errors == "ignore":
                pass
            if errors == "strict":
                raise
    if uniform:
        uniform_processors: list[
            tuple[str, Callable[..., Any], list[Any], str | None]
        ] = []
        if "microdata" in syntaxes:
            uniform_processors.append(
                (
                    "microdata",
                    _umicrodata_microformat,
                    output["microdata"],
                    schema_context,
                )
            )
        if "microformat" in syntaxes:
            uniform_processors.append(
                (
                    "microformat",
                    _umicrodata_microformat,
                    output["microformat"],
                    "http://microformats.org/wiki/",
                )
            )
        if "opengraph" in syntaxes:
            uniform_processors.append(
                (
                    "opengraph",
                    _uopengraph,
                    output["opengraph"],
                    None,
                )
            )
        if "dublincore" in syntaxes:
            uniform_processors.append(
                (
                    "dublincore",
                    _udublincore,
                    output["dublincore"],
                    None,
                )
            )

        for syntax, uniform_fn, raw, schema_ctx in uniform_processors:
            try:
                if syntax == "opengraph":
                    output[syntax] = uniform_fn(raw, with_og_array=with_og_array)
                elif syntax == "dublincore":
                    output[syntax] = uniform_fn(raw)
                else:
                    output[syntax] = uniform_fn(raw, schema_ctx)
            except Exception as e:
                if errors == "ignore":
                    output[syntax] = []
                if errors == "log":
                    output[syntax] = []
                    logger.exception(
                        "Failed to uniform extracted for {}, raises {}".format(
                            syntax, e
                        )
                    )
                if errors == "strict":
                    raise

    return output


def extract_report(
    htmlstring_or_tree: str | bytes | HtmlElement,
    base_url: str | None = None,
    encoding: str = "UTF-8",
    syntaxes: list[str] = REPORT_SYNTAXES,
    uniform: bool = False,
    return_html_node: bool = False,
    schema_context: str = "http://schema.org",
) -> dict[str, Any]:
    """Extract metadata without letting one broken block discard the rest.

    Unlike :func:`extract`, a single malformed metadata unit never fails the
    whole syntax: the unit is skipped and described in ``issues`` while every
    healthy unit is still returned in ``data``. Only JSON-LD and microdata are
    supported.

    :param htmlstring_or_tree: an HTML string, bytes or a parsed lxml tree;
        a passed tree is never modified.
    :param syntaxes: any subset of ``["microdata", "json-ld"]``.
    :param uniform: uniform the microdata output, isolating uniform failures
        to the top-level entry that causes them.
    :returns: ``{"data": {syntax: [items]}, "issues": [issue, ...]}``. Each
        issue holds the ``syntax``, the failed ``stage`` (``"extraction"`` or
        ``"uniform"``), the root XPath of the failing unit, the exception type
        and its message. Nested failures are reported once, on the top-level
        unit that owns them.
    """
    if not (isinstance(syntaxes, list) and all(v in REPORT_SYNTAXES for v in syntaxes)):
        raise ValueError(
            "syntaxes must be a list with any or all (default) of"
            "these values: {}".format(REPORT_SYNTAXES)
        )
    if isinstance(htmlstring_or_tree, (str, bytes)):
        tree = parse_html(htmlstring_or_tree, encoding=encoding)
    else:
        tree = htmlstring_or_tree

    data: dict[str, list[Any]] = {}
    issues: list[dict[str, Any]] = []

    if "microdata" in syntaxes:
        microdata, microdata_issues, microdata_nodes = MicrodataExtractor(
            add_html_node=return_html_node
        ).extract_items_report(tree, base_url)
        issues.extend(microdata_issues)
        if uniform:
            microdata, uniform_issues = _uniform_microdata_report(
                microdata, microdata_nodes, schema_context
            )
            issues.extend(uniform_issues)
        data["microdata"] = microdata

    if "json-ld" in syntaxes:
        jsonld_items, jsonld_issues = JsonLdExtractor().extract_items_report(
            tree, base_url=base_url
        )
        data["json-ld"] = jsonld_items
        issues.extend(jsonld_issues)

    return {"data": data, "issues": issues}


def _uniform_microdata_report(
    items: list[dict[str, Any]],
    nodes: list[HtmlElement],
    schema_context: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    uniform_items: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for item, node in zip(items, nodes):
        try:
            uniform_items.append(flatten_dict(item, schema_context, True))
        except Exception as e:
            issues.append(_build_issue("microdata", "uniform", node, e))
    return uniform_items, issues


def _build_issue(
    syntax: str, stage: str, node: HtmlElement, exception: Exception
) -> dict[str, Any]:
    return {
        "syntax": syntax,
        "stage": stage,
        "xpath": node.getroottree().getpath(node),
        "exception_type": type(exception).__name__,
        "message": str(exception),
    }
