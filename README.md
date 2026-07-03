# Ambition Campus — veille financements

MVP local pour sourcer chaque semaine des appels à projets, subventions, mécénats et financements utiles à une association parisienne orientée égalité des chances / aide aux étudiants.

## Ce que fait le MVP

- Lit les sources dans `config/sources.csv`.
- Scrape les pages officielles / fondations.
- Filtre les liens avec des mots-clés : appel à projets, subvention, financement, association, jeunesse, étudiant, égalité des chances, inclusion, campus, etc.
- Score chaque opportunité.
- Détecte les deadlines datées et masque par défaut les opportunités déjà expirées.
- Pour les deadlines non trouvées dans la page source, lance une recherche web ciblée et renseigne `deadline_source` quand une date est élucidée.
- Si aucune date fiable n’est trouvée après recherche, marque `deadline_status = non trouvée après recherche web` au lieu d’un vague “à vérifier”.
- Produit :
  - `outputs/YYYY-MM-DD_opportunities.csv` : importable dans Google Sheets.
  - `outputs/YYYY-MM-DD_digest.md` : résumé hebdo lisible par mail.

## Lancer la veille

```bash
cd /Users/YY/.openclaw/workspace/ambition_campus_funding_watch
python3 scripts/funding_watch.py
```

Pour une vraie exécution hebdo avec dédoublonnage :

```bash
python3 scripts/funding_watch.py --mark-seen
```

## Dashboard local

Générer le dashboard HTML autonome :

```bash
cd /Users/YY/.openclaw/workspace/ambition_campus_funding_watch
python3 scripts/build_dashboard.py
open dashboard/index.html
```

Le dashboard contient : KPIs, répartition par priorité, sources les plus actives, thèmes, recherche texte, filtres par priorité/source/thème, liens directs vers les opportunités.

Pour rafraîchir les données + le dashboard :

```bash
python3 scripts/funding_watch.py --min-score 18 --max-detail 250
python3 scripts/build_dashboard.py
```

Le fallback web est actif par défaut. Pour le désactiver en debug rapide : `--no-web-deadline-check`.
Si tu veux auditer les lignes masquées, relance avec `--include-expired`.

## Import Google Sheets

Pour l’instant, Google OAuth local renvoie `REFRESH_FAILED invalid_grant`, donc le script sort un CSV prêt à importer.

Structure recommandée du Google Sheet :

- Onglet `Opportunités` : importer `outputs/*_opportunities.csv`.
- Onglet `Sources` : importer `config/sources.csv`.
- Onglet `Pipeline` : filtrer par `priorite` et `statut`.

Colonnes clés :

- `priorite`
- `score`
- `statut`
- `titre`
- `organisme`
- `type_financement`
- `zone`
- `themes`
- `montant`
- `deadline`
- `deadline_date`
- `deadline_status`
- `deadline_source`
- `lien`
- `eligibilite_ambition_campus`
- `prochaine_action`

## Newsletter hebdomadaire automatique

Chaque vendredi à 19h (heure de Paris, été), le workflow `newsletter.yml`
génère un mail HTML avec les opportunités détectées ces 7 derniers jours
(`scripts/build_newsletter.py`), l'archive dans `outputs/newsletters/`
et l'envoie aux destinataires configurés.

L'envoi nécessite 5 secrets dans le repo GitHub
(Settings → Secrets and variables → Actions → New repository secret) :

- `SMTP_SERVER` : ex. `smtp.gmail.com`
- `SMTP_PORT` : ex. `465`
- `SMTP_USERNAME` : l'adresse d'envoi
- `SMTP_PASSWORD` : pour Gmail, un « mot de passe d'application »
  (myaccount.google.com → Sécurité → Validation en deux étapes → Mots de passe d'application)
- `MAIL_TO` : destinataires séparés par des virgules

Sans ces secrets, la newsletter est quand même générée et archivée à chaque
run — seul l'envoi est sauté. Test manuel : onglet Actions → « Newsletter
hebdomadaire » → Run workflow.

## Découverte automatique de nouvelles sources

Chaque semaine, `scripts/discover_sources.py` lance des requêtes web larges
(« appel à projets égalité des chances 2026 », etc.), repère les domaines
pertinents absents de `config/sources.csv` et les propose dans
`config/sources_candidates.csv` :

- `statut = à valider` : nouveau domaine détecté, à examiner.
- Passer le statut à `ajoutée` après l'avoir intégré dans `sources.csv`
  (avec la bonne URL de page « appels à projets » du site).
- Passer le statut à `ignorer` pour ne plus jamais re-proposer ce domaine.

Le fichier est cumulatif et trié par nombre d'occurrences ; il est commité
automatiquement par le workflow hebdo.

## Sources initiales

Priorité haute :

- Ville de Paris — appels à projets
- Région Île-de-France — aides et appels à projets
- Associations.gouv — appels à projets / financements privés / Europe
- Fondation de France
- Fondation Groupe RATP
- Jeunes.gouv
- Erasmus+ / Corps européen de solidarité

Agrégateurs (veille de second niveau — eux crawlent, on les lit) :

- Carenews — appels à projets multi-fondations
- Avise — actualités ESS
- Admical — mécénat d'entreprise
- INJEP — jeunesse / éducation populaire
- Fondations Orange et EDF

À enrichir ensuite :

- Fondations d’entreprise : SNCF, EDF, BNP Paribas, Crédit Agricole, Macif, Orange, Société Générale, etc.
- Mairies d’arrondissement parisiennes.
- Universités / CROUS / Paris 1 si dispositifs étudiants.
- Newsletters spécialisées ESS / associations / égalité des chances.

## Process interne conseillé

Chaque lundi :

1. Lire les opportunités `A` et `B`.
2. La personne responsable passe chaque ligne en : `à analyser`, `candidater`, `surveiller`, `ignorer`.
3. Pour les lignes `candidater`, créer une fiche dossier avec : règlement, deadline, pièces, budget, contact.
4. Garder un historique gagné/perdu pour améliorer le scoring.

## Limites du MVP

- Scraping HTML simple : certains sites très JavaScript peuvent être incomplets.
- Les deadlines/montants sont détectés par regex, donc à vérifier manuellement.
- Google Sheets/Gmail auto nécessitent une réauth Google.
