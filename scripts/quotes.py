#!/usr/bin/env python3
"""Citations hebdomadaires — source unique partagée newsletter + dashboard.

La citation tourne au CALENDRIER (et non au numéro d'édition) : elle change
chaque VENDREDI, jour d'envoi de la newsletter, et reste affichée toute la
semaine (vendredi → jeudi) sur le tableau de bord. Le mail du vendredi et le
site montrent donc toujours la même citation, et deux newsletters
consécutives n'affichent jamais la même.

Exception de lancement : la citation #1 est affichée dès la mise en ligne
(mercredi 16/07/2026) et court jusqu'au jeudi 23/07/2026 inclus ; la #2
démarre le vendredi 24/07/2026 (= ROTATION_ANCHOR), puis rotation chaque
vendredi, cycle après la 21e.

Le dashboard embarque la même logique en JavaScript (côté navigateur) pour
que la bascule ait lieu le bon jour même sans régénération du site — toute
modification ici doit rester alignée avec le bloc QUOTES du template dans
build_dashboard.py (la liste et l'ancre y sont injectées depuis ce module).
"""

from __future__ import annotations

from datetime import date

# Vendredi de bascule vers la citation #2 (la #1 couvre tout ce qui précède).
ROTATION_ANCHOR = date(2026, 7, 24)

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


def quote_index(d: date) -> int:
    if d < ROTATION_ANCHOR:
        return 0
    return ((d - ROTATION_ANCHOR).days // 7 + 1) % len(QUOTES)


def quote_for_date(d: date) -> str:
    return QUOTES[quote_index(d)]
