"""Tailored CV / cover-letter content generation for one queue posting.

Phase 3 scope: given an account's profile and a specific matched posting,
produce the LaTeX source for a CV and a cover letter, built on the structural
templates in ``blueprint/cv_base.tex`` and ``blueprint/cover_letter_base.tex``.

LLM AVAILABILITY (checked live in this sandbox, not assumed):
  * Outbound HTTPS to ``api.anthropic.com`` IS reachable from this sandbox's
    network proxy (a bare unauthenticated POST returned HTTP 401 - reachable,
    just unauthenticated, which is exactly what "no key" looks like).
  * No ``ANTHROPIC_API_KEY`` (or similar) environment variable is present in
    this sandbox for the app itself to authenticate with, and the `anthropic`
    Python package is not installed / not in requirements.txt.
  * Conclusion: there is no key for JobSync to actually call the API with
    here, so this module cannot exercise a real LLM call in this environment.
    It is still built as a genuine integration point: ``draft_content()`` is
    the single seam an LLM path plugs into, and it DOES call a real model
    (via the official ``anthropic`` SDK) whenever ``ANTHROPIC_API_KEY`` is
    set at runtime - e.g. once a user supplies their own key. Wire this up
    to a Settings field storing that key if/when that's added; this module
    only reads it from the environment for now.
  * Fallback path (the one that actually ran in this sandbox): a plain,
    non-AI template fill using the profile's own background text verbatim,
    split into bullet-sized lines. It never invents experience, employers,
    dates, or credentials the user did not supply - if the profile is thin,
    the output is thin too, honestly.
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any

_LATEX_SPECIAL = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


def escape_latex(text: str) -> str:
    """Escape characters LaTeX would otherwise choke on or misrender."""
    if not text:
        return ""
    out = []
    for ch in text:
        out.append(_LATEX_SPECIAL.get(ch, ch))
    return "".join(out)


def _split_background_into_bullets(background: str, max_bullets: int = 6) -> list[str]:
    """Turn free-form background text into short bullet lines, verbatim.

    Splits on newlines first, then on sentence boundaries for any long
    remaining chunks, so a single wall-of-text background still produces
    reasonable-looking bullets without rewording anything.
    """
    if not background:
        return []
    lines = [ln.strip(" -•\t") for ln in background.splitlines() if ln.strip()]
    if len(lines) <= 1 and background.strip():
        # No natural line breaks - fall back to splitting on sentences.
        sentences = re.split(r"(?<=[.!?])\s+", background.strip())
        lines = [s.strip() for s in sentences if s.strip()]
    return lines[:max_bullets] if lines else []


def _fallback_draft(profile: dict, posting: dict) -> dict:
    """Non-AI content draft: the profile's own words, verbatim, reshaped to
    fit the template's slots. No fabricated employer names, dates, or
    credentials - anything the profile didn't provide is left blank/omitted.
    """
    name = profile.get("name") or profile.get("email", "").split("@")[0] or "Applicant"
    target_role = profile.get("target_role", "")
    location = profile.get("location", "")
    background = profile.get("background", "")

    bullets = _split_background_into_bullets(background)
    if not bullets:
        bullets = ["Background details not yet provided in profile."]

    skills = sorted({w for w in re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{2,}", background)})[:12]

    cover_paragraphs = [
        (
            f"I'm applying for the {posting.get('title', 'role')} position at "
            f"{posting.get('company', 'your company')}. "
            + (bullets[0] if bullets else "")
        ).strip(),
        " ".join(bullets[1:3]).strip() or "My background aligns with this role's requirements.",
        (
            f"I'm particularly drawn to this opportunity because it matches my "
            f"target focus on {target_role or 'this field'}"
            + (f" and my preference for {location}" if location else "")
            + "."
        ),
    ]

    return {
        "name": name,
        "email": profile.get("email", ""),
        "location": location,
        "target_role": target_role,
        "profile_summary": bullets[0] if bullets else "",
        "skills": skills,
        "experience_bullets": bullets,
        "cover_paragraphs": [p for p in cover_paragraphs if p],
        "source": "fallback_template",
    }


def _llm_draft(profile: dict, posting: dict, api_key: str) -> dict | None:
    """Attempt a real Anthropic API call to draft tailored content.

    Returns None (never raises) if the SDK isn't installed, the call fails,
    or the response can't be parsed - callers should fall back to
    ``_fallback_draft`` in that case.
    """
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "You are drafting application content strictly from the "
            "candidate's own background text below. Do not invent employers, "
            "job titles, dates, or credentials that are not present in the "
            "background text.\n\n"
            f"Target role: {profile.get('target_role', '')}\n"
            f"Location: {profile.get('location', '')}\n"
            f"Candidate background:\n{profile.get('background', '')}\n\n"
            f"Job title: {posting.get('title', '')}\n"
            f"Company: {posting.get('company', '')}\n"
            f"Job description:\n{posting.get('description', '')[:4000]}\n\n"
            "Return a JSON object with keys: profile_summary (string), "
            "skills (list of strings), experience_bullets (list of strings, "
            "each grounded in the background text), cover_paragraphs (list "
            "of 3 strings for a cover letter body)."
        )
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        import json

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        return {
            "name": profile.get("name") or profile.get("email", "").split("@")[0] or "Applicant",
            "email": profile.get("email", ""),
            "location": profile.get("location", ""),
            "target_role": profile.get("target_role", ""),
            "profile_summary": data.get("profile_summary", ""),
            "skills": data.get("skills", []),
            "experience_bullets": data.get("experience_bullets", []),
            "cover_paragraphs": data.get("cover_paragraphs", []),
            "source": "llm",
        }
    except Exception:
        return None


def draft_content(profile: dict, posting: dict) -> dict:
    """Return structured content fields for the CV/cover letter templates.

    Tries a real LLM call first (only when ``ANTHROPIC_API_KEY`` is set in
    the environment); falls back to a verbatim, non-AI template fill of the
    profile's own background text otherwise. The returned dict's "source"
    key says which path produced it ("llm" or "fallback_template").
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        drafted = _llm_draft(profile, posting, api_key)
        if drafted is not None:
            return drafted
    return _fallback_draft(profile, posting)


def render_cv_tex(content: dict) -> str:
    """Fill the CV template structure (blueprint/cv_base.tex's commands)
    with drafted content, escaping LaTeX special characters throughout."""
    name = escape_latex(content.get("name", ""))
    email = escape_latex(content.get("email", ""))
    location = escape_latex(content.get("location", ""))
    role = escape_latex(content.get("target_role", ""))
    summary = escape_latex(content.get("profile_summary", ""))
    skills = content.get("skills", [])
    bullets = content.get("experience_bullets", [])

    skill_rows = "\n\n".join(
        f"\\skillrow{{{escape_latex(s)}}}{{}}" for s in skills
    ) or "\\skillrow{}{}"
    bullet_items = "\n\n".join(f"\\item {escape_latex(b)}" for b in bullets) or "\\item "

    return f"""\\documentclass[a4paper,10pt]{{article}}

\\usepackage[left=0.65in,right=0.65in,top=0.55in,bottom=0.55in]{{geometry}}
\\usepackage{{titlesec}}
\\usepackage{{enumitem}}
\\usepackage[hidelinks]{{hyperref}}
\\usepackage{{tabularx}}
\\usepackage{{array}}
\\usepackage{{ragged2e}}

\\setlength{{\\parindent}}{{0pt}}
\\setlength{{\\parskip}}{{0pt}}
\\setlength{{\\tabcolsep}}{{0pt}}
\\renewcommand{{\\familydefault}}{{\\sfdefault}}

\\titleformat{{\\section}}{{\\large\\bfseries}}{{}}{{0pt}}{{}}[\\vspace{{-5pt}}\\rule{{\\textwidth}}{{0.5pt}}]
\\titlespacing*{{\\section}}{{0pt}}{{8pt}}{{5pt}}
\\setlist[itemize]{{leftmargin=14pt,itemsep=2pt,topsep=2pt,parsep=0pt,partopsep=0pt}}

\\newcommand{{\\resumeSubheading}}[4]{{
    \\begin{{tabularx}}{{\\textwidth}}{{@{{}}X r@{{}}}}
        \\textbf{{#1}} & #2 \\\\
        \\textit{{#3}} & \\textit{{#4}}
    \\end{{tabularx}}
    \\vspace{{2pt}}
}}
\\newcommand{{\\skillrow}}[2]{{\\textbf{{#1}} #2\\\\[2pt]}}

\\begin{{document}}

\\begin{{center}}
{{\\LARGE \\textbf{{{name}}}}}\\\\[4pt]
{role}\\;|\\;{location}\\\\
\\href{{mailto:{email}}}{{{email}}}
\\end{{center}}

\\vspace{{-3pt}}

\\section*{{Profile}}
{summary}

\\section*{{Skills}}
{skill_rows}

\\section*{{Experience}}
\\begin{{itemize}}
{bullet_items}
\\end{{itemize}}

\\end{{document}}
"""


def render_cover_letter_tex(content: dict, posting: dict) -> str:
    """Fill the cover-letter template structure with drafted content."""
    name = escape_latex(content.get("name", ""))
    email = escape_latex(content.get("email", ""))
    location = escape_latex(content.get("location", ""))
    company = escape_latex(posting.get("company", ""))
    company_location = escape_latex(posting.get("location", ""))
    title = escape_latex(posting.get("title", ""))
    today = date.today().strftime("%Y-%m-%d")
    paragraphs = content.get("cover_paragraphs", [])
    body = "\n\n".join(escape_latex(p) for p in paragraphs)

    return f"""\\documentclass[a4paper,11pt]{{letter}}

\\usepackage[a4paper,margin=1in]{{geometry}}
\\usepackage{{parskip}}

\\begin{{document}}

\\begin{{flushleft}}
\\textbf{{{name}}}\\\\
{location}\\\\
{email}
\\end{{flushleft}}

\\vspace{{0.7cm}}

\\begin{{flushleft}}
\\textbf{{Date:}} {today}

\\textbf{{Subject:}} Application for {title}

Hiring Team\\\\
{company}\\\\
{company_location}
\\end{{flushleft}}

\\vspace{{0.3cm}}

Dear Hiring Team,

\\vspace{{0.3cm}}

{body}

\\vspace{{0.5cm}}

Kind regards,

{name}

\\end{{document}}
"""


def generate_application(profile: dict, posting: dict) -> dict[str, str]:
    """Top-level entry point: draft content, then render both .tex documents.

    Returns {"cv_tex": ..., "cover_letter_tex": ..., "source": "llm"|"fallback_template"}.
    """
    content = draft_content(profile, posting)
    return {
        "cv_tex": render_cv_tex(content),
        "cover_letter_tex": render_cover_letter_tex(content, posting),
        "source": content.get("source", "fallback_template"),
    }
