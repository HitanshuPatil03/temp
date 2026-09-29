"""The real-document corpus: crawl, fetch, record, verify.

Everything this platform claims about accuracy has to be measured against
documents somebody else wrote. A synthetic table proves the pipeline runs; only a
real CIL production statement — with its merged header cells, its footnote
markers, its "Prov." column and its scanned annexures — proves it works.

This module crawls the public websites of Coal India, its subsidiaries, CMPDI, the
Coal Controller Organisation and the Ministry of Coal, and records **where each
document came from**.

Four rules, because downloading other people's documents in bulk deserves care:

**The documents are not committed; the manifest is.** A government PDF is theirs,
not ours, and a repository is not a mirror. What is versioned here is the manifest
— source URL, SHA-256, size, title, retrieval date — so anyone can re-fetch the
identical corpus and verify they got the identical bytes. That is also exactly what
a gold corpus needs (roadmap 3.6): ground truth is worthless if the document it
refers to might have changed.

**Politely.** One request per second per host, a User-Agent that says who is
calling and why, ``robots.txt`` read and obeyed, and a bounded crawl — a page
budget per seed, so a site with a calendar widget cannot turn this into a spider.

**Nothing is trusted.** Every file goes through the same upload boundary as a human
upload — sniffed by content, page-capped, encrypted PDFs refused, active content
stripped. A document being from coal.gov.in is not a reason to skip the checks; it
is the ordinary case they were written for.

**Files are filed by the company they are about, not the site they sat on.** CIL's
own annual-reports page carries every subsidiary's annual report; ``SECL``'s report
belongs in ``secl/`` regardless of which host served it. Routing reads the CIL
entity table, so it uses the same aliases the extractor does.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

from mrip import log
from mrip.normalize.entities import ENTITIES

__all__ = [
    "SEEDS",
    "Candidate",
    "CorpusEntry",
    "Seed",
    "crawl",
    "discover",
    "fetch_all",
    "load_manifest",
    "summarise",
    "verify",
    "write_manifest",
]

logger = log.get_logger("mrip.corpus")

#: Identifies the caller. A site administrator who wonders what this traffic is can
#: read the answer in their access log rather than guessing.
#:
#: The ``Mozilla/5.0 (compatible; …)`` prefix is not a disguise — several of these
#: sites sit behind a WAF that rejects any User-Agent without it with a 403, and a
#: string that is both honest and accepted is better than one that is honest and
#: refused. Who we are and why is still spelled out in the comment field.
USER_AGENT = (
    "Mozilla/5.0 (compatible; MRIP-corpus/0.3; "
    "Mining Reporting Intelligence Platform; SIH26023 research prototype)"
)

#: Seconds between requests to the same host.
POLITE_DELAY = 1.0

#: Beyond this a file is almost certainly a video, or a Hindi annual report
#: scanned at 600 dpi that no demonstration will finish reading. Recorded as
#: skipped rather than silently dropped.
#:
#: 700 MB sounds indefensible until you look at what these sites publish: MCL's
#: subsidiary annual report is a 621 MB scan and WCL's Hindi annual report is
#: 183 MB. Those are exactly the documents the platform has to prove it can take.
MAX_DOWNLOAD_BYTES = 700 * 1024 * 1024

#: A crawl that cannot finish is not polite. Per seed, at most this many HTML pages
#: are read — enough for a report index and its year sub-pages, not enough for a
#: site's entire tender archive.
MAX_PAGES_PER_SEED = 80

#: File extensions worth having. ``.doc``/``.docx`` are here because several
#: subsidiaries publish monthly performance as Word, and the pipeline reads it.
_FILE_PATTERN = re.compile(r"\.(pdf|xlsx|xlsm|xls|csv|docx|doc)(?:\?|#|$)", re.I)

#: Pages worth following one hop deeper. Without this the crawler reads the whole
#: site; with it, it reads the parts that hold figures.
_FOLLOW = (
    r"annual.?report|report|statistic|performance|production|off.?take|financial|"
    r"result|publication|directory|review|physical|monthly|quarterly|provisional|"
    r"investor|disclosure|data|summary|booklet|glance|reserve|grade|import|export"
)

#: Never followed and never downloaded. Tender notices and recruitment adverts are
#: the bulk of what these sites publish and none of it carries a production figure.
_DROP = (
    r"tender|vacanc|recruit|career|e-?proc|notice.?inviting|niq\b|nit\b|corrigend|"
    r"quotation|auction|photo.?gallery|video|screen.?reader|sitemap|privacy|"
    r"accessibility|feedback|webmail|login|captcha|whatsapp|facebook|twitter|"
    r"linkedin|youtube|instagram|\.jpe?g$|\.png$|\.gif$|\.zip$|\.exe$|"
    r"trading.?window|advertisement|admit.?card|answer.?key|result.?of.?interview|"
    r"seniority|promotion|transfer.?order|obituar|tour.?programme"
)

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
#: Both quote styles and bare hrefs. Missing the single-quoted form cost this
#: crawler 402 documents on one CIL page before it was noticed.
_ANCHOR = re.compile(
    r"<a\b[^>]*?href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>(.*?)</a>",
    re.S | re.I,
)
#: Government sites also hand out documents through onclick handlers and iframes.
_LOOSE_LINK = re.compile(
    r"(?:href|src|data-href|data-url|window\.open\(\s*)['\"]([^'\"]+\.(?:pdf|xlsx|xls|csv|docx))['\"]",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Seed:
    """One page to crawl for documents, and how far to follow it."""

    company: str
    #: What the organisation is called, for the manifest and the UI.
    organisation: str
    url: str
    #: Hops of HTML to follow. 0 reads only this page; 1 also reads the report-index
    #: pages it links to, which is where most of these sites keep the files.
    depth: int = 1
    #: Only links whose URL or anchor text matches are kept. Without this a
    #: government homepage yields two hundred tender notices and four reports.
    keep: str = r".*"
    #: Links matching this are dropped even if they matched ``keep``.
    drop: str = _DROP
    #: Pages matching this are followed when ``depth`` allows.
    follow: str = _FOLLOW
    note: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def site(self) -> str:
        """The registrable domain, which is the real crawl boundary.

        WCL serves its pages from ``www.westerncoal.in`` and its documents from
        ``docs.westerncoal.in`` and ``old.westerncoal.in``; MCL keeps some annexures
        on a second subdomain. Confining the crawl to the exact hostname would have
        made those files invisible while confining it to the domain keeps it from
        wandering off to ``pmindia.gov.in``.
        """
        return _site_of(self.url)


def _site_of(url: str) -> str:
    """Registrable domain of a URL, for the ``gov.in``/``co.in``/``nic.in`` cases.

    A naive "last two labels" rule turns every Indian government host into
    ``gov.in`` and lets a crawl of Coal India wander into unrelated ministries.
    """
    host = urlparse(url).netloc.lower().split(":")[0]
    labels = host.split(".")
    if len(labels) < 3:
        return host
    if labels[-2] in {"gov", "co", "nic", "net", "org", "ac", "com"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


#: The seeds, company by company. Chosen for documents that carry *figures* —
#: production and offtake statements, coal statistics, annual reports — rather than
#: the press releases and tender notices that dominate these sites.
#:
#: Two sites are deliberately absent. ``nclcil.in`` and ``centralcoalfields.in``
#: render entirely in the browser and serve an empty shell to any client without a
#: JavaScript engine; adding a headless browser to reach them would put a 400 MB
#: dependency on the runtime egress-free host for two annual reports that CIL
#: already publishes. Both companies are still covered, from CIL's own pages.
SEEDS: tuple[Seed, ...] = (
    # ------------------------------------------------------------------ CIL
    Seed(
        company="cil",
        organisation="Coal India Limited",
        url="https://www.coalindia.in/performance/physical/",
        depth=1,
        note="Monthly provisional production and off-take of CIL and its "
        "subsidiaries — the series this platform exists to extract.",
    ),
    Seed(
        company="cil",
        organisation="Coal India Limited",
        url="https://www.coalindia.in/performance/annual-reports/",
        depth=1,
        note="Carries every subsidiary's annual report as well as CIL's own; "
        "routing files each one under the company it is about.",
    ),
    Seed(
        company="cil",
        organisation="Coal India Limited",
        url="https://www.coalindia.in/performance/financial-statements/",
        depth=1,
    ),
    Seed(
        company="cil",
        organisation="Coal India Limited",
        url="https://www.coalindia.in/performance/financial/",
        depth=1,
    ),
    Seed(
        company="cil",
        organisation="Coal India Limited",
        url="https://www.coalindia.in/investor-relations/financial-results/",
        depth=1,
    ),
    # ------------------------------------------------------ Ministry of Coal
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/coal-statistics",
        depth=1,
        note="The Ministry's own statistics — production, reserves, grades, "
        "import and export, and the monthly Coal at a Glance series.",
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/public-information/monthly-statistics-at-glance",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/public-information/statistical-report",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/public-information/monthly-summary-cabinets",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/production-and-supplies",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/quarterly-booklet",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/coal-reserves",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/import-and-export",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/coal-grades",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/output-per-man-shift",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/obr",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/coal-demand-projections",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/rsr-report",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/coal-evacuation-plan",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/major-statistics/mission-coking-coal",
        depth=0,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/public-information/reports/annual-reports",
        depth=1,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/public-information/reports/other-reports",
        depth=1,
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/nominated-authority/production-reviews",
        depth=0,
        note="Mine-level production review minutes — the closest thing the "
        "Ministry publishes to a per-mine time series.",
    ),
    Seed(
        company="moc",
        organisation="Ministry of Coal",
        url="https://coal.gov.in/nominated-authority/operationalisation-reviews",
        depth=0,
    ),
    # ------------------------------------------ Coal Controller Organisation
    Seed(
        company="coalcontroller",
        organisation="Coal Controller Organisation",
        url="https://www.coalcontroller.gov.in/provisional-coal-statistics",
        depth=1,
        note="The official provisional statistics series, one volume per year.",
    ),
    Seed(
        company="coalcontroller",
        organisation="Coal Controller Organisation",
        url="https://www.coalcontroller.gov.in/coal-directory-india",
        depth=1,
        note="Coal Directory of India — the sector's reference tables.",
    ),
    Seed(
        company="coalcontroller",
        organisation="Coal Controller Organisation",
        url="https://www.coalcontroller.gov.in/statistics-section",
        depth=1,
    ),
    Seed(
        company="coalcontroller",
        organisation="Coal Controller Organisation",
        url="https://www.coalcontroller.gov.in/research-publications",
        depth=1,
    ),
    # --------------------------------------------------------- subsidiaries
    Seed(
        company="mcl",
        organisation="Mahanadi Coalfields Limited",
        url="https://www.mahanadicoal.in/About/heproductionreport.php",
        depth=1,
        note="MCL's monthly production reports. Reachable only because robots.txt "
        "is fetched with our own User-Agent — its WAF answers Python-urllib with "
        "a 403, which the standard parser reads as disallow-all.",
    ),
    Seed(
        company="mcl",
        organisation="Mahanadi Coalfields Limited",
        url="https://www.mahanadicoal.in/Financial/hannual_report.php",
        depth=1,
    ),
    Seed(
        company="mcl",
        organisation="Mahanadi Coalfields Limited",
        url="https://www.mahanadicoal.in/About/heflashreport.php",
        depth=1,
    ),
    Seed(
        company="secl",
        organisation="South Eastern Coalfields Limited",
        url="https://secl-cil.in/index",
        depth=2,
    ),
    Seed(
        company="bccl",
        organisation="Bharat Coking Coal Limited",
        url="https://www.bcclweb.in/?page_id=25564",
        depth=1,
    ),
    Seed(
        company="bccl",
        organisation="Bharat Coking Coal Limited",
        url="https://www.bcclweb.in/?page_id=27967",
        depth=1,
    ),
    Seed(
        company="wcl",
        organisation="Western Coalfields Limited",
        url="https://old.westerncoal.in/",
        depth=2,
        note="WCL's current site is a browser-rendered shell; its documents are "
        "still served from the archived site, which is plain HTML.",
    ),
    Seed(
        company="wcl",
        organisation="Western Coalfields Limited",
        url="https://www.westerncoal.in/en/archives",
        depth=1,
    ),
    Seed(
        company="cmpdi",
        organisation="Central Mine Planning and Design Institute Limited",
        url="https://www.cmpdi.co.in/en",
        depth=2,
        note="CMPDI is the PS's own sponsoring institute — its annual reports and "
        "quarterly financial results are the closest documents to the format the "
        "platform is expected to produce.",
    ),
    Seed(
        company="ecl",
        organisation="Eastern Coalfields Limited",
        url="https://www.easterncoal.nic.in/",
        depth=1,
        note="Often unreachable from outside India; ECL's annual report is also "
        "published on CIL's own annual-reports page, so the corpus covers it "
        "either way.",
    ),
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A document link found by the crawl, before anything is downloaded."""

    company: str
    organisation: str
    title: str
    url: str
    source_page: str


@dataclass
class CorpusEntry:
    """One fetched document, and everything needed to fetch it again."""

    company: str
    organisation: str
    title: str
    url: str
    filename: str
    content_hash: str
    size_bytes: int
    content_type: str
    retrieved_at: str
    source_page: str

    @property
    def relative_path(self) -> str:
        return f"{self.company}/{self.filename}"


@dataclass
class FetchReport:
    """What a fetch run did, so the command can print something honest."""

    entries: list[CorpusEntry] = field(default_factory=list)
    already_present: int = 0
    downloaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


# ------------------------------------------------------------------ routing


#: ``(company_id, pattern)``, longest-alias-first so "central coalfields" is tested
#: before "coal india". Built from the entity table the extractor uses, so a name
#: this crawler can file is a name the pipeline can resolve.
def _routing_table() -> tuple[tuple[str, re.Pattern[str]], ...]:
    rows: list[tuple[str, str, int]] = []
    for entity in ENTITIES:
        if entity.entity_id in {"sccl", "nlcil"}:
            # Not CIL companies. Their documents are not what this corpus is for,
            # and filing them under a CIL folder would be the exact error
            # normalize/entities.py exists to prevent.
            continue
        code = entity.code.lower()
        rows.append((entity.entity_id, rf"\b{re.escape(code)}\b", len(code)))
        rows.append((entity.entity_id, re.escape(entity.name.lower()), len(entity.name)))
        for alias in entity.aliases:
            rows.append((entity.entity_id, re.escape(alias.lower()), len(alias)))
    rows.sort(key=lambda row: row[2], reverse=True)
    return tuple((company, re.compile(pattern, re.I)) for company, pattern, _ in rows)


_ROUTES = _routing_table()


def route(url: str, title: str, default: str) -> str:
    """Which company a document is *about*.

    CIL's annual-reports page carries ECL's, BCCL's, MCL's and everyone else's
    annual report. Filing them all under ``cil/`` because that is where they were
    linked would defeat the point of a company-wise corpus, so the filename and the
    anchor text are read for a company name first, and only then does the seed's own
    company apply.

    The filename is more trustworthy than the anchor text — ``WCL_Annual_Report.pdf``
    is unambiguous where a link reading "Annual Report" is not — so it is tested
    alone before the two are tested together.
    """
    name = unquote(Path(urlparse(url).path).name).replace("_", " ").replace("-", " ")
    for haystack in (name, f"{name} {title}"):
        for company, pattern in _ROUTES:
            if pattern.search(haystack):
                return company
    return default


#: ``company_id -> official name``, from the same entity table the routing patterns
#: are built from. Every company :func:`route` can return is a key here except
#: ``coalcontroller``, which is not a coal company and so is not in that table.
_ORGANISATIONS = {entity.entity_id: entity.name for entity in ENTITIES}


def organisation_of(company: str, default: str) -> str:
    """The publisher name to record for a document routed to ``company``.

    This exists because :func:`route` re-files a document per document and the
    organisation name has to be re-filed *with* it. CIL's annual-reports page
    carries ECL's annual report; routing it to ``ecl/`` while leaving the seed's
    own "Coal India Limited" on it produces a manifest that contradicts its own
    directory layout — and the manifest is the provenance record, the thing that
    is committed so a reader can check which publisher each figure came off.

    ``default`` is the seed's organisation, used for the one publisher the entity
    table does not cover.
    """
    return _ORGANISATIONS.get(company, default)


# --------------------------------------------------------------- politeness


class _Politeness:
    """One request per second per host, a cached robots.txt, and a backoff.

    The backoff is the part that matters on these sites. ``coal.gov.in`` sits
    behind a WAF that, after a few dozen requests, stops serving pages and starts
    serving a "User validation required" CAPTCHA to everyone from that address. A
    crawler that does not notice records *zero documents* for the Ministry of Coal
    and reports it as if the Ministry published nothing — which is worse than
    failing, because it is a wrong answer delivered confidently.
    """

    def __init__(self, delay: float = POLITE_DELAY) -> None:
        self._delay = delay
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._penalty: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        delay = self._delay * self._penalty.get(host, 1.0)
        previous = self._last.get(host)
        if previous is not None:
            elapsed = time.monotonic() - previous
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last[host] = time.monotonic()

    def back_off(self, url: str) -> float:
        """Slow down for this host after it pushed back. Returns the new delay."""
        host = urlparse(url).netloc
        self._penalty[host] = min(self._penalty.get(host, 1.0) * 4.0, 30.0)
        delay = self._delay * self._penalty[host]
        logger.warning("backing off", host=host, delay_seconds=round(delay, 1))
        return delay

    def is_penalised(self, url: str) -> bool:
        return self._penalty.get(urlparse(url).netloc, 1.0) > 1.0

    def allowed(self, url: str) -> bool:
        """Whether ``robots.txt`` permits fetching this URL.

        The file is fetched with **our** User-Agent rather than through
        ``RobotFileParser.read()``, which uses Python's default one. That matters
        more than it sounds: several of these sites sit behind a WAF that answers
        ``Python-urllib`` with a 403, and ``read()`` turns a 403 on robots.txt into
        *disallow everything*. Four subsidiaries whose robots.txt says
        ``User-agent: * / Allow: /`` were being skipped entirely because of it.

        A site that cannot serve its robots.txt at all is treated as permitting the
        fetch — the alternative is refusing to read a public document because a file
        that governs crawlers is missing, which is not what the file is for.
        """
        host = urlparse(url).netloc
        if host not in self._robots:
            self._robots[host] = self._read_robots(url)
        parser_or_none = self._robots[host]
        if parser_or_none is None:
            return True
        try:
            return parser_or_none.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def _read_robots(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        self.wait(robots_url)
        try:
            body, _ = _get(robots_url, timeout=20.0)
        except Exception as failure:
            logger.info(
                "no robots.txt; proceeding",
                host=parts.netloc,
                reason=type(failure).__name__,
            )
            return None
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return parser


def _encode(url: str) -> str:
    """Percent-encode the path of a URL that came out of an anchor.

    Several subsidiaries link files whose names contain spaces and Devanagari —
    ``news/pdf/NOTICE 2026 (HINDI) 21022026.pdf`` — which ``urlopen`` refuses. The
    path is re-encoded while leaving anything already encoded alone.
    """
    parts = urlparse(url)
    path = quote(unquote(parts.path), safe="/%:@&=+$,~*!()'")
    query = quote(unquote(parts.query), safe="/?:@&=+$,%~*!()'")
    return urlunparse(parts._replace(path=path, query=query))


class ChallengedError(RuntimeError):
    """The host served a bot-check instead of the page.

    Raised rather than returned so no caller can mistake a CAPTCHA page for an
    index page with no documents on it.
    """


#: What a WAF challenge looks like on these sites. Deliberately narrow: a report
#: about mine safety may well contain the word "validation".
_CHALLENGE = re.compile(
    rb"user validation required|validation request|please type the text you see|"
    rb"captcha\.(?:gif|jpg|png)|checking your browser|cf-browser-verification|"
    rb"request unsuccessful.*incident id",
    re.I | re.S,
)


def _looks_challenged(body: bytes, content_type: str) -> bool:
    if "html" not in content_type.lower() and content_type:
        return False
    return bool(_CHALLENGE.search(body[:4096]))


#: One cookie jar for the whole run. Keeping a session cookie is what an ordinary
#: browser does; several of these sites hand one out on the first request and answer
#: subsequent ones more readily. It is not an attempt to get past a challenge — a
#: challenge is respected by backing off (see :meth:`_Politeness.back_off`).
_OPENER: Any = None


def _opener() -> Any:
    global _OPENER
    if _OPENER is None:
        import http.cookiejar
        import urllib.request

        jar = http.cookiejar.CookieJar()
        _OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return _OPENER


def _request(url: str, *, extra: dict[str, str] | None = None) -> Any:
    import urllib.request

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/pdf,application/octet-stream,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        # Only encodings the standard library can undo. WCL's nginx gzips its HTML
        # whether or not it is asked to, and `urlopen` hands the raw deflate stream
        # back — which parsed as a page with zero links.
        "Accept-Encoding": "gzip, deflate",
    }
    if extra:
        headers.update(extra)
    return urllib.request.Request(  # noqa: S310 — scheme checked by the caller
        _encode(url), headers=headers
    )


def _get(url: str, *, timeout: float = 60.0) -> tuple[bytes, str]:
    """Fetch a URL, following redirects. Returns ``(body, content-type)``."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        # Everything reaching here came from an anchor on a fetched page, so a
        # `file:` or `javascript:` URL is either a mistake or a page trying to make
        # this process read its local disk.
        raise ValueError(f"Refusing to fetch a non-HTTP URL: {url!r}")

    with _opener().open(_request(url), timeout=timeout) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")
        encoding = (response.headers.get("Content-Encoding") or "").lower().strip()
    body = _decompress(body, encoding)
    if _looks_challenged(body, content_type):
        raise ChallengedError(f"bot-check served instead of the page: {url}")
    return body, content_type


def _get_large(
    url: str, *, timeout: float = 300.0, attempts: int = 4
) -> tuple[bytes, str]:
    """Fetch a document, resuming a connection the server drops mid-stream.

    The Coal Directory of India is a 250 MB volume of reference tables served from
    a host that closes the connection partway through more often than not
    (``IncompleteRead(256745671 bytes read, 22880824 more expected)``). It is also
    the single most useful statistics document in the sector, so it is worth a
    ``Range`` request rather than a shrug: each attempt asks for the bytes after
    what already arrived, and the pieces are joined.

    A server that ignores ``Range`` answers 200 with the whole file; that is
    detected by the status code and the buffer is restarted rather than doubled.
    """
    import http.client
    import urllib.error

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Refusing to fetch a non-HTTP URL: {url!r}")

    buffer = bytearray()
    content_type = ""
    last_failure: Exception | None = None

    for attempt in range(attempts):
        extra = {"Range": f"bytes={len(buffer)}-"} if buffer else None
        try:
            with _opener().open(_request(url, extra=extra), timeout=timeout) as response:
                if buffer and response.status != 206:
                    # Range ignored: this is the whole file again, not a tail.
                    buffer.clear()
                content_type = response.headers.get("Content-Type", content_type)
                encoding = (
                    (response.headers.get("Content-Encoding") or "").lower().strip()
                )
                if encoding and buffer:
                    # A compressed partial response cannot be concatenated. Start
                    # over without the Range header rather than store a broken file.
                    raise _RestartWithoutRangeError
                chunk = response.read()
            buffer.extend(_decompress(chunk, encoding))
        except _RestartWithoutRangeError:
            buffer.clear()
            continue
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as f:
            last_failure = f
            partial = getattr(f, "partial", None)
            if partial:
                buffer.extend(partial)
            logger.info(
                "connection dropped; resuming",
                url=url,
                have_bytes=len(buffer),
                attempt=attempt + 1,
            )
            time.sleep(2.0 * (attempt + 1))
            continue
        else:
            body = bytes(buffer)
            if _looks_challenged(body, content_type):
                raise ChallengedError(f"bot-check served instead of: {url}")
            return body, content_type

    if buffer and _looks_like_a_document(bytes(buffer)):
        # Better a truncated 250 MB directory than nothing: the manifest records
        # the hash of exactly what arrived, so a re-run can be compared against it.
        logger.warning("kept a partial download", url=url, bytes=len(buffer))
        return bytes(buffer), content_type
    raise last_failure or RuntimeError(f"could not download {url}")


class _RestartWithoutRangeError(Exception):
    """Internal: the server compressed a ranged response; start the fetch over."""


def _decompress(body: bytes, encoding: str) -> bytes:
    """Undo ``Content-Encoding``. An encoding we cannot undo is left alone, so the
    document sniffer downstream rejects it rather than a caller storing noise."""
    if encoding in {"gzip", "x-gzip"}:
        import gzip

        try:
            return gzip.decompress(body)
        except Exception:
            return body
    if encoding == "deflate":
        import zlib

        # Raw first: nginx's "deflate" is usually headerless despite the name.
        for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                return zlib.decompress(body, wbits)
            except zlib.error:
                continue
    return body


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", unescape(_TAG.sub(" ", text))).strip()


def _links(body: str, base: str) -> list[tuple[str, str]]:
    """Every link on a page, as ``(absolute_url, anchor_text)``.

    The href is HTML-unescaped before it is resolved. An ``&`` in a government
    filename — ``Terms & Conditions of SOR.pdf`` — arrives in the markup as
    ``&amp;``, and a URL built from the raw attribute contains a literal ``&amp;``
    that 404s. Ninety of these were lost on the first full run.
    """
    found: list[tuple[str, str]] = []
    for match in _ANCHOR.finditer(body):
        href = match.group(1) or match.group(2) or match.group(3) or ""
        if not href:
            continue
        found.append((urljoin(base, unescape(href.strip())), _clean(match.group(4))))
    for match in _LOOSE_LINK.finditer(body):
        found.append((urljoin(base, unescape(match.group(1).strip())), ""))
    return found


# ------------------------------------------------------------------- crawl


def crawl(seed: Seed, politeness: _Politeness | None = None) -> list[Candidate]:
    """Find the documents reachable from one seed, within its page budget.

    The anchor's *text* becomes the title, because a government site's filenames are
    things like ``Prodf_rnZX8AO.pdf`` while the link beside it reads "Provisional
    Production and Off-take Performance of CIL and Subsidiary Companies for the month
    of August 2026". The second is what a reviewer needs to see in a document list.
    """
    politeness = politeness or _Politeness()
    keep = re.compile(seed.keep, re.I)
    drop = re.compile(seed.drop, re.I)
    follow = re.compile(seed.follow, re.I)

    found: dict[str, Candidate] = {}
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(seed.url, seed.depth)])
    pages = 0

    while queue and pages < MAX_PAGES_PER_SEED:
        page, remaining = queue.popleft()
        page_key = page.split("#")[0]
        if page_key in visited:
            continue
        visited.add(page_key)

        if not politeness.allowed(page):
            logger.warning("robots.txt disallows this page", page=page)
            continue

        politeness.wait(page)
        try:
            body, content_type = _get(page)
        except ChallengedError:
            # The host is asking a human to prove they are one. Slow right down and
            # retry this page once; if it challenges again, leave the rest of the
            # seed alone rather than hammering a site that has said stop.
            delay = politeness.back_off(page)
            time.sleep(delay)
            try:
                body, content_type = _get(page)
            except Exception:
                logger.warning(
                    "host is serving a bot-check; abandoning this seed",
                    page=page,
                    hint="re-run `mrip-admin fetch-corpus` later to pick it up",
                )
                break
        except Exception as failure:
            logger.warning("could not read page", page=page, error=str(failure)[:160])
            continue
        pages += 1

        if "html" not in content_type.lower() and content_type:
            continue

        markup = body.decode("utf-8", errors="replace")
        for absolute, label in _links(markup, page):
            if urlparse(absolute).scheme not in {"http", "https"}:
                continue
            haystack = f"{label} {unquote(absolute)}"
            if drop.search(haystack):
                continue

            if _FILE_PATTERN.search(urlparse(absolute).path) or _FILE_PATTERN.search(
                absolute
            ):
                if not keep.search(haystack):
                    continue
                title = label or unquote(Path(urlparse(absolute).path).name)
                company = route(absolute, title, seed.company)
                found.setdefault(
                    absolute.split("#")[0],
                    Candidate(
                        company=company,
                        organisation=organisation_of(company, seed.organisation),
                        title=title[:400],
                        url=absolute.split("#")[0],
                        source_page=page,
                    ),
                )
            elif (
                remaining > 0
                and _site_of(absolute) == seed.site
                and follow.search(haystack)
                and page_key != absolute.split("#")[0]
            ):
                queue.append((absolute, remaining - 1))

    logger.info("crawled", seed=seed.url, pages=pages, documents=len(found))
    return list(found.values())


def discover(seed: Seed, politeness: _Politeness | None = None) -> list[Candidate]:
    """Backwards-compatible alias for :func:`crawl`."""
    return crawl(seed, politeness)


# ------------------------------------------------------------------- fetch


def _safe_filename(candidate: Candidate, taken: set[str]) -> str:
    """A filename that is readable, unique, and cannot escape its directory."""
    raw = unquote(Path(urlparse(candidate.url).path).name) or "document"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._") or "document"
    if not _FILE_PATTERN.search(stem):
        suffix = _FILE_PATTERN.search(candidate.url)
        stem = f"{stem}.{suffix.group(1).lower()}" if suffix else f"{stem}.pdf"
    name = stem[:120]
    key = f"{candidate.company}/{name.lower()}"
    if key in taken:
        digest = hashlib.sha256(candidate.url.encode()).hexdigest()[:8]
        name = f"{Path(name).stem}-{digest}{Path(name).suffix}"
        key = f"{candidate.company}/{name.lower()}"
    taken.add(key)
    return name


def _looks_like_a_document(body: bytes) -> bool:
    """Whether these bytes are a document rather than an HTML error page.

    A government site that has moved a file usually answers 200 with its "page not
    found" template. Writing that to ``ECL_Annual_Report.pdf`` would put a corpus
    entry in the manifest that no amount of re-fetching would fix.
    """
    head = body[:2048].lstrip()
    if head[:5] == b"%PDF-":
        return True
    if head[:2] == b"PK":  # xlsx/docx are zip containers
        return True
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # legacy .xls/.doc
        return True
    lowered = head[:512].lower()
    if lowered.startswith((b"<!doctype", b"<html", b"<?xml")) or b"<html" in lowered:
        return False
    # A CSV has no magic number; accept it if it is plausibly text with separators.
    return b"," in head or b"\t" in head or b";" in head


def fetch_all(
    root: Path,
    *,
    companies: tuple[str, ...] | None = None,
    limit_per_seed: int | None = None,
    dry_run: bool = False,
    seeds: tuple[Seed, ...] = SEEDS,
) -> FetchReport:
    """Fetch every document the seeds offer, into ``root/<company>/``.

    Skips a file already present with a non-zero size, so the command can be re-run
    after an interruption without re-downloading the corpus. That matters more than
    usual here: a full crawl takes hours at one request per second, and a site that
    starts challenging halfway through should cost the run its remaining pages, not
    everything it had already fetched.
    """
    politeness = _Politeness()
    report = FetchReport()
    taken: set[str] = set()
    seen: set[str] = set()

    for seed in seeds:
        candidates = crawl(seed, politeness)
        if companies:
            candidates = [c for c in candidates if c.company in companies]
        # The same PDF is linked from several index pages — CIL's production page
        # and its financial page both carry the quarterly statement. Without this,
        # one document becomes two manifest rows and two downloads.
        candidates = [c for c in candidates if c.url not in seen]
        seen.update(c.url for c in candidates)
        if limit_per_seed is not None:
            candidates = candidates[:limit_per_seed]

        for candidate in candidates:
            filename = _safe_filename(candidate, taken)
            directory = root / candidate.company
            target = directory / filename

            if dry_run:
                report.entries.append(
                    CorpusEntry(
                        company=candidate.company,
                        organisation=candidate.organisation,
                        title=candidate.title,
                        url=candidate.url,
                        filename=filename,
                        content_hash="",
                        size_bytes=0,
                        content_type="",
                        retrieved_at="",
                        source_page=candidate.source_page,
                    )
                )
                continue

            directory.mkdir(parents=True, exist_ok=True)

            if target.exists() and target.stat().st_size > 0:
                body = target.read_bytes()
                report.entries.append(
                    _entry(candidate, filename, body, _sniff(body, filename), target)
                )
                report.already_present += 1
                continue

            if not politeness.allowed(candidate.url):
                report.skipped.append((candidate.url, "robots.txt"))
                continue

            politeness.wait(candidate.url)
            try:
                body, content_type = _get_large(candidate.url)
            except ChallengedError:
                delay = politeness.back_off(candidate.url)
                time.sleep(delay)
                try:
                    body, content_type = _get_large(candidate.url)
                except Exception:
                    report.failed.append((candidate.url, "bot-check"))
                    continue
            except Exception as failure:
                report.failed.append((candidate.url, f"{type(failure).__name__}"))
                logger.warning(
                    "download failed", url=candidate.url, error=str(failure)[:160]
                )
                continue

            if len(body) > MAX_DOWNLOAD_BYTES:
                report.skipped.append((candidate.url, f"too large ({len(body)} bytes)"))
                continue
            if len(body) < 1024:
                # A few hundred bytes is an error page, not a report.
                report.skipped.append((candidate.url, "suspiciously small"))
                continue
            if not _looks_like_a_document(body):
                report.skipped.append((candidate.url, "served HTML, not a document"))
                continue

            target.write_bytes(body)
            report.entries.append(_entry(candidate, filename, body, content_type, target))
            report.downloaded += 1
            logger.info(
                "fetched",
                company=candidate.company,
                filename=filename,
                size_bytes=len(body),
            )

    return report


def _sniff(body: bytes, filename: str) -> str:
    """The content type of bytes already on disk.

    A re-run finds most files present and does not fetch them, so there is no
    response header to read a type from. Recording the *fetch outcome* in that
    field instead — "already present" — puts a status string where every reader of
    the manifest expects a media type, and after one full re-run that is most of
    the entries. The bytes are in hand, so they are asked directly.

    The magic number decides, except between the OOXML formats: ``.xlsx`` and
    ``.docx`` are both zip containers with the same first four bytes, and telling
    them apart means reading the archive. The extension is enough for those two and
    is only consulted once the bytes have confirmed it *is* a zip.
    """
    if body.startswith(b"%PDF"):
        return "application/pdf"
    if body.startswith(b"PK\x03\x04"):
        if filename.lower().endswith(".docx"):
            return (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if body.startswith(b"\xd0\xcf\x11\xe0"):
        return "application/vnd.ms-excel"
    return "application/octet-stream"


def _entry(
    candidate: Candidate, filename: str, body: bytes, content_type: str, path: Path
) -> CorpusEntry:
    return CorpusEntry(
        company=candidate.company,
        organisation=candidate.organisation,
        title=candidate.title,
        url=candidate.url,
        filename=filename,
        content_hash=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        content_type=content_type.split(";")[0].strip(),
        retrieved_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        source_page=candidate.source_page,
    )


# ------------------------------------------------------------------ manifest


def write_manifest(entries: list[CorpusEntry], path: Path) -> None:
    """Write the manifest that *is* committed, sorted for a readable diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": (
            "Public documents from Coal India Limited, its subsidiaries, CMPDI, the "
            "Coal Controller Organisation and the Ministry of Coal, used to test "
            "extraction against real filings. The documents themselves are not "
            "redistributed here: this manifest records where each came from and the "
            "SHA-256 of the bytes retrieved, so the corpus can be re-fetched and "
            "verified. Run: mrip-admin fetch-corpus"
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(entries),
        "companies": {
            company: summary["documents"]
            for company, summary in sorted(summarise(entries).items())
        },
        "documents": [
            asdict(entry)
            for entry in sorted(entries, key=lambda item: (item.company, item.filename))
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")


def load_manifest(path: Path) -> list[CorpusEntry]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [CorpusEntry(**row) for row in payload.get("documents", [])]


def verify(root: Path, manifest_path: Path) -> dict[str, list[str]]:
    """Re-hash the local corpus against the manifest.

    The check that makes a gold corpus mean anything: ground truth pinned to a
    document is worthless if the document might have been replaced.
    """
    result: dict[str, list[str]] = {"ok": [], "missing": [], "changed": []}
    for entry in load_manifest(manifest_path):
        path = root / entry.relative_path
        if not path.is_file():
            result["missing"].append(entry.relative_path)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.content_hash:
            result["changed"].append(entry.relative_path)
        else:
            result["ok"].append(entry.relative_path)
    return result


def summarise(entries: list[CorpusEntry]) -> dict[str, dict[str, Any]]:
    """Per-company counts and bytes, for the command's output."""
    summary: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = summary.setdefault(
            entry.company,
            {
                "organisation": entry.organisation,
                "documents": 0,
                "bytes": 0,
                "types": {},
            },
        )
        bucket["documents"] += 1
        bucket["bytes"] += entry.size_bytes
        suffix = Path(entry.filename).suffix.lower().lstrip(".") or "?"
        bucket["types"][suffix] = bucket["types"].get(suffix, 0) + 1
    return summary
