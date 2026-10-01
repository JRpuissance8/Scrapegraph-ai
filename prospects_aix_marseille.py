"""
Liste d'entreprises annonceurs potentiels autour d'Aix-Marseille.
Source : API Recherche d'entreprises (data.gouv.fr), sans cle, donnees Sirene.

Domaine a autoriser dans l'environnement Custom :
    recherche-entreprises.api.gouv.fr

Usage :
    pip install requests
    python prospects_aix_marseille.py
    python prospects_aix_marseille.py --communes 13055,13001 --effectif 03,11,12
"""

import argparse
import csv
import time

import requests

URL = "https://recherche-entreprises.api.gouv.fr/search"

# Secteurs cibles (code NAF : libelle). Modifier librement.
NAF = {
    "45.11Z": "Commerce de voitures",
    "68.31Z": "Agences immobilieres",
    "41.10A": "Promotion immobiliere de logements",
    "41.10B": "Promotion immobiliere de bureaux",
    "47.52B": "Bricolage, quincaillerie",
    "93.13Z": "Salles de sport",
    "56.10A": "Restauration traditionnelle",
    "85.59A": "Formation continue adultes",
}

DEPARTEMENT = "13"  # Bouches-du-Rhone
PER_PAGE = 25       # maximum accepte par l'API
MAX_PAGES = 400     # l'API plafonne a 10 000 resultats par recherche


def get(params, tentatives=5):
    for i in range(tentatives):
        try:
            r = requests.get(URL, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as err:
            print(f"  erreur reseau ({type(err).__name__}), nouvel essai {i + 1}/{tentatives}")
            time.sleep(2 ** (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Echec apres plusieurs essais (reseau, 429 ou 5xx), reessayer plus tard")


def recherche(naf, communes, effectif):
    page = 1
    while page <= MAX_PAGES:
        params = {
            "activite_principale": naf,
            "etat_administratif": "A",
            "per_page": PER_PAGE,
            "page": page,
        }
        if communes:
            params["code_commune"] = communes
        else:
            params["departement"] = DEPARTEMENT
        if effectif:
            params["tranche_effectif_salarie"] = effectif

        data = get(params)
        resultats = data.get("results", [])
        if not resultats:
            return
        for e in resultats:
            yield e
        if page >= data.get("total_pages", 1):
            return
        page += 1
        time.sleep(0.2)  # reste sous la limite de 7 requetes/seconde


def ligne(e, naf):
    siege = e.get("siege") or {}
    dirigeants = e.get("dirigeants") or []
    gerant = ""
    if dirigeants:
        d = dirigeants[0]
        gerant = (d.get("nom") or d.get("denomination") or "")
        prenoms = d.get("prenoms")
        if prenoms:
            gerant = f"{prenoms} {gerant}"
    return {
        "siren": e.get("siren"),
        "nom": e.get("nom_complet"),
        "secteur": NAF.get(naf, naf),
        "naf": e.get("activite_principale"),
        "effectif_tranche": e.get("tranche_effectif_salarie"),
        "adresse": siege.get("adresse"),
        "code_postal": siege.get("code_postal"),
        "commune": siege.get("libelle_commune"),
        "latitude": siege.get("latitude"),
        "longitude": siege.get("longitude"),
        "dirigeant": gerant.strip(),
        "date_creation": e.get("date_creation"),
        "site_web": "",  # a completer a l'etape 2 (scraping des sites)
        "email": "",
        "telephone": "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--communes", default="",
                    help="codes INSEE separes par des virgules (ex: 13055 Marseille, 13001 Aix)")
    ap.add_argument("--effectif", default="",
                    help="tranches d'effectif INSEE separees par des virgules (ex: 03,11,12)")
    ap.add_argument("--sortie", default="prospects_aix_marseille.csv")
    args = ap.parse_args()

    vus = set()
    lignes = []
    for naf in NAF:
        n = 0
        try:
            for e in recherche(naf, args.communes, args.effectif):
                siren = e.get("siren")
                if siren in vus:
                    continue
                vus.add(siren)
                lignes.append(ligne(e, naf))
                n += 1
        except (RuntimeError, requests.RequestException) as err:
            print(f"{naf} {NAF[naf]} : interrompu apres {n} entreprises ({err})")
            continue
        print(f"{naf} {NAF[naf]} : {n} entreprises")

    if not lignes:
        print("Aucun resultat")
        return

    with open(args.sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(lignes)
    print(f"{len(lignes)} lignes ecrites dans {args.sortie}")


if __name__ == "__main__":
    main()
