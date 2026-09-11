from __future__ import annotations
import base64
import hashlib

def _pick(seed: str, key: str, items: list[str]) -> str:
    n = int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:8], 16)
    return items[n % len(items)]

def human_avatar_svg(seed: str, size: int = 96) -> str:
    seed = (seed or "job-tracker-user").strip().lower()
    skin = _pick(seed, "skin", ["#f6c6a5","#dfaa83","#c8875c","#a9643d","#7b492c"])
    hair = _pick(seed, "hair", ["#17110d","#2b1a12","#4a2a18","#6f4324","#b27a3f"])
    shirt = _pick(seed, "shirt", ["#2563eb","#7c3aed","#059669","#dc2626","#d97706","#374151"])
    bg = _pick(seed, "bg", ["#dbeafe","#ede9fe","#dcfce7","#fee2e2","#fef3c7","#e5e7eb"])
    glasses = _pick(seed, "glasses", ["0","0","0","1"]) == "1"
    longhair = _pick(seed, "longhair", ["0","0","1"]) == "1"
    hair_shape = (
        f'<path d="M20 54 Q16 18 48 14 Q80 18 76 54 L68 68 L62 36 Q48 42 30 34 L26 66Z" fill="{hair}"/>'
        if longhair else
        f'<path d="M24 43 Q26 17 48 15 Q70 17 72 43 L67 34 Q56 39 49 29 Q39 38 27 33Z" fill="{hair}"/>'
    )
    glasses_svg = (
        '<g fill="none" stroke="#333" stroke-width="2"><rect x="29" y="42" width="14" height="11" rx="4"/>'
        '<rect x="53" y="42" width="14" height="11" rx="4"/><path d="M43 47H53"/></g>'
        if glasses else ""
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 96 96">
<rect width="96" height="96" rx="48" fill="{bg}"/>
<path d="M15 96 Q20 73 48 70 Q76 73 81 96Z" fill="{shirt}"/>
<path d="M40 67 L48 76 L56 67" fill="#fff" opacity=".92"/>
<rect x="40" y="58" width="16" height="16" rx="8" fill="{skin}"/>
<ellipse cx="48" cy="45" rx="21" ry="25" fill="{skin}"/>
{hair_shape}
<ellipse cx="40" cy="48" rx="2" ry="2.4" fill="#241a14"/><ellipse cx="56" cy="48" rx="2" ry="2.4" fill="#241a14"/>
{glasses_svg}
<path d="M43 61 Q48 65 53 61" fill="none" stroke="#8b4c3b" stroke-width="2" stroke-linecap="round"/>
<path d="M31 40 Q37 37 43 40 M53 40 Q59 37 65 40" fill="none" stroke="{hair}" stroke-width="2.1" stroke-linecap="round"/>
</svg>'''

def avatar_data_uri(seed: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(human_avatar_svg(seed).encode()).decode()

def avatar_html(seed: str, diameter: int = 42) -> str:
    return f'<img src="{avatar_data_uri(seed)}" alt="User avatar" style="width:{diameter}px;height:{diameter}px;border-radius:50%;object-fit:cover;vertical-align:middle;"/>'
