"""
Job Discovery — Scrape job listings from ATS platforms and job boards.
Supports: Greenhouse, Lever, JobSpy (Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google),
RSS feeds (RemoteOK), Adzuna, HN Who is Hiring, and custom career pages.
"""

import asyncio
import re
from dataclasses import dataclass, asdict, field


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    apply_url: str
    platform: str
    description: str = ""
    department: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def deduplicate_jobs(jobs: list) -> list:
    """Deduplicate jobs by (title_lower, company_lower) to avoid cross-source duplicates."""
    seen = set()
    unique = []
    for job in jobs:
        key = (job.title.lower().strip(), job.company.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


async def discover_greenhouse_jobs(company_slug: str, role_keywords: list[str]) -> list[Job]:
    """Discover loosely relevant jobs from one Greenhouse board."""
    import httpx

    jobs = []
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()

        for job_data in data.get("jobs", []):
            title = job_data.get("title", "")
            raw_desc = job_data.get("content", "")
            desc_lower = re.sub(r'<[^>]+>', ' ', raw_desc).lower()
            combined = f"{title.lower()} {desc_lower}"
            broad_keywords = list(set(kw.lower() for kw in role_keywords))

            if broad_keywords and not any(kw in combined for kw in broad_keywords):
                continue

            location = job_data.get("location", {}).get("name", "Unknown")
            description = re.sub(r'<[^>]+>', ' ', raw_desc)
            description = re.sub(r'\s+', ' ', description).strip()

            jobs.append(Job(
                id=str(job_data["id"]),
                title=title,
                company=company_slug,
                location=location,
                url=f"https://boards.greenhouse.io/{company_slug}/jobs/{job_data['id']}",
                apply_url=f"https://boards.greenhouse.io/{company_slug}/jobs/{job_data['id']}#app",
                platform="greenhouse",
                description=description[:5000],
                department=", ".join(
                    d.get("name", "") for d in job_data.get("departments", [])
                ),
                metadata={
                    "updated_at": job_data.get("updated_at", ""),
                    "requisition_id": job_data.get("requisition_id", ""),
                },
            ))
    except Exception as e:
        print(f"  ⚠ Greenhouse [{company_slug}]: {e}")

    return jobs


async def discover_lever_jobs(company_slug: str, role_keywords: list[str]) -> list[Job]:
    """Discover loosely relevant jobs from one Lever board."""
    import httpx

    jobs = []
    api_url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()

        for posting in data:
            title = posting.get("text", "")
            description = posting.get("descriptionPlain", "")
            combined = f"{title.lower()} {description.lower()}"
            broad_keywords = list(set(kw.lower() for kw in role_keywords))

            if broad_keywords and not any(kw in combined for kw in broad_keywords):
                continue

            categories = posting.get("categories", {})
            location = categories.get("location", "Unknown")
            jobs.append(Job(
                id=posting["id"],
                title=title,
                company=company_slug,
                location=location,
                url=posting.get("hostedUrl", ""),
                apply_url=posting.get("applyUrl", posting.get("hostedUrl", "")),
                platform="lever",
                description=description[:5000],
                department=categories.get("team", ""),
                metadata={
                    "commitment": categories.get("commitment", ""),
                    "created_at": posting.get("createdAt", ""),
                },
            ))
    except Exception as e:
        print(f"  ⚠ Lever [{company_slug}]: {e}")

    return jobs


async def discover_all_jobs(profile: dict) -> list[Job]:
    """Discover jobs from the sources enabled in profile.yaml."""
    all_jobs = []
    role_keywords = profile["preferences"]["roles"]
    boards = profile.get("target_boards", {})
    search_config = profile.get("search", {})
    source_config = search_config.get("sources", {})

    def enabled(name: str, default: bool) -> bool:
        return bool(source_config.get(name, default))

    # Greenhouse / Lever are opt-in through configured company slugs.
    gh_companies = boards.get("greenhouse", [])
    if gh_companies and enabled("greenhouse", True):
        print(f"\n🌿 Scanning {len(gh_companies)} Greenhouse boards...")
        results = await asyncio.gather(*[
            discover_greenhouse_jobs(slug, role_keywords) for slug in gh_companies
        ])
        for jobs in results:
            all_jobs.extend(jobs)
            if jobs:
                print(f"   ✅ {jobs[0].company}: {len(jobs)} matching jobs")

    lever_companies = boards.get("lever", [])
    if lever_companies and enabled("lever", True):
        print(f"\n🔧 Scanning {len(lever_companies)} Lever boards...")
        results = await asyncio.gather(*[
            discover_lever_jobs(slug, role_keywords) for slug in lever_companies
        ])
        for jobs in results:
            all_jobs.extend(jobs)
            if jobs:
                print(f"   ✅ {jobs[0].company}: {len(jobs)} matching jobs")

    if search_config.get("enabled", True) and enabled("jobspy", True):
        try:
            from utils.jobspy_source import discover_jobspy_jobs
            print("\n🔍 Searching job boards via JobSpy...")
            all_jobs.extend(discover_jobspy_jobs(profile))
        except Exception as e:
            print(f"  ⚠ JobSpy search failed: {e}")

    if enabled("rss", False):
        try:
            from utils.rss_source import discover_rss_jobs
            print("\n📡 Checking RSS feeds...")
            all_jobs.extend(discover_rss_jobs(profile))
        except Exception as e:
            print(f"  ⚠ RSS feeds failed: {e}")

    if enabled("adzuna", False):
        try:
            from utils.adzuna_source import discover_adzuna_jobs
            print("\n📊 Searching Adzuna...")
            all_jobs.extend(discover_adzuna_jobs(profile))
        except Exception as e:
            print(f"  ⚠ Adzuna failed: {e}")

    if enabled("hn", False):
        try:
            from utils.hn_source import discover_hn_jobs
            print("\n📰 Checking HN Who is Hiring...")
            all_jobs.extend(discover_hn_jobs(profile))
        except Exception as e:
            print(f"  ⚠ HN Who is Hiring failed: {e}")

    if profile.get("custom_career_pages") and enabled("career_pages", True):
        try:
            from utils.career_page_source import discover_career_page_jobs
            print("\n🌐 Scraping custom career pages...")
            all_jobs.extend(await discover_career_page_jobs(profile))
        except Exception as e:
            print(f"  ⚠ Career page scraping failed: {e}")

    before = len(all_jobs)
    all_jobs = deduplicate_jobs(all_jobs)
    if before != len(all_jobs):
        print(f"\n🔄 Deduplicated: {before} -> {len(all_jobs)} unique jobs")

    cap = int(search_config.get("max_candidates_per_run", 60) or 0)
    if cap > 0 and len(all_jobs) > cap:
        print(f"\n✂ Candidate safety cap: {len(all_jobs)} -> {cap} before AI scoring")
        all_jobs = all_jobs[:cap]

    print(f"\n📊 Total: {len(all_jobs)} candidate jobs found")
    return all_jobs
