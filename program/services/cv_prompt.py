from __future__ import annotations

import json
from datetime import datetime

PROVIDERS = {
    "ChatGPT": "https://chatgpt.com/",
    "Claude": "https://claude.ai/",
    "Gemini": "https://gemini.google.com/",
}


def build_cv_prompt(job: dict, references: str, blueprint: str, profile: dict) -> str:
    """Build a provider-neutral prompt for manual CV generation.

    JobSync prepares the evidence and locked LaTeX template locally. The
    selected AI is used directly by the user; no provider credentials are
    required or stored by JobSync.
    """
    description = str(job.get("description", "") or "").strip()[:24000]
    profile_json = json.dumps(profile or {}, ensure_ascii=False, indent=2)
    job_json = json.dumps(
        {
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "industry": job.get("industry", ""),
            "experience": job.get("experience", ""),
            "url": job.get("url", ""),
        },
        ensure_ascii=False,
        indent=2,
    )

    return f'''You are an expert human CV writer and ATS/HR reviewer.

TASK
Tailor the candidate's master LaTeX CV for ONE target vacancy. Produce a truthful,
job-specific CV that is strong for both ATS screening and human recruiter review.
Aim for approximately 85–90+ ATS/HR alignment ONLY when the candidate's real evidence
supports it. Never manufacture qualifications to reach a score.

IMPORTANT WORKFLOW
1. Analyze the target vacancy before writing.
2. Identify must-have requirements, preferred requirements, responsibilities,
   technical skills, software/tools, terminology, education, experience level,
   language requirements, and important ATS keywords.
3. Analyze the candidate evidence supplied below.
4. Map only supported candidate evidence to the vacancy.
5. Rewrite the editable CV content with fresh, natural wording that emphasizes the
   strongest truthful matches.
6. Review the result as an ATS and as a recruiter.
7. Revise weak or generic wording before returning the final document.

TRUTHFULNESS — ABSOLUTE
- Use ONLY facts supported by the candidate profile and reference CV material.
- Never invent employers, job titles, dates, qualifications, certifications,
  technologies, software, projects, responsibilities, achievements, metrics,
  management responsibility, or seniority.
- Never claim a requirement is satisfied unless the supplied evidence supports it.
- If a job requirement is missing from the candidate evidence, do not fabricate it.
- Do not inflate responsibilities or seniority.
- Do not keyword-stuff.

MASTER LATEX TEMPLATE — LOCKED
The supplied LaTeX is the master document and is the source of truth for structure,
formatting, typography, section order, header, packages and layout.

You MUST:
- preserve the exact document class;
- preserve all required packages and custom commands;
- preserve the exact header;
- preserve all section names and their exact order;
- preserve the existing number of experience entries and bullets;
- preserve the existing number of skill rows;
- preserve the existing project structure and bullet count;
- preserve the existing Additional Information structure and bullet count;
- keep the document exactly two pages;
- modify CONTENT only inside the existing editable areas.

You MUST NOT:
- add, remove or rename sections;
- reorder sections;
- redesign the CV;
- change margins, fonts, spacing or formatting commands;
- add new packages;
- change the header;
- create a new CV layout;
- copy the template's wording as writing inspiration.

ANTI-COPY REQUIREMENT
Every editable sentence and bullet must be freshly written.
Do not copy source sentences verbatim.
Do not reuse a sequence of five consecutive words from the source wording.
Do not make a superficial synonym swap.
Use different sentence structures and natural human phrasing while preserving facts.
Avoid generic AI/CV language such as "results-driven", "dynamic", "leveraged",
"passionate", "proven track record", "spearheaded", "strategic", and similar filler.

ATS REQUIREMENTS
- Use important vacancy terminology naturally where it is factually supported.
- Prioritize must-have requirements over generic keywords.
- Make technical skills explicit when supported.
- Make relevant responsibilities and tools easy for ATS parsing.
- Keep standard, readable wording.
- Do not repeat keywords unnaturally.

HR REVIEW REQUIREMENTS
- Make the first section immediately relevant to the vacancy.
- Make experience bullets evidence-based and specific.
- Emphasize relevance rather than rewriting every fact equally.
- Keep the candidate's actual seniority clear.
- Prefer concise bullets that fit the supplied layout.
- Remove weak filler when the template permits replacement.

FINAL QUALITY CHECK
Before returning the CV, check:
- factual accuracy;
- job relevance;
- ATS keyword alignment;
- requirement coverage;
- recruiter readability;
- natural human wording;
- no invented claims;
- no copied wording;
- exact template structure;
- exact header;
- exact section order;
- exactly two pages.

FINAL OUTPUT
Return ONLY the complete final LaTeX source code.
Do not wrap it in Markdown fences.
Do not provide an explanation before or after the LaTeX.

TARGET JOB
{job_json}

JOB DESCRIPTION
{description}

CANDIDATE PROFILE
{profile_json}

REFERENCE CV MATERIAL
{references}

MASTER LATEX TEMPLATE
{blueprint}
'''.strip()


def provider_url(provider: str) -> str:
    return PROVIDERS.get(provider, "https://chatgpt.com/")


def prompt_filename(job: dict, provider: str) -> str:
    title = str(job.get("title", "CV") or "CV")
    company = str(job.get("company", "") or "")
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in f"{title}_{company}").strip()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"CV_Prompt_{provider}_{safe[:70]}_{stamp}.txt"
