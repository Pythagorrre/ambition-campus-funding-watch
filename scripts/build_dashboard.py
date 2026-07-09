#!/usr/bin/env python3
"""Build a self-contained local HTML dashboard for Ambition Campus funding watch."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import ssl
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import quote, urljoin
from urllib.error import URLError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "outputs"
DASHBOARD_DIR = BASE_DIR / "dashboard"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"
MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
DEADLINE_CUES = [
    "date limite de dépôt des dossiers", "date limite", "limite de dépôt",
    "dépôt des demandes ouvert", "dépôt des candidatures", "clôture", "candidature",
    "candidatures", "jusqu'au", "jusqu’au", "avant le", "deadline",
]
STOP_AFTER_CUES = [
    "Pour en savoir plus", "Partager", "Copier", "Votre avis nous intéresse",
    "Comité de sélection", "Vote en conseil", "Prochaine session", "Pour toute question",
    "Ces informations vous", "Restez connecté", "Formulaire",
]
WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ][0-9A-Za-zÀ-ÖØ-öø-ÿ’'/-]*")
MONEY_RE = re.compile(
    r"(?:\b\d{1,3}(?:[\s\u00a0.]\d{3})*(?:[,.]\d+)?\s*(?:€|euros?\b)|\b\d+(?:[,.]\d+)?\s*%)",
    re.I,
)
AMOUNT_LABEL_RE = re.compile(
    r"\b(?:Montant(?:\s+de\s+l[’']aide)?|Aide(?:\s+régionale)?|Subvention|Dotation|Prix|Financement|Budget|Plafond(?:\s+de\s+l[’']aide)?|Taux\s+d[’']intervention)\s*:\s*",
    re.I,
)
AMOUNT_STOP_RE = re.compile(
    r"\b(?:Date\s+limite|Domaine\s+d[’']action|Type\s+de\s+projet|Bailleur|Public|Bénéficiaires|Éligibilité|Eligibilité|Critères|Modalités|Calendrier|Candidature|Contact|Pour\s+plus\s+d[’']informations|Partager|Description|Documents|Règlement|Conditions)\s*:?",
    re.I,
)


def all_csvs() -> list[Path]:
    # Tri par nom de fichier = tri chronologique (préfixe YYYY-MM-DD)
    files = sorted(OUTPUT_DIR.glob("*_opportunities.csv"))
    if not files:
        raise FileNotFoundError(f"No opportunity CSV found in {OUTPUT_DIR}")
    return files


def read_rows(paths: list[Path], include_expired: bool = False) -> list[dict[str, Any]]:
    # Agrège tous les CSV hebdo : une même opportunité (même lien) revue lors
    # d'un run ultérieur remplace l'ancienne ligne (deadline/score plus frais).
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("lien") or "").strip() or f"{row.get('titre', '')}|{row.get('organisme', '')}"
                merged[key] = row
    rows = list(merged.values())
    for row in rows:
        try:
            row["score_num"] = int(float(row.get("score") or 0))
        except ValueError:
            row["score_num"] = 0
        row["themes_list"] = [t.strip() for t in (row.get("themes") or "").split(";") if t.strip()]
        parsed_deadline = deadline_date_from_text(row.get("deadline", ""))
        if parsed_deadline:
            row["deadline_date"] = parsed_deadline
    if not include_expired:
        today = date.today().isoformat()
        rows = [r for r in rows if not ((r.get("deadline_date") or "").strip() and (r.get("deadline_date") or "").strip() < today)]
    enrich_source_details(rows)
    return rows


MONTH_MAP_FR = {m: i + 1 for i, m in enumerate(MONTHS_FR)}
MONTH_MAP_FR.update({"fevrier": 2, "aout": 8, "decembre": 12})
FRENCH_DATE_RE = re.compile(
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4})",
    re.I,
)


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def html_to_text(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", doc)
    doc = re.sub(r"(?s)<[^>]+>", " ", doc)
    return clean_space(doc)


def fetch_page_html(url: str, timeout: int = 7) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    # Comme funding_watch : certains environnements (Python macOS notamment) n'ont
    # pas de magasin de certificats configuré ; on retente sans vérification.
    last_error: Exception | None = None
    for context in (None, ssl._create_unverified_context()):
        try:
            with urlopen(req, timeout=timeout, context=context) as response:
                raw = response.read(1_500_000)
                content_type = response.headers.get("Content-Type", "")
                # PDF / binaire : inutilisable en extraction texte (sinon on affiche
                # du charabia « %PDF-1.6 %obj endobj » dans les montants).
                if raw[:5] == b"%PDF-" or (content_type and not re.search(r"html|xml|text/plain", content_type, re.I)):
                    return ""
                charset_match = re.search(r"charset=([\w.-]+)", content_type, re.I)
                charset = charset_match.group(1) if charset_match else "utf-8"
                return raw.decode(charset, "replace")
        except URLError as exc:
            last_error = exc
            if not isinstance(exc.reason, ssl.SSLError):
                raise
    raise last_error if last_error else URLError("fetch failed")


def fetch_page_text(url: str, timeout: int = 7) -> str:
    return html_to_text(fetch_page_html(url, timeout=timeout))


def normalize(text: str) -> str:
    text = html.unescape(text or "").lower()
    trans = str.maketrans({
        "à": "a", "â": "a", "ä": "a", "á": "a", "ã": "a", "å": "a",
        "ç": "c",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "î": "i", "ï": "i", "í": "i", "ì": "i",
        "ô": "o", "ö": "o", "ó": "o", "ò": "o", "õ": "o",
        "ù": "u", "û": "u", "ü": "u", "ú": "u",
        "ÿ": "y", "ñ": "n", "œ": "oe", "æ": "ae",
    })
    text = text.translate(trans)
    text = re.sub(r"[^a-z0-9€]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def deadline_date_from_text(text: str) -> str:
    text = clean_space(text)
    if not text or not any(normalize(cue) in normalize(text) for cue in DEADLINE_CUES):
        return ""
    match = FRENCH_DATE_RE.search(text)
    if not match:
        return ""
    day = int(match.group(1))
    month_key = normalize(match.group(2))
    month = MONTH_MAP_FR.get(month_key)
    year = int(match.group(3))
    if not month:
        return ""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def deadline_date_variants(value: str) -> list[str]:
    if not value:
        return []
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return [value]
    month = MONTHS_FR[d.month - 1]
    return [
        f"{d.day} {month} {d.year}",
        f"{d.day:02d} {month} {d.year}",
        f"{d.day:02d}/{d.month:02d}/{d.year}",
        f"{d.day}/{d.month}/{d.year}",
        f"{d.day:02d}-{d.month:02d}-{d.year}",
        f"{d.year}-{d.month:02d}-{d.day:02d}",
    ]


def occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    pattern = re.compile(re.escape(needle), re.I)
    for match in pattern.finditer(text):
        spans.append(match.span())
    return spans


def relevance_tokens(row: dict[str, Any]) -> list[str]:
    stop = {
        "appel", "appels", "projet", "projets", "date", "limite", "depot", "dossiers",
        "candidature", "candidatures", "jusqu", "avant", "deadline", "ouvert", "ouverte",
        "ville", "paris", "region", "ile", "france", "associations", "gouv", "pages",
    }
    source = f"{row.get('titre', '')} {row.get('deadline', '')}"
    tokens = []
    for token in re.findall(r"[a-z0-9]{4,}", normalize(source)):
        if token not in stop:
            tokens.append(token)
    return list(dict.fromkeys(tokens))[:16]


def phrase_from_words(text: str, max_words: int, *, tail: bool = False) -> str:
    matches = list(WORD_RE.finditer(text))
    if not matches:
        return clean_space(text[:90])
    chosen = matches[-max_words:] if tail else matches[:max_words]
    return clean_space(text[chosen[0].start():chosen[-1].end()])


def best_deadline_span(row: dict[str, Any], text: str) -> tuple[int, int] | None:
    date_needles = deadline_date_variants(row.get("deadline_date", ""))
    deadline = clean_space(row.get("deadline", ""))

    def collect_candidates(needles: list[str]) -> list[tuple[int, int, int]]:
        found: list[tuple[int, int, int]] = []
        norm_tokens = relevance_tokens(row)
        for needle in dict.fromkeys(n for n in needles if n):
            for start, end in occurrences(text, needle):
                window = text[max(0, start - 260):min(len(text), end + 420)]
                norm_window = normalize(window)
                score = sum(1 for token in norm_tokens if token in norm_window)
                score += 3 if any(normalize(cue) in norm_window for cue in DEADLINE_CUES) else 0
                found.append((score, start, end))
        return found

    # Prefer actual parsed dates. Only fall back to arbitrary scraped chunks when no date is present/found.
    candidates = collect_candidates(date_needles)
    if not candidates and deadline:
        fallback_needles = [phrase_from_words(deadline, 5), phrase_from_words(deadline, 5, tail=True)]
        candidates = collect_candidates(fallback_needles)

    if not candidates:
        return None
    _, start, end = max(candidates, key=lambda item: item[0])
    return start, end


def phrase_occurrences(text: str, phrase: str) -> list[tuple[int, int]]:
    if not phrase:
        return []
    return [m.span() for m in re.finditer(re.escape(phrase), text, flags=re.I)]


def phrase_needs_prefix(text: str, phrase: str, target_start: int) -> bool:
    """Text fragments pair the first matching start with a later end; repeated starts need a prefix."""
    return sum(1 for start, _ in phrase_occurrences(text, phrase) if start < target_start) > 0


def label_stripped_start(page_text: str, cue_start: int, date_start: int) -> int:
    """Drop ultra-generic labels like 'Date limite :' but keep useful labels like 'Date limite de dépôt…'."""
    intro = page_text[cue_start:date_start]
    match = re.match(r"\s*date\s+limite\s*:\s*", intro, flags=re.I)
    if match:
        return cue_start + match.end()
    return cue_start


def short_end_anchor(page_text: str, date_end: int) -> str:
    """Pick a short, stable end anchor after the deadline context."""
    after = page_text[date_end:min(len(page_text), date_end + 700)]

    # On structured pages, the next field label is usually the cleanest range endpoint.
    for pattern in [r"\bMontant\s*:\s*", r"\bBudget\s*:\s*", r"\bFinancement\s*:\s*"]:
        match = re.search(pattern, after, flags=re.I)
        if match and 10 <= match.start() <= 520:
            return phrase_from_words(after[match.start():], 2)

    # Cut before obvious UI/navigation chunks before trying sentence detection.
    end_pos = min(len(after), 260)
    after_norm = normalize(after)
    for marker in STOP_AFTER_CUES:
        marker_norm = normalize(marker)
        marker_pos = after_norm.find(marker_norm)
        if marker_pos < 0:
            continue
        # Convert approximate normalized position back by finding the literal marker when possible.
        literal_pos = after.lower().find(marker.lower())
        if literal_pos >= 0:
            marker_pos = literal_pos
        if 5 <= marker_pos < end_pos:
            end_pos = marker_pos
    snippet = after[:end_pos]

    # Prefer the first natural sentence inside the non-UI region.
    sentence = re.search(r"[.!?](?:\s|$)", snippet)
    if sentence and sentence.start() >= 25:
        snippet = snippet[:sentence.end()]

    # Avoid ending inside a word when we only used the fallback length cap.
    if end_pos == 260 and end_pos < len(after):
        word_matches = list(WORD_RE.finditer(snippet))
        # Si le texte continue sans coupure après le cap, le dernier « mot » du
        # snippet est tronqué (ex. « arrondi » pour « arrondissements ») : on le
        # retire, sinon le fragment ne matche rien et rien n'est surligné.
        if word_matches and word_matches[-1].end() == len(snippet) and WORD_RE.match(after[end_pos]):
            word_matches.pop()
        if not word_matches:
            return ""
        snippet = snippet[:word_matches[-1].end()]
    return phrase_from_words(snippet, 4, tail=True) if snippet.strip() else ""


def make_deadline_fragment(row: dict[str, Any], source_url: str, page_text: str) -> str | None:
    span = best_deadline_span(row, page_text)
    if not span:
        return None
    date_start, date_end = span
    before = page_text[max(0, date_start - 220):date_start]
    before_norm = normalize(before)
    cue_start = date_start
    for cue in DEADLINE_CUES:
        cue_norm = normalize(cue)
        idx = before_norm.rfind(cue_norm)
        if idx >= 0:
            # Approximate is enough to find the same cue in the original local slice.
            original_idx = max(before.lower().rfind(cue.lower()), 0)
            if original_idx == 0 and cue.lower() not in before.lower():
                continue
            cue_start = max(0, date_start - len(before) + original_idx)
            break

    content_start = label_stripped_start(page_text, cue_start, date_start)

    # Inclure l'heure éventuelle qui suit la date (« … 2026 à 23H59 »).
    time_tail = re.match(r"\s*(?:à|a)\s*\d{1,2}\s*[hH:]\s*(?:\d{2})?", page_text[date_end:date_end + 24])
    if time_tail:
        date_end += time_tail.end()

    # Un fragment exact « cue + date » est bien plus fiable qu'une plage début,fin
    # dont la borne de fin peut tomber dans un autre bloc de la page.
    exact_text = clean_space(page_text[content_start:date_end])
    if exact_text and len(exact_text) <= 250:
        return source_url.split("#")[0] + "#:~:text=" + quote(exact_text, safe="")

    start_text = phrase_from_words(page_text[content_start:min(len(page_text), content_start + 160)], 3)
    end_text = short_end_anchor(page_text, date_end)
    if not start_text:
        return None
    if end_text and normalize(end_text) in normalize(start_text):
        end_text = ""

    prefix_text = ""
    if phrase_needs_prefix(page_text, start_text, content_start):
        prefix_slice = page_text[max(0, content_start - 120):content_start]
        prefix_text = phrase_from_words(prefix_slice, 4, tail=True) if prefix_slice.strip() else ""

    components = []
    if prefix_text:
        components.append(quote(prefix_text, safe="") + "-")
    components.append(quote(start_text, safe=""))
    fragment = ",".join(components)
    if end_text:
        fragment += "," + quote(end_text, safe="")
    return source_url.split("#")[0] + "#:~:text=" + fragment


def fallback_deadline_fragment(row: dict[str, Any], source_url: str, page_text: str = "") -> str:
    deadline = clean_space(row.get("deadline", ""))
    if not deadline:
        return source_url

    def anchored(fragment_text: str) -> str:
        # Si on a pu lire la page et que le texte visé n'y figure pas (contenu
        # JS, page modifiée…), un fragment mort n'apporte rien : lien nu.
        if page_text and normalize(fragment_text) not in normalize(page_text):
            return source_url
        return source_url.split("#")[0] + "#:~:text=" + quote(fragment_text, safe="")

    # Le champ deadline du CSV est une capture tronquée à taille fixe : son
    # dernier mot est souvent coupé (« dans les arrondi » ne matche rien).
    # On préfère un fragment exact arrêté juste après la date + heure éventuelle.
    date_match = FRENCH_DATE_RE.search(deadline)
    if date_match:
        end = date_match.end()
        time_tail = re.match(r"\s*(?:à|a)\s*\d{1,2}\s*[hH:]\s*(?:\d{2})?", deadline[end:end + 24])
        if time_tail:
            end += time_tail.end()
        exact = clean_space(re.sub(r"^\s*date\s+limite\s*:\s*", "", deadline[:end], flags=re.I))
        if exact:
            return anchored(exact)
    start_text = phrase_from_words(re.sub(r"^\s*date\s+limite\s*:\s*", "", deadline, flags=re.I), 3)
    # Pas de date détectée : plage début,fin en écartant le dernier mot
    # (potentiellement tronqué) de l'ancre de fin.
    words = list(WORD_RE.finditer(deadline))
    end_text = ""
    if len(words) >= 7:
        end_text = clean_space(deadline[words[-4].start():words[-2].end()])
    if not end_text or normalize(end_text) in normalize(start_text):
        return anchored(start_text)
    if page_text:
        page_norm = normalize(page_text)
        if normalize(start_text) not in page_norm or normalize(end_text) not in page_norm:
            return source_url
    return source_url.split("#")[0] + "#:~:text=" + quote(start_text, safe="") + "," + quote(end_text, safe="")


def has_money_or_percent(text: str) -> bool:
    return bool(MONEY_RE.search(text or ""))


def amount_score(text: str, *, labelled: bool = False) -> int:
    norm = normalize(text)
    if not has_money_or_percent(text):
        return 0
    score = 8 if labelled else 0
    euro_hits = re.findall(r"(?:€|euros?)", text, flags=re.I)
    money_hits = MONEY_RE.findall(text)
    if euro_hits:
        score += 4
    if len(money_hits) >= 2:
        score += 4
    if re.search(r"\b(?:entre|de)\b[^.]{0,80}\b(?:et|à|a)\b", text, flags=re.I):
        score += 3
    if "%" in text and not euro_hits:
        score -= 3
    for keyword in [
        "montant", "aide", "subvention", "financement", "dotation", "plafond", "plafonnee",
        "prise en charge", "budget", "prix", "cofinancement", "taux", "entre", "jusqu",
    ]:
        if keyword in norm:
            score += 2
    # Penalize contexts that look like user-interface noise, not funding amount.
    for keyword in ["cookie", "commentaire", "newsletter", "partager", "mentions legales", "redevance", "occupation du domaine public", "arrete tarifaire", "tarifaire", "euros jour"]:
        if keyword in norm:
            score -= 8
    return score


def trim_to_word_boundary(text: str, max_len: int = 280) -> str:
    text = clean_space(text)
    if len(text) <= max_len:
        return text
    snippet = text[:max_len]
    words = list(WORD_RE.finditer(snippet))
    if words:
        snippet = snippet[:words[-1].end()]
    return clean_space(snippet.rstrip(" ,;:-")) + "…"


def clean_amount_text(segment: str) -> str:
    segment = clean_space(segment)
    segment = AMOUNT_LABEL_RE.sub("", segment, count=1).strip(" :–-•")
    # Keep at most two useful sentences; amount blurbs can be very long.
    sentence_ends = list(re.finditer(r"[.!?](?:\s|$)", segment))
    if sentence_ends:
        first_end = sentence_ends[0].end()
        second_end = sentence_ends[1].end() if len(sentence_ends) > 1 else first_end
        if first_end >= 45 and has_money_or_percent(segment[:first_end]):
            segment = segment[:first_end]
        else:
            cutoff = second_end if second_end <= 280 else first_end
            if cutoff >= 45:
                segment = segment[:cutoff]
    return trim_to_word_boundary(segment, 280)


def extract_amount_from_text(text: str) -> tuple[str, int, int] | None:
    candidates: list[tuple[int, str, int, int]] = []

    # Best case: a structured "Montant : ..." field.
    for match in AMOUNT_LABEL_RE.finditer(text):
        content_start = match.end()
        window = text[content_start:min(len(text), content_start + 850)]
        stop = AMOUNT_STOP_RE.search(window)
        end = content_start + (stop.start() if stop and stop.start() > 20 else min(len(window), 420))
        segment = text[match.start():end]
        score = amount_score(segment, labelled=True)
        if score > 0:
            candidates.append((score, clean_amount_text(segment), content_start, end))

    # Sentence-level fallback keeps ranges together (e.g. 80%, 50 000€, 5 000€ in one sentence).
    sentence_start = 0
    for sentence_end in [m.end() for m in re.finditer(r"[.!?](?:\s|$)", text)]:
        sentence = text[sentence_start:sentence_end]
        if 25 <= len(sentence) <= 700:
            score = amount_score(sentence, labelled=False)
            if score > 0:
                candidates.append((score + 4, clean_amount_text(sentence), sentence_start, sentence_end))
        sentence_start = sentence_end

    # Fallback: find a money/percentage value surrounded by amount vocabulary.
    if "\ufffd" in text[:400] or re.search(r"%PDF-|/FlateDecode|\bendobj\b", text[:4000]):
        return None
    for money in MONEY_RE.finditer(text):
        start = max(0, money.start() - 220)
        end = min(len(text), money.end() + 240)
        # Ne pas couper un mot aux bornes : un fragment #:~:text= construit sur
        # un mot tronqué ne surligne rien dans le navigateur.
        if start > 0 and WORD_RE.match(text[start]) and WORD_RE.match(text[start - 1]):
            next_space = text.find(" ", start, money.start())
            if next_space >= 0:
                start = next_space + 1
        if end < len(text) and WORD_RE.match(text[end - 1]) and WORD_RE.match(text[end]):
            prev_space = text.rfind(" ", money.end(), end)
            if prev_space >= 0:
                end = prev_space
        context = text[start:end]
        if "\ufffd" in context:
            continue
        score = amount_score(context, labelled=False)
        if score <= 0:
            continue
        # Start near the closest amount keyword before the number, when possible.
        local_before = context[:money.start() - start]
        keyword_matches = list(re.finditer(r"\b(?:aide|subvention|montant|dotation|plafond|plafonnée|financement|budget|entre|jusqu[’']?à)\b", local_before, flags=re.I))
        if keyword_matches:
            start = start + keyword_matches[-1].start()
            context = text[start:end]
        candidates.append((score, clean_amount_text(context), start, end))

    if not candidates:
        return None
    _, amount_text, start, end = max(candidates, key=lambda item: (item[0], len(item[1])))
    if not amount_text:
        return None
    return amount_text, start, end


def fallback_existing_amount(row: dict[str, Any]) -> str:
    value = clean_space(row.get("montant", ""))
    if not value or amount_score(value, labelled=False) <= 0:
        return ""
    # Avoid values that are clearly dates misdetected as amounts.
    if re.search(r"\b(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\b", value, re.I):
        return ""
    return trim_to_word_boundary(value, 180)


def make_amount_fragment(source_url: str, page_text: str, amount_text: str, start: int, end: int) -> str:
    start_text = phrase_from_words(amount_text, 3)
    end_text = phrase_from_words(amount_text, 3, tail=True) if len(amount_text) > len(start_text) + 12 else ""
    prefix_text = ""
    if phrase_needs_prefix(page_text, start_text, start):
        prefix_slice = page_text[max(0, start - 120):start]
        prefix_text = phrase_from_words(prefix_slice, 4, tail=True) if prefix_slice.strip() else ""
    components = []
    if prefix_text:
        components.append(quote(prefix_text, safe="") + "-")
    components.append(quote(start_text, safe=""))
    fragment = ",".join(components)
    if end_text and normalize(end_text) not in normalize(start_text):
        fragment += "," + quote(end_text, safe="")
    return source_url.split("#")[0] + "#:~:text=" + fragment


def source_urls_for_row(row: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("deadline_source", "lien"):
        url = (row.get(key) or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def html_links(doc: str, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", doc, flags=re.I | re.S):
        href = html.unescape(match.group(1)).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        label = clean_space(re.sub(r"(?s)<[^>]+>", " ", match.group(2)))
        if not label:
            label = url
        links.append((label, url))
    return links


def detail_link_tokens(row: dict[str, Any]) -> list[str]:
    stop = {
        "appel", "appels", "projet", "projets", "ville", "paris", "date", "limite",
        "depot", "dossiers", "candidature", "candidatures", "source", "statut", "ouvert",
        "mercredi", "jeudi", "mardi", "samedi", "dimanche", "avant", "pour", "dans",
    }
    source = f"{row.get('titre', '')} {row.get('deadline', '')} {row.get('notes', '')}"
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", normalize(source)) if t not in stop]
    return list(dict.fromkeys(tokens))[:24]


def candidate_detail_links(row: dict[str, Any], source_url: str, doc: str, *, limit: int = 4) -> list[str]:
    source_base = source_url.split("#")[0]
    tokens = detail_link_tokens(row)
    scored: list[tuple[int, str]] = []
    for label, url in html_links(doc, source_url):
        url_base = url.split("#")[0]
        if url_base == source_base:
            continue
        norm = normalize(f"{label} {url}")
        score = sum(1 for token in tokens if token in norm)
        if "/pages/" in url:
            score += 2
        if "appel" in norm or "projet" in norm:
            score += 1
        if score >= 3:
            scored.append((score, url_base))
    # Preserve best unique URLs.
    best: dict[str, int] = {}
    for score, url in scored:
        best[url] = max(score, best.get(url, 0))
    return [url for url, _ in sorted(best.items(), key=lambda item: item[1], reverse=True)[:limit]]


def fetch_cached_text(url: str, page_cache: dict[str, str]) -> str:
    if url not in page_cache:
        try:
            page_cache[url] = fetch_page_text(url)
        except Exception:
            page_cache[url] = ""
    return page_cache[url]


def fetch_cached_html(url: str, html_cache: dict[str, str]) -> str:
    if url not in html_cache:
        try:
            html_cache[url] = fetch_page_html(url)
        except Exception:
            html_cache[url] = ""
    return html_cache[url]


def enrich_source_details(rows: list[dict[str, Any]]) -> None:
    urls = sorted({url for row in rows for url in source_urls_for_row(row)})
    page_cache: dict[str, str] = {}
    html_cache: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(fetch_page_text, url): url for url in urls}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                page_cache[url] = future.result()
            except Exception:
                page_cache[url] = ""

    for row in rows:
        deadline_url = (row.get("deadline_source") or row.get("lien") or "").strip()
        if deadline_url:
            page_text = page_cache.get(deadline_url, "")
            row["deadline_href"] = make_deadline_fragment(row, deadline_url, page_text) if page_text else None
            if not row["deadline_href"]:
                row["deadline_href"] = fallback_deadline_fragment(row, deadline_url, page_text)
        else:
            row["deadline_href"] = ""

        amount_found = ""
        amount_href = ""
        for source_url in source_urls_for_row(row):
            page_text = page_cache.get(source_url, "")
            if not page_text:
                continue
            extracted = extract_amount_from_text(page_text)
            if not extracted:
                continue
            amount_found, start, end = extracted
            amount_href = make_amount_fragment(source_url, page_text, amount_found, start, end)
            break

        if not amount_found:
            for source_url in source_urls_for_row(row):
                doc = fetch_cached_html(source_url, html_cache)
                if not doc:
                    continue
                for detail_url in candidate_detail_links(row, source_url, doc):
                    page_text = fetch_cached_text(detail_url, page_cache)
                    if not page_text:
                        continue
                    extracted = extract_amount_from_text(page_text)
                    if not extracted:
                        continue
                    amount_found, start, end = extracted
                    amount_href = make_amount_fragment(detail_url, page_text, amount_found, start, end)
                    break
                if amount_found:
                    break

        if not amount_found:
            amount_found = fallback_existing_amount(row)
        row["amount_found"] = bool(amount_found)
        row["amount_possible"] = amount_found or "Non indiqué dans la source"
        row["amount_href"] = amount_href


def build_summary(rows: list[dict[str, Any]], csv_path: Path | str) -> dict:
    priority = Counter(r.get("priorite") or "Non classé" for r in rows)
    sources = Counter(r.get("organisme") or "Source inconnue" for r in rows)
    zones = Counter(r.get("zone") or "Non renseigné" for r in rows)
    types = Counter(r.get("type_financement") or "À qualifier" for r in rows)
    themes = Counter()
    for r in rows:
        themes.update(r.get("themes_list", []))
    scores = [r.get("score_num", 0) for r in rows]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "csv_path": str(csv_path),
        "total": len(rows),
        "avg_score": round(mean(scores), 1) if scores else 0,
        "max_score": max(scores) if scores else 0,
        "with_deadline": sum(1 for r in rows if (r.get("deadline_date") or r.get("deadline") or "").strip()),
        "with_amount": sum(1 for r in rows if r.get("amount_found")),
        "priority": priority,
        "sources": sources.most_common(12),
        "zones": zones.most_common(),
        "types": types.most_common(),
        "themes": themes.most_common(14),
    }


AC_FAVICON_URI = "data:image/png;base64,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"


AC_LOGO_SVG = """<svg class="brand-logo" role="img" aria-label="Ambition Campus" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 217.14 44.68"><defs><style>.aclogo-1{fill:#2e3192;}.aclogo-2{fill:none;stroke:#2e3192;stroke-miterlimit:10;}</style></defs><title>Ambition Campus</title><g id="Calque_2" data-name="Calque 2"><g id="Calque_1-2" data-name="Calque 1"><path class="aclogo-1" d="M33.81,16.76,24.6.51a1,1,0,0,0-1.73,0L11.5,19.81.14,39.13A1,1,0,0,0,1,40.64l2.68,0a1,1,0,0,0,.87-.49l9-15.32L23.69,7.62l8.87,15.66c0-.24,0-.47,0-.71A19.42,19.42,0,0,1,33.81,16.76Z"/><path class="aclogo-2" d="M29.66,29.37,24.1,19.44a.5.5,0,0,0-.86,0l-5.82,9.78L11.61,39a.49.49,0,0,0,.42.75l11.38.15L34.78,40a.51.51,0,0,0,.45-.75Z"/><path d="M71.17,31.27H64.38a1,1,0,0,0-.81.43A12,12,0,1,1,62.05,16a1,1,0,0,0,.68.28h7.48a1,1,0,0,0,.87-1.51,20,20,0,1,0,1,18A1,1,0,0,0,71.17,31.27Z"/><path class="aclogo-1" d="M88.71,28H85.13l-.68,1.65H82.62l3.44-7.7h1.76l3.44,7.7H89.39Zm-.56-1.35-1.22-2.95L85.7,26.61Z"/><path class="aclogo-1" d="M100.72,29.61V25L98.44,28.8h-.8l-2.26-3.71v4.52H93.71v-7.7h1.48l2.88,4.79,2.84-4.79h1.46l0,7.7Z"/><path class="aclogo-1" d="M112.56,26.31a1.89,1.89,0,0,1,.37,1.2,1.78,1.78,0,0,1-.79,1.56,4,4,0,0,1-2.29.54h-4v-7.7h3.76a3.66,3.66,0,0,1,2.16.54,1.71,1.71,0,0,1,.75,1.46,1.91,1.91,0,0,1-.27,1,1.8,1.8,0,0,1-.75.69A2,2,0,0,1,112.56,26.31Zm-4.92-3.06v1.82h1.77a1.83,1.83,0,0,0,1-.23.77.77,0,0,0,.34-.68.75.75,0,0,0-.34-.68,1.83,1.83,0,0,0-1-.23ZM110.78,28a.79.79,0,0,0,.36-.72c0-.63-.48-.95-1.42-.95h-2.08v1.9h2.08A2,2,0,0,0,110.78,28Z"/><path class="aclogo-1" d="M115.93,21.91h1.78v7.7h-1.78Z"/><path class="aclogo-1" d="M122.79,23.36h-2.47V21.91H127v1.45h-2.46v6.25h-1.78Z"/><path class="aclogo-1" d="M129.64,21.91h1.78v7.7h-1.78Z"/><path class="aclogo-1" d="M136.46,29.23a3.93,3.93,0,0,1,0-6.93,4.46,4.46,0,0,1,2.17-.52,4.39,4.39,0,0,1,2.15.52,3.9,3.9,0,0,1,1.51,1.42,4,4,0,0,1,.55,2,3.86,3.86,0,0,1-2.06,3.47,4.49,4.49,0,0,1-2.15.51A4.57,4.57,0,0,1,136.46,29.23Zm3.4-1.32a2.32,2.32,0,0,0,.86-.87,2.58,2.58,0,0,0,.31-1.28,2.54,2.54,0,0,0-.31-1.27,2.27,2.27,0,0,0-.86-.88,2.62,2.62,0,0,0-2.47,0,2.27,2.27,0,0,0-.86.88,2.54,2.54,0,0,0-.31,1.27,2.58,2.58,0,0,0,.31,1.28,2.32,2.32,0,0,0,.86.87,2.54,2.54,0,0,0,2.47,0Z"/><path class="aclogo-1" d="M152.89,21.91v7.7h-1.46l-3.84-4.67v4.67h-1.76v-7.7h1.47l3.83,4.68V21.91Z"/><path d="M162.52,29.17a3.73,3.73,0,0,1-1.44-1.4,4.14,4.14,0,0,1,0-4,3.75,3.75,0,0,1,1.45-1.4,4.56,4.56,0,0,1,3.65-.22,3.29,3.29,0,0,1,1.23.84l-.51.51a3.13,3.13,0,0,0-2.28-.91A3.33,3.33,0,0,0,163,23a3.05,3.05,0,0,0-1.17,1.14,3.3,3.3,0,0,0,0,3.26A3,3,0,0,0,163,28.53a3.33,3.33,0,0,0,1.66.42A3.07,3.07,0,0,0,166.9,28l.51.52a3.54,3.54,0,0,1-1.23.84,4.36,4.36,0,0,1-1.59.29A4.21,4.21,0,0,1,162.52,29.17Z"/><path d="M175.43,27.56h-4.29l-.92,2.05h-.85l3.52-7.7h.8l3.52,7.7h-.86Zm-.3-.67-1.84-4.13-1.85,4.13Z"/><path d="M188.14,21.91v7.7h-.79V23.45l-3,5.18h-.39l-3-5.15v6.13h-.78v-7.7h.67l3.34,5.71,3.32-5.71Z"/><path d="M197.49,22.62a2.66,2.66,0,0,1,0,3.86,3.48,3.48,0,0,1-2.31.7h-2.07v2.43h-.82v-7.7h2.89A3.49,3.49,0,0,1,197.49,22.62ZM196.9,26a2,2,0,0,0,0-2.85,2.63,2.63,0,0,0-1.75-.5h-2v3.85h2A2.68,2.68,0,0,0,196.9,26Z"/><path d="M202.56,28.82a3.42,3.42,0,0,1-.84-2.5V21.91h.81v4.38a2.9,2.9,0,0,0,.61,2,2.66,2.66,0,0,0,3.51,0,2.9,2.9,0,0,0,.6-2V21.91h.8v4.41a3.46,3.46,0,0,1-.83,2.5,3.59,3.59,0,0,1-4.66,0Z"/><path d="M212.63,29.4a3,3,0,0,1-1.22-.72l.32-.63a3.17,3.17,0,0,0,1.11.68,4.1,4.1,0,0,0,1.43.25,2.63,2.63,0,0,0,1.55-.37,1.18,1.18,0,0,0,.52-1,1,1,0,0,0-.28-.74,2,2,0,0,0-.69-.42,9.5,9.5,0,0,0-1.13-.32,11.93,11.93,0,0,1-1.39-.43,2.18,2.18,0,0,1-.88-.62A1.64,1.64,0,0,1,211.6,24a1.88,1.88,0,0,1,.31-1.06,2.09,2.09,0,0,1,.94-.77,3.94,3.94,0,0,1,1.59-.28,4.67,4.67,0,0,1,1.29.18,3.65,3.65,0,0,1,1.09.5l-.27.65a3.71,3.71,0,0,0-1-.48,3.77,3.77,0,0,0-1.08-.16,2.5,2.5,0,0,0-1.53.39,1.22,1.22,0,0,0-.51,1,1,1,0,0,0,.28.74,1.81,1.81,0,0,0,.71.43c.28.1.66.2,1.13.32a11.94,11.94,0,0,1,1.37.41,2.34,2.34,0,0,1,.89.62,1.66,1.66,0,0,1,.36,1.12,1.88,1.88,0,0,1-.31,1.06,2.13,2.13,0,0,1-1,.76,4.2,4.2,0,0,1-1.6.28A4.75,4.75,0,0,1,212.63,29.4Z"/></g></g></svg>"""


def json_for_html(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def html_doc(rows: list[dict[str, Any]], summary: dict) -> str:
    data_json = json_for_html({"rows": rows, "summary": summary})
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ambition Campus — Radar financements</title>
  <link rel="icon" type="image/png" href="{AC_FAVICON_URI}" />
  <link rel="apple-touch-icon" href="{AC_FAVICON_URI}" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&family=Playfair+Display:wght@700;800;900&display=swap" rel="stylesheet">
  <style>
    :root {{
      /* Brand */
      --ac-blue: #002A98;
      --ac-cyan: #0FC0E0;
      --ac-blue-secondary: #31559B;
      --ac-blue-light: #4E82C3;
      --ac-ink: #14181F;
      --ac-soft-bg: #EEF2FA;
      /* Neutrals */
      --page-bg: #F6F8FC;
      --surface: #FFFFFF;
      --surface-soft: #F8FAFF;
      --border: rgba(20, 24, 31, 0.10);
      --border-strong: rgba(0, 42, 152, 0.18);
      /* Status */
      --priority-high: #EE2129;
      --priority-high-bg: #FDE8EA;
      --priority-watch: #31559B;
      --priority-watch-bg: #EAF0FF;
      --priority-interesting: #D99A00;
      --priority-interesting-bg: #FFF4D8;
      --success: #0F9B72;
      /* Effects */
      --shadow-card: 0 10px 30px rgba(20, 24, 31, 0.07);
      --shadow-soft: 0 6px 18px rgba(20, 24, 31, 0.05);
      /* Radius */
      --radius-lg: 24px;
      --radius-md: 18px;
      --radius-sm: 999px;
      /* Layout */
      --container: 1180px;
    }}

    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      overflow-x: clip;
      font-family: "Montserrat", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ac-ink);
      background:
        radial-gradient(circle at top right, rgba(15, 192, 224, 0.18), transparent 30%),
        linear-gradient(180deg, #F8FAFF 0%, #EEF2FA 100%);
      min-height: 100vh;
    }}
    a {{ color: inherit; }}
    :focus-visible {{ outline: 3px solid rgba(15, 192, 224, 0.55); outline-offset: 3px; }}

    .app-page {{ padding: 32px 20px 56px; }}
    .app-container {{ max-width: var(--container); margin: 0 auto; }}

    /* ── Hero ─────────────────────────────────────────── */
    .hero-card {{
      position: relative; overflow: hidden;
      display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
      padding: 34px; border-radius: 32px;
      background: var(--surface); box-shadow: var(--shadow-card); border: 1px solid var(--border);
    }}
    .hero-card::before {{
      content: ""; position: absolute; right: 190px; top: 26px; width: 230px; height: 120px;
      background-image: radial-gradient(rgba(0, 42, 152, 0.15) 1.5px, transparent 1.5px);
      background-size: 16px 16px; opacity: .5; z-index: 0; pointer-events: none;
    }}
    .hero-card::after {{
      content: ""; position: absolute; right: -80px; top: -70px; width: 260px; height: 190px;
      border-radius: 48% 52% 62% 38%; background: rgba(15, 192, 224, 0.18); z-index: 0; pointer-events: none;
    }}
    .hero-main, .hero-meta {{ position: relative; z-index: 1; min-width: 0; }}
    .brand-logo {{ display: block; height: 42px; width: auto; margin-bottom: 24px; }}
    .display-title {{
      font-family: "Playfair Display", Georgia, serif; font-weight: 800; letter-spacing: -.03em;
      margin: 0; color: var(--ac-blue); font-size: clamp(38px, 6vw, 72px); line-height: 1.02;
    }}
    .hero-subtitle {{ max-width: 690px; margin: 16px 0 0; color: rgba(20, 24, 31, 0.72); font-size: 15px; line-height: 1.65; }}
    .hero-meta {{ flex: 0 0 auto; color: rgba(20, 24, 31, 0.62); font-size: 13px; white-space: nowrap; }}
    .hero-meta strong {{ color: var(--ac-ink); font-weight: 700; }}

    /* ── KPI strip ────────────────────────────────────── */
    .kpi-grid {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; margin: 20px 0; }}
    .kpi-card {{
      padding: 18px; border-radius: var(--radius-md);
      background: var(--surface); border: 1px solid var(--border); box-shadow: var(--shadow-soft);
    }}
    .kpi-label {{ color: rgba(20, 24, 31, 0.64); font-size: 12px; font-weight: 700; }}
    .kpi-value {{ margin-top: 10px; color: var(--ac-blue); font-size: 34px; line-height: 1; font-weight: 800; }}
    .kpi-value.danger {{ color: var(--priority-high); }}
    .kpi-value.cyan {{ color: var(--ac-cyan); }}
    .kpi-value.secondary {{ color: var(--ac-blue-secondary); }}
    .kpi-help {{ margin-top: 6px; color: rgba(20, 24, 31, 0.5); font-size: 11px; font-weight: 600; }}

    /* ── Toolbar ──────────────────────────────────────── */
    .toolbar {{
      position: sticky; top: 12px; z-index: 4;
      display: grid; grid-template-columns: minmax(180px, 1fr) minmax(0, 170px) minmax(0, 190px) minmax(0, 170px) auto; gap: 10px;
      padding: 12px; border-radius: 22px; margin-bottom: 20px;
      background: rgba(255, 255, 255, 0.74); border: 1px solid var(--border);
      box-shadow: var(--shadow-soft); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    }}
    .toolbar input, .toolbar select {{
      min-width: 0; width: 100%; height: 44px; border-radius: 14px; border: 1px solid var(--border);
      padding: 0 14px; background: #fff; color: var(--ac-ink); font: inherit; font-size: 14px; outline: none;
    }}
    .toolbar input:focus, .toolbar select:focus {{ border-color: var(--ac-blue-light); box-shadow: 0 0 0 4px rgba(0, 42, 152, 0.08); }}
    .reset-button {{
      height: 44px; padding: 0 16px; border-radius: 14px; border: 1px solid var(--border);
      background: var(--surface-soft); color: var(--ac-blue-secondary); font: inherit; font-size: 13px; font-weight: 700; cursor: pointer;
    }}
    .reset-button:hover {{ border-color: var(--border-strong); background: var(--ac-soft-bg); }}

    /* ── Content grid ─────────────────────────────────── */
    .content-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 20px; align-items: start; }}

    .opportunity-list {{ display: flex; flex-direction: column; gap: 14px; }}
    .opportunity-card {{
      display: grid; grid-template-columns: 64px minmax(0, 1fr) auto; gap: 18px; align-items: center;
      padding: 20px; border-radius: 22px;
      background: var(--surface); border: 1px solid var(--border); box-shadow: var(--shadow-soft);
    }}
    .score-badge {{
      display: grid; place-items: center; width: 56px; height: 56px; border-radius: 16px;
      font-size: 19px; font-weight: 800; background: var(--ac-soft-bg); color: var(--ac-blue-secondary);
    }}
    .score-high {{ background: var(--priority-high-bg); color: var(--priority-high); }}
    .score-interesting {{ background: var(--priority-interesting-bg); color: #8A5D00; }}
    .score-watch {{ background: var(--priority-watch-bg); color: var(--priority-watch); }}

    .opportunity-body {{ min-width: 0; }}
    .opportunity-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }}
    .opportunity-head h2 {{
      margin: 0; color: var(--ac-ink); font-size: 16px; line-height: 1.35; font-weight: 800; overflow-wrap: anywhere;
    }}
    .opportunity-head h2 a {{ text-decoration: none; }}
    .opportunity-head h2 a:hover {{ text-decoration: underline; text-decoration-color: var(--ac-blue-light); }}

    .tag-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
    .tag {{
      display: inline-flex; align-items: center; height: 24px; padding: 0 9px; border-radius: var(--radius-sm);
      background: var(--surface-soft); border: 1px solid var(--border); color: rgba(20, 24, 31, 0.68);
      font-size: 11px; font-weight: 600;
    }}

    .priority-badge {{
      flex: 0 0 auto; display: inline-flex; align-items: center; height: 28px; padding: 0 10px;
      border-radius: var(--radius-sm); font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em;
    }}
    .priority-high {{ color: var(--priority-high); background: var(--priority-high-bg); }}
    .priority-watch {{ color: var(--priority-watch); background: var(--priority-watch-bg); }}
    .priority-interesting {{ color: #8A5D00; background: var(--priority-interesting-bg); }}

    .meta-line, .description {{ margin: 8px 0 0; color: rgba(20, 24, 31, 0.68); font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }}
    .meta-line strong, .description strong {{ color: rgba(20, 24, 31, 0.86); }}
    .clamp-2 {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .proof-row {{ display: inline; }}
    .external-link-icon {{
      display: inline-grid; place-items: center; width: 19px; height: 19px; color: var(--ac-blue-secondary);
      text-decoration: none; border-radius: 5px; vertical-align: -4px; margin-left: 4px;
    }}
    .external-link-icon:hover {{ color: var(--ac-blue); background: rgba(0, 42, 152, 0.08); }}
    .external-link-icon svg {{ width: 15px; height: 15px; stroke: currentColor; stroke-width: 2.8; fill: none; stroke-linecap: round; stroke-linejoin: round; }}

    .open-button {{
      display: inline-flex; align-items: center; justify-content: center; height: 38px; padding: 0 18px;
      border-radius: var(--radius-sm); background: var(--ac-blue); color: #fff; text-decoration: none;
      font-size: 13px; font-weight: 800;
    }}
    .open-button:hover {{ background: #001F75; }}

    .empty-state {{
      padding: 48px 24px; text-align: center; color: rgba(20, 24, 31, 0.6); font-size: 14px; line-height: 1.6;
      background: var(--surface); border: 1px solid var(--border); border-radius: 22px; box-shadow: var(--shadow-soft);
    }}

    /* ── Insight panel ────────────────────────────────── */
    .insight-panel {{ position: sticky; top: 84px; display: flex; flex-direction: column; gap: 14px; }}
    .insight-card {{ padding: 18px; border-radius: 22px; background: var(--surface); border: 1px solid var(--border); box-shadow: var(--shadow-soft); }}
    .insight-card h3 {{ margin: 0 0 14px; color: var(--ac-blue); font-size: 15px; font-weight: 800; }}
    .stack {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; }}
    .bar-row {{ display: grid; grid-template-columns: minmax(0, 1fr) 32px; gap: 8px; align-items: center; font-size: 12px; color: rgba(20, 24, 31, 0.68); }}
    .bar-row strong {{ text-align: right; color: var(--ac-ink); }}
    .bar-label {{ display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; font-weight: 600; }}
    .bar {{ height: 8px; background: var(--ac-soft-bg); border-radius: 999px; overflow: hidden; }}
    .fill {{ height: 100%; border-radius: inherit; background: var(--ac-blue-light); }}
    .fill.a {{ background: var(--priority-high); }}
    .fill.b {{ background: var(--priority-interesting); }}
    .fill.c {{ background: var(--ac-blue-secondary); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{
      border: 1px solid var(--border); background: var(--surface-soft); padding: 6px 10px; border-radius: var(--radius-sm);
      font: inherit; font-size: 11px; font-weight: 600; color: rgba(20, 24, 31, 0.68); cursor: pointer;
    }}
    .chip:hover {{ border-color: var(--border-strong); color: var(--ac-blue); }}
    .chip strong {{ color: var(--ac-blue); }}
    .method-note {{ margin: 0; color: rgba(20, 24, 31, 0.62); font-size: 12px; line-height: 1.55; }}

    .footer-note {{ margin-top: 20px; color: rgba(20, 24, 31, 0.56); font-size: 12px; line-height: 1.5; }}

    /* ── Responsive ───────────────────────────────────── */
    @media (max-width: 1100px) {{
      .kpi-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .toolbar {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 920px) {{
      .content-grid {{ grid-template-columns: minmax(0, 1fr); }}
      .insight-panel {{ position: static; }}
      .hero-card {{ flex-direction: column; align-items: flex-start; }}
      .hero-meta {{ white-space: normal; }}
      .hero-card::before {{ display: none; }}
    }}
    @media (max-width: 640px) {{
      .app-page {{ padding: 20px 12px 40px; }}
      .hero-card {{ padding: 26px 20px; border-radius: 26px; }}
      .brand-logo {{ height: 34px; }}
      .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .toolbar {{ grid-template-columns: minmax(0, 1fr); position: static; }}
      .opportunity-card {{ grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 18px 16px; }}
      .score-badge {{ width: 48px; height: 48px; font-size: 17px; }}
      .opportunity-head {{ flex-direction: column-reverse; gap: 8px; }}
      .open-button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="app-page">
    <div class="app-container">

      <header class="hero-card">
        <div class="hero-main">
          {AC_LOGO_SVG}
          <h1 class="display-title">Vue d’ensemble sourcing</h1>
          <p class="hero-subtitle">Un tableau de bord de veille automatisée des appels à projets, subventions, fondations et autres opportunités utiles à l’association.</p>
        </div>
        <div class="hero-meta">Dernière mise à jour : <strong id="generatedAt"></strong></div>
      </header>

      <section class="kpi-grid" id="kpis" aria-label="Indicateurs clés"></section>

      <div class="toolbar" role="search">
        <input id="search" type="search" placeholder="Rechercher : mentorat, Paris, étudiants, FSE…" aria-label="Rechercher une opportunité" />
        <select id="priorityFilter" aria-label="Filtrer par priorité"><option value="">Toutes priorités</option></select>
        <select id="sourceFilter" aria-label="Filtrer par source"><option value="">Toutes sources</option></select>
        <select id="themeFilter" aria-label="Filtrer par thème"><option value="">Tous thèmes</option></select>
        <button class="reset-button" id="resetFilters" type="button">Réinitialiser</button>
      </div>

      <div class="content-grid">
        <main class="opportunity-list" id="list"></main>

        <aside class="insight-panel">
          <section class="insight-card">
            <h3>Répartition priorité</h3>
            <div class="stack" id="priorityBars"></div>
          </section>
          <section class="insight-card">
            <h3>Sources les plus actives</h3>
            <div class="stack" id="sourceBars"></div>
          </section>
          <section class="insight-card">
            <h3>Thèmes détectés</h3>
            <div class="chips" id="themeChips"></div>
          </section>
          <section class="insight-card">
            <h3>Note méthodologique</h3>
            <p class="method-note">Le score sert à prioriser, pas à décider. La deadline et l’éligibilité doivent être vérifiées sur la page officielle.</p>
          </section>
        </aside>
      </div>

      <p class="footer-note">Dashboard généré automatiquement par la veille Ambition Campus.</p>
    </div>
  </div>

  <script>
    const DATA = {data_json};
    const rows = DATA.rows;
    const summary = DATA.summary;

    const $ = (id) => document.getElementById(id);
    const priorityCode = (p) => {{
      const s = String(p || '').toLowerCase();
      if (s.startsWith('a') || s.includes('traiter')) return 'high';
      if (s.startsWith('c') || s.includes('veille')) return 'watch';
      return 'interesting';
    }};
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    const encodeTextFragment = (s) => encodeURIComponent(s).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
    const pct = (n, d) => d ? Math.round((n / d) * 100) : 0;

    function deadlineHref(r) {{
      const base = String(r.deadline_source || r.lien || '').trim();
      if (!base) return '';
      const text = String(r.deadline || '').replace(/\\s+/g, ' ').trim();
      if (!text) return base;
      let snippet = text;
      if (snippet.length > 220) {{
        snippet = snippet.slice(0, 220).replace(/\\s+\\S*$/, '').trim();
      }}
      if (!snippet) return base;
      return base.split('#')[0] + '#:~:text=' + encodeTextFragment(snippet);
    }}

    function externalIcon(href, title='Ouvrir le passage source') {{
      if (!href) return '';
      return `<a class="external-link-icon" href="${{esc(href)}}" target="_blank" rel="noreferrer" title="${{esc(title)}}" aria-label="${{esc(title)}}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4L10 14"/><path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"/></svg></a>`;
    }}

    function unique(field) {{
      return [...new Set(rows.map(r => r[field]).filter(Boolean))].sort((a,b) => a.localeCompare(b, 'fr'));
    }}
    function uniqueThemes() {{
      return [...new Set(rows.flatMap(r => r.themes_list || []))].sort((a,b) => a.localeCompare(b, 'fr'));
    }}
    function addOptions(id, values) {{
      const el = $(id);
      values.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; el.appendChild(o); }});
    }}

    function renderKpis() {{
      const a = summary.priority['A - à traiter vite'] || 0;
      const b = summary.priority['B - intéressant'] || 0;
      const c = summary.priority['C - veille'] || 0;
      $('kpis').innerHTML = [
        ['Opportunités ouvertes', summary.total, 'financements repérés, non expirés', ''],
        ['À traiter vite', a, 'priorité A — les plus pertinentes', 'danger'],
        ['À étudier', b, 'priorité B — à qualifier', ''],
        ['À garder à l’œil', c, 'priorité C — sans urgence', 'secondary'],
        ['Date limite connue', summary.with_deadline, 'opportunités avec une échéance identifiée', 'cyan'],
        ['Score moyen', summary.avg_score, 'pertinence estimée, sur 100', ''],
      ].map(([label, value, help, tone]) => `<article class="kpi-card"><div class="kpi-label">${{label}}</div><div class="kpi-value ${{tone}}">${{value}}</div><div class="kpi-help">${{help}}</div></article>`).join('');
    }}

    function barRow(label, value, max, cls='') {{
      return `<div><span class="bar-label" title="${{esc(label)}}">${{esc(label)}}</span><div class="bar-row"><div class="bar"><div class="fill ${{cls}}" style="width:${{pct(value, max)}}%"></div></div><strong>${{value}}</strong></div></div>`;
    }}
    function renderSidebars() {{
      const prClass = (label) => {{
        const code = priorityCode(label);
        return code === 'high' ? 'a' : code === 'interesting' ? 'b' : 'c';
      }};
      const prEntries = Object.entries(summary.priority);
      const maxPr = Math.max(1, ...prEntries.map(x => x[1]));
      $('priorityBars').innerHTML = prEntries.map(([label, value]) => barRow(label, value, maxPr, prClass(label))).join('');
      const sources = summary.sources.slice(0, 7);
      const maxSource = Math.max(1, ...sources.map(x => x[1]));
      $('sourceBars').innerHTML = sources.map(([label, value]) => barRow(label, value, maxSource)).join('');
      $('themeChips').innerHTML = summary.themes.map(([label, value]) => `<button type="button" class="chip" data-theme="${{esc(label)}}"><strong>${{value}}</strong>&nbsp;${{esc(label)}}</button>`).join('');
      document.querySelectorAll('[data-theme]').forEach(btn => btn.addEventListener('click', () => {{ $('themeFilter').value = btn.dataset.theme; renderList(); }}));
    }}

    function renderList() {{
      const q = $('search').value.trim().toLowerCase();
      const pr = $('priorityFilter').value;
      const src = $('sourceFilter').value;
      const theme = $('themeFilter').value;
      const filtered = rows.filter(r => {{
        const blob = [r.titre, r.organisme, r.zone, r.themes, r.deadline, r.amount_possible, r.montant, r.notes].join(' ').toLowerCase();
        return (!q || blob.includes(q)) && (!pr || r.priorite === pr) && (!src || r.organisme === src) && (!theme || (r.themes_list || []).includes(theme));
      }}).sort((a,b) => (b.score_num || 0) - (a.score_num || 0));

      $('list').innerHTML = filtered.length ? filtered.map(r => {{
        const code = priorityCode(r.priorite);
        const tags = [r.zone, r.type_financement, ...(r.themes_list || []).slice(0,4)].filter(Boolean);
        const deadlineText = r.deadline_date || r.deadline || '';
        const metaLine = `<p class="meta-line"><strong>Source :</strong> ${{esc(r.organisme)}}</p>${{deadlineText ? `<p class="meta-line"><strong>Deadline :</strong> ${{esc(deadlineText)}}</p>` : ''}}`;
        const statusHref = r.deadline_href || deadlineHref(r);
        const deadlineStatus = r.deadline_status ? `<p class="description clamp-2"><span class="proof-row"><strong>Statut deadline :</strong> ${{esc(r.deadline_status)}}${{externalIcon(statusHref, 'Ouvrir le passage source surligné')}}</span></p>` : '';
        const amountText = r.amount_possible || 'Non indiqué dans la source';
        const amount = r.amount_found && r.amount_href
          ? `<p class="description clamp-2"><span class="proof-row"><strong>Montant possible :</strong> ${{esc(amountText)}}${{externalIcon(r.amount_href, 'Ouvrir le montant dans la source')}}</span></p>`
          : `<p class="description clamp-2"><strong>Montant possible :</strong> ${{esc(amountText)}}</p>`;
        const action = r.prochaine_action ? `<p class="description clamp-2"><strong>Action :</strong> ${{esc(r.prochaine_action)}}</p>` : '';
        return `<article class="opportunity-card">
          <div class="score-badge score-${{code}}" title="Score ${{esc(r.score)}}">${{esc(r.score)}}</div>
          <div class="opportunity-body">
            <div class="opportunity-head">
              <h2><a href="${{esc(r.lien)}}" target="_blank" rel="noreferrer">${{esc(r.titre)}}</a></h2>
              <span class="priority-badge priority-${{code}}">${{esc(r.priorite)}}</span>
            </div>
            <div class="tag-row">${{tags.map(t => `<span class="tag">${{esc(t)}}</span>`).join('')}}</div>
            ${{metaLine}}${{deadlineStatus}}${{amount}}${{action}}
          </div>
          <a class="open-button" href="${{esc(r.lien)}}" target="_blank" rel="noreferrer">Ouvrir</a>
        </article>`;
      }}).join('') : `<div class="empty-state">Aucune opportunité ne correspond à ces filtres.<br>Essaie d’élargir ta recherche ou de réinitialiser les filtres.</div>`;
    }}

    function resetFilters() {{
      $('search').value = '';
      $('priorityFilter').value = '';
      $('sourceFilter').value = '';
      $('themeFilter').value = '';
      renderList();
    }}

    function init() {{
      $('generatedAt').textContent = summary.generated_at;
      renderKpis();
      renderSidebars();
      addOptions('priorityFilter', unique('priorite'));
      addOptions('sourceFilter', unique('organisme'));
      addOptions('themeFilter', uniqueThemes());
      ['search','priorityFilter','sourceFilter','themeFilter'].forEach(id => $(id).addEventListener('input', renderList));
      $('resetFilters').addEventListener('click', resetFilters);
      renderList();
    }}
    init();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=None, help="CSV to render. Defaults to aggregating all outputs/*_opportunities.csv")
    parser.add_argument("--out", type=Path, default=DASHBOARD_DIR / "index.html")
    parser.add_argument("--include-expired", action="store_true", help="keep opportunities whose deadline is already past")
    args = parser.parse_args()

    csv_paths = [args.csv] if args.csv else all_csvs()
    rows = read_rows(csv_paths, include_expired=args.include_expired)
    label = str(csv_paths[0]) if len(csv_paths) == 1 else f"{len(csv_paths)} CSV agrégés ({csv_paths[0].name} → {csv_paths[-1].name})"
    summary = build_summary(rows, label)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_doc(rows, summary), encoding="utf-8")
    print(f"Dashboard: {args.out}")
    print(f"Rows: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
