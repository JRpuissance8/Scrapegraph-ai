"""
Etape 2 : complete site web, email et telephone des prospects de l'etape 1.

Pour chaque entreprise :
  1. recherche du site :
     - avec GOOGLE_PLACES_API_KEY : API Google Places (site + telephone de
       l'etablissement, retenu si le code postal correspond) ;
     - sinon : recherche Bing "<nom> <commune>", hors annuaires et reseaux sociaux ;
  2. lecture du site (accueil, contact, mentions legales) ;
  3. un site trouve par Bing n'est retenu que s'il cite le SIREN, ou a defaut
     le nom et le code postal ;
  4. extraction de l'email et du telephone sur les pages lues.

La colonne "confiance" dit comment le site a ete valide :
siren, nom+cp, google+siren ou google.
Les resultats sont mis en cache : relancer le script reprend ou il s'est arrete.

Usage :
    pip install requests
    export GOOGLE_PLACES_API_KEY=...   # recommande, voir README de l'etape 2
    python prospects_contacts.py
    python prospects_contacts.py --entree prospects_aix_marseille.csv --limite 20
"""

import argparse
import base64
import csv
import html
import json
import os
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
PAUSE = 1.5          # secondes entre deux requetes
CANDIDATS = 3        # sites testes par entreprise
PAGES_MAX = 4        # pages lues par site (accueil compris)

# Domaines qui ne sont pas le site de l'entreprise.
EXCLUS = (
    "societe.com", "pappers.fr", "pagesjaunes.fr", "infogreffe.fr", "verif.com",
    "manageo.fr", "corporama.com", "score3.fr", "societeinfo.com", "lefigaro.fr",
    "annuaire-entreprises.data.gouv.fr", "data.gouv.fr", "insee.fr", "bodacc.fr",
    "entreprises.lefigaro.fr", "infonet.fr", "dirigeant.societe.com", "kompass.com",
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.", "tripadvisor.", "thefork.",
    "lafourchette.", "ubereats.", "deliveroo.", "justeat.", "google.", "bing.com",
    "microsoft.com", "wikipedia.org", "mappy.com", "yelp.", "cylex", "hoodspot",
    "118712.fr", "118000.fr", "annuaire", "leboncoin.fr", "indeed.", "welcometothejungle",
    "doctolib.fr", "lacentrale.fr", "autoscout24.", "seloger.com", "bienici.com",
    "logic-immo.com", "whatsapp.com", "apple.com", "waze.com", "petitfute.com",
)

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_TEL = re.compile(r"(?<![\d+])(?:\+33\s?\(?0?\)?\s?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
RE_LIEN = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
MOTS_PAGES = ("contact", "mention", "legal", "cgv", "a-propos", "qui-sommes")
EMAIL_BRUIT = ("example.", "sentry", "wixpress", "domain.", "email.com", "votre",
               "nom@", "exemple", "@2x", "u003e")

session = requests.Session()
session.headers.update(HEADERS)
robots = {}


def norm(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", txt.lower()).strip()


def domaine(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def exclu(url):
    d = domaine(url)
    return not d or any(x in d for x in EXCLUS)


def autorise(url):
    """Respecte robots.txt (en cas de doute, on autorise)."""
    racine = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    if racine not in robots:
        rp = RobotFileParser()
        try:
            r = session.get(racine + "/robots.txt", timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        robots[racine] = rp
    return robots[racine].can_fetch(UA, url)


def telecharger(url):
    if not autorise(url):
        return ""
    try:
        r = session.get(url, timeout=15)
    except requests.RequestException:
        return ""
    time.sleep(PAUSE)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return ""
    return r.text


def lien_bing(href):
    """Les liens Bing passent par /ck/a?...&u=a1<base64>."""
    href = html.unescape(href)
    if "bing.com/ck/" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if not u.startswith("a1"):
        return ""
    b = u[2:]
    try:
        return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
    except ValueError:
        return ""


def recherche_bing(requete):
    for i in range(3):
        try:
            r = session.get("https://www.bing.com/search", timeout=20,
                            params={"q": requete, "setlang": "fr", "cc": "FR"})
            break
        except requests.RequestException:
            time.sleep(2 ** (i + 1))
    else:
        return []
    time.sleep(PAUSE)
    vus, urls = set(), []
    for href in re.findall(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', r.text):
        url = lien_bing(href)
        if not url.startswith("http") or exclu(url):
            continue
        d = domaine(url)
        if d not in vus:
            vus.add(d)
            urls.append(url)
    return urls


def nom_recherche(p):
    """Nom commercial : 'LES INITIES (GUEULETON AIX)' -> 'GUEULETON AIX'."""
    nom = p.get("enseigne") or ""
    if not nom:
        entre = re.findall(r"\((.*?)\)", p["nom"])
        nom = entre[-1] if entre else re.sub(r"\(.*?\)", " ", p["nom"])
    nom = re.sub(r"\b(SAS|SASU|SARL|EURL|SA|SCI|SNC|SELARL|HOLDING)\b", " ", nom, flags=re.I)
    return re.sub(r"\s+", " ", nom).strip()


def pages_site(url):
    """Accueil + pages contact / mentions legales du meme domaine."""
    accueil = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    textes = {}
    for depart in dict.fromkeys([url, accueil]):
        t = telecharger(depart)
        if t:
            textes[depart] = t
            break
    if not textes:
        return {}
    d = domaine(accueil)
    liens = []
    for t in list(textes.values()):
        for href in RE_LIEN.findall(t):
            lien = urljoin(accueil, html.unescape(href))
            if domaine(lien) == d and any(m in lien.lower() for m in MOTS_PAGES):
                liens.append(lien.split("?")[0])
    for lien in list(dict.fromkeys(liens))[: PAGES_MAX - 1]:
        if lien not in textes:
            t = telecharger(lien)
            if t:
                textes[lien] = t
    return textes


def confiance(textes, p):
    brut = " ".join(textes.values())
    chiffres = re.sub(r"\D", "", brut)
    if p["siren"] and p["siren"] in chiffres:
        return "siren"
    texte = norm(re.sub(r"<[^>]+>", " ", brut))
    mots = [m for m in norm(nom_recherche(p)).split() if len(m) > 2]
    if p.get("code_postal") and p["code_postal"] in texte and mots \
            and all(m in texte for m in mots[:3]):
        return "nom+cp"
    return ""


def extraire(textes, site):
    brut = " ".join(textes.values())
    d = domaine(site)
    emails = re.findall(r"mailto:([^\"'?>\s]+)", brut, re.I) + RE_EMAIL.findall(brut)
    propres = []
    for e in emails:
        e = html.unescape(e).strip(".").lower()
        if RE_EMAIL.fullmatch(e) and not any(b in e for b in EMAIL_BRUIT) \
                and not e.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            propres.append(e)
    propres = list(dict.fromkeys(propres))
    # priorite aux adresses du domaine du site
    propres.sort(key=lambda e: 0 if e.split("@")[1].removeprefix("www.") == d else 1)

    texte = html.unescape(re.sub(r"<[^>]+>", " ", brut))
    tels = re.findall(r"tel:([+\d\s.\-()]+)", brut, re.I) + RE_TEL.findall(texte)
    numeros = []
    for t in tels:
        n = re.sub(r"\D", "", t)
        if n.startswith("33"):
            n = "0" + n[2:].lstrip("0")
        if len(n) == 10 and n[0] == "0" and n[1] != "0":
            numeros.append(" ".join(n[i:i + 2] for i in range(0, 10, 2)))
    numeros = list(dict.fromkeys(numeros))
    return (propres[0] if propres else ""), (numeros[0] if numeros else "")


def recherche_google(p, cle):
    """API Places (New), Text Search : premier lieu au bon code postal."""
    corps = {"textQuery": f"{nom_recherche(p)} {p.get('adresse', '')}",
             "languageCode": "fr", "regionCode": "FR", "pageSize": 5}
    try:
        lat, lng = float(p["latitude"]), float(p["longitude"])
        corps["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng},
                                            "radius": 2000.0}}
    except (KeyError, TypeError, ValueError):
        pass
    entetes = {"X-Goog-Api-Key": cle, "X-Goog-FieldMask":
               "places.displayName,places.formattedAddress,places.websiteUri,"
               "places.nationalPhoneNumber,places.businessStatus"}
    for i in range(3):
        try:
            r = requests.post("https://places.googleapis.com/v1/places:searchText",
                              json=corps, headers=entetes, timeout=20)
        except requests.RequestException:
            time.sleep(2 ** (i + 1))
            continue
        if r.status_code in (429, 500, 503):
            time.sleep(2 ** (i + 1))
            continue
        r.raise_for_status()
        break
    else:
        return None
    for lieu in r.json().get("places", []):
        if p.get("code_postal") and p["code_postal"] in lieu.get("formattedAddress", ""):
            return lieu
    return None


def enrichir(p):
    cle = os.getenv("GOOGLE_PLACES_API_KEY")
    if cle:
        lieu = recherche_google(p, cle)
        if lieu is None:
            return {"site_web": "", "email": "", "telephone": "", "confiance": ""}
        site = lieu.get("websiteUri", "")
        tel = lieu.get("nationalPhoneNumber", "")
        email, niveau = "", "google"
        if site and not exclu(site):
            textes = pages_site(site)
            if textes:
                email, tel_site = extraire(textes, site)
                tel = tel or tel_site
                if confiance(textes, p) == "siren":
                    niveau = "google+siren"
        return {"site_web": site, "email": email, "telephone": tel, "confiance": niveau}

    nom = nom_recherche(p)
    for url in recherche_bing(f"{nom} {p.get('commune', '')}")[:CANDIDATS]:
        textes = pages_site(url)
        if not textes:
            continue
        niveau = confiance(textes, p)
        if niveau:
            site = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            email, tel = extraire(textes, site)
            return {"site_web": site, "email": email, "telephone": tel,
                    "confiance": niveau}
    return {"site_web": "", "email": "", "telephone": "", "confiance": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entree", default="prospects_aix_marseille.csv")
    ap.add_argument("--sortie", default="prospects_aix_marseille_contacts.csv")
    ap.add_argument("--cache", default="prospects_contacts_cache.json")
    ap.add_argument("--limite", type=int, default=0, help="n'enrichir que les N premiers")
    args = ap.parse_args()

    with open(args.entree, encoding="utf-8-sig") as f:
        prospects = list(csv.DictReader(f, delimiter=";"))
    cache = {}
    if os.path.exists(args.cache):
        with open(args.cache, encoding="utf-8") as f:
            cache = json.load(f)

    a_traiter = prospects[: args.limite] if args.limite else prospects
    for i, p in enumerate(a_traiter, 1):
        cle = p.get("siret") or p["siren"]
        if cle not in cache:
            try:
                cache[cle] = enrichir(p)
            except Exception as err:  # une entreprise ne doit pas bloquer le lot
                print(f"  {p['nom']} : erreur {type(err).__name__}, ignoree")
                continue
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        r = cache[cle]
        print(f"[{i}/{len(a_traiter)}] {p['nom'][:40]:40} "
              f"{r['site_web'] or '-'} {r['email'] or ''} {r['telephone'] or ''}")

    champs = list(prospects[0].keys())
    if "confiance" not in champs:
        champs.append("confiance")
    for p in prospects:
        r = cache.get(p.get("siret") or p["siren"])
        if r:
            p.update(r)
        p.setdefault("confiance", "")
    with open(args.sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        w.writerows(prospects)

    trouves = [cache[k] for k in cache]
    print(f"\n{len(trouves)} entreprises traitees : "
          f"{sum(1 for r in trouves if r['site_web'])} sites, "
          f"{sum(1 for r in trouves if r['email'])} emails, "
          f"{sum(1 for r in trouves if r['telephone'])} telephones")
    print(f"Fichier ecrit : {args.sortie}")


if __name__ == "__main__":
    main()
