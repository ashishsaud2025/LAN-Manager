"""Design tokens parsed from the approved Observatory-

Only DESIGN.md values live here so visual tweaks stay centralized."""

from __future__ import annotations

from pathlib import Path

DESIGN_PATH = (Path(__file__).resolve().parent.parent
               / "design" / "lan_atlas_observatory" / "DESIGN.md")
REM_PX = 16


def _parse_frontmatter(path: Path) -> dict[str, object]:
    """Parse the simple nested mapping in DESIGN.md frontmatter."""
    text = path.read_text(encoding="utf-8")
    first = text.index("---")
    second = text.index("---", first + 3)
    body = text[first + 3:second]
    root: dict[str, object] = {}
    section: dict[str, object] | None = None
    group: dict[str, object] | None = None
    for raw in body.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, value = raw.strip().partition(":")
        key, value = key.strip(), value.strip()
        if indent == 0:
            section = {}
            root[key] = section
            group = None
        elif indent == 2:
            group = None
            assert section is not None
            if value:
                section[key] = _unquote(value)
            else:
                group = {}
                section[key] = group
        elif indent == 4:
            assert group is not None
            group[key] = _unquote(value)
    return root


def _unquote(value: str) -> str:
    """Strip one layer of surrounding quotes from a scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _to_px(value: str) -> int:
    """Convert a rem or px token to integer pixels at 16px base."""
    text = value.strip()
    if text.endswith("rem"):
        return round(float(text[:-3]) * REM_PX)
    if text.endswith("px"):
        return round(float(text[:-2]))
    return int(float(text))


_FRONTMATTER = _parse_frontmatter(DESIGN_PATH)

_COLORS_RAW = _FRONTMATTER["colors"]
assert isinstance(_COLORS_RAW, dict)
COLORS: dict[str, str] = {str(key): str(value)
                          for key, value in _COLORS_RAW.items()}

_SPACING_RAW = _FRONTMATTER["spacing"]
assert isinstance(_SPACING_RAW, dict)
_SPACE_ALIAS = {"xs": "space-xs", "sm": "space-sm", "md": "space-md",
                "lg": "space-lg", "xl": "space-xl"}
SPACE: dict[str, int] = {alias: _to_px(str(_SPACING_RAW[token]))
                         for alias, token in _SPACE_ALIAS.items()}
GUTTER_PX = _to_px(str(_SPACING_RAW["gutter"]))
MARGIN_PX = _to_px(str(_SPACING_RAW["margin"]))

_ROUNDED_RAW = _FRONTMATTER["rounded"]
assert isinstance(_ROUNDED_RAW, dict)
RADIUS: dict[str, int] = {str(key): _to_px(str(value))
                          for key, value in _ROUNDED_RAW.items()}
BUTTON_RADIUS_PX = 6

_TYPE_RAW = _FRONTMATTER["typography"]
assert isinstance(_TYPE_RAW, dict)


def _style(name: str) -> dict[str, object]:
    """Return one typography style with numeric size and weight."""
    raw = _TYPE_RAW[name]
    assert isinstance(raw, dict)
    size = raw["fontSize"]
    assert isinstance(size, str)
    weight = raw["weight"] if "weight" in raw else raw["fontWeight"]
    line = raw.get("lineHeight", "")
    return {"family": str(raw["fontFamily"]), "size": _to_px(str(size)),
            "weight": int(str(weight)), "line_height": _to_px(str(line))
            if str(line).strip() else 0,
            "letter_spacing": str(raw.get("letterSpacing", ""))}


FONTS: dict[str, object] = {
    "ui": "Inter",
    "mono": "JetBrains Mono",
    "styles": {name: _style(name) for name in (
        "headline-xl", "headline-lg", "headline-md", "title-sm", "body-md",
        "body-sm", "label-code-lg", "label-code-md", "label-code-sm",
        "section-eyebrow")},
}
