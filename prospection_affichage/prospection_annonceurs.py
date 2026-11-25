"""
Find local advertisers for the Puissance 8 billboard inventory.

Pipeline:
  1. Load the billboard inventory (CSV extracted from the Drive spec sheets).
  2. Group billboards into zones (8 km radius).
  3. For each zone and each sector query, call Google Places Text Search to
     list potential advertisers.
  4. Keep those within reach of a billboard (radius set per sector).
  5. Look for a contact email on each advertiser's website.
  6. Attach the nearest billboard and draft an individual email (in French).

Usage:
  export GOOGLE_PLACES_API_KEY=...
  python prospection_annonceurs.py --panneaux inventaire_panneaux.csv --sortie data/

Places responses are cached in <sortie>/cache_places/, so re-running the
script does not pay again for queries already made.
"""

import argparse
import concurrent.futures as cf
import csv
import hashlib
import html
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

# --------------------------------------------------------------------------
# Target sectors
# rayon_km: max distance between the advertiser and the proposed billboard
# priorite: 1 = most likely buyer of outdoor advertising
# --------------------------------------------------------------------------
SECTEURS = {
    "concession": {
        "libelle": "Concessionnaire",
        "requetes": ["concessionnaire automobile", "concession moto"],
        "rayon_km": 10,
        "priorite": 1,
        "accroche": (
            "Avant de se déplacer, les acheteurs comparent les concessions du "
            "secteur. Un panneau sur leur trajet quotidien vous place en tête "
            "de liste au moment où ils choisissent."
        ),
    },
    "promoteur": {
        "libelle": "Promoteur / constructeur",
        "requetes": [
            "promoteur immobilier",
            "constructeur maison individuelle",
        ],
        "rayon_km": 20,
        "priorite": 1,
        "accroche": (
            "Pour la commercialisation de vos programmes, un panneau grand "
            "format sur un axe local touche chaque jour les habitants du "
            "secteur, ceux qui achètent dans le neuf à proximité."
        ),
    },
    "agence_immo": {
        "libelle": "Agence immobilière",
        "requetes": ["agence immobilière"],
        "rayon_km": 4,
        "priorite": 2,
        "accroche": (
            "Les mandats se gagnent par la notoriété locale. Un panneau près "
            "de votre agence vous rend visible auprès des propriétaires du "
            "quartier qui pensent à vendre."
        ),
    },
    "pisciniste": {
        "libelle": "Pisciniste",
        "requetes": ["pisciniste", "construction piscine"],
        "rayon_km": 15,
        "priorite": 1,
        "accroche": (
            "Les projets piscine se décident entre janvier et mai. Un panneau "
            "posé dès janvier vous rend visible pendant toute la période où "
            "vos futurs clients demandent des devis."
        ),
    },
    "btp": {
        "libelle": "BTP / rénovation",
        "requetes": ["véranda pergola", "entreprise rénovation maison"],
        "rayon_km": 10,
        "priorite": 2,
        "accroche": (
            "Les particuliers choisissent souvent une entreprise qu'ils ont "
            "déjà vue près de chez eux. Un panneau sur un axe local installe "
            "votre nom dans leur esprit avant qu'ils lancent leurs travaux."
        ),
    },
    "sante": {
        "libelle": "Clinique / centre de santé",
        "requetes": ["clinique privée", "centre dentaire", "clinique esthétique"],
        "rayon_km": 10,
        "priorite": 2,
        "accroche": (
            "Un panneau à proximité fait connaître votre établissement et vos "
            "spécialités auprès des patients du secteur."
        ),
    },
    "ecole": {
        "libelle": "École privée / formation",
        "requetes": ["école privée", "enseignement supérieur privé", "auto-école"],
        "rayon_km": 10,
        "priorite": 2,
        "accroche": (
            "Les inscriptions et les journées portes ouvertes se jouent entre "
            "janvier et avril. Un panneau à proximité rappelle votre "
            "établissement aux familles du secteur pendant toute cette période."
        ),
    },
    "franchise": {
        "libelle": "Enseigne / franchise",
        "requetes": ["cuisiniste", "magasin de literie", "salle de sport"],
        "rayon_km": 6,
        "priorite": 2,
        "accroche": (
            "Votre clientèle vient de votre zone de chalandise. Un panneau sur "
            "un axe local vous rappelle à elle chaque jour, juste avant "
            "qu'elle se décide."
        ),
    },
}

SIGNATURE = "Puissance 8\n07 68 76 67 99\ncontact@puissance8.com"

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.primaryTypeDisplayName",
        "places.businessStatus",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)
ZONE_KM = 8
PAGES_MAX = 2  # 20 results per page

UA = {"User-Agent": "Mozilla/5.0 (compatible; prospection-bot/1.0)"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
EMAIL_EXCLUS = (
    "example.",
    "sentry",
    "wixpress",
    "domain.",
    "email.com",
    "votre",
    "your",
    "nom@",
    "prenom",
    "@2x",
    "exemple",
)
WEBMAILS = {
    "gmail.com",
    "orange.fr",
    "wanadoo.fr",
    "free.fr",
    "sfr.fr",
    "neuf.fr",
    "laposte.net",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "outlook.fr",
    "live.fr",
    "yahoo.fr",
    "yahoo.com",
    "icloud.com",
    "bbox.fr",
    "aol.com",
}
LOCAL_EXCLUS = re.compile(
    r"^(mediat|dpo|rgpd|gdpr|privacy|cnil|juridique|legal|webmaster|abuse|"
    r"noreply|no-reply|ne-pas-repondre|john\.doe|jean\.dupont)"
)
SITES_SANS_EMAIL = (
    "leboncoin.",
    "facebook.",
    "instagram.",
    "linktr.ee",
    "pagesjaunes.",
    "booksy.",
    "planity.",
    "doctolib.",
    "treatwell.",
    "sites.google.",
    "business.site",
)
EXT_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
LIENS_CONTACT = re.compile(r"contact|mentions|legal|nous-joindre|a-propos", re.I)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def charger_panneaux(chemin):
    panneaux = []
    with open(chemin, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if not r["latitude"]:
                continue
            r["lat"], r["lng"] = float(r["latitude"]), float(r["longitude"])
            panneaux.append(r)
    return panneaux


def zones(panneaux):
    groupes = []
    for p in panneaux:
        for g in groupes:
            if distance_km(p["lat"], p["lng"], g[0]["lat"], g[0]["lng"]) < ZONE_KM:
                g.append(p)
                break
        else:
            groupes.append([p])
    res = []
    for g in groupes:
        lat = sum(p["lat"] for p in g) / len(g)
        lng = sum(p["lng"] for p in g) / len(g)
        rayon = max(distance_km(lat, lng, p["lat"], p["lng"]) for p in g)
        res.append({"commune": g[0]["commune"], "lat": lat, "lng": lng, "rayon": rayon})
    return res


# --------------------------------------------------------------------------
# Google Places
# --------------------------------------------------------------------------
def places_recherche(cle, requete, lat, lng, rayon_m, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    resultats, token = [], None
    for page in range(PAGES_MAX):
        corps = {
            "textQuery": requete,
            "languageCode": "fr",
            "regionCode": "FR",
            "pageSize": 20,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": min(rayon_m, 50000),
                }
            },
        }
        if token:
            corps["pageToken"] = token
        h = hashlib.sha1(
            json.dumps([requete, round(lat, 4), round(lng, 4), rayon_m, page]).encode()
        ).hexdigest()
        fichier = cache_dir / f"{h}.json"
        if fichier.exists():
            data = json.loads(fichier.read_text())
        else:
            for essai in range(4):
                r = requests.post(
                    PLACES_URL,
                    json=corps,
                    timeout=30,
                    headers={
                        "X-Goog-Api-Key": cle,
                        "X-Goog-FieldMask": PLACES_FIELDS,
                    },
                )
                if r.status_code in (429, 500, 503):
                    time.sleep(2 * (essai + 1))
                    continue
                r.raise_for_status()
                break
            data = r.json()
            fichier.write_text(json.dumps(data))
        resultats.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return resultats


# --------------------------------------------------------------------------
# Email lookup on the advertiser website
# --------------------------------------------------------------------------
def _decode_cfemail(hexa):
    cle = int(hexa[:2], 16)
    return "".join(chr(int(hexa[i : i + 2], 16) ^ cle) for i in range(2, len(hexa), 2))


def _emails_page(texte):
    texte = html.unescape(texte)
    trouves = set(EMAIL_RE.findall(texte))
    for hexa in re.findall(r'data-cfemail="([0-9a-f]+)"', texte):
        try:
            trouves.add(_decode_cfemail(hexa))
        except ValueError:
            pass
    propres = set()
    for e in trouves:
        e = e.strip(".").lower()
        if e.endswith(EXT_IMAGE) or any(x in e for x in EMAIL_EXCLUS):
            continue
        propres.add(e)
    return propres


def _domaine(url):
    d = urlparse(url).netloc.lower().split(":")[0]
    return d[4:] if d.startswith("www.") else d


def chercher_email(site):
    """Never let one broken website stop the whole run."""
    try:
        return _chercher_email(site)
    except Exception as exc:  # malformed URLs, encoding errors, ...
        return "", f"erreur site: {type(exc).__name__}"


def _chercher_email(site):
    if not site:
        return "", ""
    if any(x in site for x in SITES_SANS_EMAIL):
        return "", "pas de vrai site (annuaire ou reseau social)"
    try:
        r = requests.get(site, headers=UA, timeout=12)
        if r.status_code >= 400:
            return "", "site inaccessible"
    except requests.RequestException:
        return "", "site inaccessible"
    emails = _emails_page(r.text)
    if not emails:
        liens = re.findall(r'href="([^"#]+)"', r.text)
        vus = set()
        for lien in liens:
            url = urljoin(r.url, lien)
            if (
                LIENS_CONTACT.search(lien)
                and urlparse(url).netloc == urlparse(r.url).netloc
                and url not in vus
            ):
                vus.add(url)
                try:
                    p = requests.get(url, headers=UA, timeout=12)
                    emails |= _emails_page(p.text)
                except requests.RequestException:
                    pass
            if emails or len(vus) >= 3:
                break
    if not emails:
        return "", "pas d'email sur le site"
    domaines_site = {_domaine(site), _domaine(r.url)}
    valides = []
    for e in emails:
        local, dom = e.split("@", 1)
        if LOCAL_EXCLUS.match(local):
            continue
        meme_site = any(dom == d or d.endswith("." + dom) for d in domaines_site)
        if meme_site or dom in WEBMAILS:
            valides.append((not meme_site, e))
    if not valides:
        return "", "email douteux ecarte: " + ", ".join(sorted(emails))[:120]
    # prefer the site domain, then contact@/info@, then the shortest
    valides.sort(
        key=lambda x: (
            x[0],
            x[1].split("@")[0] not in ("contact", "info", "accueil", "bonjour"),
            len(x[1]),
        )
    )
    return valides[0][1], ""


# --------------------------------------------------------------------------
# Message
# --------------------------------------------------------------------------
def phrase_distance(km):
    if km < 1:
        return f"à {int(round(km * 1000, -1))} m"
    return f"à {km:.1f} km".replace(".", ",")


def description_panneau(p):
    morceaux = []
    if p["surface_m2"]:
        morceaux.append(f"{p['surface_m2']} m²")
    if p["eclaire"].lower().startswith("oui"):
        morceaux.append("éclairé")
    lieu = re.sub(r",?\s*France$", "", (p["adresse"] or p["commune"]).strip())
    texte = (
        f"panneau {', '.join(morceaux)} situé {lieu}"
        if morceaux
        else f"panneau situé {lieu}"
    )
    if p["sens"]:
        sens = p["sens"].strip()
        if sens.lower().startswith("vers "):
            texte += f", visible en direction de {sens[5:]}"
        elif sens.lower().startswith("de "):
            texte += f", visible dans le sens {sens[0].lower() + sens[1:]}"
        else:
            texte += f", visible en direction de {sens}"
    return texte


def rediger(prospect, panneau, secteur):
    dist = phrase_distance(prospect["distance_km"])
    objet = f"{prospect['nom']} : un panneau publicitaire {dist} de chez vous"
    prix = ""
    if panneau["tarif_ht_an"]:
        prix = (
            f"La location est à {int(float(panneau['tarif_ht_an'])):,} € HT par an".replace(
                ",", " "
            )
            + " (taxe locale et frais techniques en sus)."
        )
    corps = (
        "Bonjour,\n\n"
        f"Nous disposons d'un {description_panneau(panneau)}, {dist} de votre "
        "établissement.\n\n"
        f"{secteur['accroche']}\n\n"
        f"{prix}\n\n"
        "Si cela vous intéresse, je vous envoie un photomontage avec votre "
        "visuel sur le panneau, sans engagement.\n\n"
        "Bien cordialement,\n\n"
        f"{SIGNATURE}"
    ).replace("\n\n\n\n", "\n\n")
    return objet, corps


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panneaux", required=True, help="CSV inventaire (;)")
    ap.add_argument("--sortie", default="data")
    ap.add_argument(
        "--secteurs", default=",".join(SECTEURS), help="comma-separated sector keys"
    )
    ap.add_argument("--sans-email-scan", action="store_true")
    args = ap.parse_args()

    cle = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not cle:
        raise SystemExit("GOOGLE_PLACES_API_KEY manquant")
    sortie = Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    cache = sortie / "cache_places"

    panneaux = charger_panneaux(args.panneaux)
    zs = zones(panneaux)
    secteurs = [s.strip() for s in args.secteurs.split(",") if s.strip()]
    print(f"{len(panneaux)} panneaux, {len(zs)} zones, {len(secteurs)} secteurs")

    # 1. Collect from Places
    bruts = {}
    nb_appels = 0
    for z in zs:
        for cle_s in secteurs:
            s = SECTEURS[cle_s]
            rayon_m = int((z["rayon"] + s["rayon_km"]) * 1000)
            for q in s["requetes"]:
                avant = len(list(cache.glob("*.json"))) if cache.exists() else 0
                for pl in places_recherche(cle, q, z["lat"], z["lng"], rayon_m, cache):
                    if pl.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                        continue
                    pid = pl["id"]
                    if pid not in bruts:
                        bruts[pid] = (pl, cle_s)
                nb_appels += len(list(cache.glob("*.json"))) - avant
        print(f"  zone {z['commune']}: {len(bruts)} etablissements cumules")
    print(f"{nb_appels} nouveaux appels Places")

    # 2. Keep advertisers within range of their nearest billboard
    prospects = []
    for pid, (pl, cle_s) in bruts.items():
        loc = pl.get("location") or {}
        if "latitude" not in loc:
            continue
        best = min(
            panneaux,
            key=lambda p: distance_km(
                loc["latitude"], loc["longitude"], p["lat"], p["lng"]
            ),
        )
        d = distance_km(loc["latitude"], loc["longitude"], best["lat"], best["lng"])
        if d > SECTEURS[cle_s]["rayon_km"]:
            continue
        prospects.append(
            {
                "place_id": pid,
                "nom": pl["displayName"]["text"],
                "secteur": cle_s,
                "categorie_google": (pl.get("primaryTypeDisplayName") or {}).get(
                    "text", ""
                ),
                "adresse": pl.get("formattedAddress", ""),
                "telephone": pl.get("nationalPhoneNumber", ""),
                "site": pl.get("websiteUri", ""),
                "maps": pl.get("googleMapsUri", ""),
                "distance_km": d,
                "panneau": best,
            }
        )

    # 3. National chains: same website domain on 3 or more locations
    domaines = Counter(
        urlparse(p["site"]).netloc.replace("www.", "") for p in prospects if p["site"]
    )
    for p in prospects:
        dom = urlparse(p["site"]).netloc.replace("www.", "") if p["site"] else ""
        p["chaine"] = "oui" if dom and domaines[dom] >= 3 else ""

    # 4. Emails
    if not args.sans_email_scan:
        cache_mail = sortie / "cache_emails.json"
        deja = json.loads(cache_mail.read_text()) if cache_mail.exists() else {}
        a_faire = sorted(
            {p["site"] for p in prospects if p["site"] and p["site"] not in deja}
        )
        print(f"Recherche d'email sur {len(a_faire)} sites...")
        with cf.ThreadPoolExecutor(16) as ex:
            for site, res in zip(a_faire, ex.map(chercher_email, a_faire)):
                deja[site] = res
        cache_mail.write_text(json.dumps(deja, ensure_ascii=False))
        for p in prospects:
            p["email"], p["note_email"] = deja.get(p["site"], ("", "pas de site"))
    else:
        for p in prospects:
            p["email"], p["note_email"] = "", ""

    for p in prospects:
        if p["email"] and any(x in p["site"] for x in SITES_SANS_EMAIL):
            p["email"], p["note_email"] = "", "site plateforme, pas d'email direct"

    # 5. Draft messages and export
    prospects.sort(
        key=lambda p: (
            SECTEURS[p["secteur"]]["priorite"],
            p["chaine"] == "oui",
            not p["email"],
            p["distance_km"],
        )
    )
    champs = [
        "priorite",
        "secteur",
        "nom",
        "email",
        "telephone",
        "adresse",
        "site",
        "chaine_nationale",
        "panneau_ref",
        "panneau_commune",
        "panneau_adresse",
        "panneau_surface_m2",
        "panneau_tarif_ht_an",
        "distance_m",
        "objet",
        "message",
        "note",
        "maps",
        "place_id",
    ]
    lignes = []
    emails_vus = set()
    for p in prospects:
        # one email = one message: keep the best-ranked location only
        if p["email"] in emails_vus:
            continue
        if p["email"]:
            emails_vus.add(p["email"])
        pn = p["panneau"]
        s = SECTEURS[p["secteur"]]
        objet, corps = rediger(p, pn, s)
        lignes.append(
            {
                "priorite": s["priorite"],
                "secteur": s["libelle"],
                "nom": p["nom"],
                "email": p["email"],
                "telephone": p["telephone"],
                "adresse": p["adresse"],
                "site": p["site"],
                "chaine_nationale": p["chaine"],
                "panneau_ref": pn["ref"] or pn["titre"],
                "panneau_commune": pn["commune"],
                "panneau_adresse": pn["adresse"],
                "panneau_surface_m2": pn["surface_m2"],
                "panneau_tarif_ht_an": pn["tarif_ht_an"],
                "distance_m": int(round(p["distance_km"] * 1000)),
                "objet": objet,
                "message": corps,
                "note": p["note_email"],
                "maps": p["maps"],
                "place_id": p["place_id"],
            }
        )
    out = sortie / "prospects_annonceurs.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        w.writerows(lignes)
    avec_mail = sum(1 for x in lignes if x["email"])
    print(f"{len(lignes)} prospects, dont {avec_mail} avec email -> {out}")
    print(Counter(x["secteur"] for x in lignes))


if __name__ == "__main__":
    main()
