import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = ROOT / "skn22_4th_prj"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from supabase import create_client

PREFIX_MODS = {
    "ANHYDROUS",
    "MICRONIZED",
    "MICROENCAPSULATED",
    "DRIED",
    "HYDRATED",
    "BUFFERED",
}

SUFFIX_MODS = {
    "SODIUM",
    "POTASSIUM",
    "CALCIUM",
    "MAGNESIUM",
    "ZINC",
    "HCL",
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
}

PAREN_RE = re.compile(r"\([^)]*\)")
UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|%)\b", re.I)
SEP_RE = re.compile(r"[,;/+\n]| and | AND ")
WS_RE = re.compile(r"\s{2,}")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def unset_proxy_env() -> None:
    for key in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ]:
        os.environ.pop(key, None)


def extract_ingredient_tokens(text: str):
    if not text:
        return []

    cleaned = PAREN_RE.sub(" ", str(text))
    cleaned = UNIT_RE.sub(" ", cleaned)
    parts = SEP_RE.split(cleaned)

    tokens = []
    for part in parts:
        token = WS_RE.sub(" ", part).strip(" .:-_").upper()
        if len(token) < 3:
            continue
        if not re.search(r"[A-Z]", token):
            continue
        tokens.append(token)
    return tokens


def get_rows(client, batch_size: int):
    rows = []
    start = 0
    while True:
        response = (
            client.table("unified_drug_info")
            .select("main_ingr_eng")
            .range(start, start + batch_size - 1)
            .execute()
        )
        data = response.data or []
        if not data:
            break
        rows.extend(data)
        if len(data) < batch_size:
            break
        start += batch_size
    return rows


def infer_family_map(tokens, min_count: int):
    freq = Counter(tokens)
    all_tokens = set(freq)

    suggestions = {}
    for token in sorted(all_tokens):
        parts = token.split()
        base = None

        if len(parts) >= 2 and parts[0] in PREFIX_MODS:
            cand = " ".join(parts[1:])
            if cand in all_tokens:
                base = cand

        if base is None and len(parts) >= 2 and parts[-1] in SUFFIX_MODS:
            cand = " ".join(parts[:-1])
            if cand in all_tokens:
                base = cand

        if base and base != token and freq[token] >= min_count:
            suggestions[token] = {
                "canonical": base,
                "count": freq[token],
            }

    return suggestions, freq


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--min-count", type=int, default=1)
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    unset_proxy_env()
    load_env(ROOT / ".env")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL/SUPABASE_KEY is required")

    client = create_client(url, key)
    rows = get_rows(client, args.batch_size)

    tokens = []
    for row in rows:
        tokens.extend(extract_ingredient_tokens((row or {}).get("main_ingr_eng")))

    suggestions, freq = infer_family_map(tokens, args.min_count)

    payload = {
        "rows": len(rows),
        "unique_tokens": len(set(tokens)),
        "suggestions": suggestions,
        "suggested_size": len(suggestions),
    }

    print(f"rows={payload['rows']}")
    print(f"unique_tokens={payload['unique_tokens']}")
    print(f"suggestions={len(suggestions)}")

    for src, meta in sorted(suggestions.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        print(f"{src} => {meta['canonical']} (n={meta['count']})")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved={out_path}")


if __name__ == "__main__":
    main()
