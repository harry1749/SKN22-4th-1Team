"""
Utilities for parsing and canonicalizing ingredient names.
"""

from typing import Dict, List


# Legacy rule table (disabled): parser-based normalization now used instead.
# INGREDIENT_FAMILY_MAP = {
#     "NAPROXEN SODIUM": "NAPROXEN",
#     "IBUPROFEN LYSINE": "IBUPROFEN",
#     "IBUPROFEN ARGININE": "IBUPROFEN",
#     ...
# }

PREFIX_MODIFIERS = {
    "ANHYDROUS",
    "MICRONIZED",
    "MICROENCAPSULATED",
    "DRIED",
    "HYDRATED",
    "BUFFERED",
}

SUFFIX_MODIFIERS = {
    "SODIUM",
    "POTASSIUM",
    "CALCIUM",
    "MAGNESIUM",
    "ZINC",
    "HCL",
    "HBR",
    "HYDROCHLORIDE",
    "HYDROBROMIDE",
    "PHOSPHATE",
    "SULFATE",
    "NITRATE",
    "ACETATE",
    "MALEATE",
    "CITRATE",
    "LYSINE",
    "ARGININE",
    "HYDRATE",
    "GRANULES",
    "CONCENTRATE",
    "CHLORIDE",
    "BROMIDE",
    "IODIDE",
    "SUBNITRATE",
}

SUFFIX_PHRASES = {
    "SODIUM SALT",
    "POTASSIUM SALT",
}

# Keep this minimal: only true naming synonyms, not formulation variants.
SYNONYM_MAP = {
    "PARACETAMOL": "ACETAMINOPHEN",
}

SUFFIX_ENDINGS = {
    "CHLORIDE",
    "BROMIDE",
    "IODIDE",
    "NITRATE",
    "NITRITE",
    "SULFATE",
    "PHOSPHATE",
    "ACETATE",
    "CITRATE",
    "MALEATE",
    "MESYLATE",
    "FUMARATE",
    "SUCCINATE",
}


def _is_suffix_modifier(token: str) -> bool:
    if token in SUFFIX_MODIFIERS:
        return True
    return any(token.endswith(ending) for ending in SUFFIX_ENDINGS)


def parse_ingredient_name(name: str) -> Dict[str, object]:
    raw = str(name or "").strip().upper()
    if not raw:
        return {
            "original_name": "",
            "canonical_name": "",
            "base_name": "",
            "prefix_modifiers": [],
            "suffix_modifiers": [],
        }

    tokens = [token for token in raw.split() if token]
    prefix_modifiers: List[str] = []
    suffix_modifiers: List[str] = []

    while tokens and tokens[0] in PREFIX_MODIFIERS:
        prefix_modifiers.append(tokens.pop(0))

    while len(tokens) >= 2 and " ".join(tokens[-2:]) in SUFFIX_PHRASES:
        suffix_modifiers = tokens[-2:] + suffix_modifiers
        tokens = tokens[:-2]

    while tokens and _is_suffix_modifier(tokens[-1]):
        suffix_modifiers.insert(0, tokens.pop())

    base_name = " ".join(tokens).strip()
    if not base_name:
        base_name = raw

    canonical_name = SYNONYM_MAP.get(base_name, base_name)
    return {
        "original_name": raw,
        "canonical_name": canonical_name,
        "base_name": base_name,
        "prefix_modifiers": prefix_modifiers,
        "suffix_modifiers": suffix_modifiers,
    }


def canonicalize_ingredient_name(name: str) -> str:
    return parse_ingredient_name(name)["canonical_name"]


def canonicalize_ingredient_list(values):
    if not isinstance(values, list):
        return []

    result = []
    seen = set()
    for value in values:
        normalized = canonicalize_ingredient_name(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
