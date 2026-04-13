"""
Multi-source job scraper.

Sources:
1. SerpAPI Google Jobs — US/global roles (skipped for dead regions)
2. LinkedIn — direct scrape via SerpAPI Google Search (global) or requests (Japan)
3. TokyoDev — direct HTML scrape (Japan English tech roles)
4. Indeed — direct scrape via SerpAPI Google Search (global) or requests (Japan)

Smart behaviors:
- Auto-disables Google Jobs for regions returning 0 results
- For Japan, uses direct scraping (no SerpAPI) for LinkedIn & Indeed
- Fetches full job descriptions when possible for better AI matching
"""

import re
import requests
from typing import Optional, Callable
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin


SERP_API_URL = "https://serpapi.com/search.json"

DAYS_BACK_MAP = {
    1: "date_posted:today",
    3: "date_posted:3days",
    7: "date_posted:week",
    14: "date_posted:2weeks",
    30: "date_posted:month",
}

DAYS_BACK_GOOGLE_TBS = {
    1: "qdr:d",
    3: "qdr:d3",
    7: "qdr:w",
    14: "qdr:w2",
    30: "qdr:m",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Regions where Google Jobs returned 0 (persists within a single run)
_google_jobs_dead_regions: set = set()


def _is_japan_search(location: str) -> bool:
    """Check if a search targets Japan."""
    loc = location.lower().strip()
    japan_keywords = ["japan", "tokyo", "osaka", "kyoto", "yokohama", "nagoya", "fukuoka"]
    if any(kw in loc for kw in japan_keywords):
        return True
    # Also check if it's just "jp"
    if loc == "jp":
        return True
    return False


def _fetch_page_text(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract visible text for job description enrichment."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script/style
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:max_chars]
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# JOB LINK VALIDATOR — check if a posting is still active before notifying
# ═══════════════════════════════════════════════════════════════════════════════

_CLOSED_INDICATORS = [
    "no longer accepting applications",
    "this job is no longer available",
    "this position has been filled",
    "this job has expired",
    "job no longer exists",
    "this listing has expired",
    "this job posting has been removed",
    "application deadline has passed",
    "sorry, this position is no longer open",
    "this role has been filled",
    "job is closed",
    "position closed",
    "posting has been removed",
    "this job is expired",
    "no longer available",
    "applications are closed",
    "we are no longer hiring",
    "this position has been closed",
    "job not found",
    "page not found",
]

# LinkedIn-specific: these appear in the HTML even without login
_LINKEDIN_CLOSED_INDICATORS = [
    "no longer accepting applications",
    "this job is no longer available",
    "job is closed",
    "this job has expired",
]


def is_job_still_active(url: str) -> tuple[bool, str]:
    """
    Check if a job posting is still active by fetching the page.
    Returns (is_active, reason).
    """
    if not url:
        return True, ""

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)

        # 4xx client errors = job gone
        if resp.status_code in (404, 410, 403):
            return False, f"HTTP {resp.status_code}"

        # 3xx without resolution or 5xx — assume active
        if resp.status_code >= 500:
            return True, ""

        # Check if we were redirected to a homepage or generic page (job removed, site redirects)
        final_url = resp.url.lower()
        original_domain = url.split("/")[2].lower() if len(url.split("/")) > 2 else ""
        if original_domain and original_domain in final_url:
            # Redirected to homepage = job was removed
            path = final_url.split(original_domain)[-1].strip("/")
            if not path or path in ("jobs", "careers", "search", "home", "index"):
                return False, "Redirected to homepage (job removed)"

        page_text = resp.text.lower()

        # Check page size — very small pages often mean "not found" splash screens
        if len(page_text) < 500 and resp.status_code == 200:
            # Tiny page, likely an error/redirect page
            for word in ["not found", "removed", "expired", "unavailable", "error"]:
                if word in page_text:
                    return False, f"Minimal page with '{word}'"

        # LinkedIn-specific
        if "linkedin.com" in url.lower():
            for indicator in _LINKEDIN_CLOSED_INDICATORS:
                if indicator in page_text:
                    return False, indicator
            if "/login" in final_url or "/authwall" in final_url:
                return True, ""

        # General check for all sites
        for indicator in _CLOSED_INDICATORS:
            if indicator in page_text:
                return False, indicator

        return True, ""

    except requests.exceptions.HTTPError as e:
        # Catch 410, 404 etc that might raise exceptions
        status = getattr(e.response, 'status_code', 0) if e.response else 0
        if status in (404, 410):
            return False, f"HTTP {status}"
        return True, ""

    except requests.RequestException:
        return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# LOCATION NORMALIZER
# Handles: "Tokyo", "Japan", "NYC", "New York", "Tokyo, Japan", "US", etc.
# ═══════════════════════════════════════════════════════════════════════════════

# Common city → full location mappings
CITY_ALIASES = {
    "nyc": "New York, NY",
    "new york": "New York, NY",
    "sf": "San Francisco, CA",
    "san francisco": "San Francisco, CA",
    "la": "Los Angeles, CA",
    "los angeles": "Los Angeles, CA",
    "chicago": "Chicago, IL",
    "seattle": "Seattle, WA",
    "austin": "Austin, TX",
    "boston": "Boston, MA",
    "denver": "Denver, CO",
    "dc": "Washington, DC",
    "washington": "Washington, DC",
    "london": "London, United Kingdom",
    "berlin": "Berlin, Germany",
    "paris": "Paris, France",
    "amsterdam": "Amsterdam, Netherlands",
    "dublin": "Dublin, Ireland",
    "sydney": "Sydney, Australia",
    "melbourne": "Melbourne, Australia",
    "toronto": "Toronto, Canada",
    "vancouver": "Vancouver, Canada",
    "tokyo": "Tokyo, Japan",
    "osaka": "Osaka, Japan",
    "kyoto": "Kyoto, Japan",
    "yokohama": "Yokohama, Japan",
    "nagoya": "Nagoya, Japan",
    "fukuoka": "Fukuoka, Japan",
    "singapore": "Singapore",
    "bangalore": "Bangalore, India",
    "mumbai": "Mumbai, India",
    "hyderabad": "Hyderabad, India",
    "pune": "Pune, India",
    "dubai": "Dubai, United Arab Emirates",
    "tel aviv": "Tel Aviv, Israel",
    "sao paulo": "São Paulo, Brazil",
}

# Country name/code → full country + gl code
COUNTRY_MAP = {
    "us": ("United States", "us"),
    "usa": ("United States", "us"),
    "united states": ("United States", "us"),
    "america": ("United States", "us"),
    "uk": ("United Kingdom", "gb"),
    "united kingdom": ("United Kingdom", "gb"),
    "england": ("United Kingdom", "gb"),
    "britain": ("United Kingdom", "gb"),
    "canada": ("Canada", "ca"),
    "ca": ("Canada", "ca"),
    "germany": ("Germany", "de"),
    "de": ("Germany", "de"),
    "france": ("France", "fr"),
    "fr": ("France", "fr"),
    "australia": ("Australia", "au"),
    "au": ("Australia", "au"),
    "japan": ("Japan", "jp"),
    "jp": ("Japan", "jp"),
    "india": ("India", "in"),
    "in": ("India", "in"),
    "singapore": ("Singapore", "sg"),
    "sg": ("Singapore", "sg"),
    "netherlands": ("Netherlands", "nl"),
    "nl": ("Netherlands", "nl"),
    "ireland": ("Ireland", "ie"),
    "ie": ("Ireland", "ie"),
    "spain": ("Spain", "es"),
    "es": ("Spain", "es"),
    "italy": ("Italy", "it"),
    "it": ("Italy", "it"),
    "brazil": ("Brazil", "br"),
    "br": ("Brazil", "br"),
    "mexico": ("Mexico", "mx"),
    "mx": ("Mexico", "mx"),
    "south korea": ("South Korea", "kr"),
    "korea": ("South Korea", "kr"),
    "kr": ("South Korea", "kr"),
    "israel": ("Israel", "il"),
    "uae": ("United Arab Emirates", "ae"),
    "remote": ("United States", "us"),
}


def normalize_location(raw_location: str) -> tuple[str, str]:
    """
    Normalize a location string into (serpapi_location, gl_code).

    Handles:
    - City only: "Tokyo" → ("Tokyo, Japan", "jp")
    - Country only: "Japan" → ("Japan", "jp")
    - Abbreviations: "NYC" → ("New York, NY", "us")
    - Country codes: "US" → ("United States", "us")
    - Already full: "Tokyo, Japan" → ("Tokyo, Japan", "jp")
    - Remote: "Remote" → ("United States", "us")

    Returns (location_for_serpapi, gl_code)
    """
    raw = raw_location.strip()
    raw_lower = raw.lower().strip()

    # Check city aliases first (exact match)
    if raw_lower in CITY_ALIASES:
        full_loc = CITY_ALIASES[raw_lower]
        # Determine gl code from the full location
        gl = ""
        for key, (_, code) in COUNTRY_MAP.items():
            if key in full_loc.lower():
                gl = code
                break
        return full_loc, gl

    # Check country map (exact match on name or code)
    if raw_lower in COUNTRY_MAP:
        country_name, gl = COUNTRY_MAP[raw_lower]
        return country_name, gl

    # Already has a comma (likely "City, Country" format) — try to extract gl
    if "," in raw:
        gl = ""
        for key, (_, code) in COUNTRY_MAP.items():
            if key in raw_lower:
                gl = code
                break
        return raw, gl

    # Single word that's not in our maps — try partial matching
    # Check if it's a known city within a country
    for city, full_loc in CITY_ALIASES.items():
        if raw_lower in city or city in raw_lower:
            gl = ""
            for key, (_, code) in COUNTRY_MAP.items():
                if key in full_loc.lower():
                    gl = code
                    break
            return full_loc, gl

    # Check partial country match
    for key, (country_name, code) in COUNTRY_MAP.items():
        if key in raw_lower or raw_lower in key:
            return country_name, code

    # Give up — return as-is with no gl code
    print(f"  [LocationNorm] Could not normalize '{raw}' — using as-is")
    return raw, ""


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: SerpAPI Google Jobs (US/global only)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_google_jobs(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 50,
    is_seen_fn: Optional[Callable] = None,
) -> list[dict]:
    query = f"{seniority} {title}".strip() if seniority else title

    chip = DAYS_BACK_MAP.get(days_back, "date_posted:week")

    # Normalize location
    norm_location, gl_code = normalize_location(location)

    params = {
        "engine": "google_jobs",
        "q": query,
        "location": norm_location,
        "chips": chip,
        "api_key": api_key,
        "hl": "en",
    }

    if gl_code:
        params["gl"] = gl_code

    all_jobs = []
    next_page_token = None
    # Google Jobs returns 10 per page with NO way to increase it.
    # Each page = 1 SerpAPI credit. Cap at 2 pages (20 results) to preserve budget.
    # LinkedIn/Indeed use Google Search engine which returns 100 per credit.
    max_pages = min((max_results + 9) // 10, 2)

    for page_num in range(max_pages):
        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            resp = requests.get(SERP_API_URL, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  [GoogleJobs] Error {resp.status_code}: {resp.text[:200]}")
                break
            data = resp.json()
        except requests.RequestException as e:
            print(f"  [GoogleJobs] Request failed: {e}")
            break

        raw_jobs = data.get("jobs_results", [])
        if not raw_jobs:
            if "error" in data:
                print(f"  [GoogleJobs] No results: {data['error']}")
            break

        for item in raw_jobs:
            apply_links = item.get("apply_options", [])
            link = apply_links[0].get("link", "") if apply_links else ""
            if not link:
                link = item.get("share_link", "")

            all_jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": item.get("location", location),
                "description": item.get("description", "")[:4000],
                "link": link,
                "posted_at": item.get("detected_extensions", {}).get("posted_at", ""),
                "source": "Google Jobs",
            })

        if len(all_jobs) >= max_results:
            break

        pagination = data.get("serpapi_pagination", {})
        next_page_token = pagination.get("next_page_token")
        if not next_page_token:
            break

    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: LinkedIn
# For Japan: direct scrape (no SerpAPI)
# For other regions: SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def _scrape_linkedin_direct(title: str, location: str, max_results: int = 50) -> list[dict]:
    """Scrape LinkedIn job search directly (no SerpAPI needed)."""
    jobs = []
    query = quote_plus(f"{title}")
    loc = quote_plus(location)
    url = f"https://www.linkedin.com/jobs/search?keywords={query}&location={loc}&f_TPR=r604800&position=1&pageNum=0"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [LinkedIn-Direct] HTTP {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # LinkedIn public job search uses these selectors
        cards = soup.select("div.base-card, li.result-card, div.job-search-card")

        if not cards:
            # Try alternative selectors
            cards = soup.select("[data-entity-urn]")

        for card in cards[:max_results]:
            title_el = card.select_one("h3, .base-search-card__title, .job-search-card__title")
            company_el = card.select_one("h4, .base-search-card__subtitle, .job-search-card__subtitle")
            location_el = card.select_one(".job-search-card__location, .base-search-card__metadata")
            link_el = card.select_one("a[href*='/jobs/view/'], a.base-card__full-link")

            job_title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            job_location = location_el.get_text(strip=True) if location_el else location
            link = link_el.get("href", "") if link_el else ""

            if not job_title:
                continue

            # Clean LinkedIn tracking params from URL
            if "?" in link:
                link = link.split("?")[0]

            # Try to fetch full description from the job page
            description = ""
            if link:
                description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title,
                "company": company,
                "location": job_location,
                "description": description or f"{job_title} at {company} in {job_location}",
                "link": link,
                "posted_at": "",
                "source": "LinkedIn",
            })

    except Exception as e:
        print(f"  [LinkedIn-Direct] Scraping failed: {e}")

    return jobs


def _scrape_linkedin_via_serpapi(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 50,
) -> list[dict]:
    """Scrape LinkedIn via SerpAPI Google Search."""
    norm_location, _ = normalize_location(location)
    query = f'site:linkedin.com/jobs/view {title} {norm_location}'
    if seniority:
        query = f'site:linkedin.com/jobs/view {seniority} {title} {norm_location}'

    tbs = DAYS_BACK_GOOGLE_TBS.get(days_back, "qdr:w")

    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": 100,  # Max results per API call
        "tbs": tbs,
        "hl": "en",
    }

    jobs = []
    try:
        resp = requests.get(SERP_API_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [LinkedIn-SerpAPI] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [LinkedIn-SerpAPI] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "linkedin.com/jobs" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        parts = raw_title.split(" - ")
        job_title = parts[0].strip() if parts else raw_title
        company = parts[1].strip() if len(parts) > 1 else ""
        job_location = parts[2].strip() if len(parts) > 2 else location

        job_title = re.sub(r'\s*\|.*$', '', job_title)

        # Fetch full job description for better AI matching
        description = _fetch_page_text(link, max_chars=4000) if link else ""

        jobs.append({
            "title": job_title,
            "company": company,
            "location": job_location,
            "description": description or snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "LinkedIn",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: TokyoDev (direct scrape — Japan only)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_tokyodev(title: str, max_results: int = 50) -> list[dict]:
    """Scrape TokyoDev job listings."""
    url = "https://www.tokyodev.com/jobs"
    jobs = []

    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        if resp.status_code != 200:
            print(f"  [TokyoDev] Error {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        job_cards = soup.select("a[href*='/jobs/']")

        seen_links = set()
        title_lower = title.lower()
        title_keywords = [kw for kw in title_lower.split() if len(kw) > 2]

        for card in job_cards:
            link = card.get("href", "")
            if not link or link in seen_links:
                continue
            if not link.startswith("http"):
                link = f"https://www.tokyodev.com{link}"
            seen_links.add(link)

            card_text = card.get_text(separator=" | ", strip=True)

            if len(card_text) < 10:
                continue

            # Match against any keyword from the search title
            if title_keywords and not any(kw in card_text.lower() for kw in title_keywords):
                continue

            parts = [p.strip() for p in card_text.split("|") if p.strip()]
            job_title = parts[0] if parts else card_text
            company = parts[1] if len(parts) > 1 else ""

            # Fetch full description from job page
            description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title[:100],
                "company": company[:100],
                "location": "Tokyo, Japan",
                "description": description or card_text,
                "link": link,
                "posted_at": "",
                "source": "TokyoDev",
            })

            if len(jobs) >= max_results:
                break

    except Exception as e:
        print(f"  [TokyoDev] Scraping failed: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 4: Indeed
# For Japan: direct scrape of jp.indeed.com (no SerpAPI)
# For other regions: SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def _scrape_indeed_direct(title: str, location: str, max_results: int = 50) -> list[dict]:
    """Scrape Indeed Japan directly (no SerpAPI)."""
    jobs = []

    if _is_japan_search(location):
        base_url = "https://jp.indeed.com"
    else:
        base_url = "https://www.indeed.com"

    query = quote_plus(title)
    loc = quote_plus(location)
    url = f"{base_url}/jobs?q={query}&l={loc}&fromage=7&limit={max_results}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [Indeed-Direct] HTTP {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Indeed job cards
        cards = soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div, .result")

        for card in cards[:max_results]:
            title_el = card.select_one("h2 a, .jobTitle a, a.jcs-JobTitle")
            company_el = card.select_one("[data-testid='company-name'], .companyName, .company")
            location_el = card.select_one("[data-testid='text-location'], .companyLocation, .location")

            job_title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            job_location = location_el.get_text(strip=True) if location_el else location

            if not job_title:
                continue

            # Build link
            link = ""
            if title_el:
                href = title_el.get("href", "")
                if href:
                    link = urljoin(base_url, href)

            # Try to get description from job page
            description = ""
            if link:
                description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title,
                "company": company,
                "location": job_location,
                "description": description or f"{job_title} at {company}",
                "link": link,
                "posted_at": "",
                "source": "Indeed",
            })

    except Exception as e:
        print(f"  [Indeed-Direct] Scraping failed: {e}")

    return jobs


def _scrape_indeed_via_serpapi(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 50,
) -> list[dict]:
    """Scrape Indeed via SerpAPI Google Search."""
    norm_location, _ = normalize_location(location)
    site = "jp.indeed.com" if _is_japan_search(location) else "indeed.com"

    query = f'site:{site} {title} {norm_location}'
    if seniority:
        query = f'site:{site} {seniority} {title} {norm_location}'

    tbs = DAYS_BACK_GOOGLE_TBS.get(days_back, "qdr:w")

    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": 100,  # Max results per API call
        "tbs": tbs,
        "hl": "en",
    }

    jobs = []
    try:
        resp = requests.get(SERP_API_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [Indeed-SerpAPI] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [Indeed-SerpAPI] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "indeed.com" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        clean_title = re.sub(r'\s*[\|\-]\s*Indeed.*$', '', raw_title, flags=re.IGNORECASE)
        parts = clean_title.split(" - ", 1)
        job_title = parts[0].strip()
        company = parts[1].strip() if len(parts) > 1 else ""

        # Fetch full description
        description = _fetch_page_text(link, max_chars=4000) if link else ""

        jobs.append({
            "title": job_title,
            "company": company,
            "location": location,
            "description": description or snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "Indeed",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 5: JapanDev (direct scrape — server-rendered HTML)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_japandev(title: str, max_results: int = 50) -> list[dict]:
    """Scrape JapanDev job listings — 283+ curated English tech jobs in Japan."""
    url = "https://japan-dev.com/jobs"
    jobs = []

    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        if resp.status_code != 200:
            print(f"  [JapanDev] Error {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # JapanDev structure: h2 > a[href=/jobs/company/slug] for job titles
        job_headings = soup.select("h2 a[href*='/jobs/']")

        seen_links = set()
        title_keywords = [kw for kw in title.lower().split() if len(kw) > 2]

        for heading in job_headings:
            href = heading.get("href", "")
            if not href or href in seen_links or href == "/jobs" or href == "/jobs/":
                continue

            full_link = f"https://japan-dev.com{href}" if not href.startswith("http") else href
            seen_links.add(href)

            job_title = heading.get_text(strip=True)
            if not job_title or len(job_title) < 5:
                continue

            # Keyword match — broad matching for tech roles
            if title_keywords and not any(kw in job_title.lower() for kw in title_keywords):
                continue

            # Company name is usually in the parent card, after the heading
            parent = heading.find_parent("li") or heading.find_parent("div")
            company = ""
            if parent:
                # Company name appears as text right after the h2, often with ・ separator
                all_text = parent.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in all_text.split("\n") if l.strip()]
                # Find the line after the job title
                for i, line in enumerate(lines):
                    if job_title in line and i + 1 < len(lines):
                        company_line = lines[i + 1]
                        # Clean up company name (remove descriptions after ・)
                        company = company_line.split("・")[0].strip()
                        break

            # Fetch full description from job page
            description = _fetch_page_text(full_link, max_chars=4000)

            jobs.append({
                "title": job_title[:100],
                "company": company[:100],
                "location": "Japan",
                "description": description or job_title,
                "link": full_link,
                "posted_at": "",
                "source": "JapanDev",
            })

            if len(jobs) >= max_results:
                break

    except Exception as e:
        print(f"  [JapanDev] Scraping failed: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 6: GaijinPot (direct scrape — may fail from some IPs)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_gaijinpot(title: str, max_results: int = 50) -> list[dict]:
    """Scrape GaijinPot IT jobs. May fail from GitHub Actions IPs."""
    url = f"https://jobs.gaijinpot.com/en/job?category=22&keywords={quote_plus(title)}"
    jobs = []

    try:
        resp = requests.get(url, timeout=15, headers=HEADERS)
        if resp.status_code != 200:
            print(f"  [GaijinPot] HTTP {resp.status_code} — site may block this IP")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Job detail links
        job_links = soup.select("a[href*='/en/job/'][href*='/details/']")
        seen_links = set()

        for link_el in job_links[:max_results]:
            href = link_el.get("href", "")
            if not href or href in seen_links:
                continue

            full_link = f"https://jobs.gaijinpot.com{href}" if not href.startswith("http") else href
            seen_links.add(href)

            card_text = link_el.get_text(strip=True)
            if len(card_text) < 5:
                continue

            jobs.append({
                "title": card_text[:100],
                "company": "",
                "location": "Japan",
                "description": card_text,
                "link": full_link,
                "posted_at": "",
                "source": "GaijinPot",
            })

    except Exception as e:
        print(f"  [GaijinPot] Unreachable: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# UNIFIED SCRAPER
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_jobs(
    api_key: str,
    title: str,
    location: str,
    seniority: str = "",
    days_back: int = 7,
    max_results: int = 100,
    is_seen_fn: Optional[Callable] = None,
    sources: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scrape jobs from multiple sources.

    Smart behaviors:
    - Google Jobs auto-disabled for dead regions
    - Japan searches use direct scraping (saves SerpAPI credits)
    - Full descriptions fetched for better AI matching
    """
    global _google_jobs_dead_regions

    if sources is None:
        sources = ["google_jobs", "linkedin", "tokyodev", "indeed", "japandev", "gaijinpot"]

    all_jobs = []
    japan_search = _is_japan_search(location)
    region_key = location.strip().lower()

    # Per-source limit — use max_results for each source to maximize coverage
    per_source_limit = max_results

    # ── Google Jobs ──
    if "google_jobs" in sources:
        if region_key in _google_jobs_dead_regions:
            print(f"  ⏭️  Skipping Google Jobs (returned 0 for '{location}' earlier)")
        else:
            print(f"  📡 Fetching from Google Jobs...")
            gj = scrape_google_jobs(api_key, title, location, seniority, days_back, per_source_limit, is_seen_fn)
            print(f"  [GoogleJobs] {len(gj)} results")
            if len(gj) == 0:
                _google_jobs_dead_regions.add(region_key)
                print(f"  ⚠️  Google Jobs returned 0 — disabled for '{location}'")
            all_jobs.extend(gj)

    # ── LinkedIn ──
    if "linkedin" in sources:
        if japan_search:
            print(f"  📡 Fetching from LinkedIn (direct scrape — no SerpAPI)...")
            li = _scrape_linkedin_direct(title, location, per_source_limit)
        else:
            print(f"  📡 Fetching from LinkedIn (via SerpAPI)...")
            li = _scrape_linkedin_via_serpapi(api_key, title, location, seniority, days_back, per_source_limit)
        print(f"  [LinkedIn] {len(li)} results")
        all_jobs.extend(li)

    # ── TokyoDev ──
    if "tokyodev" in sources:
        if japan_search or "remote" in location.lower():
            print(f"  📡 Fetching from TokyoDev (direct scrape — no SerpAPI)...")
            td = scrape_tokyodev(title, per_source_limit)
            print(f"  [TokyoDev] {len(td)} results")
            all_jobs.extend(td)

    # ── JapanDev ──
    if "japandev" in sources:
        if japan_search or "remote" in location.lower():
            print(f"  📡 Fetching from JapanDev (direct scrape — no SerpAPI)...")
            jd = scrape_japandev(title, max_results=per_source_limit)
            print(f"  [JapanDev] {len(jd)} results")
            all_jobs.extend(jd)

    # ── GaijinPot ──
    if "gaijinpot" in sources:
        if japan_search or "remote" in location.lower():
            print(f"  📡 Fetching from GaijinPot (direct scrape — no SerpAPI)...")
            gp = scrape_gaijinpot(title, max_results=per_source_limit)
            print(f"  [GaijinPot] {len(gp)} results")
            all_jobs.extend(gp)

    # ── Indeed ──
    if "indeed" in sources:
        if japan_search:
            # Indeed Japan blocks direct scraping (403) and SerpAPI returns poor results
            print(f"  ⏭️  Skipping Indeed for Japan (blocked)")
        else:
            print(f"  📡 Fetching from Indeed (via SerpAPI)...")
            ind = _scrape_indeed_via_serpapi(api_key, title, location, seniority, days_back, per_source_limit)
            print(f"  [Indeed] {len(ind)} results")
            all_jobs.extend(ind)

    print(f"  📊 Total from all sources: {len(all_jobs)}")
    return all_jobs