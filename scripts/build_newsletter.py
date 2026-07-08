#!/usr/bin/env python3
"""Newsletter hebdo : les opportunités détectées ces N derniers jours.

Format léger : liste groupée par priorité, une ligne par appel
(titre en noir cliquable + « deadline le JJ/MM/AAAA » en gris), puce colorée
selon la priorité (rouge = à traiter vite, ambre = à étudier, bleu = à surveiller).

Produit outputs/newsletters/YYYY-MM-DD_newsletter.html (corps de mail) + .txt,
et écrit subject / html_path / count dans $GITHUB_OUTPUT si disponible.

Usage:
    python3 scripts/build_newsletter.py --days 7
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import re
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "outputs"
NEWSLETTER_DIR = OUTPUT_DIR / "newsletters"

DASHBOARD_URL = "https://ambition-campus-funding-watch.pages.dev/"

# label + couleur de puce par catégorie de priorité
CATEGORIES = {
    "A": ("À traiter vite", "#EE2129"),
    "B": ("À étudier", "#D99A00"),
    "C": ("À garder à l'œil", "#31559B"),
}


def priority_code(priorite: str) -> str:
    s = (priorite or "").lower()
    if s.startswith("a") or "traiter" in s:
        return "A"
    if s.startswith("c") or "veille" in s:
        return "C"
    return "B"


def clean_title(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s*[|\-–·]\s*$", "", t)  # séparateur en fin de titre
    # dédup d'un nom d'organisme répété en fin de titre
    for n in (4, 3, 2):
        w = t.split()
        if len(w) >= 2 * n and w[-n:] == w[-2 * n:-n]:
            t = " ".join(w[:-n]).rstrip(" -–|·")
    return t.strip()


def fr_date(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (iso or "")


def esc(s) -> str:
    return html.escape(str(s or ""))


def recent_rows(days: int) -> list[dict[str, str]]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    merged: dict[str, dict[str, str]] = {}
    for path in sorted(OUTPUT_DIR.glob("*_opportunities.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("lien") or "").strip() or f"{row.get('titre', '')}|{row.get('organisme', '')}"
                merged[key] = row
    rows = [r for r in merged.values() if (r.get("date_detection") or "") >= cutoff]
    rows.sort(key=lambda r: ({"A": 0, "B": 1, "C": 2}[priority_code(r.get("priorite"))],
                             -int(float(r.get("score") or 0))))
    return rows


def row_html(r: dict[str, str], color: str) -> str:
    titre = esc(clean_title(r.get("titre")) or "Sans titre")
    lien = esc(r.get("lien") or "#")
    dl = r.get("deadline_date") or ""
    deadline = f' <span style="color:#999;">· deadline le {fr_date(dl)}</span>' if dl else ""
    return (
        '<tr>'
        f'<td style="width:16px;vertical-align:top;padding:3px 8px 3px 0;color:{color};font-size:15px;line-height:1.5;">●</td>'
        f'<td style="padding:3px 0;font-size:14px;line-height:1.5;">'
        f'<a href="{lien}" style="color:#1a1a1a;text-decoration:none;">{titre}</a>{deadline}</td>'
        '</tr>'
    )


def build(days: int) -> tuple[str, str, str, int]:
    rows = recent_rows(days)
    today = date.today()
    subject = f"Veille financements Ambition Campus : {len(rows)} nouvelles opportunités ({today.strftime('%d/%m/%Y')})"

    html_parts: list[str] = []
    txt_parts: list[str] = []
    for code in ("A", "B", "C"):
        grp = [r for r in rows if priority_code(r.get("priorite")) == code]
        if not grp:
            continue
        label, color = CATEGORIES[code]
        html_parts.append(
            f'<h2 style="font-size:14px;font-weight:700;margin:22px 0 6px;color:#555;'
            f'text-transform:uppercase;letter-spacing:.03em;">{label} ({len(grp)})</h2>'
        )
        html_parts.append(
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
            + "".join(row_html(r, color) for r in grp)
            + '</table>'
        )
        txt_parts.append(f"\n{label} ({len(grp)})")
        for r in grp:
            dl = r.get("deadline_date") or ""
            suffix = f" (deadline le {fr_date(dl)})" if dl else ""
            txt_parts.append(f"- {clean_title(r.get('titre'))}{suffix}\n  {r.get('lien')}")

    if not rows:
        empty = "Aucune nouvelle opportunité détectée cette semaine."
        html_parts.append(f'<p style="color:#555;">{empty}</p>')
        txt_parts.append(empty)

    body_html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:0 auto;color:#333;font-size:14px;line-height:1.5;">
<h1 style="font-size:19px;margin:0 0 4px;color:#002A98;font-weight:700;">Veille financements Ambition Campus</h1>
<p style="color:#777;margin:0 0 4px;">Semaine du {today.strftime('%d/%m/%Y')} · {len(rows)} nouvelles opportunités</p>
{''.join(html_parts)}
<p style="margin:24px 0 0;"><a href="{DASHBOARD_URL}" style="color:#33507a;">→ Voir le détail sur le tableau de bord</a></p>
<p style="color:#aaa;font-size:12px;margin-top:14px;">Le score sert à prioriser, pas à décider : deadline et éligibilité à vérifier sur la page officielle. Newsletter automatique de la veille Ambition Campus.</p>
</div>"""

    body_txt = (
        f"Veille financements Ambition Campus\n"
        f"Semaine du {today.strftime('%d/%m/%Y')} · {len(rows)} nouvelles opportunités\n"
        + "\n".join(txt_parts)
        + f"\n\nTableau de bord : {DASHBOARD_URL}\n"
    )
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
