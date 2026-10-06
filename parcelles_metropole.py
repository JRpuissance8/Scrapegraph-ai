"""
Parcelles cadastrales detenues par la Metropole d'Aix-Marseille-Provence.

Sources (open data, sans cle) :
  - DGFiP, "Fichiers des parcelles des personnes morales" (data.economie.gouv.fr) :
    parcelle, adresse, contenance, nature de culture, SIREN et nom du proprietaire ;
  - cadastre Etalab (cadastre.data.gouv.fr) : contour de la parcelle, d'ou le
    centroide (latitude / longitude) et les liens cartes.

Proprietaires retenus :
  - la Metropole (SIREN 200054807) et ses identifiants DGFiP sans SIREN ("U...") ;
  - les intercommunalites fusionnees dans la Metropole au 1er janvier 2016
    (colonne statut = "ex-EPCI") : le cadastre n'a pas toujours ete mis a jour,
    mais ces biens appartiennent desormais a la Metropole.

Seules les personnes morales figurent dans ce fichier DGFiP : c'est suffisant ici.

Usage :
    pip install requests
    python parcelles_metropole.py
    python parcelles_metropole.py --sans-geo          # sans le cadastre Etalab
    python parcelles_metropole.py --zip pm2025.zip    # zip DGFiP deja telecharge
"""

import argparse
import csv
import gzip
import io
import json
import os
import re
import time
import zipfile
from collections import OrderedDict

import requests

URL_ZIP = ("https://data.economie.gouv.fr/api/v2/catalog/datasets/"
           "fichiers-des-locaux-et-des-parcelles-des-personnes-morales/attachments/"
           "fichier_des_parcelles_situation_2025_dpts_01_a_56_zip")
FICHIER_DEPT = "PM_25_NB_130.csv"
URL_CADASTRE = ("https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/"
                "communes/13/{insee}/cadastre-{insee}-parcelles.json.gz")

METROPOLE = "200054807"
# Intercommunalites fusionnees dans la Metropole le 1er janvier 2016.
EX_EPCI = {
    "241300391": "CU Marseille Provence Metropole",
    "241300227": "CC Marseille Provence Metropole (avant 2001)",
    "U15656069": "CU Marseille Provence Metropole",
    "U26748262": "CU Marseille Provence Metropole (ports de plaisance)",
    "241300276": "CA du Pays d'Aix",
    "241300177": "SAN Ouest Provence",
    "241300409": "CA du Pays de Martigues",
    "241300201": "CA Agglopole Provence (Salon Etang de Berre Durance)",
    "U11649335": "CA Agglopole Provence (Salon Etang de Berre Durance)",
    "241300268": "CA du Pays d'Aubagne et de l'Etoile",
}
RE_METROPOLE = re.compile(r"METROPOLE D.?AIX.?MARSEILLE.?PROVENCE")

# Colonnes du fichier DGFiP 2025 (l'entete contient deux "Contenance").
DEP, DIR, COM, NOM_COM, PREFIXE, SECTION, PLAN, VOIRIE, INDICE = range(9)
NATURE_VOIE, NOM_VOIE, CONTENANCE, SUF, CULTURE, CONT_SUF, DROIT = 11, 12, 13, 14, 15, 16, 17
SIREN, DENOMINATION = 19, 23


def get(url, tentatives=8, **kw):
    for i in range(tentatives):
        try:
            r = requests.get(url, timeout=120, **kw)
        except (requests.ConnectionError, requests.Timeout) as err:
            print(f"  erreur reseau ({type(err).__name__}), nouvel essai {i + 1}/{tentatives}")
            time.sleep(min(2 ** (i + 1), 30))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(2 ** (i + 1), 30))
            continue
        return r
    raise RuntimeError(f"Echec apres {tentatives} essais : {url}")


def lire_dgfip(chemin_zip):
    if not os.path.exists(chemin_zip):
        print(f"Telechargement du fichier DGFiP (~245 Mo) vers {chemin_zip}")
        r = get(URL_ZIP)
        r.raise_for_status()
        with open(chemin_zip, "wb") as f:
            f.write(r.content)
    with zipfile.ZipFile(chemin_zip) as z:
        nom = next(n for n in z.namelist() if n.endswith(FICHIER_DEPT))
        with z.open(nom) as f:
            lignes = list(csv.reader(io.TextIOWrapper(f, encoding="utf-8"), delimiter=";"))
    return lignes[1:]


def statut(ligne):
    siren = ligne[SIREN]
    if siren == METROPOLE or RE_METROPOLE.search(ligne[DENOMINATION]):
        return "Metropole"
    if siren in EX_EPCI:
        return "ex-EPCI"
    return ""


def insee(ligne):
    return ligne[DEP] + ligne[COM]


def idu(ligne):
    """Identifiant cadastral a 14 caracteres, celui du cadastre Etalab."""
    return (insee(ligne) + (ligne[PREFIXE] or "000").zfill(3)
            + ligne[SECTION].strip().zfill(2) + ligne[PLAN].zfill(4))


def adresse(ligne):
    numero = ligne[VOIRIE].lstrip("0") + ligne[INDICE]
    voie = " ".join(x for x in (ligne[NATURE_VOIE], ligne[NOM_VOIE]) if x)
    return " ".join(x for x in (numero, voie) if x)


def parcelles(lignes):
    """Une ligne par parcelle (le fichier DGFiP a une ligne par subdivision et par droit)."""
    res = OrderedDict()
    for lg in lignes:
        st = statut(lg)
        if not st:
            continue
        cle = idu(lg)
        p = res.setdefault(cle, {
            "idu": cle,
            "code_insee": insee(lg),
            "commune": lg[NOM_COM],
            "section": lg[SECTION].strip(),
            "numero": lg[PLAN],
            "prefixe": lg[PREFIXE],
            "adresse": adresse(lg),
            "contenance_m2": int(lg[CONTENANCE] or 0),
            "natures_culture": [],
            "statut": st,
            "titulaire_cadastre": [],
            "siren_titulaire": [],
            "droit": [],
        })
        if st == "Metropole":
            p["statut"] = "Metropole"
        culture = lg[CULTURE].split(" - ", 1)[-1]
        if culture and culture not in p["natures_culture"]:
            p["natures_culture"].append(culture)
        nom = EX_EPCI.get(lg[SIREN], lg[DENOMINATION]) if st == "ex-EPCI" else lg[DENOMINATION]
        for champ, val in (("titulaire_cadastre", nom), ("siren_titulaire", lg[SIREN]),
                           ("droit", lg[DROIT].split(" - ", 1)[-1].strip())):
            if val not in p[champ]:
                p[champ].append(val)
    return list(res.values())


def centroide(geom):
    """Centroide du plus grand anneau exterieur (formule de l'aire signee)."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    meilleur, aire_max = None, -1.0
    for poly in polys:
        pts = poly[0]
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            c = x1 * y2 - x2 * y1
            a += c
            cx += (x1 + x2) * c
            cy += (y1 + y2) * c
        if a and abs(a) > aire_max:
            aire_max = abs(a)
            meilleur = (cy / (3 * a), cx / (3 * a))
    if meilleur is None:
        x, y = polys[0][0][0]
        meilleur = (y, x)
    return round(meilleur[0], 6), round(meilleur[1], 6)


def geolocaliser(liste, dossier_cache):
    os.makedirs(dossier_cache, exist_ok=True)
    par_commune = {}
    for p in liste:
        par_commune.setdefault(p["code_insee"], []).append(p)
    for i, (code, ps) in enumerate(sorted(par_commune.items()), 1):
        chemin = os.path.join(dossier_cache, f"{code}.json.gz")
        if not os.path.exists(chemin):
            try:
                r = get(URL_CADASTRE.format(insee=code))
            except RuntimeError as err:
                print(f"  {code} : {err}, relancer le script pour reprendre")
                continue
            if r.status_code != 200:
                print(f"  {code} : cadastre indisponible ({r.status_code})")
                continue
            with open(chemin, "wb") as f:
                f.write(r.content)
        with gzip.open(chemin, "rt", encoding="utf-8") as f:
            geo = {ft["id"]: ft for ft in json.load(f)["features"]}
        trouves = 0
        for p in ps:
            ft = geo.get(p["idu"])
            if ft and ft.get("geometry"):
                p["latitude"], p["longitude"] = centroide(ft["geometry"])
                trouves += 1
        print(f"[{i}/{len(par_commune)}] {code} {ps[0]['commune']}: "
              f"{trouves}/{len(ps)} parcelles localisees")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="parcelles_pm_2025.zip", help="zip DGFiP (telecharge si absent)")
    ap.add_argument("--sortie", default="parcelles_metropole_amp.csv")
    ap.add_argument("--cache", default="cadastre_cache", help="dossier des fichiers cadastre Etalab")
    ap.add_argument("--sans-geo", action="store_true", help="ne pas calculer lat/lon")
    args = ap.parse_args()

    liste = parcelles(lire_dgfip(args.zip))
    if not args.sans_geo:
        geolocaliser(liste, args.cache)

    champs = ["idu", "code_insee", "commune", "prefixe", "section", "numero", "adresse",
              "contenance_m2", "natures_culture", "statut", "titulaire_cadastre",
              "siren_titulaire", "droit", "latitude", "longitude", "google_maps", "geoportail"]
    for p in liste:
        for champ in ("natures_culture", "titulaire_cadastre", "siren_titulaire", "droit"):
            p[champ] = ", ".join(p[champ])
        lat, lon = p.get("latitude"), p.get("longitude")
        p["google_maps"] = f"https://www.google.com/maps?q={lat},{lon}" if lat else ""
        p["geoportail"] = (f"https://www.geoportail.gouv.fr/carte?c={lon},{lat}&z=19"
                           "&l0=CADASTRALPARCELS.PARCELLAIRE_EXPRESS::GEOPORTAIL:OGC:WMTS(1)"
                           "&l1=ORTHOIMAGERY.ORTHOPHOTOS::GEOPORTAIL:OGC:WMTS(1)&permalink=yes"
                           if lat else "")
    liste.sort(key=lambda p: (p["commune"], p["section"], p["numero"]))
    with open(args.sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(liste)

    metro = sum(1 for p in liste if p["statut"] == "Metropole")
    surface = sum(p["contenance_m2"] for p in liste) / 10000
    geo = sum(1 for p in liste if p.get("latitude"))
    print(f"\n{len(liste)} parcelles ({metro} au nom de la Metropole, "
          f"{len(liste) - metro} au nom d'un ex-EPCI), {surface:,.0f} ha, "
          f"{geo} localisees")
    print(f"Fichier ecrit : {args.sortie}")


if __name__ == "__main__":
    main()
