"""AI Resume Tailoring — Generates complete truthful resume content per job posting."""


def tailor_resume(job_description: str, base_resume_text: str, profile: dict, brain=None) -> dict:
    """Generate tailored content plus a complete structured resume for PDF rendering."""
    if not base_resume_text:
        return {
            "tailored_summary": "",
            "tailored_bullets": [],
            "emphasis_areas": [],
            "keywords_to_include": [],
            "deemphasize": [],
            "tailored_cover_letter": "",
            "full_resume": {},
            "error": "No resume text available",
        }

    if brain is None:
        from utils.brain import ClaudeBrain
        brain = ClaudeBrain(verbose=False, profile=profile)

    skills = profile.get("skills", {})
    roles = profile.get("preferences", {}).get("roles", [])

    prompt = f"""You are an expert executive resume consultant. Tailor this candidate's resume to the specific job posting while preserving factual accuracy.

CANDIDATE'S BASE RESUME:
{base_resume_text[:12000]}

TARGET ROLES: {', '.join(roles)}
KEY SKILLS: {', '.join(skills.get('primary', []))}

JOB POSTING:
{job_description[:9000]}

Return one JSON object with these fields:
{{
  "tailored_summary": "2-3 sentence summary for this job",
  "tailored_bullets": ["up to 8 strongest rewritten bullets"],
  "emphasis_areas": ["areas to emphasize"],
  "keywords_to_include": ["job keywords that are actually supported by the resume"],
  "deemphasize": ["less relevant areas"],
  "tailored_cover_letter": "3 paragraph specific cover letter",
  "full_resume": {{
    "headline": "ATS-friendly headline targeted to this role",
    "summary": "complete professional summary",
    "skills": ["12-20 concise skills/keywords"],
    "experience": [
      {{
        "title": "job title exactly or conservatively paraphrased from source",
        "company": "company",
        "location": "location if source provides it",
        "dates": "dates if source provides them",
        "bullets": ["2-6 truthful role-specific bullets based ONLY on source resume"]
      }}
    ],
    "education": [{{"school":"", "degree":"", "date":""}}],
    "certifications": [{{"name":"", "issuer":"", "date":""}}],
    "additional": ["optional short factual items"]
  }}
}}

Rules:
- Never invent an employer, title, credential, metric, deal, client, achievement, date, responsibility, or skill.
- Every factual statement must be supported by the base resume text.
- Reorder and rewrite for relevance, but do not materially change meaning.
- If a requirement is not supported, omit it instead of pretending.
- Keep the full resume approximately 2 pages when rendered: prioritize the most relevant experience but preserve enough career history to show seniority.
- Mirror useful terminology from the job posting only where it truthfully maps to the candidate's experience.
- Do not use first person in the resume.
- Cover letter may be first person but must remain factual.
"""

    defaults = {
        "tailored_summary": "",
        "tailored_bullets": [],
        "emphasis_areas": [],
        "keywords_to_include": [],
        "deemphasize": [],
        "tailored_cover_letter": "",
        "full_resume": {},
    }
    try:
        result = brain.ask_json(prompt, timeout=180, component="resume_tailoring")
        for key, default in defaults.items():
            if key not in result:
                result[key] = default
        return result
    except Exception as e:
        return {**defaults, "error": str(e)}
