#!/usr/bin/env python3
"""Résume une liste d'articles RSS (lue sur stdin) en un brief markdown via l'API Anthropic.

Stdlib uniquement (urllib) — pas de SDK, conformément aux contraintes du projet.
La clé API est lue dans la variable d'environnement ANTHROPIC_API_KEY.

Entrée attendue sur stdin : la sortie texte de news_brief.py (articles groupés par
catégorie). Sortie : le brief markdown sur stdout.

Usage:
    python3 news_brief.py ... | python3 summarize.py --kind daily
    python3 news_brief.py ... | python3 summarize.py --kind weekly
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date

API_URL = "https://api.anthropic.com/v1/messages"
# ↓↓↓ Un seul endroit à changer pour arbitrer coût / qualité :
#   claude-opus-4-7   (le plus capable, ~le plus cher)
#   claude-sonnet-4-6 (bon compromis)
#   claude-haiku-4-5  (le moins cher, suffisant pour un digest d'actu)
MODEL = "claude-sonnet-4-6"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 4000

SYSTEM_DAILY = """Tu produis le brief d'actualité matinal de Jérémie, en français, à partir d'une liste d'articles RSS déjà récupérés (fournie par l'utilisateur).

Rédige un brief hiérarchisé et SÉLECTIF :
- Une section par catégorie, dans cet ordre : Featured (actu générale), Tech FR, Tech EN, Apple, Android, Media/Adtech. Préfixe chaque titre d'un emoji.
- GARDE uniquement ce qui compte vraiment : actu de fond, annonces produit ou stratégiques majeures, sécurité/cyber, business (levées, résultats, M&A), décisions réglementaires.
- ÉCARTE systématiquement : bons plans, codes promo, ventes flash, tests/comparatifs produits, listes putaclic ("X choses à savoir", "voici pourquoi…"), articles lifestyle.
- Pour chaque item retenu : une seule phrase de synthèse suivie du lien markdown vers la source. Regroupe les doublons (même sujet, plusieurs sources) en un seul item multi-liens.
- Vise 15 à 20 items au total. Si une catégorie n'a rien d'important, écris « RAS ».
- Commence par un titre « ☕ Brief du [date fournie, en toutes lettres] ».

RÈGLE ABSOLUE : n'utilise QUE les articles fournis, avec leurs liens exacts. N'invente aucun article, n'ajoute rien de ta propre connaissance, n'utilise pas de sources qui ne sont pas dans la liste. Si la liste est pauvre, fais un brief court — ne comble jamais avec du contenu inventé."""

SYSTEM_WEEKLY = """Tu produis le digest hebdomadaire de Jérémie, en français, à partir d'une liste d'articles RSS déjà récupérés (fournie par l'utilisateur), couvrant les 7 derniers jours sur des thématiques à publication lente.

Rédige un digest hiérarchisé et SÉLECTIF :
- Une section par catégorie ayant du contenu intéressant, préfixée d'un emoji. Ordre suggéré : Analytics/Data, Culture, Mode, Photo, Luxe, Paris, Vélo, Automobile, Social, Blog, Jobs.
- GARDE ce qui a une vraie valeur : analyses de fond, tendances, sorties/événements marquants, retours d'expérience, guides utiles. Pour Analytics/Data, privilégie articles techniques et études.
- ÉCARTE le promotionnel pur, les redites et le contenu sans intérêt.
- Pour chaque item : une phrase de synthèse + lien markdown vers la source. Regroupe les doublons.
- Vise 15 à 25 items au total. Une catégorie sans rien d'intéressant peut être omise.
- Commence par un titre « 📚 Digest de la semaine — [date fournie] ».

RÈGLE ABSOLUE : n'utilise QUE les articles fournis, avec leurs liens exacts. N'invente aucun article, n'ajoute rien de ta propre connaissance. Si la liste est pauvre, fais un digest court — ne comble jamais avec du contenu inventé."""


def call_anthropic(system, user_text, api_key):
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        # cache_control sur le bloc system : hygiène correcte, mais sans effet réel
        # ici (TTL 5 min/1 h, appels espacés de 24 h → cache toujours froid ; de plus
        # ce prompt système fait < 4096 tokens, seuil de cache d'Opus 4.7).
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": user_text}],
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    last_err = None
    for attempt in range(5):
        req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read())
            return "".join(
                b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (408, 409, 429) or e.code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise SystemExit(f"Erreur API non récupérable : {last_err}")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
            time.sleep(min(2 ** attempt, 30))
    raise SystemExit(f"Échec après plusieurs tentatives : {last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["daily", "weekly"], required=True)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY manquante dans l'environnement.")

    articles = sys.stdin.read().strip()

    # Garde-fou anti-hallucination : news_brief.py ne produit de sections "## " que
    # s'il a récupéré des articles. Aucune section => aucune donnée => on s'arrête
    # sans rien inventer (c'est exactement le piège du sandbox cloud qu'on évite ici).
    if "## " not in articles:
        print(
            f"# ⚠️ Aucun article récupéré ({date.today().isoformat()})\n\n"
            "Le script de récupération n'a renvoyé aucun article "
            "(flux injoignables ou fenêtre vide). Brief non généré — "
            "aucun contenu inventé."
        )
        return

    system = SYSTEM_DAILY if args.kind == "daily" else SYSTEM_WEEKLY
    user_text = (
        f"Date du jour : {date.today().isoformat()}.\n\n"
        "Voici les articles bruts des flux RSS, groupés par catégorie "
        "(format : - [source] titre / lien). Rédige le brief en suivant "
        "strictement les consignes système et en n'utilisant QUE ces articles :\n\n"
        + articles
    )

    print(call_anthropic(system, user_text, api_key))


if __name__ == "__main__":
    main()
