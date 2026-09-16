from __future__ import annotations

import os

import json
import ast
import re
import tempfile
from pathlib import Path
from typing import Iterable


__all__ = ["extract_text", "analyze_blueprint", "build_external_ai_prompt", "load_ai_cv_generation_prompt", "extract_latex_code", "validate_external_latex"]


def load_ai_cv_generation_prompt() -> str:
    """Load the user-approved HR-focused CV generation instructions.

    This prompt is the canonical instruction set for every CV generation path.
    JobSync keeps it separate from the model payload so the locked LaTeX template
    can be rendered locally instead of forcing the local model to reproduce LaTeX.
    """
    prompt_path = Path(__file__).resolve().parent / "ai_cv_generation_prompt.txt"
    try:
        text = prompt_path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError as exc:
        raise RuntimeError(f"The AI CV generation instruction file is missing: {exc}") from exc
    if not text:
        raise RuntimeError("The AI CV generation instruction file is empty.")
    return text


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".tex"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        import pymupdf
        doc = pymupdf.open(path)
        return "\n".join(page.get_text() for page in doc)
    if suffix == ".docx":
        from docx import Document
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    raise ValueError(f"Unsupported file type: {suffix}")


def _plain_text_from_tex(tex: str) -> str:
    """Convert a LaTeX CV into readable content while hiding layout commands."""
    s = tex or ""
    # Keep the useful arguments from common CV macros.
    s = re.sub(r"\\resumeSubheading\{([^{}]*)\}\{([^{}]*)\}\{([^{}]*)\}\{([^{}]*)\}",
               r"\1 | \3 | \4", s)
    s = re.sub(r"\\resumeProject\{([^{}]*)\}\{([^{}]*)\}", r"\1 | \2", s)
    s = re.sub(r"\\skillrow\{([^{}]*)\}\{([^{}]*)\}", r"\1: \2", s)
    s = re.sub(r"\\section\*?\{([^{}]*)\}", r"\n## \1\n", s)
    s = re.sub(r"\\item\s*", "\n- ", s)
    # Strip hyperlinks but keep visible link text.
    s = re.sub(r"\\href\{[^{}]*\}\{([^{}]*)\}", r"\1", s)
    # Remove remaining commands/braces, preserving their argument text where possible.
    s = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", s)
    s = s.replace("{", " ").replace("}", " ")
    s = s.replace("~", " ")
    s = re.sub(r"\s+", " ", s)
    # Restore useful line breaks around bullets/headings after whitespace collapse.
    s = re.sub(r"\s+-\s+", "\n- ", s)
    s = re.sub(r"\s+##\s+", "\n## ", s)
    return s.strip()


def build_reference_context(refs: Iterable[dict], max_chars_each: int = 14000) -> str:
    chunks = []
    for r in list(refs)[:3]:
        raw = r.get("text", "") or ""
        name = str(r.get("name", "reference"))
        if name.lower().endswith(".tex"):
            content = _plain_text_from_tex(raw)
        else:
            content = raw
        chunks.append(f"REFERENCE DOCUMENT: {name}\n{content[:max_chars_each]}")
    return "\n\n---\n\n".join(chunks)


def _clean_code_fence(output: str) -> str:
    output = output.strip()
    if output.startswith("```"):
        lines = output.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        output = "\n".join(lines)
    return output.strip()


def _json_from_output(output: str) -> dict:
    """Parse model JSON robustly, including common local-model Python-dict output."""
    clean = _clean_code_fence(output)
    # Remove Qwen reasoning blocks if the model leaked them despite think=false.
    if "</think>" in clean:
        clean = clean.rsplit("</think>", 1)[1].strip()
    # First try strict JSON.
    try:
        value = json.loads(clean)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass

    # Find the largest balanced JSON/object-like block instead of using a greedy
    # regex, which can accidentally consume multiple objects or trailing text.
    candidates = []
    starts = [i for i, ch in enumerate(clean) if ch == "{"]
    for start in starts:
        depth = 0
        in_string = False
        quote = ""
        escape = False
        for i in range(start, len(clean)):
            ch = clean[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(clean[start:i + 1])
                    break
    for candidate in sorted(candidates, key=len, reverse=True):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, TypeError):
            # Qwen frequently emits a Python-style dict with single quotes.
            try:
                value = ast.literal_eval(candidate)
                if isinstance(value, dict):
                    return value
            except (ValueError, SyntaxError, TypeError):
                continue
    raise RuntimeError("The AI did not return a valid structured CV object. Please try Generate again.")


def _latex_escape(value: str) -> str:
    # AI returns plain text only. Escape LaTeX special characters before insertion.
    out = str(value or "")
    replacements = [
        ("\\", r"\\textbackslash{}"),
        ("&", r"\\&"),
        ("%", r"\\%"),
        ("$", r"\\$"),
        ("#", r"\\#"),
        ("_", r"\\_"),
        ("{", r"\\{"),
        ("}", r"\\}"),
    ]
    for a, b in replacements:
        out = out.replace(a, b)
    return out


def analyze_blueprint(tex: str) -> dict:
    sections = []
    for m in re.finditer(r"\\section\*\{([^}]*)\}", tex):
        sections.append(m.group(1))
    return {
        "documentclass": re.search(r"\\documentclass\[([^]]+)\]\{([^}]+)\}", tex).group(0) if re.search(r"\\documentclass\[[^]]+\]\{[^}]+\}", tex) else "",
        "page_setup": {
            "left": re.search(r"left=([^,]+)", tex).group(1).strip() if re.search(r"left=([^,]+)", tex) else "",
            "right": re.search(r"right=([^,]+)", tex).group(1).strip() if re.search(r"right=([^,]+)", tex) else "",
            "top": re.search(r"top=([^,]+)", tex).group(1).strip() if re.search(r"top=([^,]+)", tex) else "",
            "bottom": re.search(r"bottom=([^\n]+)", tex).group(1).strip() if re.search(r"bottom=([^\n]+)", tex) else "",
        },
        "font_family": "sans serif" if r"\\renewcommand{\\familydefault}{\\sfdefault}" in tex else "template-defined",
        "sections": sections,
        "skill_rows": len(re.findall(r"\\skillrow\{", tex)),
        "experience_entries": len(re.findall(r"\\resumeSubheading\{", tex)),
        "project_entries": len(re.findall(r"\\resumeProject\{", tex)),
        "new_pages": len(re.findall(r"\\newpage", tex)),
    }


def _replace_between(tex: str, start: str, end: str, replacement: str) -> str:
    pattern = re.compile(re.escape(start) + r"(.*?)" + re.escape(end), re.S)
    if not pattern.search(tex):
        raise RuntimeError(f"Blueprint structure not found between {start!r} and {end!r}.")
    return pattern.sub(start + "\n" + replacement.rstrip() + "\n" + end, tex, count=1)


def _extract_item_texts(itemize_block: str) -> list[str]:
    return [re.sub(r"\\item\s*", "", x, count=1).strip() for x in re.findall(r"\\item\s+.*?(?=\\item\s+|\\end\{itemize\})", itemize_block, re.S)]


def _merge_items(ai_items: list[str] | None, original_items: list[str], count: int) -> list[str]:
    """Require the AI to supply every content item; never silently copy source bullets."""
    ai = ai_items or []
    if len(ai) != count:
        raise RuntimeError(
            f"AI returned {len(ai)} bullets, but the template requires exactly {count}. "
            "The CV was not generated so the source CV cannot be copied unchanged."
        )
    merged = [str(x).strip() for x in ai]
    if any(not x for x in merged):
        raise RuntimeError("AI returned an empty CV bullet. The CV was not generated.")
    return merged


def _render_bullets(items: list[str] | None, count: int, original_items: list[str] | None = None) -> str:
    originals = original_items or []
    clean = _merge_items(items, originals, count)
    return "\\begin{itemize}\n" + "\n".join(f"\\item {_latex_escape(x)}" for x in clean) + "\n\\end{itemize}"


def _extract_skill_rows(template: str) -> list[tuple[str, str]]:
    rows = []
    for m in re.finditer(r"\\skillrow\{([^}]*)\}\{([^}]*)\}", template):
        rows.append((m.group(1), m.group(2)))
    return rows


def _render_skill_rows(rows: list[dict] | None, count: int, original_rows: list[tuple[str, str]] | None = None) -> str:
    originals = original_rows or []
    prepared = []
    ai_rows = rows or []
    if len(ai_rows) != count:
        raise RuntimeError(
            f"AI returned {len(ai_rows)} skill rows, but the template requires exactly {count}. "
            "The source skill text will not be copied as a fallback."
        )
    for i in range(count):
        row = ai_rows[i] if isinstance(ai_rows[i], dict) else {}
        label = str(row.get("label", "")).strip()
        text = str(row.get("text", "")).strip()
        if not label or not text:
            raise RuntimeError("AI returned an incomplete skill row. The CV was not generated.")
        prepared.append((label, text))
    return "\n".join(f"\\skillrow{{{_latex_escape(label)}}}{{{_latex_escape(text)}}}" for label, text in prepared)


def _extract_experience_slots(template: str) -> list[tuple[str, str, int]]:
    # Match all four command arguments. A lazy ``.*?}`` stops at the first ``{}``
    # argument and was the reason valid blueprints could not be rendered.
    heading_pattern = r"(?P<head>\\resumeSubheading\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\s*\n)(?P<body>\\begin\{itemize\}.*?\\end\{itemize\})"
    matches = list(re.finditer(heading_pattern, template, re.S))
    out = []
    for m in matches:
        head = m.group("head")
        bullet_count = len(re.findall(r"\\item\s+", m.group("body")))
        out.append((head, m.group("body"), bullet_count))
    return out


def _render_experience(template: str, entries: list[dict]) -> str:
    heading_pattern = r"(?P<head>\\resumeSubheading\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\s*\n)(?P<body>\\begin\{itemize\}.*?\\end\{itemize\})"
    matches = list(re.finditer(heading_pattern, template, re.S))
    if not matches:
        raise RuntimeError("Blueprint contains no resumeSubheading/itemize experience blocks.")
    if not entries:
        entries = [{} for _ in matches]
    elif len(entries) < len(matches):
        entries = entries + [entries[-1]] * (len(matches) - len(entries))
    result = template
    offset = 0
    for idx, m in enumerate(matches):
        original = m.group(0)
        body = m.group("body")
        count = max(1, len(re.findall(r"\\item\s+", body)))
        bullets = entries[idx].get("bullets", []) if isinstance(entries[idx], dict) else []
        original_items = _extract_item_texts(body)
        replacement = m.group("head") + "\n" + _render_bullets(bullets, count, original_items)
        a = m.start() + offset
        b = m.end() + offset
        result = result[:a] + replacement + result[b:]
        offset += len(replacement) - len(original)
    return result


def _render_project(template: str, bullets: list[str]) -> str:
    pattern = re.compile(r"(?P<head>\\resumeProject\{[^{}]*\}\{[^{}]*\}\s*\n)(?P<body>\\begin\{itemize\}.*?\\end\{itemize\})", re.S)
    m = pattern.search(template)
    if not m:
        return template
    count = max(1, len(re.findall(r"\\item\s+", m.group("body"))))
    original_items = _extract_item_texts(m.group("body"))
    replacement = m.group("head") + "\n" + _render_bullets(bullets, count, original_items)
    return template[:m.start()] + replacement + template[m.end():]


def _render_additional_info(template: str, bullets: list[str]) -> str:
    # Tailor this section while preserving the existing list structure/count.
    marker_start = r"\\section*{Additional Information}"
    pos = template.find(r"\\section*{Additional Information}")
    if pos < 0:
        return template
    end = template.find(r"\\end{document}", pos)
    if end < 0:
        return template
    section = template[pos:end]
    m = re.search(r"\\begin\{itemize\}.*?\\end\{itemize\}", section, re.S)
    if not m:
        return template
    count = max(1, len(re.findall(r"\\item\s+", m.group(0))))
    original_items = _extract_item_texts(m.group(0))
    replacement = _render_bullets(bullets, count, original_items)
    section = section[:m.start()] + replacement + section[m.end():]
    return template[:pos] + section + template[end:]


def _section_spans(tex: str) -> list[tuple[str, int, int, int]]:
    """Return section heading, heading start/end, and content end in template order."""
    matches = list(re.finditer(r"\\section\*\{([^}]*)\}", tex))
    spans: list[tuple[str, int, int, int]] = []
    for i, m in enumerate(matches):
        next_start = matches[i + 1].start() if i + 1 < len(matches) else tex.find(r"\end{document}")
        if next_start < 0:
            next_start = len(tex)
        spans.append((m.group(1).strip(), m.start(), m.end(), next_start))
    return spans


def _replace_section_body(tex: str, heading: str, replacement: str) -> str:
    """Replace only the body of an exact section heading, preserving the heading and delimiters."""
    pattern = re.compile(r"(\\section\*\{" + re.escape(heading) + r"\})([\s\S]*?)(?=\\section\*\{|\\end\{document\})")
    m = pattern.search(tex)
    if not m:
        raise RuntimeError(f"Blueprint section not found: {heading!r}.")
    body = "\n" + replacement.rstrip() + "\n\n"
    return tex[:m.end(1)] + body + tex[m.end():]


def render_cv_from_blueprint(template: str, data: dict) -> str:
    output = template
    sections = _section_spans(template)
    if not sections:
        raise RuntimeError("Blueprint contains no LaTeX sections; cannot safely tailor the CV.")
    headings = [name for name, _, _, _ in sections]

    # Profile: use the actual Profile section from the user's template.
    profile_heading = next((name for name in headings if name.lower() == "profile"), None)
    if profile_heading:
        original_profile = re.search(r"\\section\*\{" + re.escape(profile_heading) + r"\}(.*?)(?=\\section\*\{|\\end\{document\})", template, re.S)
        original_body = original_profile.group(1).strip() if original_profile else ""
        new_profile = str(data.get("profile", "")).strip()
        if not new_profile:
            raise RuntimeError("AI returned no new Profile content. The source Profile will not be copied.")
        output = _replace_section_body(output, profile_heading, _latex_escape(new_profile))

    # Technical/skills: find whichever section contains the template's skillrow commands.
    skill_count = len(re.findall(r"\\skillrow\{", template))
    if skill_count:
        skill_heading = None
        for i, (name, _, end, content_end) in enumerate(sections):
            if re.search(r"\\skillrow\{", template[end:content_end]):
                skill_heading = name
                break
        if skill_heading:
            skills = _render_skill_rows(data.get("skills", []), skill_count, _extract_skill_rows(template))
            output = _replace_section_body(output, skill_heading, skills)

    # Experience: preserve employer/title/date blocks exactly; replace only bullets.
    if re.search(r"\\resumeSubheading\{", template):
        output = _render_experience(output, data.get("experience", []))

    # Project: preserve project title/date and structure; replace only bullets.
    if re.search(r"\\resumeProject\{", template):
        output = _render_project(output, data.get("project_bullets", []))

    # Additional information, when present, retains its original location and list shape.
    output = _render_additional_info(output, data.get("additional_information", []))

    # Strong template integrity checks.
    for token in (r"\documentclass", r"\begin{document}", r"\end{document}"):
        if token not in output:
            raise RuntimeError(f"Generated CV failed template integrity check: missing {token}.")

    doc_start = output.find(r"\begin{document}")
    if doc_start < 0 or output.find(r"\documentclass", 0, doc_start) < 0:
        raise RuntimeError("Generated CV appears to have lost the document preamble.")

    # These structural elements must remain exactly as supplied by the user.
    for pattern, label in [
        (r"\\section\*\{", "section count"),
        (r"\\skillrow\{", "skill-row count"),
        (r"\\newpage", "page-break count"),
    ]:
        if len(re.findall(pattern, output)) != len(re.findall(pattern, template)):
            raise RuntimeError(f"Generated CV failed template integrity check: {label} changed.")

    return output

def _normalize_content(text: str) -> str:
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text or "")
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _source_content_snapshot(template: str) -> str:
    """Collect editable source text only; LaTeX layout and fixed metadata are ignored."""
    parts: list[str] = []
    for m in re.finditer(r"\\section\*\{([^}]*)\}(.*?)(?=\\section\*\{|\\end\{document\})", template, re.S):
        body = m.group(2)
        parts.extend(_extract_item_texts(body))
        for label, text in _extract_skill_rows(body):
            parts.extend([label, text])
        # Include plain profile/project prose that is not a command-only line.
        plain = re.sub(r"\\(begin|end)\{[^}]+\}", " ", body)
        plain = re.sub(r"\\[a-zA-Z]+(?:\[[^]]*\])?\{[^{}]*\}", " ", plain)
        if plain.strip():
            parts.append(plain)
    return _normalize_content(" ".join(parts))


def _generated_content_snapshot(data: dict) -> str:
    parts: list[str] = [str(data.get("profile", ""))]
    for row in data.get("skills", []) or []:
        if isinstance(row, dict):
            parts.extend([str(row.get("label", "")), str(row.get("text", ""))])
    for entry in data.get("experience", []) or []:
        if isinstance(entry, dict):
            parts.extend(str(x) for x in entry.get("bullets", []) or [])
    parts.extend(str(x) for x in data.get("project_bullets", []) or [])
    parts.extend(str(x) for x in data.get("additional_information", []) or [])
    return _normalize_content(" ".join(parts))


def _content_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _validate_generated_data(data: dict, meta: dict, exp_slots: list, project_count: int, add_count: int) -> None:
    if not isinstance(data, dict):
        raise RuntimeError("AI did not return a JSON object.")
    if not str(data.get("profile", "")).strip():
        raise RuntimeError("AI returned an empty Profile.")
    skills = data.get("skills")
    if not isinstance(skills, list) or len(skills) != meta["skill_rows"]:
        raise RuntimeError(f"AI must return exactly {meta['skill_rows']} skill rows.")
    exp = data.get("experience")
    if not isinstance(exp, list) or len(exp) != len(exp_slots):
        raise RuntimeError(f"AI must return exactly {len(exp_slots)} experience entries.")
    for i, entry in enumerate(exp):
        if not isinstance(entry, dict) or not isinstance(entry.get("bullets"), list):
            raise RuntimeError(f"AI returned invalid experience entry {i + 1}.")
        required = exp_slots[i][2]
        if len(entry["bullets"]) != required or any(not str(x).strip() for x in entry["bullets"]):
            raise RuntimeError(f"AI must return exactly {required} non-empty bullets for experience entry {i + 1}.")
    project = data.get("project_bullets")
    if not isinstance(project, list) or len(project) != project_count or any(not str(x).strip() for x in project):
        raise RuntimeError(f"AI must return exactly {project_count} non-empty project bullets.")
    additional = data.get("additional_information")
    if not isinstance(additional, list) or len(additional) != add_count or any(not str(x).strip() for x in additional):
        raise RuntimeError(f"AI must return exactly {add_count} non-empty additional-information bullets.")



def _editable_source_items(template: str) -> list[str]:
    """Extract the actual editable wording from the blueprint for anti-copy checks."""
    items: list[str] = []

    # Profile paragraphs.
    for m in re.finditer(
        r"\\section\*\{([^}]*)\}(.*?)(?=\\section\*\{|\\end\{document\})",
        template,
        re.S,
    ):
        heading = m.group(1).strip().lower()
        body = m.group(2)
        if heading == "profile":
            plain = re.sub(r"\\begin\{[^}]+\}|\\end\{[^}]+\}", " ", body)
            plain = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r" \1 ", plain)
            plain = re.sub(r"\\[A-Za-z@]+\*?", " ", plain)
            plain = re.sub(r"[{}]", " ", plain)
            if plain.strip():
                items.append(plain.strip())

    # Skill rows.
    for label, value in _extract_skill_rows(template):
        items.extend([label, value])

    # Experience/project/additional bullets.
    for body in re.findall(r"\\begin\{itemize\}.*?\\end\{itemize\}", template, re.S):
        items.extend(_extract_item_texts(body))

    return [x.strip() for x in items if x and x.strip()]


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _copy_similarity(a: str, b: str) -> float:
    """Similarity tuned for CV sentences; normalized token comparison."""
    import difflib
    aa = " ".join(_word_tokens(a))
    bb = " ".join(_word_tokens(b))
    if not aa or not bb:
        return 0.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def _has_copied_phrase(candidate: str, sources: list[str], phrase_words: int = 5) -> bool:
    """Reject sentences containing an unchanged 5-word phrase from a source item."""
    c = _word_tokens(candidate)
    if len(c) < phrase_words:
        return False
    source_tokens = [_word_tokens(s) for s in sources]
    for st in source_tokens:
        if len(st) < phrase_words:
            continue
        source_phrases = {" ".join(st[i:i+phrase_words]) for i in range(len(st) - phrase_words + 1)}
        cand_phrases = {" ".join(c[i:i+phrase_words]) for i in range(len(c) - phrase_words + 1)}
        if source_phrases & cand_phrases:
            return True
    return False


def _find_copied_items(data: dict, source_items: list[str]) -> list[str]:
    candidates: list[str] = [str(data.get("profile", ""))]
    for row in data.get("skills", []) or []:
        if isinstance(row, dict):
            candidates.extend([str(row.get("label", "")), str(row.get("text", ""))])
    for entry in data.get("experience", []) or []:
        if isinstance(entry, dict):
            candidates.extend(str(x) for x in entry.get("bullets", []) or [])
    candidates.extend(str(x) for x in data.get("project_bullets", []) or [])
    candidates.extend(str(x) for x in data.get("additional_information", []) or [])

    bad: list[str] = []
    for candidate in candidates:
        if not candidate.strip():
            continue
        if _has_copied_phrase(candidate, source_items, 5):
            bad.append(candidate)
            continue
        if any(_copy_similarity(candidate, source) >= 0.78 for source in source_items):
            bad.append(candidate)
    return bad


def extract_latex_code(output: str) -> str:
    """Extract a complete LaTeX document from an AI response."""
    text = str(output or "").strip()
    if not text:
        return ""
    fenced = re.findall(r"```(?:latex|tex)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates = fenced or [text]
    for candidate in candidates:
        candidate = candidate.strip()
        start = candidate.find(r"\documentclass")
        end = candidate.rfind(r"\end{document}")
        if start >= 0 and end >= start:
            return candidate[start:end + len(r"\end{document}")].strip()
    return ""



def load_builtin_template(document_type: str) -> str:
    """Load the bundled LaTeX template from the packaged or user data location.

    The source ZIP keeps ``blueprint`` beside ``program`` while packaged
    installations keep editable copies under ``user_blueprints``.  Resolve
    both locations explicitly so the CV workflow never looks for a template
    inside ``program/blueprint`` (which does not exist in the shipped layout).
    """
    name = "cv_base.tex" if document_type == "CV" else "cover_letter_base.tex"
    candidates: list[Path] = []

    # Packaged/user-data copy (used by installed JobSync).
    try:
        from services.app_paths import BASE_DIR as DATA_BASE_DIR
        if DATA_BASE_DIR:
            candidates.append(Path(DATA_BASE_DIR) / "user_blueprints" / name)
    except Exception:
        pass

    # Bundled source copy: blueprint is a sibling of program.
    candidates.append(Path(__file__).resolve().parents[2] / "blueprint" / name)

    # Backward-compatible location for older development layouts.
    candidates.append(Path(__file__).resolve().parents[1] / "blueprint" / name)

    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")

    searched = "\n".join(str(path) for path in candidates)
    raise FileNotFoundError(
        f"Built-in {document_type} template is missing. Searched:\n{searched}"
    )


def _structure_signature(tex: str) -> dict:
    return {
        "documentclass": re.search(r"^\\documentclass.*$", tex, re.M).group(0).strip() if re.search(r"^\\documentclass.*$", tex, re.M) else "",
        "preamble": tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else tex,
        "sections": re.findall(r"\\section\*\{([^}]*)\}", tex),
        "newcommands": re.findall(r"\\newcommand\{([^}]+)\}", tex),
        "newpage": len(re.findall(r"\\newpage", tex)),
        "skillrow": len(re.findall(r"\\skillrow\{", tex)),
        "resume_subheading": len(re.findall(r"\\resumeSubheading\{", tex)),
        "resume_project": len(re.findall(r"\\resumeProject\{", tex)),
        "itemize": len(re.findall(r"\\begin\{itemize\}", tex)),
        "flushleft": len(re.findall(r"\\begin\{flushleft\}", tex)),
    }


def validate_external_latex(document_type: str, latex: str, template: str, strict_structure: bool = True) -> tuple[bool, str]:
    """Validate an AI-returned complete LaTeX document.

    ``strict_structure=True`` is used for the explicit external-provider workflow.
    Local CV generation uses ``False`` so the model can omit source sections that are
    genuinely absent from the uploaded CV while still preserving the template preamble.
    """
    clean = _clean_code_fence(latex)
    if not clean:
        return False, "The AI response is empty."
    if r"\documentclass" not in clean or r"\begin{document}" not in clean or r"\end{document}" not in clean:
        return False, "The response is not a complete LaTeX document."
    expected = _structure_signature(template)
    actual = _structure_signature(clean)
    if not strict_structure:
        # Keep the visual foundation locked, but allow the AI to omit a section that is
        # absent from the candidate source. This is the intended simple CV workflow.
        if expected["documentclass"] and actual["documentclass"] != expected["documentclass"]:
            return False, "The document class was changed. The template is locked."
        # In simple local-CV mode the AI is allowed to emit a new document body and
        # omit source sections. Do not reject it for a body/preamble comparison; only
        # require a valid complete LaTeX document.
        # Basic delimiter sanity prevents obviously broken LaTeX from reaching Overleaf.
        depth = 0
        escaped = False
        for ch in clean:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return False, "The generated LaTeX contains an unmatched closing brace."
        if depth != 0:
            return False, "The generated LaTeX contains unmatched braces."
        return True, clean
    if actual["documentclass"] != expected["documentclass"]:
        return False, "The document class was changed. The template is locked."
    if actual["preamble"] != expected["preamble"]:
        return False, "The LaTeX preamble/page setup was changed. The template is locked."
    for key, label in [
        ("sections", "section names/order"),
        ("newcommands", "custom commands"),
        ("newpage", "page breaks"),
        ("skillrow", "skill rows"),
        ("resume_subheading", "experience blocks"),
        ("resume_project", "project blocks"),
        ("itemize", "list structure"),
        ("flushleft", "letter address blocks"),
    ]:
        if actual[key] != expected[key]:
            return False, f"The template structure changed: {label}."
    if document_type == "CV" and expected["newpage"] != 1:
        return False, "The built-in CV template is not configured as expected."
    # extract_latex_code/cleaning allows the user to paste the exact code block
    # returned by ChatGPT, Claude, or Gemini. Reject prose that survived extraction.
    if clean.startswith("Here is"):
        return False, "Paste the complete LaTeX code block returned by the AI."
    return True, clean


def build_external_ai_prompt(
    job: dict,
    references: str,
    profile: dict,
    template: str,
    document_type: str,
    provider: str,
    latest_pdf_text: str = "",
    **_compat: object,
) -> str:
    """Build a complete, copy/paste-ready prompt for ChatGPT, Claude or Gemini."""
    job_description = str(job.get("description") or "").strip()
    job_url = str(job.get("url") or "").strip()
    title = str(job.get("title") or "").strip()
    company = str(job.get("company") or "").strip()
    location = str(job.get("location") or "").strip()
    template_text = str(template or "").strip()

    if document_type == "CV":
        # The uploaded AI CV Generation Prompt is the canonical instruction set
        # for every CV generation request, including external providers.
        canonical = load_ai_cv_generation_prompt()
        source = references.strip() or "No uploaded reference document was supplied. Use the candidate profile as the available evidence source."
        if latest_pdf_text:
            source += "\n\nLATEST USER-APPROVED FINAL PDF CONTENT (local extraction)\n" + str(latest_pdf_text).strip()[:18000]
        job_json = json.dumps({
            "title": title,
            "company": company,
            "location": location,
            "url": job_url,
        }, ensure_ascii=False, indent=2)
        profile_text = json.dumps(profile or {}, ensure_ascii=False, indent=2)
        return f"""{canonical}

==================== JOBSYNC GENERATION INPUT ====================
TARGET JOB
{job_json}

JOB DESCRIPTION
{job_description or 'No full job description was stored. Use only the supplied job metadata and evidence. Never invent missing requirements.'}

CANDIDATE PROFILE
{profile_text}

REFERENCE CV / CANDIDATE EVIDENCE
{source}

MASTER LATEX TEMPLATE
The following template is LOCKED. Apply the canonical instructions to its existing content areas only. Do not redesign it.

{template_text}

==================== FINAL OUTPUT ====================
Return ONLY the complete finished LaTeX source, from \\documentclass through \\end{{document}}, in one latex code block. Do not add explanations before or after it.
""".strip()
    source = references.strip() or "No uploaded reference document was supplied. Use the candidate profile as the available evidence source."
    latest_pdf_text = str(latest_pdf_text or "").strip()
    if latest_pdf_text:
        source += "\n\nLATEST USER-APPROVED FINAL PDF CONTENT (local extraction)\n" + latest_pdf_text[:18000]
    profile_text = json.dumps(profile or {}, ensure_ascii=False, indent=2)
    if document_type == "CV":
        output_rules = """
CV OUTPUT RULES
- Return the FULL finished LaTeX source directly in your chat reply.
- Put the entire source in ONE Markdown code block tagged `latex` so the user can use the AI interface's Copy control and paste the complete source into Overleaf.
- Do NOT create, attach, upload, or offer a downloadable `.tex`/text file. Return the LaTeX directly in the chat only.
- Do NOT add any explanation, commentary, analysis, title, or text outside that single `latex` code block.
- The result must be complete LaTeX source that the user can paste directly into Overleaf.
- Preserve all factual candidate information unless a more specific profile/reference field supplies a correction; never invent.
"""
        if template_text:
            output_rules += """- A LaTeX template was supplied and is LOCKED. Preserve its document class, preamble, packages, custom commands, page geometry, header design, section names and order, page break, list structure, and visual system exactly. Change candidate/job content only.
- Keep the CV exactly two pages. Do not solve length by changing font size, margins, spacing rules, packages, or layout settings.
"""
        else:
            output_rules += """- No LaTeX template was supplied. Generate a complete, professional, ATS-friendly LaTeX CV from the candidate profile, reference documents, and target job. Use a clean layout suitable for Overleaf.
"""
    else:
        output_rules = """
COVER LETTER OUTPUT RULES
- Return the FULL finished LaTeX source directly in your chat reply.
- Put the entire source in ONE Markdown code block tagged `latex` so the user can use the AI interface's Copy control and paste the complete source into Overleaf.
- Do NOT create, attach, upload, or offer a downloadable `.tex`/text file. Return the LaTeX directly in the chat only.
- Do NOT add any explanation, commentary, analysis, title, or text outside that single `latex` code block.
- The result must be complete LaTeX source that the user can paste directly into Overleaf.
- Fill sender/application information from the candidate/job evidence and never invent facts.
"""
        if template_text:
            output_rules += """- The supplied template is LOCKED. Preserve its document class, preamble, packages, geometry, sender/application blocks, greeting, closing, spacing and formatting exactly. Write only inside its existing content areas.
"""
        else:
            output_rules += """- No LaTeX template was supplied. Generate a complete, professional one-page LaTeX cover letter suitable for Overleaf.
"""
        output_rules += """- Do not add unrelated sections or redesign a supplied template.
- Keep the letter focused and normally around 250–400 words unless the evidence makes a different length necessary.
"""
    return f"""You are preparing one real job application. Act simultaneously as a senior recruiter, hiring manager, ATS reviewer, and expert human CV/cover-letter writer.

AI ACCOUNT SELECTED: {provider}
DOCUMENT TO GENERATE: {document_type}

==================== TARGET JOB ====================
Job title: {title or 'Not provided'}
Company: {company or 'Not provided'}
Location: {location or 'Not provided'}
Job posting URL: {job_url or 'Not provided'}

JOB POSTING / DESCRIPTION
{job_description or 'No full description was stored. Use the URL when accessible; otherwise use only the supplied job metadata and candidate evidence. Never invent missing requirements.'}

==================== CANDIDATE PROFILE ====================
{profile_text}

==================== CANDIDATE / PRIOR DOCUMENT EVIDENCE ====================
{source}

==================== LOCKED MASTER LATEX TEMPLATE ====================
Treat the following LaTeX as the exact master format. It is a locked design specification. You may rewrite the content inside the existing content areas, but you may NOT alter the template settings or structure.

{template_text or "NO LATEX TEMPLATE SUPPLIED — generate a complete LaTeX document from the candidate/job evidence."}

==================== DEEP HR / RECRUITER ANALYSIS ====================
Before writing, silently determine:
1. What the employer actually needs this person to accomplish.
2. Which responsibilities and requirements are most likely to drive screening.
3. Which requirements are mandatory versus preferred.
4. Which technical terminology matters because it describes real work in the role.
5. Which candidate evidence genuinely matches those needs.
6. Which evidence deserves space because it would make a recruiter keep reading.
7. Which gaps exist and how to position the candidate honestly without hiding or inventing qualifications.
8. What makes this candidate credible for this specific vacancy compared with a generic applicant.

Do not simply copy keywords from the vacancy. Translate the employer's needs into credible candidate evidence.

==================== HUMAN WRITING STANDARD ====================
Do NOT write like an AI template.
Avoid clichés and generic filler, including: results-driven, highly motivated, dynamic professional, passionate professional, proven track record, hardworking, team player, strategic thinker, detail-oriented, go-getter, leveraged, spearheaded, utilized, responsible for, successfully, excellent communication skills.
Do not replace these with synonyms that carry the same empty meaning.
Every sentence must communicate useful evidence, scope, method, technical capability, problem solved, result, responsibility, context, or clear role-specific motivation where supported.
Prefer concrete work over self-description. Use numbers only when the candidate evidence supplies them. Never manufacture metrics, tools, employers, dates, qualifications, software, technologies, achievements, responsibilities, projects, ownership, or seniority.
Do not keyword-stuff.

==================== ATS + HUMAN BALANCE ====================
Use relevant terminology naturally where the candidate's evidence supports it. Prioritize the real hiring criteria and readability. Keep the result concise, credible, specific, and easy for a recruiter to scan.

==================== FINAL SILENT HR REVIEW ====================
Before returning the document, silently review it as the hiring manager:
- Is the candidate's relevance to this exact vacancy obvious quickly?
- Does each important claim have factual support?
- Is the wording human, specific, and vacancy-focused?
- Is anything generic, inflated, repetitive, or cliché?
- Does the document make the candidate easier to interview, not merely easier to keyword-match?
Rewrite weak content before returning the final source.

{output_rules}
IMPORTANT FINAL OUTPUT FORMAT:
Return exactly ONE `latex` code block containing the COMPLETE finished document from `\\documentclass` through `\\end{{document}}`. The user must be able to click Copy and paste the entire source. Do not return a file, attachment, download link, or any text outside the code block. The template is locked; content is tailored, structure is not.
""".strip()

