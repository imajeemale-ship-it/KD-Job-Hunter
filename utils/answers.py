"""
Cached answer matcher — avoids calling Claude CLI for common application questions.
Pattern-matches question text against known Q&A pairs.
"""

import re
from typing import Optional

# Map of keyword patterns → profile.yaml common_answers keys
QUESTION_PATTERNS = {
    # Work authorization
    r"(authorized|legally|eligible).*(work|employ)": "authorized_to_work",
    r"(require|need|sponsor).*visa": "require_sponsorship",
    r"(sponsorship|visa\s*sponsor)": "require_sponsorship",
    r"work\s*(authorization|permit|eligibility)": "authorized_to_work",

    # Experience
    r"(years?|yrs?).*(experience|professional)": "years_experience",
    r"(how long|how many).*(work|experience|industry)": "years_experience",

    # Relocation
    r"(reloc|move|willing to relocate)": "willing_to_relocate",

    # Salary
    r"(salary|compensation|pay|wage).*expect": "salary_expectation",
    r"(desired|expected).*(salary|compensation|pay)": "salary_expectation",

    # Start date
    r"(start|begin|available).*(date|when|earliest)": "earliest_start_date",
    r"(when|how soon).*(start|begin|available|join)": "earliest_start_date",

    # How did you hear
    r"(how did you|where did you|how.*(hear|find|learn))": "how_did_you_hear",
    r"(source|referral|hear about)": "how_did_you_hear",

    # EEO / Demographics (usually optional — answer with "prefer not to say")
    r"(gender|sex)\b": "gender",
    r"(race|ethnic|ethnicity)": "race_ethnicity",
    r"(veteran|military|armed forces)": "veteran_status",
    r"(disabilit|handicap)": "disability_status",
}


def find_cached_answer(question: str, common_answers: dict) -> Optional[str]:
    """
    Try to match a question against known patterns.
    Returns the answer string if found, None if Claude should handle it.
    """
    question_lower = question.lower().strip()

    for pattern, answer_key in QUESTION_PATTERNS.items():
        if re.search(pattern, question_lower):
            answer = common_answers.get(answer_key)
            if answer:
                return answer

    return None


def get_personal_field(field_name: str, personal: dict) -> Optional[str]:
    """
    Try to match a form field label to a personal info field.
    Returns the value if matched, None otherwise.
    """
    field_lower = field_name.lower().strip()

    mappings = {
        r"(first\s*name|given\s*name|fname)": "first_name",
        r"(last\s*name|surname|family\s*name|lname)": "last_name",
        r"(full\s*name|your\s*name|name)": lambda p: f"{p['first_name']} {p['last_name']}",
        r"(email|e-mail)": "email",
        r"(phone|mobile|cell|telephone)": "phone",
        r"(city|location|address)": "location",
        r"(linkedin|linked\s*in)": "linkedin",
        r"(github|git\s*hub)": "github",
        r"(portfolio|website|personal\s*site|url)": "portfolio",
    }

    for pattern, key_or_fn in mappings.items():
        if re.search(pattern, field_lower):
            if callable(key_or_fn):
                return key_or_fn(personal)
            return personal.get(key_or_fn)

    return None


# Autonomous answers use exact questions and narrow aliases. Broad fuzzy matches
# (e.g. total experience for years of Kubernetes) are not applicant facts.
def _question_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def trusted_answer(question: str, profile: dict):
    """Return only configured facts; never ask a model to supply an answer."""
    key = _question_key(question)
    for section in ("verified_answers", "common_answers"):
        for label, answer in profile.get(section, {}).items():
            if _question_key(label) == key and answer is not None and answer != "":
                return str(answer)
    aliases = {
        "first name": "first_name", "given name": "first_name",
        "last name": "last_name", "family name": "last_name", "surname": "last_name",
        "email": "email", "email address": "email", "your email": "email",
        "phone": "phone", "phone number": "phone", "mobile phone": "phone",
        "location": "location", "current location": "location",
        "linkedin": "linkedin", "linkedin url": "linkedin", "linkedin profile": "linkedin",
        "github": "github", "github url": "github",
        "portfolio": "portfolio", "website": "portfolio", "personal website": "portfolio",
        "current company": "current_company",
    }
    personal = profile.get("personal", {})
    if key in ("name", "full name", "your name"):
        if personal.get("first_name") and personal.get("last_name"):
            return f"{personal['first_name']} {personal['last_name']}"
        return None
    if key in aliases:
        value = personal.get(aliases[key])
        return str(value) if value is not None and value != "" else None
    common_aliases = {
        "salary expectation": "salary_expectation",
        "salary expectations": "salary_expectation",
        "desired salary": "salary_expectation",
        "expected compensation": "salary_expectation",
        "how did you hear about us": "how_did_you_hear",
        "how did you hear about this job": "how_did_you_hear",
        "earliest start date": "earliest_start_date",
        "years of professional experience": "years_experience",
        "total years of experience": "years_experience",
        "are you willing to relocate": "willing_to_relocate",
        "will you require visa sponsorship": "require_sponsorship",
        "do you require sponsorship": "require_sponsorship",
        "are you authorized to work": "authorized_to_work",
        "are you legally authorized to work": "authorized_to_work",
        "gender": "gender", "race ethnicity": "race_ethnicity",
        "veteran status": "veteran_status", "disability status": "disability_status",
    }
    value = profile.get("common_answers", {}).get(common_aliases.get(key))
    return str(value) if value is not None and value != "" else None
