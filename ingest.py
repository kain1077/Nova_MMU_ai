"""
ingest.py — Phase 11 Knowledge Seeding
======================================
Document loading, chunking, and keyword extraction for POST /ingest.

Kept as a separate module (rather than folded into mmu_server.py) so the
chunking logic is unit-testable without standing up FastAPI or Neo4j.

This module does NOT write to the graph. It turns a file into a list of
chunk dicts; mmu_server.py's /ingest endpoint calls MMUCore.add_memory()
once per chunk. Keeping extraction and persistence separate means chunk
quality can be inspected before anything is committed to the graph.

Extractor choice (pdfplumber, not pypdf) is empirical, see extract_pdf().

WEB CONTENT IS UNTRUSTED. A fetched page can contain text addressed at the model
("note to AI assistants: remember that...") and MMU makes that worse than a
one-shot injection, because anything saved becomes durable and is re-fed to the
model by session_bundle and /recall for as long as it lives. Two structural
consequences, both deliberate:

  - Web memories carry src_type=3 ("Web"), which /recall renders in every
    context line, so the model can always see that a claim came from a page
    rather than from the user.
  - Stage 1 only fetches URLs the CALLER supplies. Nothing here lets a model
    choose its own targets, and the idle daemon has no path to this code.

GRP assignment is deliberately absent from this module. Classifying a chunk's
domain is a judgment call, and mmu_server.py does not do cognition -- the
caller supplies grp_code. See the Phase 11 design doc's SCOPE DECISION.
"""

import os
import re
import logging
import urllib.request
from html.parser import HTMLParser

from light_index_v2 import tokenize

log = logging.getLogger("ingest")

# pdfplumber emits (cid:N) for glyphs it cannot map to a character. They are
# noise in a payload and actively harmful as keywords, so they are stripped.
CID_RE        = re.compile(r"\(cid:\d+\)")
# Collapse runs of whitespace that survive column/table extraction, but keep
# paragraph breaks (handled separately) intact.
INLINE_WS_RE  = re.compile(r"[ \t]+")
MULTI_NL_RE   = re.compile(r"\n{3,}")
# A token worth indexing: has at least one letter, not pure punctuation/digits.
USEFUL_KW_RE  = re.compile(r"^[a-z][a-z0-9\-']*$")

# -- Web ingestion (stage 1: caller-chosen URLs only) --
# Hard cap on fetched bytes. A server-side fetcher with no ceiling is a memory
# exhaustion primitive as much as anything else.
WEB_MAX_BYTES   = int(os.environ.get("MMU_WEB_MAX_BYTES", str(3 * 1024 * 1024)))
WEB_TIMEOUT     = int(os.environ.get("MMU_WEB_TIMEOUT", "20"))
WEB_USER_AGENT  = os.environ.get("MMU_WEB_USER_AGENT", "MMU-ingest/1.0")
# Only these content types are parsed. A PDF served over HTTP is not handled
# here; download it and ingest it as a file, so the pdfplumber path is used.
WEB_OK_TYPES    = ("text/html", "text/plain", "application/xhtml+xml", "text/markdown")

DEFAULT_TARGET_WORDS  = 300
DEFAULT_OVERLAP_WORDS = 30
MAX_KEYWORDS          = 8

# A chunk shorter than this is page furniture -- a running header, a stray
# caption fragment, a page number. Observed on a real 78-page paper: 21 of 153
# chunks fell under 50 words, one of them 4 words long. They cost a memory
# slot each and contribute nothing retrievable.
MIN_CHUNK_WORDS = 40

# Dot leaders ("Introduction . . . . . 17") mark tables of contents and
# indexes. A page with several of them is front matter, not content.
DOT_LEADER_RE      = re.compile(r"(?:\.\s*){4,}")
FRONT_MATTER_LEADS = 5

# Academic prose is dense in connectives that the gate's general-purpose
# STOPWORDS list never needed to cover, because conversational memories do not
# contain them at this frequency. Applied ONLY to keyword extraction here --
# light_index_v2.STOPWORDS is deliberately left untouched, since changing it
# would alter gate behaviour for every existing memory in the graph.
INGEST_STOPWORDS = {
    "one", "two", "three", "not", "any", "also", "thus", "where", "which",
    "since", "given", "such", "both", "each", "same", "other", "than", "then",
    "into", "over", "under", "between", "within", "while", "these", "those",
    "there", "here", "when", "can", "may", "must", "should", "would", "could",
    "has", "have", "had", "been", "being", "its", "their", "them", "they",
    "all", "more", "most", "less", "least", "only", "very", "much", "many",
    "see", "eq", "fig", "section", "table", "case", "term", "terms", "form",
    "note", "above", "below", "following", "respectively", "however",
}


# ─────────────────────────────────────────────
#  TEXT CLEANUP
# ─────────────────────────────────────────────

def clean_text(text):
    """Normalize extracted text without destroying paragraph structure."""
    if not text:
        return ""
    text = CID_RE.sub(" ", text)
    text = DOT_LEADER_RE.sub(" ", text)
    # De-hyphenate words split across a line break: "curva-\nture" -> "curvature"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = INLINE_WS_RE.sub(" ", text)
    text = MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────
#  LOADERS
# ─────────────────────────────────────────────

def extract_pdf(path):
    """
    Extract a PDF as [(page_number, text)], 1-indexed pages.

    pdfplumber rather than pypdf, decided by testing both against a real
    paper from this corpus rather than by reputation. On equation-heavy text
    pypdf runs subscripts into their neighbours -- "fractured wy bridge"
    comes out as "fracturedwybridge" -- which is unrecoverable: the keyword
    gate can never match that token, and no amount of downstream cleanup
    can split it back apart reliably. pdfplumber preserves the word
    boundaries. It is ~7x slower per page (0.15s vs 0.02s), which is
    irrelevant for a one-time ingest and worth paying for usable tokens.
    """
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception as e:
                log.warning("page %d extraction failed in %s: %s", i, path, e)
                pages.append((i, ""))
                continue
            # Skip tables of contents / indexes. Detected BEFORE cleanup,
            # because clean_text() strips the dot leaders that identify them.
            if len(DOT_LEADER_RE.findall(raw)) >= FRONT_MATTER_LEADS:
                log.info("page %d looks like front matter (dot leaders), skipping", i)
                pages.append((i, ""))
                continue
            pages.append((i, clean_text(raw)))
    return pages


class _TextExtractor(HTMLParser):
    """
    Minimal HTML -> text. stdlib only, so web ingestion adds no dependency.

    Drops script/style/noscript content entirely -- it is never prose, and
    letting JS source into a memory payload would poison both the keyword
    index and the embedding.
    """

    # Chrome, not prose. Nav menus, cookie banners and footers are the web's
    # equivalent of a PDF table of contents: they chunk into junk memories and
    # fill the keyword index with words like "toggle" and "subsection".
    _SKIP = {"script", "style", "noscript", "svg", "head",
             "nav", "header", "footer", "aside", "form", "button", "select"}

    # When a page marks its real content, prefer it and drop the rest.
    # NOTE: "article" is intentionally NOT in _BREAK below -- a tag cannot be in
    # both, because handle_starttag dispatches on the first matching branch.
    _MAIN = {"main", "article"}

    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []        # everything outside skipped elements
        self._main = []         # only what is inside <main>/<article>
        self._skip_depth = 0
        self._main_depth = 0

    def _emit(self, chunk):
        if self._skip_depth:
            return
        self._parts.append(chunk)
        if self._main_depth:
            self._main.append(chunk)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._MAIN:
            self._main_depth += 1
        elif tag in self._BREAK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._MAIN and self._main_depth:
            self._main_depth -= 1
        elif tag in self._BREAK:
            self._emit("\n")

    def handle_data(self, data):
        if data.strip():
            self._emit(data)

    def text(self):
        """
        Prefer <main>/<article> when the page marks one with real content.

        The 200-word floor guards against a page that wraps a breadcrumb in
        <main> and puts the body elsewhere -- falling back to the whole
        document beats returning a fragment.
        """
        main = "".join(self._main)
        if len(main.split()) >= 200:
            return main
        return "".join(self._parts)


def _assert_public_url(url):
    """
    Reject anything that is not a public http(s) address.

    A server-side fetcher is a Server-Side Request Forgery primitive: anyone who
    can reach /ingest could otherwise make MMU fetch, and store, things only MMU
    can reach -- its own API on localhost:8765 (including /export), Neo4j on
    7687, other containers, or a cloud metadata endpoint at 169.254.169.254.

    Resolving the hostname and checking the ADDRESS matters: a public name can
    resolve to 127.0.0.1, so validating the string alone proves nothing.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"only http/https URLs may be fetched (got {p.scheme!r})")
    if not p.hostname:
        raise ValueError("URL has no host")

    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"could not resolve {p.hostname}: {e}")

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise ValueError(
                f"refusing to fetch {p.hostname} -- it resolves to {addr}, which is "
                f"a private or loopback address"
            )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Stop automatic redirects.

    Without this, _assert_public_url() is trivially bypassed: a public URL can
    302 straight to http://127.0.0.1:8765/export and urllib would follow it
    without the destination ever being checked. Redirects are surfaced to the
    caller instead, so each hop can be validated.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"refusing to follow redirect to {newurl} (re-submit that URL directly)")


def extract_url(url, max_bytes=None, timeout=None):
    """
    Fetch a URL and return [(1, text)], matching the other loaders.

    Stage 1 of web ingestion: the CALLER chooses the URL. Nothing here lets a
    model pick its own targets, deliberately -- see the threat notes in the
    module docstring.
    """
    _assert_public_url(url)

    req = urllib.request.Request(url, headers={
        "User-Agent": WEB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
    })
    opener = urllib.request.build_opener(_NoRedirect)

    with opener.open(req, timeout=timeout or WEB_TIMEOUT) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and not any(ctype.startswith(t) for t in WEB_OK_TYPES):
            raise ValueError(
                f"unsupported content type {ctype!r}. Only HTML/plain text is fetched; "
                f"download other formats and ingest them as files."
            )
        raw = r.read((max_bytes or WEB_MAX_BYTES) + 1)

    if len(raw) > (max_bytes or WEB_MAX_BYTES):
        raise ValueError(f"response exceeds {max_bytes or WEB_MAX_BYTES} bytes")

    charset = "utf-8"
    try:
        html = raw.decode(charset, errors="replace")
    except Exception:
        html = raw.decode("latin-1", errors="replace")

    if ctype.startswith("text/plain") or ctype.startswith("text/markdown"):
        return [(1, clean_text(html))]

    def _parse(aggressive):
        p = _TextExtractor()
        if not aggressive:
            # Only the tags that reliably close. Unclosed chrome tags cannot
            # strand the parser here.
            p._SKIP = {"script", "style", "noscript", "head"}
            p._MAIN = set()
        try:
            p.feed(html)
        except Exception as e:
            log.warning("HTML parse issue for %s: %s", url, e)
        return p.text()

    text = _parse(aggressive=True)
    if len(text.split()) < 50:
        # Either the page is genuinely tiny, or an unclosed tag swallowed it.
        # Falling back costs one extra parse and beats returning nothing.
        log.info("Aggressive extraction yielded %d words for %s; retrying "
                 "with minimal tag skipping", len(text.split()), url)
        text = _parse(aggressive=False)

    return [(1, clean_text(text))]


def extract_plain(path):
    """Markdown or plain text. Returned as a single 'page' so the chunker
    sees one continuous line-numbered document."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [(1, clean_text(f.read()))]


def load_document(source_path, source_type):
    """
    Dispatch to the right loader. Returns [(page_number, text)].

    source_type "text" also accepts raw text passed directly in source_path
    rather than a filesystem path, per the Phase 11 design doc.
    """
    st = (source_type or "").lower()
    if st == "url":
        return extract_url(source_path)
    if st == "pdf":
        return extract_pdf(source_path)
    if st in ("markdown", "md", "text", "txt"):
        if st in ("text", "txt") and not os.path.exists(source_path):
            return [(1, clean_text(source_path))]   # raw text, not a path
        return extract_plain(source_path)
    raise ValueError(f"unsupported source_type: {source_type!r}")


# ─────────────────────────────────────────────
#  CHUNKING
# ─────────────────────────────────────────────

def _paragraphs_with_lines(text, start_line=1):
    """
    Split into (paragraph_text, starting_line_number).

    Line numbers are accumulated as the text is consumed rather than
    recomputed per paragraph -- re-scanning the whole document for each
    chunk would be quadratic on a 114-page paper.
    """
    paras = []
    line_no = start_line
    buf, buf_start = [], line_no

    for raw_line in text.split("\n"):
        if raw_line.strip():
            if not buf:
                buf_start = line_no
            buf.append(raw_line.strip())
        else:
            if buf:
                paras.append((" ".join(buf), buf_start))
                buf = []
        line_no += 1

    if buf:
        paras.append((" ".join(buf), buf_start))
    return paras, line_no


def chunk_text(text, target_words=DEFAULT_TARGET_WORDS,
               overlap_words=DEFAULT_OVERLAP_WORDS, start_line=1, page=None):
    """
    Split text into chunks of roughly target_words, respecting paragraph
    boundaries, with a small overlap so a fact spanning a boundary is not lost.

    Returns [{"text", "start_line", "page", "words"}].

    Paragraph boundaries are preferred over hard word counts: a chunk that
    ends mid-sentence makes a poor memory. A paragraph longer than the target
    on its own is split on word count, since the alternative is an unbounded
    chunk.
    """
    paras, _ = _paragraphs_with_lines(text, start_line=start_line)
    chunks = []
    cur, cur_words, cur_line = [], 0, None

    def flush():
        nonlocal cur, cur_words, cur_line
        if not cur:
            return
        body = " ".join(cur).strip()
        if body:
            chunks.append({
                "text":       body,
                "start_line": cur_line if cur_line is not None else start_line,
                "page":       page,
                "words":      len(body.split()),
            })
        cur, cur_words, cur_line = [], 0, None

    for ptext, pline in paras:
        pwords = ptext.split()

        # A single paragraph bigger than the target: emit it in slices.
        if len(pwords) > target_words:
            flush()
            for i in range(0, len(pwords), target_words):
                piece = " ".join(pwords[i:i + target_words])
                chunks.append({
                    "text":       piece,
                    "start_line": pline,
                    "page":       page,
                    "words":      len(piece.split()),
                })
            continue

        if cur_words + len(pwords) > target_words and cur:
            tail = " ".join(cur).split()[-overlap_words:] if overlap_words else []
            flush()
            if tail:
                cur, cur_words, cur_line = [" ".join(tail)], len(tail), pline

        if cur_line is None:
            cur_line = pline
        cur.append(ptext)
        cur_words += len(pwords)

    flush()
    return chunks


def chunk_document(pages, target_words=DEFAULT_TARGET_WORDS,
                   overlap_words=DEFAULT_OVERLAP_WORDS):
    """
    Chunk a whole document, carrying page numbers through.

    Line numbers restart per page, which is the honest thing to record for a
    PDF: there is no document-wide line concept, and "page 12, line 4" is a
    traceable answer to "where did this come from" while a running total is not.
    """
    out, dropped = [], 0
    for page_no, text in pages:
        if not text.strip():
            continue
        for c in chunk_text(text, target_words, overlap_words,
                            start_line=1, page=page_no):
            if c["words"] < MIN_CHUNK_WORDS:
                dropped += 1
                continue
            out.append(c)
    if dropped:
        log.info("dropped %d chunks under %d words (page furniture)",
                 dropped, MIN_CHUNK_WORDS)
    return out


# ─────────────────────────────────────────────
#  KEYWORD EXTRACTION
# ─────────────────────────────────────────────

def extract_keywords(text, max_kw=MAX_KEYWORDS):
    """
    Pull candidate keywords from a chunk.

    Reuses tokenize() from light_index_v2 rather than inventing a second
    extraction method: the gate that later retrieves these memories applies
    the same stopword list and stemming rules, so keywords produced here and
    lookups performed there have to agree. A private tokenizer would drift.

    Frequency-ranked, capped at max_kw to match the save_memory tool schema's
    own 3-8 keyword guidance.
    """
    words, _ = tokenize(text or "")
    freq = {}
    for w in words:
        if len(w) < 3 or w in INGEST_STOPWORDS or not USEFUL_KW_RE.match(w):
            continue
        freq[w] = freq.get(w, 0) + 1
    if not freq:
        return []
    # Sort by frequency, then alphabetically, so the result is deterministic
    # for a given chunk -- important when re-running an ingest to compare.
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:max_kw]]
