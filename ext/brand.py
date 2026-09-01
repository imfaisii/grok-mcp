"""Brand-identity inspection tool.

`inspect_brand` fetches a live page and its linked stylesheets and reports
measured brand facts about its visual identity: colours, fonts, corner radii,
and effect usage. It does not name palette roles or propose a kit; a caller
model turns these facts into judgement calls.
"""

import asyncio
import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_GENERIC_FONTS = {"sans-serif", "serif", "monospace", "system-ui", "inherit"}
_CSS_BYTE_CAP = 2 * 1024 * 1024

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_CSS_VAR_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;{}]+)[;}]")
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+)[;}]", re.IGNORECASE)
_RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;{}]+)[;}]", re.IGNORECASE)
_COLOR_SCAN_RE = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b"
    r"|(?:rgba?|hsla?|oklch)\([^)]*\)",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))"""
)
# Tailwind v4 compiles its default theme into `@layer theme` and its utility
# set into `@layer utilities`, in full, whether the site uses them or not.
# These patterns identify that generated noise so it can be dropped out of
# the reported facts instead of being counted as the site's own design.
_LAYER_OPEN_RE = re.compile(r"@layer\s+([^{;]+)\{", re.IGNORECASE)
_TW_COLOR_SCALE_RE = re.compile(r"^--color-[a-z]+-\d+$", re.IGNORECASE)
_TW_BARE_COLOR_NAMES = {"--color-white", "--color-black"}
_FRAMEWORK_DECL_RE = re.compile(r"(?:--tw-[\w-]+|--color-[a-z]+-\d+)\s*:\s*[^;{}]*;?", re.IGNORECASE)
_REM_VALUE_RE = re.compile(r"^(-?[0-9]*\.?[0-9]+)rem$", re.IGNORECASE)
_PX_LENGTH_RE = re.compile(r"^(-?[0-9]*\.?[0-9]+(?:e[-+]?[0-9]+)?)px$", re.IGNORECASE)
_VAR_REF_RE = re.compile(r"^var\(\s*(--[\w-]+)\s*\)$", re.IGNORECASE)
_IMPORTANT_RE = re.compile(r"\s*!important\s*$", re.IGNORECASE)
_RULE_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_BG_TOKEN_NAMES = (
    "--background",
    "--background-primary",
    "--bg",
    "--color-background",
    "--background-color",
    "--background-section",
)


def _num(token: str, percent_base: float = 1.0) -> float:
    """Parse a CSS numeric token, resolving a trailing % or deg suffix."""
    token = token.strip()
    if token.endswith("%"):
        return float(token[:-1]) / 100 * percent_base
    if token.endswith("deg"):
        return float(token[:-3])
    return float(token)


def _clamp255(v: int) -> int:
    return max(0, min(255, v))


def _to_hex(r: int, g: int, b: int) -> str:
    return f"{r:02X}{g:02X}{b:02X}"


def _hex_to_rgb(hexval: str) -> Tuple[int, int, int]:
    return int(hexval[0:2], 16), int(hexval[2:4], 16), int(hexval[4:6], 16)


def _composite(r: int, g: int, b: int, a: float, bg: Tuple[int, int, int]) -> str:
    """Flatten a translucent colour over an opaque background colour."""
    br, bgg, bb = bg
    return _to_hex(
        _clamp255(round(a * r + (1 - a) * br)),
        _clamp255(round(a * g + (1 - a) * bgg)),
        _clamp255(round(a * b + (1 - a) * bb)),
    )


def _split_components(inner: str) -> List[str]:
    return [p for p in re.split(r"[,\s/]+", inner.strip()) if p]


def _parse_hex(raw: str) -> Optional[Tuple[int, int, int, float]]:
    digits = raw[1:]
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    if len(digits) not in (6, 8):
        return None
    try:
        r, g, b = int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
        a = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
    except ValueError:
        return None
    return r, g, b, a


def _hsl_to_rgb(h: float, s: float, l: float) -> Tuple[int, int, int]:
    """Standard CSS Color 4 hsl-to-rgb formula (handles s == 0 with no special case)."""
    def channel(n: float) -> int:
        k = (n + h / 30) % 12
        a = s * min(l, 1 - l)
        return _clamp255(round((l - a * max(-1, min(k - 3, 9 - k, 1))) * 255))

    return channel(0), channel(8), channel(4)


def _linear_to_srgb(c: float) -> int:
    value = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return _clamp255(round(value * 255))


def _oklch_to_rgb(L: float, C: float, H: float) -> Tuple[int, int, int]:
    """OKLCH to sRGB: OKLCH -> OKLab -> LMS cubed -> linear sRGB -> sRGB transfer."""
    rad = math.radians(H)
    a, b = C * math.cos(rad), C * math.sin(rad)

    l = L + 0.3963377774 * a + 0.2158037573 * b
    m = L - 0.1055613458 * a - 0.0638541728 * b
    s = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l ** 3, m ** 3, s ** 3

    r_lin = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g_lin = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b_lin = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return _linear_to_srgb(r_lin), _linear_to_srgb(g_lin), _linear_to_srgb(b_lin)


def _parse_color(raw: str) -> Optional[Tuple[int, int, int, float]]:
    """Parse a single colour literal (hex, rgb/rgba, hsl/hsla, oklch) to RGBA."""
    raw = raw.strip()
    if raw.startswith("#"):
        return _parse_hex(raw)
    match = re.match(r"(rgba?|hsla?|oklch)\(([^)]*)\)", raw, re.IGNORECASE)
    if not match:
        return None
    kind = match.group(1).lower().rstrip("a")
    parts = _split_components(match.group(2))
    if len(parts) < 3:
        return None
    try:
        alpha = _num(parts[3]) if len(parts) >= 4 else 1.0
        if kind == "rgb":
            r, g, b = (round(_num(p, 255)) for p in parts[:3])
        elif kind == "hsl":
            r, g, b = _hsl_to_rgb(_num(parts[0]), _num(parts[1]), _num(parts[2]))
        else:
            r, g, b = _oklch_to_rgb(_num(parts[0]), float(parts[1]), _num(parts[2]))
    except ValueError:
        return None
    return _clamp255(r), _clamp255(g), _clamp255(b), max(0.0, min(1.0, alpha))


def _parse_attrs(tag: str) -> dict:
    """Parse an HTML tag's attributes into a name -> value dict, order independent."""
    attrs = {}
    for m in _ATTR_RE.finditer(tag):
        name = m.group(1).lower()
        value = next((g for g in m.groups()[1:] if g is not None), "")
        attrs[name] = value
    return attrs


def _extract_stylesheets_and_inline(html: str, base_url: str) -> Tuple[List[str], List[str]]:
    """Find linked stylesheet URLs (resolved against `base_url`) and inline <style> text."""
    urls = []
    seen = set()
    for tag_match in _LINK_TAG_RE.finditer(html):
        attrs = _parse_attrs(tag_match.group(0))
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href")
        if "stylesheet" in rel and href:
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)
    return urls, _STYLE_BLOCK_RE.findall(html)


def _rank_by_size(client: httpx.Client, urls: List[str]) -> List[str]:
    """Order stylesheet URLs largest-first using a HEAD Content-Length probe."""
    # ponytail: compressed responses often omit Content-Length; those sort last.
    sized = []
    for u in urls:
        size = 0
        try:
            size = int(client.head(u).headers.get("content-length", 0) or 0)
        except httpx.HTTPError:
            pass
        sized.append((size, u))
    sized.sort(key=lambda pair: pair[0], reverse=True)
    return [u for _, u in sized]


def _is_framework_var(name: str) -> bool:
    """True for a Tailwind-generated custom property.

    Covers a --tw-* tracking var, a --color-<name>-<number> palette scale
    entry, and the bare --color-white / --color-black. Anything else is
    treated as an author token.
    """
    if name.startswith("--tw-"):
        return True
    if name in _TW_BARE_COLOR_NAMES:
        return True
    return bool(_TW_COLOR_SCALE_RE.match(name))


def _harvest_variables(css: str) -> Dict[str, str]:
    """All `--name: value` declarations anywhere in the CSS, last declared wins per name.

    Runs before any layer stripping, so an author token survives no matter
    which layer it lives in, including a compiled `@theme inline { ... }`
    block that would otherwise be stripped along with the rest of the theme.

    A later declaration only overwrites an earlier one when it is at least
    as readable to our colour parser: a Lightning CSS style build commonly
    emits the same custom property twice, a plain hex colour first and a
    lab() form of the same colour second as the precise value for browsers
    that support it. We do not parse lab() yet, so blindly taking the last
    write would drop the token instead of degrading to the hex value a
    non-supporting browser would itself fall back to.
    """
    variables = {}
    for m in _CSS_VAR_RE.finditer(css):
        name, value = m.group(1), m.group(2).strip()
        if name in variables and _COLOR_SCAN_RE.fullmatch(variables[name]) and not _COLOR_SCAN_RE.fullmatch(value):
            continue
        variables[name] = value
    return variables


def _find_matching_brace(css: str, open_index: int) -> int:
    """Index of the '}' matching the '{' at open_index, a string-aware brace counter."""
    depth = 0
    in_string = None
    i = open_index
    n = len(css)
    while i < n:
        ch = css[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    # ponytail: unterminated block, only reachable on truncated or malformed CSS.
    return n - 1


def _strip_layers(css: str) -> Tuple[str, int]:
    """Remove top-level `@layer theme { ... }` and `@layer utilities { ... }` blocks.

    These are the two layers Tailwind v4 compiles its full default theme and
    full utility set into, regardless of what the site actually uses. Author
    CSS in `@layer base` or outside any layer is left untouched. A regex
    alone cannot balance nested braces, hence the small scanner. A CSS file
    with no such layers passes through unchanged.
    """
    strip_names = {"theme", "utilities"}
    out = []
    removed = 0
    pos = 0
    for m in _LAYER_OPEN_RE.finditer(css):
        if m.start() < pos:
            continue  # inside a block already resolved above
        layer_names = {n.strip().lower() for n in m.group(1).split(",")}
        end = _find_matching_brace(css, m.end() - 1)
        if layer_names & strip_names:
            out.append(css[pos:m.start()])
            removed += (end + 1) - m.start()
            pos = end + 1
    out.append(css[pos:])
    return "".join(out), removed


def _scan_colors(css: str) -> Tuple[Counter, List[Tuple[int, int, int, float]]]:
    """Scan CSS for colour literals, split into opaque hex counts and translucent RGBA hits."""
    opaque_counts = Counter()
    translucent = []
    for m in _COLOR_SCAN_RE.finditer(css):
        color = _parse_color(m.group(0))
        if not color:
            continue
        r, g, b, a = color
        if a >= 1.0:
            opaque_counts[_to_hex(r, g, b)] += 1
        else:
            translucent.append((r, g, b, a))
    return opaque_counts, translucent


def _find_body_background(css: str) -> Optional[Tuple[int, int, int]]:
    """First opaque colour from a `body` selector's background or background-color declaration."""
    # ponytail: flat rule scan, does not look inside @media blocks, add nested-brace
    # awareness if a body background only ever shows up behind a prefers-color-scheme query.
    for m in _RULE_BLOCK_RE.finditer(css):
        selectors = [s.strip() for s in m.group(1).split(",")]
        if not any(s == "body" or s.startswith(("body.", "body:", "body[", "body#")) for s in selectors):
            continue
        decl_match = re.search(r"background(?:-color)?\s*:\s*([^;]+)", m.group(2), re.IGNORECASE)
        if not decl_match:
            continue
        token = _COLOR_SCAN_RE.search(decl_match.group(1))
        color = _parse_color(token.group(0)) if token else None
        if color and color[3] >= 1.0:
            return color[0], color[1], color[2]
    return None


def _resolve_canvas(
    stripped_css: str,
    author_vars: Dict[str, str],
    opaque_counts: Counter,
    notes: List[str],
) -> Tuple[Tuple[int, int, int], str]:
    """Resolve the page's real background colour: an author token, then body{}, then frequency.

    This is the compositing base used everywhere a translucent colour needs
    flattening, so getting it right is what keeps a dark site's near-black
    surfaces from being reported as white.
    """
    for name in _BG_TOKEN_NAMES:
        raw = author_vars.get(name)
        if raw is None:
            continue
        color = _parse_color(raw)
        if color and color[3] >= 1.0:
            r, g, b, _a = color
            notes.append(f"background canvas resolved from the {name} CSS variable")
            return (r, g, b), _to_hex(r, g, b)

    body_rgb = _find_body_background(stripped_css)
    if body_rgb:
        notes.append("background canvas resolved from a body selector's background declaration")
        return body_rgb, _to_hex(*body_rgb)

    if opaque_counts:
        dominant_hex = opaque_counts.most_common(1)[0][0]
        notes.append(f"background canvas not found in a background variable or body selector, falling back to the most frequent opaque colour #{dominant_hex}")
        return _hex_to_rgb(dominant_hex), dominant_hex

    notes.append("no opaque colour found on the page; alpha colours composited over white as a fallback")
    return (255, 255, 255), "FFFFFF"


def _build_hex_frequency(
    opaque_counts: Counter,
    translucent: List[Tuple[int, int, int, float]],
    dominant_rgb: Tuple[int, int, int],
    dominant_hex: str,
    notes: List[str],
) -> List[dict]:
    """Composite translucent colours over the resolved canvas, rank everything by frequency."""
    hex_counts = opaque_counts.copy()
    if translucent:
        for r, g, b, a in translucent:
            hex_counts[_composite(r, g, b, a, dominant_rgb)] += 1
        notes.append(f"colours with alpha under 1 were composited over the page background #{dominant_hex}")
    return [{"hex": f"#{h}", "count": c} for h, c in hex_counts.most_common(20)]


def _resolve_color_variables(author_vars: Dict[str, str], dominant_rgb: Tuple[int, int, int]) -> Dict[str, str]:
    """Author custom properties whose value is a single colour literal, normalised to hex."""
    variables = {}
    for name, value in author_vars.items():
        if not _COLOR_SCAN_RE.fullmatch(value):
            continue
        color = _parse_color(value)
        if not color:
            continue
        r, g, b, a = color
        hexval = _composite(r, g, b, a, dominant_rgb) if a < 1.0 else _to_hex(r, g, b)
        variables[name] = f"#{hexval}"
    return variables


def _balanced_parens(value: str) -> bool:
    return value.count("(") == value.count(")")


def _resolve_font_var(name: str, author_vars: Dict[str, str]) -> Optional[str]:
    """First concrete family from an author variable's value, or None if it is itself a var()."""
    raw = author_vars.get(name)
    if raw is None:
        return None
    first = _IMPORTANT_RE.sub("", raw).split(",")[0].strip().strip("'\"")
    return None if not first or first.startswith("var(") else first


def _extract_fonts(all_css: str, author_vars: Dict[str, str]) -> List[str]:
    """Distinct font-family first choices in document order, generics only as a last resort.

    A trailing `!important` is stripped before a family is recorded, and an
    entry left with unbalanced parentheses, such as a fallback list inside a
    var() call that a plain comma split tore open, is discarded rather than
    kept broken. A bare var(--name) reference resolves against the harvested
    author variables when that variable holds a concrete family name;
    otherwise it is kept intact as the var(--name) token, since that still
    tells the caller which token drives type.
    """
    fonts = []
    generics = []
    for m in _FONT_FAMILY_RE.finditer(all_css):
        value = _IMPORTANT_RE.sub("", m.group(1))
        first = value.split(",")[0].strip()
        name = first if first.startswith("var(") else first.strip("'\"")
        if not name or not _balanced_parens(name):
            continue
        ref = _VAR_REF_RE.match(name)
        if ref:
            name = _resolve_font_var(ref.group(1), author_vars) or name
        if name.lower() in _GENERIC_FONTS:
            if name not in generics:
                generics.append(name)
            continue
        if name not in fonts:
            fonts.append(name)
    return (fonts or generics)[:10]


def _radius_px(value: str) -> str:
    """Normalise a single-token radius value to px; rem converts at 16px per rem."""
    match = _REM_VALUE_RE.match(value.strip())
    if not match:
        return value.strip()
    px = float(match.group(1)) * 16
    return f"{px:g}px"


_RADIUS_PILL_PX = 200


def _radius_length_px(value: str) -> Optional[float]:
    """Read a normalised radius token as a finite px number, or None if it isn't a plain length."""
    if value == "0":
        return 0.0
    match = _PX_LENGTH_RE.match(value)
    if not match:
        return None
    px = float(match.group(1))
    return px if math.isfinite(px) else None


def _normalize_radius_value(value: str, author_vars: Dict[str, str]) -> Optional[str]:
    """Resolve one border-radius value to a normalised px token, or None to drop it.

    A bare var(--name) reference resolves against the harvested author
    variables when that variable holds a length; an unresolved reference is
    dropped. A calc(...) expression is never evaluated, so a value using one
    is dropped too. Everything else is normalised to px, rem converting at
    16px per rem. A value at or above 200px, including a compiled
    float-overflow placeholder for a pill shape, folds into a single
    9999px entry instead of standing on its own.
    """
    value = value.strip()
    ref = _VAR_REF_RE.match(value)
    if ref:
        resolved = author_vars.get(ref.group(1))
        if resolved is None:
            return None
        value = resolved.strip()

    if "calc(" in value.lower():
        return None

    value = _radius_px(value)
    px = _radius_length_px(value)
    if px is None:
        return None
    return "9999px" if px >= _RADIUS_PILL_PX else value


def _extract_radii(all_css: str, author_vars: Dict[str, str]) -> List[dict]:
    """Distinct border-radius values with counts, sorted descending, top 10.

    `border-radius:` declarations are counted directly, resolving a bare
    var() reference against the harvested author variables. Author
    `--radius` and `--radius-*` custom properties are folded into the same
    count table so a radius token that is only ever referenced via var()
    still shows up here. Every surviving value is normalised to px (rem
    converts at 16px per rem); a calc() expression is dropped rather than
    evaluated, and anything at or above 200px, including a compiled
    float-overflow pill placeholder, folds into a single 9999px entry
    instead of being listed on its own.
    """
    counts = Counter()
    for m in _RADIUS_RE.finditer(all_css):
        value = " ".join(m.group(1).split())
        normalized = _normalize_radius_value(value, author_vars) if value else None
        if normalized:
            counts[normalized] += 1
    for name, value in author_vars.items():
        if name == "--radius" or name.startswith("--radius-"):
            normalized = _normalize_radius_value(value, author_vars)
            if normalized:
                counts[normalized] += 1
    return [{"value": v, "count": c} for v, c in counts.most_common(10)]


def _extract_effects(all_css: str) -> Dict[str, int]:
    """Plain occurrence counts for the effect signals that separate flat from glassy design."""
    return {
        "linear_gradient": len(re.findall(r"linear-gradient\(", all_css, re.IGNORECASE)),
        "radial_gradient": len(re.findall(r"radial-gradient\(", all_css, re.IGNORECASE)),
        "box_shadow": len(re.findall(r"box-shadow\s*:", all_css, re.IGNORECASE)),
        "backdrop_filter": len(re.findall(r"backdrop-filter\s*:", all_css, re.IGNORECASE)),
        "text_shadow": len(re.findall(r"text-shadow\s*:", all_css, re.IGNORECASE)),
    }


def _inspect_brand_sync(url: str, max_stylesheets: int = 6) -> dict:
    """Fetch a live page and report measured facts about its visual identity.

    Pulls the page HTML, follows its linked stylesheets (plus inline <style>
    blocks), and reports the colours, fonts, corner radii, and effect counts
    found in the CSS. Framework CSS is filtered out before analysis: a
    compiled Tailwind v4 `@layer theme` (the full default palette) and
    `@layer utilities` (the full utility set) are stripped before colours
    and effects are measured, and `--tw-*` / `--color-<name>-<n>` scale
    variables never appear in css_variables. The background used to
    composite translucent colours is resolved from an author background
    token or a `body` rule where possible, instead of being guessed from
    raw colour frequency. Reports raw facts only, no interpretation: a
    caller model decides what the facts mean, names palette roles, or
    writes a kit.

    Args:
        url: Page to inspect. Must be http or https.
        max_stylesheets: Max stylesheets to download; the largest ones (by
            Content-Length) are preferred when more are linked than this.

    Returns:
        A dict with "url" (final URL), "fetched" ({html_bytes, stylesheets,
        css_bytes}), "css_variables" ({name: "#RRGGBB"}, author tokens
        only), "hex_frequency" (top 20 {hex, count}), "fonts" (top 10,
        document order), "radii" (top 10 {value, count}), "effects" (counts
        of linear_gradient/radial_gradient/box_shadow/backdrop_filter/
        text_shadow), and "notes" (one line per thing the caller should
        know).
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parsed.scheme!r}: only http and https are allowed")

    notes = []
    with httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
        response = client.get(url)
        response.raise_for_status()
        html = response.text
        final_url = str(response.url)
        html_bytes = len(response.content)

        stylesheet_urls, inline_css = _extract_stylesheets_and_inline(html, final_url)
        if len(stylesheet_urls) > max_stylesheets:
            stylesheet_urls = _rank_by_size(client, stylesheet_urls)[:max_stylesheets]

        css_parts = list(inline_css)
        css_bytes = sum(len(c.encode("utf-8", "ignore")) for c in inline_css)
        fetched_stylesheets = []
        truncated = False

        for sheet_url in stylesheet_urls:
            if css_bytes >= _CSS_BYTE_CAP:
                truncated = True
                break
            try:
                sheet_response = client.get(sheet_url)
                sheet_response.raise_for_status()
            except httpx.HTTPError as exc:
                notes.append(f"stylesheet failed to fetch: {sheet_url} ({type(exc).__name__})")
                continue
            text = sheet_response.text
            encoded = text.encode("utf-8", "ignore")
            remaining = _CSS_BYTE_CAP - css_bytes
            if len(encoded) > remaining:
                text = encoded[:remaining].decode("utf-8", "ignore")
                truncated = True
            css_bytes += len(text.encode("utf-8", "ignore"))
            css_parts.append(text)
            fetched_stylesheets.append(sheet_url)

    if truncated:
        notes.append("total CSS truncated at the ~2MB cap")

    all_css = "\n".join(css_parts)
    if len(all_css.strip()) < 200:
        notes.append("very little CSS found; page may require JavaScript to render its styles")

    original_host, final_host = urlsplit(url).netloc, urlsplit(final_url).netloc
    if original_host and final_host and original_host != final_host:
        notes.append(f"redirected to a different host: {original_host} -> {final_host}")

    raw_vars = _harvest_variables(all_css)
    author_vars = {}
    framework_var_count = 0
    for name, value in raw_vars.items():
        if _is_framework_var(name):
            framework_var_count += 1
        else:
            author_vars[name] = value

    stripped_css, bytes_removed = _strip_layers(all_css)
    color_scan_css = _FRAMEWORK_DECL_RE.sub("", stripped_css)
    notes.append(f"dropped {framework_var_count} framework css variable(s) and stripped {bytes_removed} byte(s) of @layer theme/utilities before colour and effect analysis")

    opaque_counts, translucent = _scan_colors(color_scan_css)
    dominant_rgb, dominant_hex = _resolve_canvas(stripped_css, author_vars, opaque_counts, notes)
    hex_frequency = _build_hex_frequency(opaque_counts, translucent, dominant_rgb, dominant_hex, notes)
    if len(hex_frequency) < 3:
        notes.append(
            "fewer than 3 distinct hex colours were found; this site declares its colour through "
            "css variables rather than literal hex values, so css_variables is the authoritative palette here"
        )

    return {
        "url": final_url,
        "fetched": {
            "html_bytes": html_bytes,
            "stylesheets": fetched_stylesheets,
            "css_bytes": css_bytes,
        },
        "css_variables": _resolve_color_variables(author_vars, dominant_rgb),
        "hex_frequency": hex_frequency,
        "fonts": _extract_fonts(all_css, author_vars),
        "radii": _extract_radii(all_css, author_vars),
        "effects": _extract_effects(stripped_css),
        "notes": notes,
    }


async def inspect_brand(url: str, max_stylesheets: int = 6) -> dict:
    """Fetch a live page and report measured facts about its visual identity.

    Pulls the page HTML, follows its linked stylesheets (plus inline <style>
    blocks), and reports the colours, fonts, corner radii, and effect counts
    found in the CSS. Framework CSS is filtered out before analysis: a
    compiled Tailwind v4 `@layer theme` (the full default palette) and
    `@layer utilities` (the full utility set) are stripped before colours
    and effects are measured, and `--tw-*` / `--color-<name>-<n>` scale
    variables never appear in css_variables. The background used to
    composite translucent colours is resolved from an author background
    token or a `body` rule where possible, instead of being guessed from
    raw colour frequency. Reports raw facts only, no interpretation: a
    caller model decides what the facts mean, names palette roles, or
    writes a kit.

    Args:
        url: Page to inspect. Must be http or https.
        max_stylesheets: Max stylesheets to download; the largest ones (by
            Content-Length) are preferred when more are linked than this.

    Returns:
        A dict with "url" (final URL), "fetched" ({html_bytes, stylesheets,
        css_bytes}), "css_variables" ({name: "#RRGGBB"}, author tokens
        only), "hex_frequency" (top 20 {hex, count}), "fonts" (top 10,
        document order), "radii" (top 10 {value, count}), "effects" (counts
        of linear_gradient/radial_gradient/box_shadow/backdrop_filter/
        text_shadow), and "notes" (one line per thing the caller should
        know).
    """
    return await asyncio.to_thread(_inspect_brand_sync, url, max_stylesheets)


def register(mcp):
    mcp.tool()(inspect_brand)
