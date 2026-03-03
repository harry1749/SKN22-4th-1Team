import argparse
import os

import pandas as pd
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE_NAME = "kcd_info"
DEFAULT_CSV_CANDIDATES = [
    "kcd9_120_1080_code_kor_eng.csv",
    "kcd9_classification_v5.csv",
    "kcd9_classification_v4.csv",
    "kcd9_classification_v3.csv",
]


def resolve_csv_path(explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path if os.path.exists(explicit_path) else None
    for candidate in DEFAULT_CSV_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if "kcd_code" not in df.columns:
        raise ValueError("CSV must include 'kcd_code' column.")

    # Backward compatibility: older files may have only `kcd_name`.
    if "kcd_name_kor" not in df.columns and "kcd_name" in df.columns:
        df = df.rename(columns={"kcd_name": "kcd_name_kor"})

    if "kcd_name_kor" not in df.columns:
        raise ValueError("CSV must include 'kcd_name_kor' column.")

    if "kcd_name_eng" not in df.columns:
        df["kcd_name_eng"] = ""

    return df[["kcd_code", "kcd_name_kor", "kcd_name_eng"]].fillna("")


def sync_kcd9(csv_path: str | None = None, batch_size: int = 1000) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Error: SUPABASE_URL or SUPABASE_KEY is not set in .env")
        return

    resolved_csv_path = resolve_csv_path(csv_path)
    if not resolved_csv_path:
        print(f"Error: KCD CSV file not found. Tried: {DEFAULT_CSV_CANDIDATES}")
        return

    print(f"--- [START] KCD9 upsert ({resolved_csv_path}) ---")
    df = normalize_dataframe(pd.read_csv(resolved_csv_path))
    total_count = len(df)
    print(f"Total records: {total_count}")

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    for i in range(0, total_count, batch_size):
        batch_df = df.iloc[i : i + batch_size]
        rows = batch_df.to_dict(orient="records")
        try:
            supabase.table(TABLE_NAME).upsert(rows, on_conflict="kcd_code").execute()
            print(f"Progress: {min(i + batch_size, total_count)}/{total_count}")
        except Exception as e:
            print(f"Error saving batch {i}~{i + batch_size}: {e}")

    print("--- [FINISH] KCD9 upsert completed ---")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upsert KCD9 CSV into Supabase.")
    parser.add_argument(
        "--csv",
        dest="csv_path",
        default=None,
        help="CSV path to upload (default: auto-detect).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Upsert batch size.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sync_kcd9(csv_path=args.csv_path, batch_size=args.batch_size)
