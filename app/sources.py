"""Finds job listings on the hospital careers sites.

Most Victorian public hospitals run SAP SuccessFactors "Career Site Builder"
(addresses like careers.xxx.org.au/job/<title>/<id>/). Those sites offer
an RSS feed and a search page, so we try the feed first and fall back to
reading the search page.
"""
import logging
import re
import time
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("sources")

HEADERS = {"User-Agent": "HospitalAutoApply/1.0 (personal job alerts; low frequency)"}
POLITE_DELAY_S = 2.0  # pause between requests to the same site
TIMEOUT = 30


def _get(url: str) -> httpx.Response:
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def _sf_from_rss(base: str, hospital: str) -> list[dict]:
    jobs = []
    for locale in ("en_GB", "en_US"):
        url = f"{base}/services/rss/job/?locale={locale}&keywords=()"
        try:
            feed = feedparser.parse(_get(url).text)
        except Exception as e:  # noqa: BLE001
            log.info("RSS %s failed: %s", url, e)
            continue
        for e in feed.entries:
            jobs.append({
                "hospital": hospital,
                "title": re.sub(r"\s*\(.*?\)\s*$", "", e.get("title", "")).strip(),
                "url": e.get("link", ""),
                "posted": e.get("published", ""),
                "location": "",
                "description": BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True),
            })
        if jobs:
            return jobs
    return jobs


def _sf_from_search(base: str, hospital: str, pages: int = 3) -> list[dict]:
    jobs = []
    for page in range(pages):
        url = f"{base}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow={page * 25}"
        if page:
            time.sleep(POLITE_DELAY_S)
        try:
            soup = BeautifulSoup(_get(url).text, "html.parser")
        except Exception as e:  # noqa: BLE001
            log.warning("Search page %s failed: %s", url, e)
            break
        rows = soup.select("tr.data-row") or soup.select("li.job-tile, div.job-tile")
        if not rows:
            # Generic fallback: any link that looks like a job page
            for a in soup.select('a[href*="/job/"]'):
                jobs.append({"hospital": hospital, "title": a.get_text(strip=True),
                             "url": urljoin(base, a["href"]), "location": "", "posted": ""})
            break
        for row in rows:
            a = row.select_one("a.jobTitle-link") or row.select_one('a[href*="/job/"]')
            if not a:
                continue
            loc = row.select_one(".jobLocation")
            date = row.select_one(".jobDate")
            jobs.append({
                "hospital": hospital,
                "title": a.get_text(strip=True),
                "url": urljoin(base, a["href"]),
                "location": loc.get_text(" ", strip=True) if loc else "",
                "posted": date.get_text(strip=True) if date else "",
            })
    # de-duplicate
    seen, out = set(), []
    for j in jobs:
        if j["url"] and j["title"] and j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


def successfactors_jobs(hospital: dict) -> list[dict]:
    base = hospital["careers_url"].rstrip("/")
    jobs = _sf_from_rss(base, hospital["name"])
    if not jobs:
        jobs = _sf_from_search(base, hospital["name"])
    log.info("%s: %d listings", hospital["name"], len(jobs))
    return jobs


def same_site(url: str, careers_url: str) -> bool:
    """SSRF / data-integrity guard: only follow job links on the hospital's own careers host.

    Browsers treat '\\' like '/' and honour 'user@host', so anything that could be parsed differently by
    Python and Chromium (backslashes, userinfo, control/space characters, odd schemes) is rejected outright.
    """
    from urllib.parse import urlsplit
    if not isinstance(url, str) or re.search(r"[\\\s\x00-\x1f\x7f]", url):
        return False
    a, b = urlsplit(url), urlsplit(careers_url)
    if a.scheme not in ("http", "https") or "@" in a.netloc or not a.hostname:
        return False
    return a.hostname.lower() == (b.hostname or "").lower() and a.scheme == b.scheme


def job_key(url: str) -> str:
    """Identity of a vacancy for de-duplication: host + SuccessFactors job ID if present, else the
    lower-cased path. Query strings (tracking parameters) and fragments are ignored."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    m = re.search(r"/job/(?:.*/)?(\d{5,})/?$", u.path)
    ident = m.group(1) if m else u.path.lower().rstrip("/")
    return f"{(u.hostname or '').lower()}|{ident}"


def clean_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc.lower(), u.path, "", ""))


def job_description(url: str) -> str:
    """Full ad text from the job page (for the AI to read)."""
    time.sleep(POLITE_DELAY_S)
    try:
        soup = BeautifulSoup(_get(url).text, "html.parser")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not open %s: %s", url, e)
        return ""
    node = (soup.select_one(".jobdescription") or soup.select_one('[itemprop="description"]')
            or soup.select_one(".job") or soup.body)
    return node.get_text("\n", strip=True)[:12000] if node else ""


def title_matches(title: str, cfg: dict) -> bool:
    t = title.lower()
    if any(x.lower() in t for x in cfg.get("exclude_keywords", [])):
        return False
    for kw in cfg.get("include_keywords", []):
        kw = kw.lower()
        # whole-word match for short words like "psa"
        if len(kw) <= 4:
            if re.search(rf"\b{re.escape(kw)}\b", t):
                return True
        elif kw in t:
            return True
    return False


def fetch_for(hospitals: list[dict]) -> list[dict]:
    """Listings from every given SuccessFactors hospital (each site read once, shared by all users)."""
    found = []
    for h in hospitals:
        if h["platform"] != "successfactors" or not h.get("careers_url"):
            continue
        try:
            for j in successfactors_jobs(h):
                if not same_site(j["url"], h["careers_url"]):
                    log.warning("Ignoring off-site listing link %s", j["url"][:120])
                    continue
                j["platform"] = h["platform"]
                j["login_env"] = h["login_env"]
                found.append(j)
            time.sleep(POLITE_DELAY_S)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed reading %s: %s", h["name"], e)
    return found


def fetch_all(cfg: dict) -> list[dict]:
    """Single-user version: hospitals come from config.yaml."""
    hs = []
    for h in cfg.get("hospitals", []):
        if h.get("enabled") and h.get("platform") == "successfactors" and h.get("careers_url", "").startswith("http"):
            hs.append({**h, "login_env": h.get("login_env", "")})
    return fetch_for(hs)
