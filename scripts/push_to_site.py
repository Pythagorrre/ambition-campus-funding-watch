#!/usr/bin/env python3
"""Pousse les opportunités de la veille vers le site ambitioncampus.com.

Lit le CSV d'opportunités le plus récent de outputs/ et l'envoie à
POST {SITE_URL}/api/veille/sync (endpoint du site, table `aap`).

Variables d'environnement requises (sinon le script ne fait rien et sort en 0,
pour ne jamais casser le workflow GitHub Actions) :
  SITE_URL         ex. https://ambitioncampus.com
  SITE_SYNC_TOKEN  jeton partagé (secret GitHub + secret wrangler VEILLE_SYNC_TOKEN)

Règles d'envoi :
  - on n'envoie pas les opportunités fermées / expirées ;
  - le site insère les nouveaux id en statut « nouveau » et ne touche jamais
    aux statuts gérés côté site (déposé, écarté…), seulement aux métadonnées.
"""
import csv
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

OUTPUTS = Path(__file__).resolve().parent.parent / "outputs"


def all_rows() -> dict[str, dict]:
    """Agrège tous les CSV d'opportunités (chaque fichier est un delta) — dédup par id, le plus récent gagne."""
    rows: dict[str, dict] = {}
    for path in sorted(OUTPUTS.glob("*_opportunities.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rid = (row.get("id") or "").strip()
                if rid:
                    rows[rid] = row
    return rows


def main() -> int:
    site_url = os.environ.get("SITE_URL", "").rstrip("/")
    token = os.environ.get("SITE_SYNC_TOKEN", "")
    if not site_url or not token:
        print("push_to_site : SITE_URL / SITE_SYNC_TOKEN absents — étape ignorée.")
        return 0

    merged = all_rows()
    if not merged:
        print("push_to_site : aucun CSV d'opportunités dans outputs/.")
        return 0

    today = datetime.date.today().isoformat()
    opportunities = []
    for rid, row in merged.items():
        statut = (row.get("statut") or "").strip().lower()
        deadline_status = (row.get("deadline_status") or "").strip().lower()
        if statut in ("fermé", "ferme") or deadline_status in ("expiré", "expire"):
            continue
        deadline = (row.get("deadline_date") or "").strip()
        if deadline and deadline < today:  # échéance passée : inutile côté suivi
            continue
        opportunities.append({
            "external_id": rid,
            "title": (row.get("titre") or "").strip(),
            "funder": (row.get("organisme") or "").strip(),
            "deadline": (row.get("deadline_date") or "").strip(),
            "amount": (row.get("montant") or "").strip(),
            "url": (row.get("lien") or "").strip(),
        })

    if not opportunities:
        print("push_to_site : rien d'ouvert à pousser.")
        return 0

    req = urllib.request.Request(
        f"{site_url}/api/veille/sync",
        data=json.dumps({"opportunities": opportunities}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as exc:  # non bloquant pour le workflow
        print(f"push_to_site : échec de l'envoi ({exc}) — non bloquant.")
        return 0

    print(
        f"push_to_site : {len(opportunities)} ouverts (sur {len(merged)} connus) → "
        f"{result.get('inserted', 0)} ajoutés, {result.get('updated', 0)} mis à jour, "
        f"{result.get('skipped', 0)} ignorés."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
