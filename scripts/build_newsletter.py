#!/usr/bin/env python3
"""Newsletter hebdo : les opportunités détectées ces N derniers jours.

Produit outputs/newsletters/YYYY-MM-DD_newsletter.html (corps de mail HTML,
styles inline) + .txt (fallback texte) et écrit le sujet dans $GITHUB_OUTPUT
si disponible (clé `subject`), sinon sur stdout.

Usage:
    python3 scripts/build_newsletter.py
    python3 scripts/build_newsletter.py --days 7
"""

from __future__ import annotations

import argparse
import csv
import html
import os
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "outputs"
NEWSLETTER_DIR = OUTPUT_DIR / "newsletters"

PRIORITY_LABELS = {
    "A": "🔥 Priorité A — à traiter vite",
    "B": "⭐ Priorité B — à étudier",
    "C": "👀 Priorité C — à surveiller",
}


def recent_rows(days: int) -> list[dict[str, str]]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    merged: dict[str, dict[str, str]] = {}
    for path in sorted(OUTPUT_DIR.glob("*_opportunities.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("lien") or "").strip() or f"{row.get('titre', '')}|{row.get('organisme', '')}"
                merged[key] = row
    rows = [r for r in merged.values() if (r.get("date_detection") or "") >= cutoff]
    rows.sort(key=lambda r: (r.get("priorite") or "Z", -int(float(r.get("score") or 0))))
    return rows


def item_html(row: dict[str, str]) -> str:
    titre = html.escape(row.get("titre") or "Sans titre")
    lien = html.escape(row.get("lien") or "#")
    organisme = html.escape(row.get("organisme") or "")
    deadline = html.escape(row.get("deadline_date") or row.get("deadline") or "non précisée")
    montant = html.escape(row.get("montant") or "")
    bits = [f"<a href=\"{lien}\" style=\"color:#1a5fb4;font-weight:600;text-decoration:none\">{titre}</a>"]
    meta = [organisme, f"deadline : {deadline}"]
    if montant:
        meta.append(f"montant : {montant}")
    bits.append(f"<div style=\"color:#555;font-size:13px;margin-top:2px\">{' · '.join(m for m in meta if m)}</div>")
    return "<li style=\"margin:0 0 12px 0\">" + "".join(bits) + "</li>"


def build(days: int) -> tuple[str, str, str, int]:
    rows = recent_rows(days)
    today = date.today()
    subject = f"Veille financements Ambition Campus — {len(rows)} nouveauté(s) au {today.strftime('%d/%m/%Y')}"

    sections_html: list[str] = []
    sections_txt: list[str] = []
    for prio in ("A", "B", "C"):
        group = [r for r in rows if (r.get("priorite") or "").upper().startswith(prio)]
        if not group:
            continue
        sections_html.append(f"<h2 style=\"font-size:16px;margin:24px 0 8px\">{PRIORITY_LABELS[prio]} ({len(group)})</h2>")
        sections_html.append("<ul style=\"padding-left:18px;margin:0\">" + "".join(item_html(r) for r in group) + "</ul>")
        sections_txt.append(f"\n{PRIORITY_LABELS[prio]} ({len(group)})\n")
        for r in group:
            deadline = r.get("deadline_date") or r.get("deadline") or "non précisée"
            sections_txt.append(f"- {r.get('titre')}\n  {r.get('organisme')} · deadline : {deadline}\n  {r.get('lien')}")

    other = [r for r in rows if not (r.get("priorite") or "").upper()[:1] in ("A", "B", "C")]
    if other:
        sections_html.append(f"<h2 style=\"font-size:16px;margin:24px 0 8px\">Autres ({len(other)})</h2>")
        sections_html.append("<ul style=\"padding-left:18px;margin:0\">" + "".join(item_html(r) for r in other) + "</ul>")

    if not rows:
        empty = "Aucune nouvelle opportunité détectée cette semaine."
        sections_html.append(f"<p>{empty}</p>")
        sections_txt.append(empty)

    dashboard_url = "https://pythagorrre.github.io/ambition-campus-funding-watch/"
    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:0 auto;color:#222">
<h1 style="font-size:20px">Veille financements — nouveautés des {days} derniers jours</h1>
<p style="color:#555">Semaine du {today.strftime('%d/%m/%Y')} · {len(rows)} nouvelle(s) opportunité(s)</p>
{''.join(sections_html)}
<p style="margin-top:28px"><a href="{dashboard_url}" style="color:#1a5fb4">→ Voir toutes les opportunités ouvertes sur le tableau de bord</a></p>
<p style="color:#999;font-size:12px">Newsletter automatique de la veille Ambition Campus.</p>
</div>"""
    body_txt = (f"Veille financements — nouveautés des {days} derniers jours\n"
                f"Semaine du {today.strftime('%d/%m/%Y')} · {len(rows)} nouvelle(s) opportunité(s)\n"
                + "\n".join(sections_txt)
                + f"\n\nTableau de bord : {dashboard_url}\n")
    return subject, body_html, body_txt, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    subject, body_html, body_txt, count = build(args.days)
    NEWSLETTER_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{date.today().isoformat()}_newsletter"
    html_path = NEWSLETTER_DIR / f"{stem}.html"
    txt_path = NEWSLETTER_DIR / f"{stem}.txt"
    html_path.write_text(body_html, encoding="utf-8")
    txt_path.write_text(body_txt, encoding="utf-8")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"subject={subject}\n")
            fh.write(f"html_path={html_path}\n")
            fh.write(f"count={count}\n")
    print(f"Sujet: {subject}")
    print(f"HTML: {html_path}")
    print(f"Nouveautés: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
