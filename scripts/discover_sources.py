#!/usr/bin/env python3
"""Découverte hebdomadaire de nouvelles sources de financement.

Lance des requêtes web larges (via les mêmes moteurs que funding_watch),
repère les domaines pertinents qui ne sont pas encore dans config/sources.csv
et les propose dans config/sources_candidates.csv pour validation humaine.

Le fichier candidates est cumulatif : les lignes existantes gardent leur
statut (`à valider` par défaut ; passer à `ajoutée` ou `ignorer` à la main).
Un domaine `ignoré` ne sera plus re-proposé.

Usage:
    python3 scripts/discover_sources.py
    python3 scripts/discover_sources.py --max-results 12
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "scripts"))

import funding_watch as fw  # noqa: E402

CANDIDATES_PATH = BASE_DIR / "config" / "sources_candidates.csv"
FIELDNAMES = [
    "domaine", "statut", "occurrences", "exemple_titre", "exemple_lien",
    "requetes", "premiere_detection", "derniere_detection",
]

# Domaines déjà couverts par ailleurs ou sans intérêt comme « source » durable.
EXTRA_SKIP_HOST_PARTS = {
    "wikipedia.org", "leboncoin.fr", "pagesjaunes.fr", "eventbrite.", "meetup.com",
    "cairn.info", "legifrance.gouv.fr", "journal-officiel.gouv.fr",
}

POSITIVE_TITLE_TERMS = [
    "appel a projets", "appel a projet", "appel à projets", "appel à projet",
    "subvention", "financement", "mécénat", "mecenat", "fondation", "bourse",
    "dotation", "prix", "concours", "fonds",
]


def discovery_queries() -> list[str]:
    year = date.today().year
    return [
        f"appel à projets association jeunesse {year}",
        f"appel à projets égalité des chances {year}",
        f"subvention association aide aux étudiants Paris {year}",
        f"appel à projets éducation insertion jeunes Île-de-France {year}",
        "fondation mécénat jeunesse égalité des chances appel à projets",
        f"appel à projets lutte contre le décrochage scolaire {year}",
        f"financement association étudiants précarité {year}",
        f"appel à projets mentorat orientation jeunes {year}",
    ]


def known_domains() -> set[str]:
    domains: set[str] = set()
    with fw.SOURCES_PATH.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            host = urlparse((row.get("url") or "").strip()).netloc.lower()
            if host:
                domains.add(host.removeprefix("www."))
    return domains


def load_candidates() -> dict[str, dict[str, str]]:
    if not CANDIDATES_PATH.exists():
        return {}
    with CANDIDATES_PATH.open(newline="", encoding="utf-8") as fh:
        return {row["domaine"]: dict(row) for row in csv.DictReader(fh) if row.get("domaine")}


def title_is_relevant(title: str) -> bool:
    norm = fw.normalize(title)
    return any(fw.normalize(term) in norm for term in POSITIVE_TITLE_TERMS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-results", type=int, default=10, help="résultats analysés par requête")
    args = parser.parse_args()

    known = known_domains()
    candidates = load_candidates()
    today = date.today().isoformat()
    new_domains = 0

    for query in discovery_queries():
        try:
            results = fw.search_deadline_web(query, max_results=args.max_results)
        except Exception as exc:
            print(f"⚠ requête en échec ({exc}): {query}")
            continue
        for title, url in results:
            host = urlparse(url).netloc.lower().removeprefix("www.")
            if not host or host in known:
                continue
            if any(part in host for part in EXTRA_SKIP_HOST_PARTS):
                continue
            if not title_is_relevant(title):
                continue
            entry = candidates.get(host)
            if entry:
                if entry.get("statut", "").strip().lower() == "ignorer":
                    continue
                entry["occurrences"] = str(int(entry.get("occurrences") or 0) + 1)
                entry["derniere_detection"] = today
                if query not in entry.get("requetes", ""):
                    entry["requetes"] = (entry.get("requetes", "") + " ; " + query).strip(" ;")
            else:
                candidates[host] = {
                    "domaine": host,
                    "statut": "à valider",
                    "occurrences": "1",
                    "exemple_titre": title[:160],
                    "exemple_lien": url,
                    "requetes": query,
                    "premiere_detection": today,
                    "derniere_detection": today,
                }
                new_domains += 1

    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        candidates.values(),
        key=lambda r: (r.get("statut") != "à valider", -int(r.get("occurrences") or 0)),
    )
    with CANDIDATES_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in ordered:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    print(f"Sources candidates: {CANDIDATES_PATH}")
    print(f"Nouveaux domaines cette fois: {new_domains} — total à valider: "
          f"{sum(1 for r in candidates.values() if r.get('statut') == 'à valider')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
