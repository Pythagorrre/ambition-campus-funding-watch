#!/usr/bin/env python3
"""Newsletter hebdo — format « v18 » (charte Ambition Campus).

Carte d'en-tête (logo hébergé + semaine + bulle bleue + citation tournante),
puis 3 sections de priorité (À traiter vite / À étudier / À garder à l'œil)
avec pastilles colorées, une ligne par appel (« titre · échéance JJ/MM/AAAA »),
et un bouton vers le tableau de bord.

Ne liste QUE les appels détectés dans les N derniers jours. Si 0 nouveauté,
le workflow n'envoie pas de mail (condition sur le compteur `count`).

Deux numéros distincts :
- numéro de SEMAINE (basé sur la date, 1 → 47) affiché dans la bulle bleue ;
- numéro d'ÉDITION (persistant, incrémenté seulement sur un envoi réel) pour
  l'objet et la rotation des citations.

Écrit subject / html_path / count dans $GITHUB_OUTPUT si disponible.
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
EDITION_FILE = BASE_DIR / "data" / "newsletter_edition.txt"

DASHBOARD_URL = "https://ambition-campus-funding-watch.pages.dev/"
LOGO_URL = "https://ambition-campus-funding-watch.pages.dev/email-logo.png"

START_MONDAY = date(2026, 7, 13)  # semaine #1
TOTAL_WEEKS = 47                  # dernière édition la semaine du 19 juin 2027
HERO_TITLE = "Les nouveaux appels à projets de la semaine"

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

# label + couleur de pastille par priorité
CATEGORIES = {
    "A": ("Priorité 1",
          'À traiter <span style="text-decoration:underline;text-decoration-color:#0FC0E0;'
          'text-decoration-thickness:3px;text-underline-offset:4px;">vite</span>', "#0FC0E0"),
    "B": ("Priorité 2", "À étudier", "#31559B"),
    "C": ("Priorité 3", "À garder à l'œil", "#8EA7D4"),
}

# Citations tournantes (dans l'ordre, une par édition, cycle après la 21e).
QUOTES = [
    "Dans ces dossiers de subvention, il y a des futurs ingénieurs, avocates, fondateurs, députés, astronautes. Ils ne le savent pas encore.",
    "On ne lève pas des fonds. On lève des plafonds.",
    "Chaque budget obtenu devient un atelier, une masterclass, un mentorat, une visite de campus puis une vie qui change de trajectoire.",
    "Chaque dossier gagné aujourd'hui, c'est des lycéens qui reviendront en bénévoles demain.",
    "Chaque dossier rempli aujourd'hui profite à une génération qui remplira ceux de la suivante.",
    "Chaque dossier gagné plante une graine. On ne verra pas l'arbre, et ceux qui s'abriteront sous son ombre ne sauront pas qui l'a plantée.",
    "Le financement dure un an. La trajectoire qu'il déclenche dure toute une vie.",
    "Aucun placement ne rapporte autant qu'un élève qui découvre qu'il en est capable.",
    "Un financement, c'est du carburant. La fusée, c'est eux.",
    "Un formulaire de ces appels fait 12 pages. La trajectoire qu'il finance fera soixante ans.",
    "Un budget accordé, c'est une porte qui s'ouvre. Un élève qui la franchit, c'est une génération qui suit.",
    "Les financeurs voient un projet. Nous, on voit les visages de ceux qu'il va toucher.",
    "Une subvention se compte en euros. Son effet se compte en déclics.",
    "Il suffit d'une rencontre pour changer un parcours. Notre travail, c'est de financer la rencontre.",
    "La confiance en soi ne tombe pas du ciel. Elle s'organise, elle s'accompagne, et elle se finance.",
    "Le potentiel existe déjà. Le financement ne le crée pas. Il le libère.",
    "On ne fabrique pas des talents. On finance les conditions pour qu'ils se révèlent.",
    "Le financeur signe un chèque. Le bénévole donne son samedi. L'élève change de vie.",
    "On ne verra pas tout de suite ce que ce financement a changé. Rendez-vous dans dix ans, à la remise des diplômes.",
    "Les intérêts composés existent aussi en éducation. Un déclic à 17 ans rapporte toute une vie.",
    "L'impact d'un mentor ne s'arrête pas au mentoré. Il touche ses frères, ses sœurs, ses amis, son quartier.",
]

# <head> + styles (repris du modèle validé) + verrou "mode clair" (anti-inversion
# en mode sombre sur mobile). Chaîne simple : les accolades CSS ne sont pas interpolées.
HEAD_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <meta name="format-detection" content="telephone=no,address=no,email=no,date=no,url=no">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <title>Veille financements — Ambition Campus</title>
  <style>
    :root { color-scheme:light; supported-color-schemes:light; }
    html, body { margin:0 !important; padding:0 !important; width:100% !important; background:#EEF2FA; color-scheme:light; }
    table { border-collapse:collapse !important; border-spacing:0 !important; }
    a { text-decoration:none; }
    .email-shell { width:100%; background:#EEF2FA; }
    .container { width:100%; max-width:720px; margin:0 auto; }
    .mobile-pad { padding-left:34px; padding-right:34px; }
    @media only screen and (max-width:740px) {
      .container { width:100% !important; max-width:100% !important; }
      .mobile-pad { padding-left:20px !important; padding-right:20px !important; }
      .hero-title { font-size:20px !important; line-height:26px !important; white-space:normal !important; }
      .section-title { font-size:25px !important; line-height:31px !important; }
      .opportunity-line { font-size:10.5px !important; line-height:15px !important; }
      .full-button { display:block !important; width:100% !important; box-sizing:border-box !important; text-align:center !important; }
    }
  </style>
</head>"""


def priority_code(priorite: str) -> str:
    s = (priorite or "").lower()
    if s.startswith("a") or "traiter" in s:
        return "A"
    if s.startswith("c") or "veille" in s:
        return "C"
    return "B"


def clean_title(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s*[|\-–·]\s*$", "", t)
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


def week_number(today: date) -> int:
    monday = today - timedelta(days=today.weekday())
    return max(1, min(TOTAL_WEEKS, (monday - START_MONDAY).days // 7 + 1))


def monday_fr(today: date) -> str:
    monday = today - timedelta(days=today.weekday())
    return f"{monday.day} {MOIS[monday.month - 1]} {monday.year}"


def read_edition() -> int:
    try:
        return int(EDITION_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


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


def _line(r: dict[str, str]) -> str:
    t = esc(clean_title(r.get("titre")) or "Sans titre")
    dl = r.get("deadline_date") or ""
    return t + (f" · échéance {fr_date(dl)}" if dl else "")


def _row(text: str, dot: str, last: bool) -> str:
    border = "" if last else "border-bottom:1px solid #E5EAF3;"
    return (
        '<tr><td width="18" valign="top" style="padding:8px 0 7px;">'
        f'<div style="width:6px;height:6px;background:{dot};border-radius:50%;margin-top:4px;"></div></td>'
        f'<td valign="top" style="padding:7px 0;{border}">'
        f'<div class="opportunity-line" style="font-family:Arial,Helvetica,sans-serif;font-size:10.5px;line-height:15px;color:#202733;">{text}</div>'
        '</td></tr>'
    )


def _section(rows: list[dict[str, str]], code: str) -> str:
    eyebrow, title_html, dot = CATEGORIES[code]
    grp = [r for r in rows if priority_code(r.get("priorite")) == code]
    if not grp:
        return ""
    body = "".join(_row(_line(r), dot, i == len(grp) - 1) for i, r in enumerate(grp))
    return (
        '<tr><td class="mobile-pad" style="background:#EEF2FA;padding:34px 34px 30px;">'
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:14px;font-weight:800;'
        f'letter-spacing:1.5px;color:#0FC0E0;text-transform:uppercase;">{eyebrow} · {len(grp)} AAP</div>'
        f'<div class="section-title" style="font-family:Georgia,\'Times New Roman\',serif;font-size:28px;'
        f'line-height:34px;font-weight:700;color:#002A98;margin-top:6px;margin-bottom:16px;">{title_html}</div>'
        '<table role="presentation" width="100%" style="background:#FFFFFF;border-radius:16px;">'
        f'<tr><td style="padding:6px 18px 7px;"><table role="presentation" width="100%">{body}</table></td></tr></table>'
        '</td></tr>'
    )


def build(days: int, edition: int) -> tuple[str, str, str, int]:
    rows = recent_rows(days)
    count = len(rows)
    today = date.today()
    week = week_number(today)
    quote = QUOTES[(edition - 1) % len(QUOTES)]
    sections = "".join(_section(rows, c) for c in ("A", "B", "C"))
    subject = f"Newsletter #{edition} - AAP Ambition Campus"
    preheader = f"{count} nouvel{'s' if count > 1 else ''} appel{'s' if count > 1 else ''} à projets détecté{'s' if count > 1 else ''} cette semaine."

    body_html = f"""{HEAD_HTML}<body>
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{esc(preheader)}</div>
<table role="presentation" class="email-shell" width="100%"><tr><td align="center" style="padding:28px 12px 42px;">
<table role="presentation" class="container" width="720">
<tr><td class="mobile-pad" style="padding:0 34px;">
<table role="presentation" width="100%" style="background:#FFFFFF;border-radius:20px;">
<tr><td style="padding:26px 28px 4px;"><table role="presentation" width="100%"><tr>
<td valign="middle"><img src="{LOGO_URL}" alt="Ambition Campus" width="132" style="display:block;width:132px;height:auto;border:0;"></td>
<td align="right" valign="middle">
<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#697386;">Semaine du</div>
<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:19px;font-weight:700;color:#14181F;">{monday_fr(today)}</div>
</td></tr></table></td></tr>
<tr><td style="padding:30px 40px 18px;"><table role="presentation" width="100%" style="background:#002A98;border-radius:16px;"><tr><td style="padding:22px 22px 20px;">
<div class="hero-title" style="font-family:Georgia,'Times New Roman',serif;font-size:21px;line-height:28px;font-weight:700;color:#FFFFFF;letter-spacing:-0.3px;text-align:center;white-space:nowrap;">{HERO_TITLE}</div>
<div style="font-family:Georgia,'Times New Roman',serif;font-size:13px;line-height:19px;font-style:italic;color:#0FC0E0;text-align:center;margin-top:10px;">Newsletter #{week} / {TOTAL_WEEKS}</div>
</td></tr></table></td></tr>
<tr><td style="padding:14px 28px 26px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:26px;font-style:italic;color:#3C4453;padding-left:18px;">«&nbsp;{esc(quote)}&nbsp;»</div></td></tr>
</table></td></tr>
{sections}
<tr><td class="mobile-pad" align="center" style="padding:14px 34px 6px;">
<a class="full-button" href="{DASHBOARD_URL}" style="display:inline-block;background:#0FC0E0;color:#002A98;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;font-weight:900;padding:15px 24px;border-radius:999px;">Ouvrir le tableau de bord</a>
</td></tr>
</table></td></tr></table>
</body></html>"""

    txt_lines = [f"Newsletter #{edition} — AAP Ambition Campus",
                 f"Semaine du {monday_fr(today)}", "", f"« {quote} »", ""]
    for code in ("A", "B", "C"):
        label, _, _ = CATEGORIES[code]
        grp = [r for r in rows if priority_code(r.get("priorite")) == code]
        if not grp:
            continue
        txt_lines.append(f"\n{label} ({len(grp)})")
        for r in grp:
            dl = r.get("deadline_date") or ""
            suffix = f" (échéance {fr_date(dl)})" if dl else ""
            txt_lines.append(f"- {clean_title(r.get('titre'))}{suffix}\n  {r.get('lien')}")
    txt_lines.append(f"\nTableau de bord : {DASHBOARD_URL}")
    body_txt = "\n".join(txt_lines)
    return subject, body_html, body_txt, count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    # Édition à afficher = dernière envoyée + 1. On ne persiste (incrémente) que
    # si un mail part réellement cette semaine (count > 0) : les semaines vides
    # ne consomment pas de numéro d'édition.
    edition = read_edition() + 1
    subject, body_html, body_txt, count = build(args.days, edition)

    NEWSLETTER_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{date.today().isoformat()}_newsletter"
    html_path = NEWSLETTER_DIR / f"{stem}.html"
    txt_path = NEWSLETTER_DIR / f"{stem}.txt"
    html_path.write_text(body_html, encoding="utf-8")
    txt_path.write_text(body_txt, encoding="utf-8")

    if count > 0:
        EDITION_FILE.parent.mkdir(parents=True, exist_ok=True)
        EDITION_FILE.write_text(str(edition), encoding="utf-8")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"subject={subject}\n")
            fh.write(f"html_path={html_path}\n")
            fh.write(f"count={count}\n")
    print(f"Sujet: {subject}")
    print(f"Édition: #{edition} | count: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
