import os
import re
import asyncio
import logging
from supabase import create_client, Client
from services.ai_service_v2 import AIService

logger = logging.getLogger(__name__)


class SupabaseService:
    _client = None

    @classmethod
    def get_client(cls) -> Client:
        if cls._client:
            return cls._client

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")

        if not url or not key:
            logger.error("SUPABASE_URL and SUPABASE_KEY must be set in .env")
            return None

        cls._client = create_client(url, key)
        return cls._client

    @classmethod
    async def get_dur_by_ingr(cls, ingr_text: str):
        if not ingr_text:
            return []
        ingr_list = [
            i.strip()
            for i in ingr_text.replace(",", "/").split("/")
            if len(i.strip()) > 1
        ]
        dur_data = await cls._get_dur_data_from_supabase(ingr_list)
        results = []
        for d in dur_data:
            results.append(
                {
                    "type": d["dur_type"],
                    "ingr_name": d["ingr_kor_name"],
                    "warning_msg": d["prohbt_content"] or d["remark"],
                    "severity": d["critical_value"],
                }
            )
        return results

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))
        enriched_data = []
        from services.drug_service import DrugService as OriginalDrugService

        for ingr in unique_ingrs:
            durs = await cls._get_kr_durs_supabase(ingr)
            fda_warn = await OriginalDrugService.get_fda_warnings_by_ingr(ingr)
            if fda_warn:
                summary = await AIService.summarize_fda_warning(fda_warn)
                if summary:
                    fda_warn = summary
            enriched_data.append(
                {"ingredient": ingr, "kr_durs": durs, "fda_warning": fda_warn}
            )
        return enriched_data

    @classmethod
    async def _get_kr_durs_supabase(cls, ingr_name):
        if not ingr_name:
            return []
        target_name = ingr_name.strip()
        if not target_name:
            return []
        client = cls.get_client()
        if not client:
            return []
        dur_list = []
        try:
            is_korean = bool(re.search("[가-힣]", target_name))
            if is_korean:
                response = (
                    client.table("dur_master")
                    .select("*")
                    .ilike("ingr_kor_name", f"%{target_name}%")
                    .execute()
                )
            else:
                response = (
                    client.table("dur_master")
                    .select("*")
                    .ilike("ingr_eng_name", f"%{target_name.lower()}%")
                    .execute()
                )
            dur_list = response.data
        except Exception as e:
            logger.error(f"[Supabase] DUR query error for '{target_name}': {e}")
            return []

        DUR_TYPE_KOR_MAP = {
            "PREGNANCY": "임부 금기/주의",
            "COMBINED": "병용 금기",
            "AGE_SPECIFIC": "연령 금기",
            "ELDERLY": "노인 주의",
            "MAX_CAPACITY": "용량 주의",
            "MAX_DURATION": "투여 기간 주의",
            "EFFICACY_DUPLICATE": "효능 중복 주의",
            "DOSAGE_DUPLICATE": "용법 주의",
            "ADMINISTRATION_DUPLICATE": "투여 경로 주의",
            "LACTATION": "수유부 주의",
            "WEIGHT": "체중 주의",
            "KIDNEY": "신장 질환 주의",
            "LIVER": "간 질환 주의",
            "G6PD": "특정 효소 결핍 주의",
            "PEDIATRIC": "소아 주의",
        }

        grouped_results = {}
        for d in dur_list:
            kor_type = DUR_TYPE_KOR_MAP.get(d["dur_type"], d["dur_type"])
            content = (d["prohbt_content"] or d["remark"] or "").strip()
            if not content:
                continue
            if kor_type not in grouped_results:
                grouped_results[kor_type] = {
                    "type": kor_type,
                    "kor_name": d["ingr_kor_name"],
                    "warnings": set(),
                }
            grouped_results[kor_type]["warnings"].add(content)

        results = []
        for val in grouped_results.values():
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append(
                {
                    "type": val["type"],
                    "kor_name": val["kor_name"],
                    "warning": combined_warning,
                }
            )
        return results

    @classmethod
    async def _get_dur_data_from_supabase(cls, ingr_list: list):
        client = cls.get_client()
        if not client:
            return []
        all_results = []
        for ingr in ingr_list:
            if not ingr:
                continue
            target = ingr.strip()
            try:
                if bool(re.search("[가-힣]", target)):
                    response = (
                        client.table("dur_master")
                        .select("*")
                        .ilike("ingr_kor_name", f"%{target}%")
                        .execute()
                    )
                else:
                    response = (
                        client.table("dur_master")
                        .select("*")
                        .ilike("ingr_eng_name", f"%{target.lower()}%")
                        .execute()
                    )
                if response.data:
                    all_results.extend(response.data)
            except Exception as e:
                logger.error(f"[Supabase] Batch DUR query error for '{target}': {e}")
        return all_results

    @classmethod
    async def get_symptom_cache(cls, query_text: str):
        client = cls.get_client()
        if not client:
            return None
        try:
            response = (
                client.table("search_cache")
                .select("*")
                .eq("query_text", query_text)
                .limit(1)
                .execute()
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            logger.error(f"[Cache] Error reading cache for '{query_text}': {e}")
        return None

    @classmethod
    async def set_symptom_cache(
        cls,
        query_text: str,
        category: str,
        fda_data: list,
        dur_data: list,
        final_answer: str,
        recommended_ingredients: list,
    ):
        client = cls.get_client()
        if not client:
            return False
        try:
            payload = {
                "query_text": query_text,
                "category": category,
                "fda_data": fda_data if fda_data else [],
                "dur_data": dur_data if dur_data else [],
                "final_answer": final_answer,
                "recommended_ingredients": (
                    recommended_ingredients if recommended_ingredients else []
                ),
            }
            client.table("search_cache").upsert(
                payload, on_conflict="query_text"
            ).execute()
            return True
        except Exception as e:
            logger.error(f"[Cache] Error saving cache for '{query_text}': {e}")
            return False
