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
specific job posting. Your task is to produce TARGETED TEXT REPLACEMENTS that tailor the
resume for this role while keeping the original document's structure and formatting intact.

## Rules

1. **Never fabricate** experience, skills, or qualifications. Only rephrase, reorder, and
   emphasize what already exists in the original resume.
2. **Mirror the JD's language** — if the JD says "microservices architecture", use that exact
   phrase instead of "distributed systems" (if the candidate has that experience).
3. **Keep replacements precise** — each "original" string must be an EXACT substring from the
   resume text provided. Do not paraphrase the original; copy it character-for-character.
4. **Quantify more** — if the original says "improved performance", keep it unless you can
   rephrase to highlight relevance. Don't invent metrics.
5. **Replacement length** — tailored text should be roughly similar length to the original
   (±30%) so it fits the same layout space.
6. Only replace bullet points and descriptions that benefit from tailoring. Leave things like
   company names, dates, job titles, education details, and contact info unchanged.

## Output Format

Return ONLY a valid JSON object:
{
  "replacement_pairs": [
    {
      "original": "<EXACT text from the resume to find>",
      "tailored": "<rewritten version optimized for the target job>",
      "section": "<which section this is from: experience|summary|skills|other>"
    }
  ],
  "summary_replacement": {
    "original": "<exact current summary/objective text, if one exists>",
    "tailored": "<new 2-3 sentence summary targeted at this specific role>"
  },
  "skills_replacement": {
    "original": "<exact current skills line/section text>",
    "tailored": "<reordered skills with most relevant to this job listed first>"
  },
  "candidate_name": "<full name from resume>",
  "contact_info": "<email, phone, location, LinkedIn if found in resume>",
  "tailoring_notes": "<brief explanation of what you changed and why>"
}

IMPORTANT: The "original" field in each replacement pair must be an EXACT copy-paste match
from the resume text. If you cannot find the exact text, skip that replacement. Partial or
approximate matches will cause the replacement to fail silently.
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
            temperature=0.3,
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

    user_message = f"""## ORIGINAL RESUME:
{resume_text[:6000]}

## TARGET JOB:
**Title:** {job_title}
**Company:** {job_company}
**Description:**
{job_description[:4000]}

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

    user_message = f"""## CANDIDATE RESUME:
{resume_text[:6000]}

## TARGET JOB:
**Title:** {job_title}
**Company:** {job_company}
**Description:**
{job_description[:4000]}

Write a tailored cover letter. Reference specific experiences from the resume — do NOT fabricate."""

    return _call_openai(api_key, COVER_LETTER_PROMPT, user_message, model)
