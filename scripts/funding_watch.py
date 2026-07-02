#!/usr/bin/env python3
"""Weekly funding/opportunity watcher for Ambition Campus.

Stdlib-only MVP:
- reads config/sources.csv
- scrapes each source page
- extracts candidate opportunity links using keywords
- scores relevance for a Paris association focused on equality of opportunity / student aid
- writes outputs/YYYY-MM-DD_opportunities.csv and outputs/YYYY-MM-DD_digest.md

Usage:
    python3 scripts/funding_watch.py
    python3 scripts/funding_watch.py --mark-seen
    python3 scripts/funding_watch.py --min-score 4 --max-detail 50
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import ssl
import sys
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "outputs"
SOURCES_PATH = CONFIG_DIR / "sources.csv"
SEEN_PATH = DATA_DIR / "seen.json"

USER_AGENT = "AmbitionCampusFundingWatch/0.1 (+https://ambition-campus.local)"

POSITIVE_TERMS = {
    "appel": 3,
    "appel a projets": 5,
    "appel a projet": 5,
    "appel à projets": 5,
    "appel à projet": 5,
    "ami": 3,
    "appel a manifestation": 4,
    "subvention": 5,
    "financement": 4,
    "fonds": 3,
    "aide": 3,
    "dotation": 3,
    "mecenat": 4,
    "mécénat": 4,
    "fondation": 3,
    "concours": 2,
    "prix": 2,
    "association": 4,
    "associations": 4,
    "jeunesse": 5,
    "jeune": 4,
    "jeunes": 4,
    "etudiant": 5,
    "étudiant": 5,
    "etudiante": 5,
    "étudiante": 5,
    "campus": 4,
    "education": 4,
    "éducation": 4,
    "egalite des chances": 8,
    "égalité des chances": 8,
    "insertion": 5,
    "orientation": 4,
    "solidarite": 3,
    "solidarité": 3,
    "inclusion": 4,
    "quartiers": 3,
    "politique de la ville": 4,
    "vie associative": 4,
    "benevolat": 2,
    "bénévolat": 2,
    "engagement": 3,
    "rse": 3,
}

LOCATION_TERMS = {
    "paris": 4,
    "ile-de-france": 4,
    "île-de-france": 4,
    "idf": 3,
    "france": 2,
    "europe": 1,
}

DEADLINE_TERMS = {
    "deadline": 3,
    "date limite": 4,
    "candidature": 3,
    "candidater": 3,
    "depot": 3,
    "dépôt": 3,
    "jusqu au": 2,
    "jusqu’au": 2,
    "avant le": 2,
}

NEGATIVE_TERMS = {
    "collectivites uniquement": -8,
    "collectivités uniquement": -8,
    "communes uniquement": -7,
    "entreprises uniquement": -6,
    "marches publics": -3,
    "marchés publics": -3,
    "emploi public": -4,
    "recrutement": -4,
    "nomination": -4,
    "formation interne": -3,
}

THEME_RULES = [
    ("égalité des chances", ["egalite des chances", "égalité des chances", "inclusion", "quartiers", "politique de la ville"]),
    ("aide aux étudiants", ["etudiant", "étudiant", "campus", "universite", "université", "bourse"]),
    ("jeunesse", ["jeunesse", "jeune", "jeunes", "engagement"]),
    ("éducation/orientation", ["education", "éducation", "orientation", "mentor", "tutorat"]),
    ("insertion", ["insertion", "emploi", "professionnelle"]),
    ("vie associative", ["association", "associations", "vie associative", "benevolat", "bénévolat"]),
    ("Europe", ["erasmus", "europe", "européen", "europeen"]),
]

TYPE_RULES = [
    ("subvention", ["subvention", "aide", "fonds", "dotation"]),
    ("appel à projets", ["appel a projets", "appel à projets", "appel a projet", "appel à projet"]),
    ("mécénat/fondation", ["mecenat", "mécénat", "fondation", "rse"]),
    ("concours/prix", ["concours", "prix"]),
    ("Europe", ["erasmus", "europe", "européen", "europeen"]),
]

DEADLINE_CONTEXT_PATTERNS = [
    # High-confidence deadline wording. Keep enough context to include dates written later in the sentence.
    re.compile(
        r"(?:date limite|limite de d[ée]p[ôo]t|cl[ôo]ture(?: des candidatures)?|deadline|"
        r"d[ée]p[ôo]t[^\.\n]{0,80}(?:fix[ée]e?|jusqu[’']?au)|"
        r"candidatures?[^\.\n]{0,120}(?:avant le|jusqu[’']?au|sont ouvertes jusqu[’']?au)|"
        r"avant le|jusqu[’']?au)"
        r"[^\.\n]{0,220}",
        re.I,
    ),
]

DATE_VALUE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"),
    re.compile(
        r"\b(\d{1,2})(?:er)?\s+"
        r"(janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|septembre|octobre|novembre|decembre|décembre)"
        r"\s+(\d{4})\b",
        re.I,
    ),
]

MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
}

DEADLINE_HINTS = [
    "date limite", "limite de depot", "limite de dépôt", "cloture", "clôture", "deadline",
    "candidature", "candidatures", "avant le", "jusqu au", "jusqu'au", "jusqu’au", "depot", "dépôt",
]

SEARCH_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
SEARCH_BLACKLIST_HOST_PARTS = {
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "twitter.com", "x.com",
    "duckduckgo.com", "google.com", "bing.com", "yahoo.com",
}

AMOUNT_PATTERNS = [
    # Prefer labelled funding amounts/ranges. Never match a bare "jusqu'au <date>" as an amount.
    re.compile(
        r"\b(?:montant|subvention|aide|dotation|plafond|financement|budget|taux)[^\.\n]{0,220}"
        r"(?:\d[\d\s\.]{1,}\s?(?:€|euros?)|\d{1,3}\s?%)[^\.\n]{0,120}",
        re.I,
    ),
    re.compile(
        r"\b(?:entre|de)\s+\d[\d\s\.]{1,}\s?(?:€|euros?)\s+(?:et|à|a)\s+\d[\d\s\.]{1,}\s?(?:€|euros?)\b",
        re.I,
    ),
    re.compile(r"\b\d[\d\s\.]{2,}\s?(?:€|euros?)\b", re.I),
]

STOP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".ico", ".zip", ".mp4", ".mp3"
)

GENERIC_LABEL_TERMS = {
    "aller au contenu", "aller au pied", "menu", "accueil", "contact", "mentions legales", "mentions légales",
    "politique des cookies", "politique de protection", "accessibilite", "accessibilité", "plan du site", "faq",
    "se connecter", "s inscrire", "s'inscrire", "newsletter", "facebook", "linkedin", "twitter", "instagram",
    "donnees et api", "données et api", "statistiques", "la demarche", "la démarche", "portails", "cartographie",
    "page suivante", "derniere page", "dernière page", "foire aux questions", "code source",
}

BAD_PATH_PARTS = {
    "/comptes/", "/contact", "/mentions", "/faq", "/stats", "/data", "/plan-du-site",
    "/accessibil", "/politique", "/newsletter", "/region-recrute", "/toutes-les-faq",
}

ACTION_TERMS = [
    "appel a projets", "appel à projets", "appel a projet", "appel à projet", "appel projets",
    "appel a initiatives", "appel à initiatives", "appel a propositions", "appel à propositions",
    "appel a manifestation", "appel à manifestation", "subvention", "aide", "fonds", "fdva",
    "concours", "prix", "dotation", "candidature", "candidatez", "soutien",
]

INFO_PAGE_TERMS = [
    "c est quoi", "c'est quoi", "guide", "etude", "étude", "publie", "modeles socio economiques",
    "modèles socio-économiques", "generosite", "générosité", "mediateur", "médiateur", "recense",
    "ressources", "foire aux questions", "faq", "mode d emploi", "mode d'emploi", "faire appel",
    "subventions marches publics", "subventions marchés publics", "aides d etat", "aides d’état",
]

GENERIC_PATHS = {
    "/appels-projets", "/financements-prives", "/programmes-et-financements-europeens",
    "/subventions-marches-publics-et-aides-detat", "/aides-et-appels-a-projets", "/aides/",
}


def normalize(text: str) -> str:
    text = html.unescape(text or "")
    trans = str.maketrans({
        "à": "a", "â": "a", "ä": "a", "á": "a", "ã": "a", "å": "a",
        "ç": "c",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "î": "i", "ï": "i", "í": "i", "ì": "i",
        "ô": "o", "ö": "o", "ó": "o", "ò": "o", "õ": "o",
        "ù": "u", "û": "u", "ü": "u", "ú": "u",
        "ÿ": "y", "ñ": "n", "œ": "oe", "æ": "ae",
        "À": "a", "Â": "a", "Ä": "a", "Á": "a", "Ã": "a", "Å": "a",
        "Ç": "c", "É": "e", "È": "e", "Ê": "e", "Ë": "e",
        "Î": "i", "Ï": "i", "Ô": "o", "Ö": "o", "Ù": "u", "Û": "u", "Ü": "u",
    })
    text = text.translate(trans).lower()
    text = re.sub(r"[^a-z0-9€]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href_stack: list[str | None] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False
        self.meta_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        if tag.lower() == "a":
            self._href_stack.append(self._current_href)
            self._current_href = attrs_dict.get("href")
            self._current_text = []
        elif tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "meta":
            name = (attrs_dict.get("name") or attrs_dict.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.meta_description:
                self.meta_description = attrs_dict.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = clean_space(" ".join(self._current_text))
            self.links.append((self._current_href, text))
            self._current_href = self._href_stack.pop() if self._href_stack else None
            self._current_text = []
        elif tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)
        if self._in_title:
            self.title_parts.append(data)


def fetch(url: str, timeout: int = 25) -> tuple[str, str, str | None]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    contexts = [None, ssl._create_unverified_context()]
    last_error: Exception | None = None
    for context in contexts:
        try:
            with urlopen(req, timeout=timeout, context=context) as response:
                raw = response.read(1_500_000)
                content_type = response.headers.get("Content-Type", "")
                charset_match = re.search(r"charset=([\w.-]+)", content_type, re.I)
                charset = charset_match.group(1) if charset_match else "utf-8"
                return raw.decode(charset, "replace"), response.geturl(), None
        except Exception as exc:  # retry once with unverified SSL
            last_error = exc
    return "", url, f"{type(last_error).__name__}: {last_error}"


def parse_html(doc: str) -> LinkExtractor:
    parser = LinkExtractor()
    try:
        parser.feed(doc)
    except Exception:
        pass
    return parser


def html_to_text(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", doc)
    doc = re.sub(r"(?s)<[^>]+>", " ", doc)
    return clean_space(doc)


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith(("utm_", "mtm_"))]
    parsed = parsed._replace(fragment="", query=urlencode(query, doseq=True))
    return urlunparse(parsed)


def looks_like_candidate(title: str, url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    label_norm = normalize(title)
    haystack = normalize(f"{title} {url}")
    if not haystack or len(label_norm) < 4:
        return False
    if path.endswith(STOP_EXTENSIONS):
        return False
    if path.rstrip("/") in {p.rstrip("/") for p in GENERIC_PATHS}:
        return False
    if any(part in path for part in BAD_PATH_PARTS):
        return False
    if any(term in label_norm for term in GENERIC_LABEL_TERMS):
        return False
    if any(normalize(term) in label_norm for term in INFO_PAGE_TERMS) and not any(t in label_norm for t in ["appel", "fdva", "concours", "prix"]):
        return False
    if "nombre de resultat associe" in label_norm or "nombre de resultats associes" in label_norm:
        return False
    # Facet/search pages are usually not opportunities; keep real detail pages instead.
    if parsed.query and ("f%5b0%5d" in parsed.query.lower() or "f[0]" in parsed.query.lower()):
        return False
    if label_norm in {"aides et appels a projets", "trouver des aides", "actualites", "programmes d aides"}:
        return False
    return any(normalize(term) in haystack for term in ACTION_TERMS)


def extract_first(patterns: Iterable[re.Pattern[str]], text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return clean_space(match.group(0))[:180]
    return ""


def _safe_date(year: int, month: int, day: int) -> date | None:
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_date_values(text: str) -> list[tuple[date, tuple[int, int]]]:
    dates: list[tuple[date, tuple[int, int]]] = []

    for match in DATE_VALUE_PATTERNS[0].finditer(text):
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            dates.append((parsed, match.span()))

    for match in DATE_VALUE_PATTERNS[1].finditer(text):
        parsed = _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if parsed:
            dates.append((parsed, match.span()))

    for match in DATE_VALUE_PATTERNS[2].finditer(text):
        month = MONTHS.get(match.group(2).lower()) or MONTHS.get(normalize(match.group(2)))
        if not month:
            continue
        parsed = _safe_date(int(match.group(3)), month, int(match.group(1)))
        if parsed:
            dates.append((parsed, match.span()))

    # Deduplicate dates while preserving their first local context.
    seen: set[tuple[date, tuple[int, int]]] = set()
    unique: list[tuple[date, tuple[int, int]]] = []
    for item in dates:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def deadline_confidence(context: str) -> int:
    norm = normalize(context)
    if any(term in norm for term in ["date limite", "limite de depot", "cloture", "deadline"]):
        return 3
    if "depot" in norm and any(term in norm for term in ["fixee", "fixe", "jusqu au"]):
        return 3
    if "candidature" in norm and any(term in norm for term in ["avant le", "jusqu au", "cloture"]):
        return 2
    if any(term in norm for term in ["avant le", "jusqu au"]):
        return 1
    return 0


def extract_deadline(text: str) -> tuple[str, str]:
    """Return (human context, ISO date) for the best detected deadline, if any."""
    candidates: list[tuple[int, date, str]] = []

    for pattern in DEADLINE_CONTEXT_PATTERNS:
        for match in pattern.finditer(text):
            context = clean_space(match.group(0))
            confidence = deadline_confidence(context)
            for parsed, _ in find_date_values(context):
                if confidence:
                    candidates.append((confidence, parsed, context))

    if not candidates:
        normalized_hints = [normalize(hint) for hint in DEADLINE_HINTS]
        for parsed, span in find_date_values(text):
            start = max(0, span[0] - 170)
            end = min(len(text), span[1] + 90)
            context = clean_space(text[start:end])
            if any(hint in normalize(context) for hint in normalized_hints):
                confidence = deadline_confidence(context) or 1
                candidates.append((confidence, parsed, context))

    if not candidates:
        return "", ""

    confidence, parsed, context = max(candidates, key=lambda item: (item[0], item[1]))
    return context[:180], parsed.isoformat()


def has_stale_year_hint(text: str, today: date) -> bool:
    norm = normalize(text)
    if not any(term in norm for term in ["appel", "concours", "candidature", "reglement", "projet"]):
        return False
    years = [int(y) for y in re.findall(r"\b20\d{2}\b", text)]
    return bool(years) and max(years) < today.year


def deadline_status(deadline_iso: str, title_and_url: str, today: date) -> str:
    if deadline_iso:
        try:
            parsed = date.fromisoformat(deadline_iso)
        except ValueError:
            return "à vérifier"
        return "expiré" if parsed < today else "ouvert"
    if has_stale_year_hint(title_and_url, today):
        return "probablement expiré (année passée)"
    return "à vérifier"


def is_expired_status(status: str) -> bool:
    return status.startswith("expiré") or status.startswith("probablement expiré")


def fetch_search_page(url: str, timeout: int = 6) -> str:
    req = Request(url, headers={"User-Agent": SEARCH_USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(req, timeout=timeout) as response:
        raw = response.read(600_000)
        content_type = response.headers.get("Content-Type", "")
        charset_match = re.search(r"charset=([\w.-]+)", content_type, re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        return raw.decode(charset, "replace")


def unwrap_search_url(href: str) -> str:
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return params.get("uddg", href)
    if "yahoo.com" in parsed.netloc and "/RU=" in href:
        # Yahoo redirect format embeds the target between /RU= and /RK=.
        try:
            target = href.split("/RU=", 1)[1].split("/RK=", 1)[0]
            return unquote(target)
        except Exception:
            return href
    return href


def is_search_result_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        return False
    host = parsed.netloc.lower()
    if any(part in host for part in SEARCH_BLACKLIST_HOST_PARTS):
        return False
    if parsed.path.lower().endswith(STOP_EXTENSIONS + (".pdf", ".doc", ".docx", ".xls", ".xlsx")):
        return False
    return True


def search_deadline_web(query: str, max_results: int = 6) -> list[tuple[str, str]]:
    """Return (title, url) results from search engines using raw HTTP fallbacks."""
    engines = [
        "https://lite.duckduckgo.com/lite/?q=" + quote(query),
        "https://search.yahoo.com/search?p=" + quote(query),
    ]
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for engine_url in engines:
        try:
            doc = fetch_search_page(engine_url)
        except Exception:
            continue
        parser = parse_html(doc)
        for href, label in parser.links:
            title = clean_space(label)
            url = canonical_url(unwrap_search_url(href))
            if not title or not is_search_result_url(url) or url in seen:
                continue
            # Drop engine navigation / cached labels.
            title_norm = normalize(title)
            if title_norm in {"images", "videos", "news", "next", "next page", "search"}:
                continue
            seen.add(url)
            results.append((title, url))
            if len(results) >= max_results:
                return results
        if results:
            return results[:max_results]
    return results


def deadline_queries(row: "Opportunity") -> list[str]:
    title = re.split(r"\s+[|–-]\s+", row.titre, maxsplit=1)[0]
    title = clean_space(title.replace("| Associations.gouv.fr", ""))
    base_terms = "date limite candidature clôture deadline"
    year_terms = f"{date.today().year} {date.today().year + 1}"
    # One focused query is intentional: multiple broad variants are slow and can pull unrelated deadlines.
    return [f"{title} {row.organisme} {base_terms} {year_terms}"]


def relevance_tokens(row: "Opportunity") -> list[str]:
    stop = {
        "appel", "appels", "projet", "projets", "concours", "prix", "aide", "aides", "fonds", "dotation",
        "subvention", "financement", "financements", "mecenat", "fondation", "association", "associations",
        "prive", "prives", "gouv", "france", "ville", "paris", "region", "ile", "franciliens", "territoires", "service",
        "public", "actualites", "actualite", "candidature", "candidatures", "date", "limite", "cloture",
        "deadline", "soutien", "programme", "programmes", "europe", "europeen", "europeenne",
    }
    tokens = []
    source_texts = [f"{row.titre} {row.organisme}", re.sub(r"[’']", "", f"{row.titre} {row.organisme}")]
    for source_text in source_texts:
        for token in re.findall(r"[a-z0-9]{3,}", normalize(source_text)):
            if token in stop:
                continue
            tokens.append(token)
    return list(dict.fromkeys(tokens))[:8]


def is_relevant_deadline_result(row: "Opportunity", title: str, url: str, extra_context: str = "") -> bool:
    haystack = normalize(f"{title} {url} {extra_context[:1200]}")
    tokens = relevance_tokens(row)
    if tokens:
        return any(token in haystack for token in tokens)

    # If no distinctive token exists, accept same-domain results as a conservative fallback.
    source_host = urlparse(row.lien).netloc.lower().removeprefix("www.")
    result_host = urlparse(url).netloc.lower().removeprefix("www.")
    if source_host and (result_host == source_host or result_host.endswith("." + source_host)):
        return True

    return False


def update_deadline_from_context(row: "Opportunity", context: str, source_url: str, note_label: str) -> bool:
    deadline, deadline_iso = extract_deadline(context[:80_000])
    if not deadline_iso:
        return False
    row.deadline = deadline
    row.deadline_date = deadline_iso
    row.deadline_status = deadline_status(deadline_iso, f"{row.titre} {row.lien}", date.today())
    row.deadline_source = source_url
    note = f"deadline via recherche web ({note_label}): {source_url}"
    row.notes = ((row.notes + "; ") if row.notes else "") + note
    row.notes = row.notes[:500]
    return True


def resolve_deadline_with_web(row: "Opportunity", max_results: int = 8) -> bool:
    """Use web search + result fetching to resolve rows still marked 'à vérifier'."""
    fetched_pages = 0
    max_pages = 5 if row.priorite.startswith("A") else max(1, min(max_results, 2))
    for query in deadline_queries(row):
        results = search_deadline_web(query, max_results=max_results)
        # Some search-result titles already contain the deadline.
        for result_title, result_url in results:
            if not is_relevant_deadline_result(row, result_title, result_url):
                continue
            if update_deadline_from_context(row, result_title, result_url, "titre résultat"):
                return True
        for result_title, result_url in results:
            if fetched_pages >= max_pages:
                break
            if not is_relevant_deadline_result(row, result_title, result_url):
                continue
            fetched_pages += 1
            try:
                doc, final_url, err = fetch(result_url, timeout=3)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                doc = ""
                final_url = result_url
            if err or not doc:
                continue
            parser = parse_html(doc)
            fetched_title = clean_space(" ".join(parser.title_parts)) or result_title
            text = html_to_text(doc)
            context = f"{fetched_title}\n{result_title}\n{text}"
            if update_deadline_from_context(row, context, final_url or result_url, "page résultat"):
                return True
    row.deadline_status = "non trouvée après recherche web"
    if row.priorite.startswith(("A", "B")):
        row.priorite = "C - veille"
    row.prochaine_action = "Surveiller / vérifier manuellement si stratégique : aucune deadline fiable trouvée après recherche web"
    row.notes = (((row.notes + "; ") if row.notes else "") + "deadline recherchée sur le web: non trouvée")[:500]
    return False


def resolve_unknown_deadlines_with_web(rows: list["Opportunity"], limit: int, max_results: int) -> tuple[int, int]:
    checked = 0
    resolved = 0
    for row in rows:
        if checked >= limit:
            break
        if row.deadline_status != "à vérifier":
            continue
        checked += 1
        if resolve_deadline_with_web(row, max_results=max_results):
            resolved += 1
    return checked, resolved


def find_themes(text: str) -> str:
    norm = normalize(text)
    themes = []
    for theme, terms in THEME_RULES:
        if any(normalize(term) in norm for term in terms):
            themes.append(theme)
    return "; ".join(dict.fromkeys(themes))


def find_type(text: str) -> str:
    norm = normalize(text)
    for typ, terms in TYPE_RULES:
        if any(normalize(term) in norm for term in terms):
            return typ
    return "à qualifier"


def score_candidate(text: str, source_type: str = "") -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    norm = normalize(text)

    for term, points in POSITIVE_TERMS.items():
        if normalize(term) in norm:
            score += points
            if points >= 4:
                reasons.append(term)
    for term, points in LOCATION_TERMS.items():
        if normalize(term) in norm:
            score += points
    for term, points in DEADLINE_TERMS.items():
        if normalize(term) in norm:
            score += points
    if re.search(r"\d[\d\s\.]{2,}\s?(?:€|euros?)", text, re.I):
        score += 3
        reasons.append("montant détecté")
    for term, points in NEGATIVE_TERMS.items():
        if normalize(term) in norm:
            score += points
            reasons.append(f"malus: {term}")
    if source_type == "foundation":
        score += 3
    if source_type == "public":
        score += 1
    return score, list(dict.fromkeys(reasons))[:8]


def priority(score: int) -> str:
    # Calibrated for this watcher: source pages are keyword-rich, so low scores are only weak signals.
    if score >= 50:
        return "A - à traiter vite"
    if score >= 30:
        return "B - intéressant"
    if score >= 18:
        return "C - veille"
    return "D - bruit"


@dataclass
class Opportunity:
    id: str
    date_detection: str
    priorite: str
    score: int
    statut: str
    titre: str
    organisme: str
    type_financement: str
    zone: str
    themes: str
    montant: str
    deadline: str
    deadline_date: str
    deadline_status: str
    deadline_source: str
    lien: str
    eligibilite_ambition_campus: str
    pieces_probables: str
    responsable: str
    prochaine_action: str
    notes: str


def make_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def snippet_around(text: str, needles: list[str], max_len: int = 360) -> str:
    norm_text = normalize(text)
    pos = -1
    for needle in needles:
        n = normalize(needle)
        if n and n in norm_text:
            # approximate position in original by searching unaccented impossible; fallback to first word
            first = needle.split()[0]
            pos = text.lower().find(first.lower())
            break
    if pos < 0:
        return clean_space(text[:max_len])
    start = max(0, pos - max_len // 3)
    return clean_space(text[start:start + max_len])


def collect_source(source: dict[str, str], max_links: int, fetch_details: bool) -> tuple[list[Opportunity], str | None]:
    page, final_url, error = fetch(source["url"])
    if error:
        return [], error

    parsed = parse_html(page)
    page_title = clean_space(" ".join(parsed.title_parts)) or source["name"]
    page_text = html_to_text(page)
    base = final_url or source["url"]

    raw_candidates: dict[str, str] = {}

    # Source/listing pages are used to discover links, not treated as opportunities themselves.
    for href, label in parsed.links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = canonical_url(urljoin(base, href))
        # Avoid jumping to unrelated social/platform links.
        host = urlparse(full).netloc.lower()
        if any(bad in host for bad in ["facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com", "youtube.com"]):
            continue
        label = clean_space(label) or Path(urlparse(full).path).stem.replace("-", " ")
        if looks_like_candidate(label, full):
            raw_candidates.setdefault(full, label)
        if len(raw_candidates) >= max_links:
            break

    opportunities: list[Opportunity] = []
    for url, link_title in raw_candidates.items():
        detail_text = ""
        detail_title = link_title
        detail_error = ""
        if fetch_details:
            doc, _, err = fetch(url, timeout=18)
            if err:
                detail_error = err
            else:
                p = parse_html(doc)
                fetched_title = clean_space(" ".join(p.title_parts))
                detail_title = fetched_title if fetched_title and len(fetched_title) > 6 else link_title
                detail_text = html_to_text(doc)
        # Score mostly on the candidate page title + early content. Full listing pages contain too many unrelated cards
        # and would inflate every link.
        focused_detail = (detail_text or page_text[:1200])[:2500]
        combined = f"{detail_title}\n{link_title}\n{focused_detail}"
        deadline_source = f"{detail_title}\n{link_title}\n{detail_text or page_text}"
        score, reasons = score_candidate(combined, source.get("source_type", ""))
        notes = "; ".join(reasons)
        if detail_error:
            notes = (notes + "; " if notes else "") + f"détail non chargé: {detail_error[:80]}"
        deadline, deadline_iso = extract_deadline(deadline_source[:80_000])
        status = deadline_status(deadline_iso, f"{detail_title} {link_title} {url}", date.today())
        amount = extract_first(AMOUNT_PATTERNS, combined)
        themes = find_themes(combined)
        typ = find_type(combined)
        elig = "Probable si association loi 1901 éligible; vérifier règlement" if score >= 7 else "À qualifier"
        action = "Lire le règlement + confirmer éligibilité + noter deadline" if score >= 14 else "Surveiller / qualifier rapidement"
        opportunities.append(Opportunity(
            id=make_id(url),
            date_detection=date.today().isoformat(),
            priorite=priority(score),
            score=score,
            statut="nouveau",
            titre=detail_title[:220],
            organisme=source["name"],
            type_financement=typ,
            zone=source.get("geo", ""),
            themes=themes,
            montant=amount,
            deadline=deadline,
            deadline_date=deadline_iso,
            deadline_status=status,
            deadline_source=url if deadline_iso else "",
            lien=url,
            eligibilite_ambition_campus=elig,
            pieces_probables="statuts; RIB; budget prévisionnel; rapport d'activité; présentation projet",
            responsable="à assigner",
            prochaine_action=action,
            notes=notes[:500],
        ))
    return opportunities, None


def read_sources() -> list[dict[str, str]]:
    with SOURCES_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(ids: Iterable[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(set(ids)), ensure_ascii=False, indent=2), encoding="utf-8")


def write_outputs(rows: list[Opportunity], errors: list[str]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    csv_path = OUTPUT_DIR / f"{today}_opportunities.csv"
    md_path = OUTPUT_DIR / f"{today}_digest.md"

    fieldnames = list(asdict(rows[0]).keys()) if rows else [
        "id", "date_detection", "priorite", "score", "statut", "titre", "organisme", "type_financement", "zone", "themes", "montant", "deadline", "deadline_date", "deadline_status", "deadline_source", "lien", "eligibilite_ambition_campus", "pieces_probables", "responsable", "prochaine_action", "notes"
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    buckets = ["A - à traiter vite", "B - intéressant", "C - veille", "D - bruit"]
    lines = [f"# Veille financements Ambition Campus — {today}", ""]
    lines.append(f"Résultat: {len(rows)} opportunité(s) candidate(s) détectée(s).")
    lines.append("")
    if errors:
        lines.append("## Sources à corriger / indisponibles")
        for err in errors[:20]:
            lines.append(f"- {err}")
        lines.append("")
    for bucket in buckets:
        subset = [r for r in rows if r.priorite == bucket]
        if not subset:
            continue
        icon = {"A - à traiter vite": "🔥", "B - intéressant": "🟡", "C - veille": "👀", "D - bruit": "🗑️"}[bucket]
        lines.append(f"## {icon} {bucket} ({len(subset)})")
        for i, r in enumerate(subset[:25], 1):
            extra = []
            if r.deadline_date:
                extra.append(f"deadline: {r.deadline_date}")
            elif r.deadline:
                extra.append(f"deadline: {r.deadline}")
            if r.deadline_status and r.deadline_status != "ouvert":
                extra.append(f"statut deadline: {r.deadline_status}")
            if r.montant:
                extra.append(f"montant: {r.montant}")
            if r.themes:
                extra.append(f"thèmes: {r.themes}")
            extra_txt = " — " + " ; ".join(extra) if extra else ""
            lines.append(f"{i}. **{r.titre}** — {r.organisme} — score {r.score}{extra_txt}")
            lines.append(f"   - Lien: {r.lien}")
            if r.deadline_source and r.deadline_source != r.lien:
                lines.append(f"   - Source deadline: {r.deadline_source}")
            lines.append(f"   - Action: {r.prochaine_action}")
            if r.notes:
                lines.append(f"   - Notes: {r.notes}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=int, default=5, help="minimum score kept in CSV/digest")
    parser.add_argument("--max-links-per-source", type=int, default=80)
    parser.add_argument("--max-detail", type=int, default=250, help="max candidate pages to fetch for detail")
    parser.add_argument("--mark-seen", action="store_true", help="persist detected IDs so future runs only show new ones")
    parser.add_argument("--include-seen", action="store_true", help="include previously seen opportunities")
    parser.add_argument("--include-expired", action="store_true", help="keep opportunities whose detected deadline is already past")
    parser.add_argument("--no-web-deadline-check", action="store_true", help="skip web search fallback for deadlines still marked 'à vérifier'")
    parser.add_argument("--max-web-deadline-checks", type=int, default=60, help="max 'à vérifier' rows to investigate with web search")
    parser.add_argument("--max-web-results", type=int, default=8, help="max search results fetched per deadline query")
    args = parser.parse_args(argv)

    sources = read_sources()
    seen = load_seen()
    all_rows: list[Opportunity] = []
    errors: list[str] = []
    detail_budget = args.max_detail

    for source in sources:
        fetch_details = detail_budget > 0
        rows, error = collect_source(source, args.max_links_per_source, fetch_details)
        if fetch_details:
            detail_budget -= min(detail_budget, len(rows))
        if error:
            errors.append(f"{source['name']}: {error}")
        all_rows.extend(rows)

    # First dedupe by URL/id, keep highest score. Then investigate unknown deadlines online.
    deduped: dict[str, Opportunity] = {}
    for row in all_rows:
        if row.score < args.min_score:
            continue
        if not args.include_seen and row.id in seen:
            continue
        prev = deduped.get(row.id)
        if prev is None or row.score > prev.score:
            deduped[row.id] = row

    candidates = sorted(deduped.values(), key=lambda r: (r.priorite, -r.score, r.deadline_date or "9999-12-31"))
    web_checked = 0
    web_resolved = 0
    if not args.no_web_deadline_check:
        web_checked, web_resolved = resolve_unknown_deadlines_with_web(
            candidates,
            limit=args.max_web_deadline_checks,
            max_results=args.max_web_results,
        )

    expired_count = 0
    rows: list[Opportunity] = []
    for row in candidates:
        if not args.include_expired and is_expired_status(row.deadline_status):
            expired_count += 1
            continue
        rows.append(row)
    rows = sorted(rows, key=lambda r: (r.priorite, -r.score, r.deadline_date or "9999-12-31"))
    csv_path, md_path = write_outputs(rows, errors)

    if args.mark_seen:
        save_seen(seen | {r.id for r in rows})

    print(f"CSV: {csv_path}")
    print(f"Digest: {md_path}")
    print(f"Kept: {len(rows)} candidates")
    if not args.no_web_deadline_check:
        print(f"Web deadline checks: {web_checked}, resolved: {web_resolved}")
    if expired_count:
        print(f"Expired hidden: {expired_count}")
    if errors:
        print(f"Source errors: {len(errors)}", file=sys.stderr)
        for err in errors[:10]:
            print(f"- {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
