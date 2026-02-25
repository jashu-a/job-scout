"""
AI Matcher & Document Generator using OpenAI API.

Three capabilities:
1. Score resume vs job description match
2. Generate a tailored resume (adapted to the job)
3. Generate a tailored cover letter
"""

import json
from openai import OpenAI


# ── PROMPT 1: Matching / Scoring ──────────────────────────────────────────────

MATCH_SYSTEM_PROMPT = """\
You are a senior technical recruiter with 15+ years of experience screening candidates.
You will receive a candidate's resume and a job posting. Your job is to evaluate the match
with the precision of someone whose reputation depends on quality placements.

## Evaluation Criteria (weighted)

1. **Hard Skills Match (35%)** — Do the candidate's technical skills, tools, languages, and
   frameworks align with the job requirements? Distinguish between "must-have" and "nice-to-have"
   in the JD. Partial credit for adjacent/transferable skills.

2. **Experience Level & Scope (25%)** — Does the candidate's years of experience, project scale,
   team size, and scope of responsibility match what the role demands? A senior role needs
   leadership evidence; a mid-level role needs execution evidence.

3. **Domain & Industry Relevance (20%)** — Has the candidate worked in the same or closely
   related industry/domain? E.g., fintech experience matters for a fintech role. Generic
   experience scores lower here.

4. **Education & Certifications (10%)** — Does the candidate meet the stated education
   requirements? Relevant certifications add value. Overqualification is neutral, not negative.

5. **Soft Signals & Culture Fit (10%)** — Resume writing quality, evidence of collaboration,
   communication, leadership, open-source contributions, publications, etc.

## Scoring Guide

- **90-100**: Near-perfect match. Would advance to final round at top companies.
- **75-89**: Strong match. Clearly qualified, minor gaps only.
- **60-74**: Moderate match. Has core skills but missing some key requirements or experience level is off.
- **40-59**: Weak match. Significant gaps in skills or experience. Would be a stretch hire.
- **20-39**: Poor match. Fundamentally different skill set or experience level.
- **0-19**: No meaningful match.

## Output Format

Return ONLY a valid JSON object (no markdown fences, no extra text):
{
  "score": <int 0-100>,
  "reasoning": "<3-4 sentences explaining the score, referencing specific resume items and JD requirements>",
  "key_matches": ["<specific skill/experience #1>", "<#2>", "<#3>"],
  "key_gaps": ["<specific missing requirement #1>", "<#2>", "<#3>"],
  "seniority_fit": "<under-leveled | good fit | over-leveled>",
  "recommendation": "<strong yes | yes | maybe | no>"
}
"""

# ── PROMPT 2: Tailored Resume ────────────────────────────────────────────────

TAILORED_RESUME_PROMPT = """\
You are an expert resume writer. You will receive a candidate's original resume TEXT and a
specific job posting. Your task is to produce TARGETED TEXT REPLACEMENTS that make this resume
clearly tailored for this specific role.

## Critical Rules

1. **Every replacement must be VISIBLY DIFFERENT** — a reader comparing the original and tailored
   version side by side should immediately notice the changes. Don't just swap single words.
2. **Never fabricate** — only rephrase, reorder, and emphasize what exists. But DO rephrase
   aggressively to match the JD's terminology and priorities.
3. **Mirror the JD's exact language** — if the JD says "CI/CD pipelines", replace "deployment
   automation" with "CI/CD pipelines". Match their vocabulary precisely.
4. **Rewrite bullet points to lead with relevance** — if a bullet mentions 3 things and only
   one is relevant to this job, restructure to lead with the relevant part.
5. **The "original" string must be EXACT** — copy it character-for-character from the resume text.
6. **Make 5-10 meaningful replacements minimum** — focus on:
   - Professional summary / objective (rewrite entirely for this role)
   - Top 3-5 most relevant experience bullets (rewrite to emphasize JD alignment)
   - Skills section (reorder to put most relevant skills first)
7. **Summary is most important** — always provide a completely rewritten summary that mentions
   the target company name, role title, and 2-3 key JD requirements the candidate meets.

## Output Format

Return ONLY a valid JSON object:
{
  "replacement_pairs": [
    {
      "original": "<EXACT text from the resume to find — must match character for character>",
      "tailored": "<meaningfully rewritten version targeting this specific job>",
      "section": "<experience|summary|skills|other>"
    }
  ],
  "summary_replacement": {
    "original": "<exact current summary/objective text, if one exists>",
    "tailored": "<completely rewritten 2-3 sentence summary mentioning the company name and role>"
  },
  "skills_replacement": {
    "original": "<exact current skills line/section text>",
    "tailored": "<reordered skills with JD-relevant skills first, using JD's exact terminology>"
  },
  "candidate_name": "<full name from resume>",
  "contact_info": "<email, phone, location, LinkedIn if found>",
  "tailoring_notes": "<what you changed and why — be specific>"
}

IMPORTANT: Generate at least 5 replacement_pairs. Each tailored text should be noticeably
different from the original — not just a single word change. If the job description is short,
use the job title and company to infer what skills and experiences to emphasize.
"""

# ── PROMPT 3: Cover Letter ───────────────────────────────────────────────────

COVER_LETTER_PROMPT = """\
You are an expert career coach who writes compelling, authentic cover letters.
You will receive a candidate's resume and a specific job posting.

## Rules

1. **Conversational but professional** — avoid stiff, generic language like "I am writing to
   express my interest in..." Instead, open with something specific about the company or role
   that shows genuine understanding.
2. **Tell a story** — connect the candidate's specific experiences to 2-3 key requirements
   of the role. Use the STAR method implicitly (situation, task, action, result).
3. **Show, don't tell** — instead of "I am a team player", reference a specific collaboration
   from their resume that produced results.
4. **Company research** — reference something specific about the company (from the JD or
   company name) to show this isn't a generic letter.
5. **Keep it concise** — 3-4 paragraphs max. Hiring managers skim.
6. **End with confidence** — close with enthusiasm and a clear call to action, not with
   "I hope to hear from you."
7. **Never fabricate** — only reference experiences and skills from the actual resume.

## Output Format

Return ONLY a valid JSON object:
{
  "cover_letter": "<the full cover letter text with paragraph breaks as \\n\\n>",
  "opening_hook": "<the first sentence, for preview purposes>",
  "key_themes": ["<theme1>", "<theme2>", "<theme3>"]
}
"""


def _call_openai(api_key: str, system_prompt: str, user_message: str, model: str) -> dict:
    """Generic OpenAI call with JSON parsing."""
    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.5,
            max_tokens=3000,
        )

        content = response.choices[0].message.content.strip()

        # Clean potential markdown fences
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        # Also handle ```json prefix
        if content.startswith("json\n"):
            content = content[5:]

        result = json.loads(content)
        result["_error"] = None
        return result

    except json.JSONDecodeError as e:
        return {"_error": f"JSON parse error: {e}", "_raw": content if 'content' in dir() else ""}
    except Exception as e:
        return {"_error": str(e)}


def match_resume_to_job(
    api_key: str,
    resume_text: str,
    job_title: str,
    job_company: str,
    job_description: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Score how well a resume matches a job description."""

    user_message = f"""## CANDIDATE RESUME:
{resume_text[:6000]}

## JOB POSTING:
**Title:** {job_title}
**Company:** {job_company}
**Description:**
{job_description[:4000]}"""

    result = _call_openai(api_key, MATCH_SYSTEM_PROMPT, user_message, model)

    # Normalize output for backward compatibility
    if result.get("_error"):
        return {
            "score": 0,
            "reasoning": f"AI matching failed: {result['_error']}",
            "key_matches": [],
            "key_gaps": [],
            "seniority_fit": "unknown",
            "recommendation": "no",
            "error": result["_error"],
        }

    result["error"] = None
    return result


def generate_tailored_resume(
    api_key: str,
    resume_text: str,
    job_title: str,
    job_company: str,
    job_description: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Generate a tailored resume adapted to a specific job."""

    # If description is too short, construct a richer prompt from what we have
    if len(job_description.strip()) < 100:
        desc_section = (
            f"**Description:** (Limited info available — tailor based on the role title and company)\n"
            f"This is a {job_title} position at {job_company}. "
            f"Focus your tailoring on skills and experiences most relevant to a typical "
            f"{job_title} role. Emphasize the company name '{job_company}' in the summary."
        )
    else:
        desc_section = f"**Description:**\n{job_description[:4000]}"

    user_message = f"""## ORIGINAL RESUME:
{resume_text[:6000]}

## TARGET JOB:
**Title:** {job_title}
**Company:** {job_company}
{desc_section}

CRITICAL: This resume MUST be uniquely tailored for {job_title} at {job_company}.
- The summary MUST mention "{job_company}" by name and "{job_title}" as the target role.
- Replacement pairs must reference specific requirements from THIS job description.
- Do NOT produce generic replacements that could apply to any job.
Tailor the resume for this specific role. Do NOT fabricate anything — only rephrase and reorder."""

    return _call_openai(api_key, TAILORED_RESUME_PROMPT, user_message, model)


def generate_cover_letter(
    api_key: str,
    resume_text: str,
    job_title: str,
    job_company: str,
    job_description: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Generate a tailored cover letter for a specific job."""

    # Handle missing or empty company name
    if not job_company or not job_company.strip():
        company_display = "your company"
    else:
        company_display = job_company

    # If description is too short, enrich the prompt
    if len(job_description.strip()) < 100:
        desc_section = (
            f"**Description:** (Limited info available)\n"
            f"This is a {job_title} position at {company_display}. "
            f"Write the cover letter based on how the candidate's experience "
            f"relates to a typical {job_title} role."
        )
    else:
        desc_section = f"**Description:**\n{job_description[:4000]}"

    user_message = f"""## CANDIDATE RESUME:
{resume_text[:6000]}

## TARGET JOB:
**Title:** {job_title}
**Company:** {company_display}
{desc_section}

Write a tailored cover letter addressed to {company_display}.
Reference specific experiences from the resume — do NOT fabricate.
IMPORTANT: Always use the company name "{company_display}" in the letter."""

    return _call_openai(api_key, COVER_LETTER_PROMPT, user_message, model)