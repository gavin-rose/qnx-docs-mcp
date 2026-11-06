"""QNX documentation MCP server.

Searches the public QNX docs (qnx.com/developers/docs) using the Oxygen WebHelp
search index those sites already publish, and fetches individual topics on
demand, converted to Markdown. Nothing is mirrored: the search index is cached
locally for speed, and pages are fetched live when a tool asks for them.

Run:  uv run server.py
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from bisect import bisect_left
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from mcp.server.mcpserver import MCPServer

BASE = "https://www.qnx.com/developers/docs/"
DOCSETS = {
    "8.0": "QNX SDP 8.0 (OS, Screen, libraries, utilities, IDE, DDK, ...)",
    "qnxeverywhere": (
        "QNX Everywhere (free non-commercial QNX 8.0, Raspberry Pi images, porting guide)"
    ),
    "relnotes8.0": "QNX SDP 8.0 release notes",
    "BSP8.0": "QNX SDP 8.0 board support packages",
    "7.1": "QNX SDP 7.1",
}
DEFAULT_DOCSET = "8.0"
INDEX_TTL = 7 * 24 * 3600
PAGE_CACHE_SIZE = 64
DESCRIPTION_LIMIT = 1200
USER_AGENT = "qnx-docs-mcp/0.1 (personal documentation lookup)"
CACHE_DIR = Path(os.environ.get("QNX_DOCS_CACHE", Path.home() / ".cache" / "qnx-docs-mcp"))

mcp = MCPServer(
    "qnx-docs",
    instructions="Search and read the official QNX documentation (qnx.com/developers/docs). "
    "Search first, then read the most relevant topics before answering QNX questions.",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
_client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)


class DocIndex:
    """Oxygen WebHelp search index for one docset."""

    def __init__(self, docset: str):
        self.docset = docset
        self.topics: list[tuple[str, str, str]] = []
        self.words: dict[str, str] = {}
        self.stopwords: set[str] = set()
        self._sorted_words: list[str] = []

    @staticmethod
    def _parse_js(text: str):
        start = text.index("=") + 1
        return json.loads(text[start:].strip().rstrip(";"))

    def _fetch(self, name: str) -> str:
        cache = CACHE_DIR / self.docset / f"{name}.js"
        if cache.exists() and time.time() - cache.stat().st_mtime < INDEX_TTL:
            return cache.read_text(encoding="utf-8")
        url = f"{BASE}{self.docset}/oxygen-webhelp/app/search/index/{name}.js"
        try:
            r = _client.get(url)
            r.raise_for_status()
        except httpx.HTTPError:
            if cache.exists():
                return cache.read_text(encoding="utf-8")
            raise
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(r.text, encoding="utf-8")
        return r.text

    def load(self) -> DocIndex:
        """Download (or read from cache) the topic list and word index."""
        for entry in self._parse_js(self._fetch("htmlFileInfoList")):
            path, title, summary = (entry.split("@@@") + ["", ""])[:3]
            self.topics.append((path, html.unescape(title), html.unescape(summary)))
        for name in ("index-1", "index-2", "index-3"):
            self.words.update(self._parse_js(self._fetch(name)))
        self.stopwords = set(self._parse_js(self._fetch("stopwords")))
        self._sorted_words = sorted(self.words)
        return self

    def _postings(self, word: str) -> dict[int, tuple[int, bool]]:
        """Map topic index to (weight, hit in title) for one indexed word."""
        out: dict[int, tuple[int, bool]] = {}
        raw = self.words.get(word)
        if not raw:
            return out
        for item in raw.split(","):
            parts = item.split("*")
            if len(parts) < 2:
                continue
            try:
                idx, weight = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            in_title = len(parts) > 2 and "-1" in parts[2].split("$")
            out[idx] = (weight, in_title)
        return out

    def _expand(self, term: str, limit: int = 30) -> list[str]:
        """Exact word, or prefix matches if the term ends with '*' or has no exact hit."""
        prefix = term.rstrip("*")
        if not term.endswith("*") and term in self.words:
            return [term]
        i = bisect_left(self._sorted_words, prefix)
        found = []
        while i < len(self._sorted_words) and self._sorted_words[i].startswith(prefix):
            found.append(self._sorted_words[i])
            i += 1
            if len(found) >= limit:
                break
        return found

    def _score_terms(self, terms: list[str]) -> tuple[dict[int, float], dict[int, int]]:
        """Return (score per topic, number of query terms matched per topic)."""
        scores: dict[int, float] = {}
        matched: dict[int, int] = {}
        for term in terms:
            seen: set[int] = set()
            exact_word = term.rstrip("*")
            for word in self._expand(term):
                factor = 1.0 if word == exact_word else 0.4
                for idx, (weight, in_title) in self._postings(word).items():
                    score = (weight + 1) * factor + (25 if in_title else 0)
                    scores[idx] = scores.get(idx, 0) + score
                    seen.add(idx)
            for idx in seen:
                matched[idx] = matched.get(idx, 0) + 1
        return scores, matched

    def search(self, query: str, limit: int) -> list[tuple[tuple[str, str, str], float]]:
        """Rank topics for a query, returning ((path, title, summary), score) pairs."""
        q = query.lower().strip()
        terms = [t for t in re.findall(r"[^\s,;\"“”]+", q) if t not in self.stopwords]
        terms = [t.removesuffix("()") for t in terms]
        if not terms:
            return []
        scores, matched = self._score_terms(terms)
        bare = q.replace("()", "")
        ranked = []
        for idx, score in scores.items():
            if idx >= len(self.topics):
                continue
            coverage = matched[idx] / len(terms)
            title = self.topics[idx][1].lower().replace("()", "")
            bonus = 200 if title == bare else (60 if bare in title else 0)
            ranked.append((score * coverage**2 + bonus, idx))
        ranked.sort(reverse=True)
        return [(self.topics[i], round(sc, 1)) for sc, i in ranked[:limit]]


_indexes: dict[str, DocIndex] = {}


def get_index(docset: str) -> DocIndex:
    """Return the loaded index for a docset, loading it on first use."""
    if docset not in DOCSETS:
        raise ValueError(f"Unknown docset {docset!r}. Choose one of: {', '.join(DOCSETS)}")
    if docset not in _indexes:
        _indexes[docset] = DocIndex(docset).load()
    return _indexes[docset]


_page_cache: OrderedDict[str, str] = OrderedDict()


def _resolve_url(path_or_url: str, docset: str) -> str:
    s = path_or_url.strip()
    if s.startswith(("http://", "https://")):
        url = s.replace("http://", "https://", 1)
    else:
        url = urljoin(f"{BASE}{docset}/", s.lstrip("/"))
    u = urlparse(url)
    if u.hostname not in ("www.qnx.com", "qnx.com") or not u.path.startswith("/developers/docs/"):
        raise ValueError("Only pages under https://www.qnx.com/developers/docs/ can be read.")
    return url.split("#")[0]


def _to_md(fragment: str) -> str:
    md = markdownify(fragment, heading_style="ATX", code_language="")
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _article_html(url: str) -> str:
    """Fetch a topic and return its cleaned <article> HTML (cached in memory)."""
    if url in _page_cache:
        _page_cache.move_to_end(url)
        return _page_cache[url]
    r = _client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.select_one('article[role="article"]') or soup.select_one("main") or soup.body
    for tag in main.select("script, style, nav, .wh_related_links, .navheader, .navfooter"):
        tag.decompose()
    labels = main.select("span.label")
    if labels:
        line = soup.new_tag("p")
        line.string = "Labels: " + " · ".join(label.get_text(strip=True) for label in labels)
        labels[0].parent.replace_with(line)
    # Absolute links can be followed with read_qnx_doc. Tooltip titles repeat
    # the target's summary inline, so drop them.
    for a in main.find_all("a", href=True):
        a["href"] = urljoin(url, a["href"])
        a.attrs.pop("title", None)
    for img in main.find_all("img", src=True):
        img["src"] = urljoin(url, img["src"])
    cleaned = str(main)
    _page_cache[url] = cleaned
    if len(_page_cache) > PAGE_CACHE_SIZE:
        _page_cache.popitem(last=False)
    return cleaned


def _page_markdown(url: str) -> str:
    return _to_md(_article_html(url))


def _page_sections(url: str) -> tuple[str, dict[str, str]]:
    """Return (page title, {section heading: markdown}) for a reference topic."""
    art = BeautifulSoup(_article_html(url), "html.parser")
    h1 = art.find("h1")
    title = re.sub(r"\s+", " ", h1.get_text()).replace(" ,", ",").strip() if h1 else ""
    sections: dict[str, str] = {}
    for sec in art.find_all("section"):
        head = sec.find(["h2", "h3"])
        if not head:
            continue
        name = head.get_text(" ", strip=True).rstrip(":").strip()
        head.decompose()
        if name and name not in sections:
            sections[name] = _to_md(str(sec))
    return title, sections


def _title_names(title: str) -> list[str]:
    """Split a title like 'MsgSend(), MsgSend_r()' into ['msgsend', 'msgsend_r']."""
    return [n.strip().removesuffix("()").strip().lower() for n in title.split(",") if n.strip()]


@mcp.tool()
def list_qnx_docsets() -> str:
    """List the QNX documentation sets this server can search."""
    return "\n".join(
        f"- {k}{' (default)' if k == DEFAULT_DOCSET else ''}: {v}  ({BASE}{k}/)"
        for k, v in DOCSETS.items()
    )


@mcp.tool()
def search_qnx_docs(query: str, docset: str = DEFAULT_DOCSET, limit: int = 10) -> str:
    """Search the official QNX documentation (uses the docs site's own search index).

    Good queries are API names (screen_create_window, MsgSend, pthread_create),
    utilities (pidin, slog2info, mkifs), or a few keywords (resource manager
    io_read, buildfile startup script). Words match exactly (no stemming);
    end a word with * for a prefix match (e.g. "slog2*").

    Args:
        query: words to search for.
        docset: one of 8.0 (default), qnxeverywhere, relnotes8.0, BSP8.0, 7.1.
        limit: max results (1-50).
    """
    idx = get_index(docset)
    results = idx.search(query, max(1, min(limit, 50)))
    if not results:
        return (
            f"No results for {query!r} in docset {docset}. "
            "Try fewer or different words, or a * prefix."
        )
    lines = [f"Results for {query!r} in QNX docs {docset}:"]
    for n, ((path, title, summary), _) in enumerate(results, 1):
        lines.append(f"\n{n}. {title}\n   path: {path}\n   url: {BASE}{docset}/{path}")
        if summary:
            lines.append(f"   {summary[:300]}")
    lines.append("\nUse read_qnx_doc with a path or url to read a topic.")
    return "\n".join(lines)


@mcp.tool()
def read_qnx_doc(
    path_or_url: str, docset: str = DEFAULT_DOCSET, offset: int = 0, max_chars: int = 20000
) -> str:
    """Fetch one QNX documentation topic and return it as Markdown.

    Args:
        path_or_url: a path from search_qnx_docs (e.g.
            com.qnx.doc.screen/topic/manual/cscreen_about.html) or a full
            https://www.qnx.com/developers/docs/... URL (e.g. a link from a page).
        docset: docset the path is relative to (ignored for full URLs).
        offset: character offset, to continue reading a long page.
        max_chars: max characters to return.
    """
    url = _resolve_url(path_or_url, docset)
    md = _page_markdown(url)
    chunk = md[offset : offset + max_chars]
    end = offset + len(chunk)
    if end < len(md):
        chunk += f"\n\n[... truncated: {len(md) - end} more chars; call again with offset={end}]"
    return f"Source: {url}\n\n{chunk}"


_FUNC_SECTIONS = ["Synopsis", "Arguments", "Library", "Returns", "Errors", "Classification"]


def _reference_paths(idx: DocIndex, name: str) -> list[str]:
    """Paths of topics titled with this function name, main library reference first."""
    paths = [path for path, title, _ in idx.topics if name in _title_names(title)]
    paths.sort(key=lambda p: ("lib_ref" not in p, p))
    return paths


def _format_reference(url: str, also: list[str], full_description: bool) -> str:
    title, sections = _page_sections(url)
    out = [f"# {title}", f"Source: {url}"]
    if also:
        out.append("Also documented in: " + ", ".join(also[:5]))
    for sec in _FUNC_SECTIONS:
        if sec in sections:
            out.append(f"\n## {sec}\n{sections[sec]}")
    desc = sections.get("Description")
    if desc:
        if not full_description and len(desc) > DESCRIPTION_LIMIT:
            desc = (
                desc[:DESCRIPTION_LIMIT].rsplit("\n", 1)[0]
                + "\n\n[... description shortened; pass full_description=true or use read_qnx_doc]"
            )
        out.append(f"\n## Description\n{desc}")
    others = [s for s in sections if s not in _FUNC_SECTIONS and s != "Description"]
    if others:
        out.append("\nOther sections on the page: " + ", ".join(others))
    if not any(sec in sections for sec in _FUNC_SECTIONS) and not desc:
        out.append("\n(This page isn't laid out as an API reference entry; here it is in full.)\n")
        out.append(_page_markdown(url)[:8000])
    return "\n".join(out)


@mcp.tool()
def lookup_qnx_function(
    name: str, docset: str = DEFAULT_DOCSET, full_description: bool = False
) -> str:
    """Look up a QNX C API function (or macro/struct) reference entry.

    Returns the parts you need while coding: synopsis (#includes + prototype),
    arguments, the library to link against, return values, errno values, and
    the classification/safety table (cancellation point, signal/interrupt
    handler safety, thread safety). The description is shortened unless
    full_description is true. Use read_qnx_doc for the whole page.

    Args:
        name: function name, e.g. MsgSend, screen_create_window, pthread_create
            (trailing "()" is fine).
        docset: 8.0 (default) or 7.1, etc.
        full_description: include the complete Description section.
    """
    idx = get_index(docset)
    want = name.strip().removesuffix("()").strip().lower()
    paths = _reference_paths(idx, want)
    if not paths:
        msg = f"No reference entry titled {name!r} in docset {docset}."
        alt = idx.search(want, 5)
        if alt:
            msg += " Closest topics:\n" + "\n".join(f"- {t} ({p})" for (p, t, _), _ in alt)
        return msg
    url = _resolve_url(paths[0], docset)
    return _format_reference(url, paths[1:], full_description)


if __name__ == "__main__":
    mcp.run()
