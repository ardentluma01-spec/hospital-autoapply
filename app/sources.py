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


def _sf_from_search(base: str, hospital: str, pages: int = 3, q: str = "", max_results: int = 100) -> list[dict]:
    """Site search. Pages are walked by the number of rows each page really shows (these sites show
    10 per page, not 25), until a page adds nothing new or max_results is reached."""
    from urllib.parse import quote_plus
    jobs, seen = [], set()
    startrow = 0
    for page in range(max(pages, 1) * 10):
        if len(jobs) >= max_results:
            break
        url = f"{base}/search/?q={quote_plus(q)}&sortColumn=referencedate&sortDirection=desc&startrow={startrow}"
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
                u = urljoin(base, a["href"])
                if u not in seen and a.get_text(strip=True):
                    seen.add(u)
                    jobs.append({"hospital": hospital, "title": a.get_text(strip=True),
                                 "url": u, "location": "", "posted": ""})
            break
        added = 0
        for row in rows:
            a = row.select_one("a.jobTitle-link") or row.select_one('a[href*="/job/"]')
            if not a:
                continue
            u = urljoin(base, a["href"])
            if u in seen or not a.get_text(strip=True):
                continue
            seen.add(u)
            added += 1
            loc = row.select_one(".jobLocation")
            date = row.select_one(".jobDate")
            jobs.append({
                "hospital": hospital,
                "title": a.get_text(strip=True),
                "url": u,
                "location": loc.get_text(" ", strip=True) if loc else "",
                "posted": date.get_text(strip=True) if date else "",
            })
        if not added:
            break
        startrow += len(rows)
        label = soup.select_one(".paginationLabel")
        total = re.search(r"of\s+([\d,]+)", label.get_text(" ")) if label else None
        if total and startrow >= int(total.group(1).replace(",", "")):
            break
    return jobs


def successfactors_jobs(hospital: dict, queries=()) -> list[dict]:
    """Newest listings (RSS / first pages) PLUS a site search for each of your job titles, so older
    PSA ads on big job boards aren't missed."""
    base = hospital["careers_url"].rstrip("/")
    jobs = _sf_from_rss(base, hospital["name"])
    if not jobs:
        jobs = _sf_from_search(base, hospital["name"])
    for q in queries:
        time.sleep(POLITE_DELAY_S)
        jobs += _sf_from_search(base, hospital["name"], q=q, max_results=40)
    seen, out = set(), []
    for j in jobs:
        if j.get("url") and j.get("title") and j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    log.info("%s: %d listings", hospital["name"], len(out))
    return out


def legacy_successfactors_jobs(hospital: dict, max_pages: int = 8) -> list[dict]:
    """Older SAP SuccessFactors boards (career10.successfactors.com/career?company=...) are built with
    JavaScript, so they're read with the browser. Job links carry 'career_job_req_id'."""
    from playwright.sync_api import sync_playwright
    out, seen = [], set()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        try:
            page.goto(hospital["careers_url"], wait_until="networkidle", timeout=60000)
            for _ in range(max_pages):
                for a in page.locator('a[href*="career_job_req_id"], a[href*="jobReqId"]').all():
                    try:
                        href, title = a.get_attribute("href") or "", (a.inner_text() or "").strip()
                    except Exception:  # noqa: BLE001
                        continue
                    url = urljoin(page.url, href)
                    if title and url not in seen and len(title) < 200:
                        seen.add(url)
                        out.append({"hospital": hospital["name"], "title": title, "url": url, "location": "", "posted": ""})
                nxt = page.get_by_role("link", name=re.compile(r"^\s*(next|›|>|»)\s*$", re.I))
                if not nxt.count():
                    break
                nxt.first.click(timeout=8000)
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(POLITE_DELAY_S)
        except Exception as e:  # noqa: BLE001
            log.warning("Legacy board %s failed: %s", hospital["name"], e)
        finally:
            b.close()
    log.info("%s: %d listings (legacy board)", hospital["name"], len(out))
    return out


def pageup_jobs(hospital: dict, max_clicks: int = 15) -> list[dict]:
    """PageUp boards (e.g. careers.alfredhealth.org.au/jobs) list 10 jobs and load the rest with a
    'Show more jobs' button, so they're read with the browser. Watch-only: you get the link + letter."""
    from playwright.sync_api import sync_playwright
    out, seen = [], set()
    base = hospital["careers_url"].rstrip("/")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        try:
            from urllib.parse import urlsplit
            start = base + "/jobs" if urlsplit(base).path in ("", "/") else base
            page.goto(start, wait_until="networkidle", timeout=60000)
            for _ in range(max_clicks):
                more = page.get_by_role("button", name=re.compile(r"show more|load more|more jobs", re.I))
                if not more.count():
                    more = page.get_by_role("link", name=re.compile(r"show more|load more|more jobs", re.I))
                if not more.count() or not more.first.is_visible():
                    break
                more.first.click(timeout=8000)
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1)
            for a in page.locator('a[href*="/job/"]').all():
                try:
                    href, title = a.get_attribute("href") or "", (a.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                url = urljoin(page.url, href)
                title = title.split("\n")[0].strip()
                if title and url not in seen and len(title) < 200:
                    seen.add(url)
                    out.append({"hospital": hospital["name"], "title": title, "url": url, "location": "", "posted": ""})
        except Exception as e:  # noqa: BLE001
            log.warning("PageUp board %s failed: %s", hospital["name"], e)
            raise
        finally:
            b.close()
    log.info("%s: %d listings (PageUp board)", hospital["name"], len(out))
    return out


def smartrecruiters_jobs(hospital: dict) -> list[dict]:
    """SmartRecruiters boards (e.g. jobs.smartrecruiters.com/Epworth) have a public JSON list of postings."""
    from urllib.parse import urlsplit, quote
    company = urlsplit(hospital["careers_url"]).path.strip("/").split("/")[0]
    out, offset = [], 0
    while offset < 1000:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{quote(company)}/postings?limit=100&offset={offset}").json()
        items = data.get("content") or []
        for it in items:
            loc = it.get("location") or {}
            out.append({"hospital": hospital["name"], "title": (it.get("name") or "").strip(),
                        "url": f"https://jobs.smartrecruiters.com/{company}/{it.get('id')}",
                        "location": ", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
                        "posted": (it.get("releasedDate") or "")[:10]})
        offset += len(items)
        if not items or offset >= int(data.get("totalFound") or 0):
            break
        time.sleep(POLITE_DELAY_S)
    log.info("%s: %d listings (SmartRecruiters)", hospital["name"], len(out))
    return out


READERS = ("successfactors", "successfactors_legacy", "pageup", "smartrecruiters")


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
    text = ""
    try:
        soup = BeautifulSoup(_get(url).text, "html.parser")
        node = (soup.select_one(".jobdescription") or soup.select_one('[itemprop="description"]')
                or soup.select_one(".job") or soup.body)
        text = node.get_text("\n", strip=True)[:12000] if node else ""
    except Exception as e:  # noqa: BLE001
        log.warning("Could not open %s: %s", url, e)
    if len(text) >= 300:
        return text
    # JavaScript-built pages (older job boards): read with the browser
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, wait_until="networkidle", timeout=45000)
            text = pg.inner_text("body")[:12000]
            b.close()
    except Exception as e:  # noqa: BLE001
        log.warning("Browser read of %s failed: %s", url, e)
    return text


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


def fetch_for(hospitals: list[dict], queries=(), report: dict | None = None) -> list[dict]:
    """Listings from every given hospital. If `report` is given, it's filled with
    {hospital name: {"listings": n, "error": "..."}} so the dashboard can show what happened."""
    found = []
    for h in hospitals:
        if h["platform"] not in READERS or not h.get("careers_url"):
            if report is not None:
                report[h["name"]] = {"listings": 0, "error": "This site type isn't read yet"}
            continue
        try:
            if h["platform"] == "successfactors":
                listing = successfactors_jobs(h, queries)
            elif h["platform"] == "pageup":
                listing = pageup_jobs(h)
            elif h["platform"] == "smartrecruiters":
                listing = smartrecruiters_jobs(h)
            else:
                listing = legacy_successfactors_jobs(h)
            n = 0
            for j in listing:
                if not same_site(j["url"], h["careers_url"]):
                    log.warning("Ignoring off-site listing link %s", j["url"][:120])
                    continue
                j["platform"] = h["platform"]
                j["login_env"] = h["login_env"]
                found.append(j)
                n += 1
            if report is not None:
                report[h["name"]] = {"listings": n, "error": "" if n else "No listings could be read"}
            time.sleep(POLITE_DELAY_S)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed reading %s: %s", h["name"], e)
            if report is not None:
                report[h["name"]] = {"listings": 0, "error": f"Couldn't read the site ({type(e).__name__})"}
    return found


def fetch_all(cfg: dict, report: dict | None = None) -> list[dict]:
    """Single-user version: hospitals come from config.yaml."""
    hs = []
    for h in cfg.get("hospitals", []):
        if h.get("enabled") and h.get("careers_url", "").startswith("http"):
            hs.append({**h, "login_env": h.get("login_env", "")})
    # search terms: your job titles, skipping tiny ones like "psa" (matched in titles anyway)
    queries = [k for k in cfg.get("include_keywords", []) if len(k) > 3][:40]
    return fetch_for(hs, queries, report)
