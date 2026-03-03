import argparse
import os
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
from supabase import Client, create_client


class UnifiedLoaderToSupabase:
    def __init__(self):
        load_dotenv()

        self.supabase_url = os.getenv("SUPABASE_URL", "")
        self.supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.supabase_key = self.supabase_service_role_key or os.getenv("SUPABASE_KEY", "")

        self.eyak_table = "eyak_info"
        self.permit_table = "drug_permit_info"
        self.target_table = "unified_drug_info"

        if not self.supabase_url or not self.supabase_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) are required"
            )

        self.supabase: Client = create_client(self.supabase_url, self.supabase_key)

    def _fetch_all_rows(self, table_name: str, columns: str, page_size: int) -> List[dict]:
        rows: List[dict] = []
        offset = 0

        while True:
            response = (
                self.supabase.table(table_name)
                .select(columns)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            batch = response.data or []
            if not batch:
                break

            rows.extend(batch)
            print(
                f"- fetched {table_name}: {len(rows)} rows"
            )

            if len(batch) < page_size:
                break
            offset += page_size

        return rows

    @staticmethod
    def _build_permit_index(permit_rows: List[dict]) -> Dict[str, dict]:
        indexed: Dict[str, dict] = {}
        for permit in permit_rows:
            key = (permit.get("item_name") or "").strip()
            if not key:
                continue
            # Keep the first match, same behavior as Django .first() in previous logic.
            if key not in indexed:
                indexed[key] = permit
        return indexed

    @staticmethod
    def _to_unified_row(eyak: dict, permit_info: Optional[dict]) -> Optional[dict]:
        item_seq = eyak.get("item_seq")
        if not item_seq:
            return None

        row = {
            "item_seq": item_seq,
            "item_name": eyak.get("item_name"),
            "entp_name": eyak.get("entp_name"),
            "efficacy": eyak.get("efficacy"),
            "use_method": eyak.get("use_method"),
            "precautions": eyak.get("precautions"),
            "interaction": eyak.get("interaction"),
            "side_effects": eyak.get("side_effects"),
            "item_image": eyak.get("item_image"),
            "etc_otcc_name": None,
            "main_ingr_eng": None,
            "main_ingr_kor": None,
            "source_updated_at": None,
        }

        if permit_info:
            row["etc_otcc_name"] = permit_info.get("etc_otcc_name")
            row["main_ingr_eng"] = permit_info.get("main_ingr_eng")
            row["main_ingr_kor"] = permit_info.get("main_ingr_kor")
            row["source_updated_at"] = permit_info.get("source_updated_at")

        return row

    def process_unification(
        self,
        source_page_size: int = 1000,
        upsert_batch_size: int = 500,
        sleep_sec: float = 0.0,
        max_failures_per_batch: int = 2,
        limit: Optional[int] = None,
    ):
        print(f"--- [START] build + upsert ({self.target_table}) ---")

        eyak_columns = (
            "item_seq,item_name,entp_name,efficacy,use_method,precautions,"
            "interaction,side_effects,item_image"
        )
        permit_columns = "item_name,etc_otcc_name,main_ingr_eng,main_ingr_kor,source_updated_at"

        eyak_rows = self._fetch_all_rows(self.eyak_table, eyak_columns, source_page_size)
        permit_rows = self._fetch_all_rows(
            self.permit_table, permit_columns, source_page_size
        )
        permit_index = self._build_permit_index(permit_rows)

        if limit is not None and limit > 0:
            eyak_rows = eyak_rows[:limit]

        unified_rows: List[dict] = []
        skipped = 0
        for eyak in eyak_rows:
            key = (eyak.get("item_name") or "").strip()
            permit_info = permit_index.get(key)
            row = self._to_unified_row(eyak, permit_info)
            if row is None:
                skipped += 1
                continue
            unified_rows.append(row)

        print(
            f"- prepared rows={len(unified_rows)} (source={len(eyak_rows)}, skipped={skipped})"
        )

        if not unified_rows:
            print("- no rows to upsert, stop")
            return

        max_failures_per_batch = max(1, int(max_failures_per_batch))
        total_upserted = 0

        for i in range(0, len(unified_rows), upsert_batch_size):
            batch = unified_rows[i : i + upsert_batch_size]
            fail_count = 0

            while True:
                try:
                    self.supabase.table(self.target_table).upsert(
                        batch,
                        on_conflict="item_seq",
                    ).execute()
                    total_upserted += len(batch)
                    print(
                        f"- upserted {min(i + len(batch), len(unified_rows))}/{len(unified_rows)}"
                    )
                    break
                except Exception as e:
                    fail_count += 1
                    print(f"! batch upsert failed: {e}")
                    if fail_count >= max_failures_per_batch:
                        print(
                            f"! fail_count={fail_count} reached limit={max_failures_per_batch}, stop"
                        )
                        print(
                            f"--- [FINISH] total_upserted={total_upserted}, prepared={len(unified_rows)} ---"
                        )
                        return
                    time.sleep(2)

            if sleep_sec > 0:
                time.sleep(sleep_sec)

        print(
            f"--- [FINISH] total_upserted={total_upserted}, prepared={len(unified_rows)} ---"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Build unified drug rows from Supabase source tables and upsert to unified_drug_info"
    )
    parser.add_argument("--source-page-size", type=int, default=1000)
    parser.add_argument("--upsert-batch-size", type=int, default=500)
    parser.add_argument("--sleep-sec", type=float, default=0.0)
    parser.add_argument("--max-failures-per-batch", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    loader = UnifiedLoaderToSupabase()
    loader.process_unification(
        source_page_size=args.source_page_size,
        upsert_batch_size=args.upsert_batch_size,
        sleep_sec=args.sleep_sec,
        max_failures_per_batch=args.max_failures_per_batch,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
