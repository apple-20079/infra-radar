#!/usr/bin/env python3
"""Infra Radar: collecte quotidienne d'offres sur les sites de recrutement des employeurs.

Lit robot/firms.json, interroge les sources de chaque acteur (Workday, Greenhouse, Lever,
Ashby, SmartRecruiters, Oracle Recruiting Cloud), garde les postes juniors dans les 4 villes
cibles et écrit :
  data/jobs.json       offres retenues (avec date de publication et extrait de description)
  data/discovery.json  sources trouvées automatiquement pour chaque acteur
  data/status.json     bilan de l'exécution (par acteur : ok / erreur / nombre d'offres)

Uniquement des adresses publiques, une exécution par jour, requêtes espacées.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMS = os.path.join(ROOT, "robot", "firms.json")
DATA = os.path.join(ROOT, "data")
UA = "infra-radar/1.0 (personal job tracker; https://github.com/apple-20079/infra-radar)"
TODAY = dt.date.today()
TIERS = {int(t) for t in os.environ.get("RADAR_TIERS", "1").split(",")}
DISCOVERY_MAX_AGE_DAYS = 7
PAUSE = 0.4

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,fr;q=0.8"})


# ---------------------------------------------------------------- filtres
CITIES = {
    "London": [r"\blondon\b", r"\blondres\b"],
    "Paris": [r"\bparis\b", r"\bla d[ée]fense\b"],
    "New York": [r"\bnew york\b", r"\bnyc\b", r"\bny,", r"\bmanhattan\b"],
    "Hong Kong": [r"\bhong kong\b", r"\bhk\b", r"\bhksar\b"],
}
INCLUDE = re.compile(
    r"\b(analyst|analyste|graduate|grad\b|new analyst|campus|intern|internship|off[- ]?cycle|"
    r"stage|stagiaire|v\.?i\.?e\b|volontariat|international volunteer|junior|entry[- ]level|"
    r"early careers?|trainee|programme|program|associate analyst)\b", re.I)
EXCLUDE = re.compile(
    r"\b(senior|sr\.?|director|directeur|vice president|vp\b|managing director|\bmd\b|principal|"
    r"head of|summer|spring|insight|week|winter school|software|developer|engineer|"
    r"engineering|data scien|devops|cyber|it support|it project|operations|compliance|"
    r"audit|accounting|accountant|comptab|tax|fiscal|legal|counsel|juriste|paralegal|hr\b|"
    r"human (resources|capital)|payroll|recruit|talent acquisition|marketing|communications|"
    r"sales|trader|trading|equity research|research analyst|wealth|private bank|private client|"
    r"administrative|assistant|receptionist|facilities|alternance|apprenti|apprentice|"
    r"executive assistant|kyc|aml|anti-money|fund accounting|fund administration|treasury ops|"
    r"middle office|back office|risk|risques|benefits|treasury|investor relations|public affairs|"
    r"charitable|foundation|programmer|apps dev|architect|technical program|pricing|data(?! ?cent)|machine learning|"
    r"quantitative|quant|contr[ôo]leur|commercial|[ée]v[èe]nementiel|ing[ée]nieur|ia|appels? d.offres|"
    r"gestion priv[ée]e|global markets|markets (associate|analyst)|isa|it|functional|grc|dataops|evaluator|events?|"
    r"loan servicing|mortgage|retail banking|branch|customer|client service|veterans|research|support|"
    r"product analyst|ratings|ai|fixed income business|transformation)\b", re.I)
START_HINT = re.compile(
    r"((january|february|march|april|may|june|july|august|september|october|november|december|"
    r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|"
    r"jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.?\s+20(2[6-9]))", re.I)


def city_of(text):
    t = (text or "").lower()
    for city, pats in CITIES.items():
        if any(re.search(p, t) for p in pats):
            return city
    return None


MONTHS = {"jan": 1, "janv": 1, "january": 1, "janvier": 1, "feb": 2, "february": 2, "fevrier": 2, "février": 2,
          "mar": 3, "march": 3, "mars": 3, "apr": 4, "april": 4, "avril": 4, "may": 5, "mai": 5, "jun": 6,
          "june": 6, "juin": 6, "jul": 7, "july": 7, "juillet": 7, "aug": 8, "august": 8, "aout": 8, "août": 8,
          "sep": 9, "sept": 9, "september": 9, "septembre": 9, "oct": 10, "october": 10, "octobre": 10,
          "nov": 11, "november": 11, "novembre": 11, "dec": 12, "december": 12, "decembre": 12, "décembre": 12}


def months_in(text):
    out = []
    for m in START_HINT.finditer(text or ""):
        word = re.sub(r"\s+20\d\d$", "", m.group(1)).rstrip(".").lower()
        n = MONTHS.get(word)
        if n:
            out.append(int("20" + m.group(3)) * 100 + n)
    return out


def too_early(title):
    """Vrai si le titre annonce uniquement des débuts avant septembre 2027."""
    ms = months_in(title)
    return bool(ms) and max(ms) < 202709


def title_ok(title):
    return (bool(title) and not EXCLUDE.search(title) and bool(INCLUDE.search(title))
            and not too_early(title))


def keep(title, location):
    return title_ok(title) and bool(city_of(location or title))


def clean(txt, n=1800):
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt or ""))
    return re.sub(r"\s+", " ", txt).strip()[:n]


def start_hints(text):
    return sorted({m.group(1) for m in START_HINT.finditer(text or "")})[:6]


def get(url, **kw):
    time.sleep(PAUSE)
    return S.get(url, timeout=25, **kw)


def post(url, **kw):
    time.sleep(PAUSE)
    return S.post(url, timeout=25, **kw)


# ---------------------------------------------------------------- adaptateurs
def workday_posted(raw):
    r = (raw or "").lower()
    if "today" in r:
        return TODAY.isoformat()
    if "yesterday" in r:
        return (TODAY - dt.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s+days?", r)
    if m and "+" not in r:
        return (TODAY - dt.timedelta(days=int(m.group(1)))).isoformat()
    return None


def fetch_workday(src):
    host, tenant, site = src["host"], src["tenant"], src["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []
    for term in ["analyst", "graduate", "intern", "off-cycle", "VIE", "junior", "stage", "programme"]:
        offset = 0
        while offset < 200:
            r = post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
                     headers={"Accept": "application/json"})
            r.raise_for_status()
            d = r.json()
            posts = d.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "title": p.get("title"), "location": p.get("locationsText") or "",
                    "url": f"https://{host}/en-US/{site}{path}",
                    "posted": workday_posted(p.get("postedOn")), "postedRaw": p.get("postedOn"),
                    "_detail": f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                })
            offset += 20
            if offset >= (d.get("total") or 0) or not posts:
                break
    return out


def detail_workday(job):
    r = get(job["_detail"], headers={"Accept": "application/json"})
    if r.ok:
        info = (r.json() or {}).get("jobPostingInfo") or {}
        job["description"] = clean(info.get("jobDescription"))
        if info.get("startDate"):
            job["posted"] = job.get("posted") or info["startDate"][:10]
        locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
        locs = [l for l in locs if l]
        if locs:
            job["location"] = " | ".join(locs)


def fetch_greenhouse(src):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": j.get("title"), "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"),
            "posted": (j.get("first_published") or j.get("updated_at") or "")[:10] or None,
            "description": clean(j.get("content")),
        })
    return out


def fetch_lever(src):
    r = get(f"https://api.lever.co/v0/postings/{src['slug']}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        ts = j.get("createdAt")
        out.append({
            "title": j.get("text"), "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl"),
            "posted": dt.datetime.utcfromtimestamp(ts / 1000).date().isoformat() if ts else None,
            "description": clean(j.get("descriptionPlain") or j.get("description")),
        })
    return out


def fetch_ashby(src):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": j.get("title"), "location": j.get("location") or "",
            "url": j.get("jobUrl"), "posted": (j.get("publishedAt") or "")[:10] or None,
            "description": clean(j.get("descriptionPlain") or j.get("descriptionHtml")),
        })
    return out


def fetch_smartrecruiters(src):
    out, offset = [], 0
    while offset < 1000:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{src['slug']}/postings",
                params={"limit": 100, "offset": offset})
        r.raise_for_status()
        d = r.json()
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append({
                "title": j.get("name"),
                "location": ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                "url": f"https://jobs.smartrecruiters.com/{src['slug']}/{j.get('id')}",
                "posted": (j.get("releasedDate") or "")[:10] or None,
            })
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


def fetch_oracle(src):
    host, site = src["host"], src["site"]
    out, seen = [], set()
    for term in ["analyst", "graduate", "intern", "programme", "VIE"]:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit=100,offset=0,keyword=%22{term}%22,sortBy=POSTING_DATES_DESC")
        r = get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        items = r.json().get("items") or []
        for req in (items[0].get("requisitionList") if items else []) or []:
            if req.get("Id") in seen:
                continue
            seen.add(req.get("Id"))
            locs = [req.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in req.get("secondaryLocations") or []]
            out.append({
                "title": req.get("Title"), "location": " | ".join(l for l in locs if l),
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}",
                "posted": (req.get("PostedDate") or "")[:10] or None,
                "description": clean(req.get("ShortDescriptionStr")),
            })
    return out


def fetch_eightfold(src):
    host = f"https://{src['slug']}.eightfold.ai"
    domain = src.get("domain") or f"{src['slug']}.com"
    out, seen = [], set()
    for term in ["analyst", "graduate", "intern", "off-cycle", "programme"]:
        start = 0
        while start < 200:
            r = get(f"{host}/api/apply/v2/jobs", params={"domain": domain, "start": start, "num": 50, "query": term})
            r.raise_for_status()
            d = r.json()
            for p in d.get("positions", []):
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                ts = p.get("t_create")
                out.append({"title": p.get("name"), "location": " | ".join(p.get("locations") or [p.get("location") or ""]),
                            "url": p.get("canonicalPositionUrl") or f"{host}/careers/job/{p.get('id')}",
                            "posted": dt.datetime.utcfromtimestamp(ts).date().isoformat() if ts else None,
                            "description": clean(p.get("job_description"))})
            start += 50
            if start >= d.get("count", 0) or not d.get("positions"):
                break
    return out


def fetch_recruitee(src):
    r = get(f"https://{src['slug']}.recruitee.com/api/offers/")
    r.raise_for_status()
    out = []
    for o in r.json().get("offers", []):
        out.append({"title": o.get("title"), "location": ", ".join(x for x in [o.get("city"), o.get("country")] if x)
                    or o.get("location") or "", "url": o.get("careers_url"),
                    "posted": (o.get("published_at") or o.get("created_at") or "")[:10] or None,
                    "description": clean(o.get("description"))})
    return out


def fetch_teamtailor(src):
    r = get(f"https://{src['slug']}.teamtailor.com/jobs.rss")
    r.raise_for_status()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        def tag(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", item, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
        posted = tag("pubDate")
        try:
            posted = dt.datetime.strptime(posted[:16], "%a, %d %b %Y").date().isoformat()
        except ValueError:
            posted = None
        locs = " | ".join(re.findall(r"<tt:city>(.*?)</tt:city>", item)) or tag("tt:location") or tag("location")
        out.append({"title": tag("title"), "location": locs or tag("description")[:200], "url": tag("link"),
                    "posted": posted, "description": clean(tag("description"))})
    return out


def fetch_htmllist(src):
    """Pages HTML listant des offres (ex. Rothschild & Co) : liens vers les fiches, titre = texte du lien."""
    out, seen = [], set()
    for page_url in src["pages"]:
        for n in range(1, src.get("maxPages", 3) + 1):
            url = page_url.replace("{page}", str(n))
            r = get(url)
            if not r.ok:
                break
            found = 0
            for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
                full = urljoin(r.url, html.unescape(href))
                if not re.search(src["link"], full) or full in seen:
                    continue
                title = clean(text, 200)
                if len(title) < 8:
                    continue
                seen.add(full)
                found += 1
                loc = title + " " + full.replace("-", " ")
                if not city_of(loc) and src.get("defaultCity", {}).get(page_url):
                    loc += " " + src["defaultCity"][page_url]
                if not city_of(loc) and title_ok(title) and src.get("details", 0) > 0:
                    src["details"] -= 1
                    try:
                        d = get(full)
                        text = clean(d.text, 20000) if d.ok else ""
                        c = city_of(text[:6000])
                        if c:
                            loc += " " + c
                    except requests.RequestException:
                        pass
                out.append({"title": title, "location": loc, "url": full, "posted": None})
            if "{page}" not in page_url or not found:
                break
    return out


ADAPTERS = {"htmllist": fetch_htmllist, "eightfold": fetch_eightfold, "recruitee": fetch_recruitee, "teamtailor": fetch_teamtailor,
            "workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "smartrecruiters": fetch_smartrecruiters, "oracle": fetch_oracle}


# ---------------------------------------------------------------- découverte
ATS_PATTERNS = [
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([A-Za-z0-9_\-]+)")),
    ("eightfold", re.compile(r"https?://([a-z0-9\-]+)\.eightfold\.ai")),
    ("recruitee", re.compile(r"https?://([a-z0-9\-]+)\.recruitee\.com")),
    ("teamtailor", re.compile(r"https?://([a-z0-9\-]+)\.teamtailor\.com")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_\-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_\-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_\-.]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_\-]+)")),
    ("oracle", re.compile(r"https?://([a-z0-9\-]+\.fa(?:\.[a-z0-9\-]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([A-Za-z0-9_]+)")),
]
OTHER_ATS = re.compile(r"(taleo\.net|successfactors|eightfold\.ai|teamtailor\.com|welcometothejungle\.com|"
                       r"workable\.com|avature\.net|icims\.com|phenompeople|jobvite|recruitee\.com|"
                       r"bamboohr|personio|softgarden|talent-soft|cornerstoneondemand|beetween|flatchr)", re.I)
SKIP_SLUGS = {"embed", "jobs", "job", "careers", "en", "fr", "www", "api", "search", "home"}


def from_url(u):
    for ats, pat in ATS_PATTERNS:
        m = pat.search(u)
        if not m:
            continue
        if ats == "workday":
            tenant, shard, site = m.groups()
            if site.lower() in {"job", "jobs", "login", "wday", "details"} or re.fullmatch(r"[a-z]{2}-[a-z]{2}", site.lower()):
                continue
            return {"ats": "workday", "host": f"{tenant}.{shard}.myworkdayjobs.com", "tenant": tenant, "site": site}
        if ats == "oracle":
            return {"ats": "oracle", "host": m.group(1), "site": m.group(2)}
        if ats in ("eightfold", "recruitee", "teamtailor"):
            if m.group(1) in {"www", "app", "careers", "jobs", "api"}:
                continue
            return {"ats": ats, "slug": m.group(1)}
        slug = m.group(1)
        if slug.lower() in SKIP_SLUGS:
            continue
        return {"ats": ats, "slug": slug}
    return None


def scan_page(url):
    """Renvoie (sources trouvées, autres logiciels repérés, liens carrière à suivre)."""
    found, others, follow = [], set(), []
    try:
        r = get(url, allow_redirects=True)
    except requests.RequestException as e:
        return found, {f"erreur: {type(e).__name__}"}, follow
    body = r.text if r.ok else ""
    candidates = [r.url] + re.findall(r"""(?:href|src|data-url|action)\s*=\s*["']([^"']+)["']""", body)
    candidates += re.findall(r"https?://[^\s\"'<>\\]+", body)
    for c in candidates:
        full = urljoin(r.url, html.unescape(c))
        s = from_url(full)
        if s and s not in found:
            found.append(s)
        m = OTHER_ATS.search(full)
        if m:
            others.add(m.group(1).lower())
        if (urlparse(full).netloc == urlparse(r.url).netloc and
                re.search(r"(student|graduate|campus|early|career|carri|opportunit|job|offre|join)", full, re.I)):
            follow.append(full.split("#")[0])
    return found, others, list(dict.fromkeys(follow))[:6]


def slug_guesses(name):
    base = re.sub(r"\(.*?\)", "", name).lower()
    base = re.sub(r"[^a-z0-9 &]", "", base.replace("&", " and "))
    words = [w for w in base.split() if w not in {"the", "and", "co", "inc", "llc", "lp", "group", "management",
                                                    "partners", "capital", "asset", "global", "investment",
                                                    "investments", "infrastructure", "company"}]
    full = re.sub(r"[^a-z0-9]", "", base)
    short = "".join(words) or full
    first = words[0] if words else full
    return list(dict.fromkeys([full, short, first, short + "partners", full + "careers"]))[:5]


def name_key(name):
    w = [x for x in re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\(.*?\)", "", name.lower())).split()
         if x not in {"the", "and", "co", "group", "management", "partners", "capital", "global", "investment",
                      "investments", "company", "inc", "llc", "lp", "asset"}]
    return w[0] if w else ""


def probe_board(ats, slug, firm_name):
    """Une source devinée n'est gardée que si elle appartient bien à l'acteur (nom retrouvé)."""
    key = name_key(firm_name)
    try:
        if ats == "greenhouse":
            r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            return r.ok and key in (r.json().get("name") or "").lower()
        if ats == "lever":
            r = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 5})
            d = r.json() if r.ok else []
            return bool(d) and any(key in json.dumps(p).lower() for p in d)
        if ats == "ashby":
            r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "false"})
            d = r.json() if r.ok else {}
            return bool(d.get("jobs")) and any(key in json.dumps(p).lower() for p in d["jobs"][:5])
    except (requests.RequestException, ValueError):
        return False
    return False


WD_SHARDS = ["wd1", "wd3", "wd5", "wd103", "wd12", "wd10", "wd108"]
WD_SITES = ["External", "Careers", "External_Careers", "{T}_Careers", "{t}", "{T}", "Campus", "Students",
            "Early_Careers", "Experienced", "External_Career_Site", "{T}Careers"]


def probe_workday(firm_name):
    deadline = time.time() + 45
    for tenant in slug_guesses(firm_name)[:2]:
        if len(tenant) < 2:
            continue
        for shard in WD_SHARDS:
            if time.time() > deadline:
                return None
            host = f"{tenant}.{shard}.myworkdayjobs.com"
            try:
                r = get(f"https://{host}/", allow_redirects=False)
            except requests.RequestException:
                continue
            if r.status_code in (301, 302, 303, 307, 308):
                site = from_url(urljoin(f"https://{host}/", r.headers.get("Location", "")))
                if site and site.get("ats") == "workday":
                    return site
            if r.status_code != 200 or tenant not in r.text.lower():
                continue
            for pattern in WD_SITES:
                site = pattern.replace("{T}", tenant.capitalize()).replace("{t}", tenant)
                try:
                    p = post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                             json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
                except requests.RequestException:
                    continue
                if p.ok and "jobPostings" in p.text:
                    return {"ats": "workday", "host": host, "tenant": tenant, "site": site, "guessed": True}
    return None


def discover(firm):
    found, others = [], set()
    if firm.get("careersUrl"):
        f1, o1, follow = scan_page(firm["careersUrl"])
        found += f1
        others |= o1
        if not found:
            for u in follow:
                f2, o2, _ = scan_page(u)
                found += [s for s in f2 if s not in found]
                others |= o2
                if found:
                    break
    if not found:
        for g in slug_guesses(firm["name"]):
            for ats in ("greenhouse", "lever", "ashby"):
                if probe_board(ats, g, firm["name"]):
                    found.append({"ats": ats, "slug": g, "guessed": True})
                    break
            if found:
                break
    if not found:
        wd = probe_workday(firm["name"])
        if wd:
            found.append(wd)
    return {"sources": found, "otherAts": sorted(others), "checked": TODAY.isoformat()}


# ---------------------------------------------------------------- VIE (Business France)
def fetch_vie(firms):
    """Offres VIE publiées sur Mon Volontariat International, pour les acteurs de la liste."""
    names = {re.sub(r"[^a-z0-9]", "", f["name"].lower().split("/")[0].split("(")[0]): f for f in firms}
    out = []
    body = {"limit": 100, "skip": 0, "query": "", "activitySectorId": [], "missionsTypesIds": [],
            "missionsDurations": [], "gerographicZones": [], "countriesIds": [], "studiesLevelId": [],
            "companiesSizes": [], "specializationsIds": [], "entreprisesIds": [0], "missionStartDate": None}
    for q in ["analyst", "M&A", "finance", "investment", "infrastructure", "energy", "private equity"]:
        body["query"], body["skip"] = q, 0
        r = post("https://civiweb-api-prd.azurewebsites.net/api/Offers/search", json=body,
                 headers={"Content-Type": "application/json", "Origin": "https://mon-vie-via.businessfrance.fr"})
        r.raise_for_status()
        for o in r.json().get("result", []):
            org = re.sub(r"[^a-z0-9]", "", (o.get("organizationName") or "").lower())
            firm = next((f for k, f in names.items() if k and (k in org or org.startswith(k[:8]))), None)
            loc = f"{o.get('cityName') or ''}, {o.get('countryName') or ''}"
            if not city_of(loc):
                continue
            out.append({
                "firm": firm["name"] if firm else o.get("organizationName"), "firmId": firm["id"] if firm else None,
                "category": firm["category"] if firm else "Hors liste", "tier": firm["tier"] if firm else 3,
                "ats": "vie", "title": o.get("missionTitle"), "location": loc, "city": city_of(loc),
                "url": f"https://mon-vie-via.businessfrance.fr/offres/{o.get('id')}",
                "posted": (o.get("creationDate") or "")[:10] or None,
                "start": (o.get("missionStartDate") or "")[:10] or None,
                "duration": o.get("missionDuration"),
                "description": clean(o.get("missionDescription")),
            })
    dedup = {j["url"]: j for j in out}
    return list(dedup.values())


# ---------------------------------------------------------------- principal
def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main():
    firms = load(FIRMS, [])
    disc = load(os.path.join(DATA, "discovery.json"), {})
    prev = {j["url"]: j for j in load(os.path.join(DATA, "jobs.json"), {}).get("jobs", [])}
    status, jobs = {}, []

    for firm in firms:
        if firm.get("tier", 2) not in TIERS:
            continue
        fid = firm["id"]
        if firm.get("noRobot"):
            status[fid] = {"name": firm["name"], "sources": [], "matched": 0,
                           "errors": ["pas de source automatique (couvert par la recherche web)"]}
            continue
        sources = list(firm.get("sources") or [])
        d = disc.get(fid)
        stale = not d or (TODAY - dt.date.fromisoformat(d.get("checked", "2000-01-01"))).days > DISCOVERY_MAX_AGE_DAYS
        if not sources and stale:
            d = disc[fid] = discover(firm)
        if not sources and d:
            sources = d.get("sources", [])
        st = status[fid] = {"name": firm["name"], "sources": [], "matched": 0, "errors": []}
        if not sources:
            st["errors"].append("aucune source trouvée" + (f" (logiciel repéré : {', '.join(d.get('otherAts', []))})"
                                                             if d and d.get("otherAts") else ""))
            continue
        for src in sources:
            fn = ADAPTERS.get(src.get("ats"))
            label = src.get("ats") + ":" + (src.get("slug") or f"{src.get('host')}/{src.get('site')}")
            st["sources"].append(label)
            if not fn:
                continue
            try:
                raw = fn(src)
            except (requests.RequestException, ValueError, KeyError) as e:
                st["errors"].append(f"{label}: {type(e).__name__} {str(e)[:120]}")
                continue
            st.setdefault("total", 0)
            st["total"] += len(raw)
            if src["ats"] == "htmllist":
                st["sample"] = [r_["title"] for r_ in raw[:12]]
            for j in raw:
                if not title_ok(j.get("title")):
                    continue
                multi = bool(re.search(r"\d+\s+locations?", j.get("location") or "", re.I))
                if src["ats"] == "workday" and (j["url"] not in prev or multi):
                    try:
                        detail_workday(j)
                    except (requests.RequestException, ValueError):
                        pass
                elif j["url"] in prev and multi:
                    j["location"] = prev[j["url"]].get("location", j["location"])
                if not keep(j.get("title"), j.get("location")):
                    continue
                j.pop("_detail", None)
                j.update({"firm": firm["name"], "firmId": fid, "category": firm["category"],
                          "tier": firm["tier"], "ats": src["ats"], "city": city_of(j.get("location") or j["title"])})
                jobs.append(j)
                st["matched"] += 1

    try:
        vie = fetch_vie(firms)
        jobs += vie
        status["_vie"] = {"name": "Mon Volontariat International", "matched": len(vie), "errors": []}
    except (requests.RequestException, ValueError, KeyError) as e:
        status["_vie"] = {"name": "Mon Volontariat International", "matched": 0,
                          "errors": [f"{type(e).__name__} {str(e)[:160]}"]}

    final, sig = {}, set()
    for j in jobs:
        if not j.get("url"):
            continue
        key = (j["firm"], (j.get("title") or "").strip().lower(), j.get("city"))
        if key in sig:
            continue
        sig.add(key)
        j["id"] = hashlib.sha1(j["url"].encode()).hexdigest()[:12]
        j["firstSeen"] = prev.get(j["url"], {}).get("firstSeen", TODAY.isoformat())
        j["startHints"] = start_hints((j.get("title") or "") + " " + (j.get("description") or ""))
        if j["url"] in prev:
            for k in ("description", "posted"):
                if not j.get(k) and prev[j["url"]].get(k):
                    j[k] = prev[j["url"]][k]
        final[j["url"]] = j

    os.makedirs(DATA, exist_ok=True)
    out = sorted(final.values(), key=lambda x: (x["tier"], x["firm"], x["title"] or ""))
    with open(os.path.join(DATA, "jobs.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                   "count": len(out), "jobs": out}, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "discovery.json"), "w", encoding="utf-8") as fh:
        json.dump(disc, fh, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(DATA, "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": TODAY.isoformat(), "tiers": sorted(TIERS), "firms": status}, fh,
                  ensure_ascii=False, indent=1)
    ok = sum(1 for s in status.values() if s.get("sources") and not s["errors"])
    print(f"{len(out)} offres retenues, {ok}/{len(status)} acteurs sans erreur")
    return 0


if __name__ == "__main__":
    sys.exit(main())
