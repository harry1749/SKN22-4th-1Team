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
        """
        [DrugService.get_dur_by_ingr 대체]
        제품 검색 시 성분 텍스트(예: "Acetaminophen, Caffeine")로 DUR 조회
        """
        if not ingr_text:
            return []
            
        # Parse ingredients
        ingr_list = [i.strip() for i in ingr_text.replace(',', '/').split('/') if len(i.strip()) > 1]
        
        # Use common logic
        dur_data = await cls._get_dur_data_from_supabase(ingr_list)
        
        # Convert to expected format for search_result.html
        # Expected: { "type", "ingr_name", "warning_msg", "severity" }
        results = []
        for d in dur_data:
            results.append({
                "type": d['dur_type'],
                "ingr_name": d['ingr_kor_name'],
                "warning_msg": d['prohbt_content'] or d['remark'],
                "severity": d['critical_value']
            })
        return results

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """
        [DrugService.get_enriched_dur_info 대체]
        LangGraph 등에서 사용하는 상세 정보 조회 (FDA Warning + DUR)
        """
        # 1. 고유 성분명으로 정리
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))
        enriched_data = []
        
        from services.drug_service import DrugService as OriginalDrugService

        for ingr in unique_ingrs:
            # KR DUR 조회 (Supabase)
            durs = await cls._get_kr_durs_supabase(ingr)

            # Pinecone 벡터 검색으로 경고 정보 조회
            warn = await OriginalDrugService.get_warnings_by_ingredient(ingr)

            enriched_data.append({
                "ingredient": ingr,
                "kr_durs": durs,
                "fda_warning": warn
            })
            
        return enriched_data

    @classmethod
    async def _get_kr_durs_supabase(cls, ingr_name):
        """
        단일 성분에 대해 Supabase DUR 조회 및 그룹화 (DrugService._get_kr_durs_async 로직 재현)
        """
        if not ingr_name: return []
        
        # Clean
        target_name = ingr_name.strip()
        if not target_name: return []

        # (Synonyms logic omitted for brevity, or can be added if needed. Supabase has limited "OR" querying flexibility compared to Django Q objects)
        # For this version, we stick to simple ILIKE matching
        
        client = cls.get_client()
        if not client: return []

        dur_list = []
        try:
            is_korean = bool(re.search('[가-힣]', target_name))

            def _sync_query():
                if is_korean:
                    return (
                        client.table("dur_master")
                        .select("*")
                        .ilike("ingr_kor_name", f"%{target_name}%")
                        .execute()
                        .data
                    )
                else:
                    return (
                        client.table("dur_master")
                        .select("*")
                        .ilike("ingr_eng_name", f"%{target_name.lower()}%")
                        .execute()
                        .data
                    )

            dur_list = await asyncio.to_thread(_sync_query)
        except Exception as e:
            logger.error(f"[Supabase] DUR query error for '{target_name}': {e}")
            return []
            
        # Group & Translation Logic (Copied from DrugService)
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
            d_type = d['dur_type']
            kor_type = DUR_TYPE_KOR_MAP.get(d_type, d_type)
            content = (d['prohbt_content'] or d['remark'] or "").strip()
            
            if not content: continue
            
            if kor_type not in grouped_results:
                grouped_results[kor_type] = {
                    "type": kor_type,
                    "original_type": d_type,
                    "kor_name": d['ingr_kor_name'],
                    "warnings": set()
                }
            grouped_results[kor_type]["warnings"].add(content)
            
        results = []
        for key, val in grouped_results.items():
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append({
                "type": val["type"],
                "kor_name": val["kor_name"],
                "warning": combined_warning
            })
            
        return results

    @classmethod
    async def _get_dur_data_from_supabase(cls, ingr_list: list):
        """
        Helper to get raw DUR data from Supabase for multiple ingredients
        """
        client = cls.get_client()
        if not client: return []
        
        all_results = []
        for ingr in ingr_list:
            if not ingr: continue
            target = ingr.strip()
            try:
                if bool(re.search('[가-힣]', target)):
                    response = client.table("dur_master").select("*").ilike("ingr_kor_name", f"%{target}%").execute()
                else:
                    response = client.table("dur_master").select("*").ilike("ingr_eng_name", f"%{target.lower()}%").execute()
                    
                if response.data:
                    all_results.extend(response.data)
            except Exception as e:
                logger.error(f"[Supabase] Batch DUR query error for '{target}': {e}")
                
        return all_results

    @classmethod
    async def get_symptom_cache(cls, query_text: str):
        """
        AI가 정제한 해시키(예: headache_severe_none)를 기반으로 캐시된 응답 조회
        """
        client = cls.get_client()
        if not client: return None
        
        try:
            # Query by unique query_text matching
            response = client.table("search_cache").select("*").eq("query_text", query_text).limit(1).execute()
            if response.data and len(response.data) > 0:
                logger.info(f"[Cache Hit] query='{query_text}'")
                return response.data[0]
        except Exception as e:
            logger.error(f"[Cache] Error reading cache for '{query_text}': {e}")
            
        return None

    @classmethod
    async def set_symptom_cache(cls, query_text: str, category: str, drug_data: list, dur_data: list, final_answer: str, recommended_ingredients: list):
        """
        새로 생성된 응답을 DB에 캐싱 (백그라운드 비동기 처리를 권장)
        """
        client = cls.get_client()
        if not client: return False
        
        try:
            payload = {
                "query_text": query_text,
                "category": category,
                "fda_data": drug_data if drug_data else [],
                "dur_data": dur_data if dur_data else [],
                "final_answer": final_answer,
                "recommended_ingredients": recommended_ingredients if recommended_ingredients else []
            }
            client.table("search_cache").upsert(payload, on_conflict="query_text").execute()
            logger.info(f"[Cache Saved] query='{query_text}'")
            return True
        except Exception as e:
            logger.error(f"[Cache] Error saving cache for '{query_text}': {e}")
            return False

    @classmethod
    async def get_roadmap_cache(cls, query_text: str):
        """
        성분명과 규격 기반 해시키(예: roadmap_500.0_acetaminophen_ibuprofen)로 
        캐시된 US OTC Roadmap 매칭 및 약사 소통 카드를 조회
        """
        client = cls.get_client()
        if not client: return None
        
        try:
            response = client.table("roadmap_cache").select("*").eq("query_text", query_text).limit(1).execute()
            if response.data and len(response.data) > 0:
                logger.info(f"[Roadmap Cache Hit] query='{query_text}'")
                return response.data[0]
        except Exception as e:
            logger.error(f"[Roadmap Cache] Error reading cache for '{query_text}': {e}")
            
        return None

    @classmethod
    async def set_roadmap_cache(cls, query_text: str, mapping_result: dict, pharmacist_card: dict, dosage_warnings: list):
        """
        새로 생성된 US OTC Roadmap 캐싱
        """
        client = cls.get_client()
        if not client: return False
        
        try:
            payload = {
                "query_text": query_text,
                "mapping_result": mapping_result if mapping_result else {},
                "pharmacist_card": pharmacist_card if pharmacist_card else {},
                "dosage_warnings": dosage_warnings if dosage_warnings else []
            }
            client.table("roadmap_cache").insert(payload).execute()
            logger.info(f"[Roadmap Cache Saved] query='{query_text}'")
            return True
        except Exception as e:
            logger.error(f"[Roadmap Cache] Error saving cache for '{query_text}': {e}")
            return False
