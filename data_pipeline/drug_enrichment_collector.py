import argparse
import os
import time
from datetime import datetime
from urllib.parse import unquote

import requests
from dotenv import load_dotenv
from supabase import Client, create_client


class DrugEnrichmentToSupabase:
    def __init__(self):
        load_dotenv()

        raw_key = os.getenv("KR_API_KEY", "")
        self.service_key = unquote(raw_key) if raw_key else ""
        self.base_url = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07"
        self.endpoint = "getDrugPrdtPrmsnDtlInq06"

        self.supabase_url = os.getenv("SUPABASE_URL", "")
        self.supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.supabase_key = self.supabase_service_role_key or os.getenv("SUPABASE_KEY", "")
        self.table_name = "drug_permit_info"

        if not self.service_key:
            raise ValueError("KR_API_KEY is required")
        if not self.supabase_url or not self.supabase_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) are required"
            )

        self.supabase: Client = create_client(self.supabase_url, self.supabase_key)

    @staticmethod
    def _format_date(date_str):
        if not date_str:
            return None
        raw = str(date_str).strip()
        if len(raw) < 8:
            return None
        try:
            return datetime.strptime(raw[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None

    def _map_item(self, item: dict):
        # Requested mapping: etc_otcc_name must come from ETC_OTC_CODE.
        etc_otc_code = (item.get("ETC_OTC_CODE") or "").strip() or None

        row = {
            "item_seq": item.get("ITEM_SEQ"),
            "item_name": item.get("ITEM_NAME"),
            "item_eng_name": item.get("ITEM_ENG_NAME") or "",
            "entp_name": item.get("ENTP_NAME"),
            "etc_otcc_name": item.get("ETC_OTC_CODE"),
            "main_ingr_eng": item.get("MAIN_INGR_ENG"),
            "main_ingr_kor": item.get("MAIN_ITEM_INGR"),
            "source_updated_at": self._format_date(item.get("ITEM_PERMIT_DATE")),
        }
        return row

    def collect_and_upsert(
        self,
        start_page: int = 1,
        max_pages: int = None,
        num_of_rows: int = 100,
        sleep_sec: float = 0.1,
        max_failures_per_page: int = 2,
    ):
        print(f"--- [START] collect -> upsert ({self.table_name}) ---")
        page = start_page
        total_upserted = 0
        total_missing_code = 0
        fail_count = 0
        max_failures_per_page = max(1, int(max_failures_per_page))

        while True:
            if max_pages is not None and page >= start_page + max_pages:
                print(f"- reached max_pages={max_pages}, stop")
                break

            params = {
                "serviceKey": self.service_key,
                "pageNo": page,
                "numOfRows": num_of_rows,
                "type": "json",
            }
            url = f"{self.base_url}/{self.endpoint}"

            try:
                res = requests.get(url, params=params, timeout=30)
                res.raise_for_status()
                payload = res.json()
            except Exception as e:
                print(f"! page={page} request failed: {e}")
                fail_count += 1
                if fail_count >= max_failures_per_page:
                    print(
                        f"! page={page} fail_count={fail_count} reached limit={max_failures_per_page}, stop"
                    )
                    break
                time.sleep(2)
                continue

            body = payload.get("body") or {}
            total_count = body.get("totalCount")
            items = body.get("items") or []
            if isinstance(items, dict):
                items = [items]

            if not items:
                print(f"- page={page}: no items, stop")
                break

            rows = []
            missing_code_on_page = 0
            for item in items:
                row = self._map_item(item)
                if not row.get("item_seq"):
                    continue
                if not row.get("etc_otcc_name"):
                    missing_code_on_page += 1
                rows.append(row)

            if rows:
                try:
                    self.supabase.table(self.table_name).upsert(
                        rows,
                        on_conflict="item_seq",
                    ).execute()
                except Exception as e:
                    print(f"! page={page} upsert failed: {e}")
                    fail_count += 1
                    if fail_count >= max_failures_per_page:
                        print(
                            f"! page={page} fail_count={fail_count} reached limit={max_failures_per_page}, stop"
                        )
                        break
                    time.sleep(2)
                    continue

            fail_count = 0
            total_upserted += len(rows)
            total_missing_code += missing_code_on_page

            progress = "?"
            if isinstance(total_count, int) and total_count > 0:
                progress = f"{min(page * num_of_rows, total_count)}/{total_count}"

            print(
                f"- page={page} upserted={len(rows)} missing_ETC_OTC_CODE={missing_code_on_page} progress={progress}"
            )

            if len(items) < num_of_rows:
                break

            page += 1
            time.sleep(sleep_sec)

        print(
            f"--- [FINISH] total_upserted={total_upserted}, total_missing_ETC_OTC_CODE={total_missing_code} ---"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Collect drug permit data from KR API and upsert directly to Supabase"
    )
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--num-of-rows", type=int, default=100)
    parser.add_argument("--sleep-sec", type=float, default=0.1)
    args = parser.parse_args()

    collector = DrugEnrichmentToSupabase()
    collector.collect_and_upsert(
        start_page=args.start_page,
        max_pages=args.max_pages,
        num_of_rows=args.num_of_rows,
        sleep_sec=args.sleep_sec,
    )


if __name__ == "__main__":
    main()
