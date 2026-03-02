import logging
from asgiref.sync import sync_to_async
from django.db.models import Q
import asyncio
import re
from .pinecone_service import PineconeService

logger = logging.getLogger(__name__)


class DrugService:
    # 성분명 매핑 테이블 (generic_name -> KR DUR ingr_eng_name)
    MANUAL_INGR_MAPPING = {
        "DIVALPROEX SODIUM": "VALPROIC ACID",
        "DIVALPROEX": "VALPROIC ACID",
        # 필요 시 추가
    }

    @classmethod
    async def search_drug(cls, name: str):
        """
        특정 제품명으로 Pinecone 벡터 검색 (비동기)
        상세 정보(적응증, 경고, 용법)를 포함하여 반환
        """
        filter_dict = {
            "$or": [
                {"brand_name": {"$eq": name.upper()}},
                {"generic_name": {"$eq": name.upper()}}
            ]
        }
        
        try:
            matches = await PineconeService.search(query_text=name, filter_dict=filter_dict, top_k=1)

            if not matches:
                return None

            metadata = matches[0].get("metadata", {})

            generic_name = metadata.get("generic_name", "")
            substance_name = metadata.get("substance_name", "")
            active_ingredient = metadata.get("active_ingredient", "")

            combined_ingrs = list(set(filter(None, [generic_name, substance_name, active_ingredient])))
            ingr_text = ", ".join(combined_ingrs)

            return {
                "brand_name": metadata.get("brand_name", name),
                "active_ingredients": ingr_text or "Ingredient Not Found",
                "ingredients": ingr_text,
                "indications": metadata.get("indications_and_usage", "Indications not provided"),
                "warnings": metadata.get("warnings", "Warnings not provided"),
                "dosage": metadata.get("dosage_and_administration", "Dosage info not provided"),
            }
        except Exception as e:
            logger.error(f"Error searching drug via Pinecone: {e}")
            return None

    @classmethod
    async def get_ingredients_by_symptoms(cls, keywords: list):
        """영어 증상 키워드로 Pinecone 벡터 검색을 통해 관련 성분명 추출"""
        ingredient_counts = {}

        for kw in keywords:
            filter_dict = {"source_section": "indications_and_usage"}
            matches = await PineconeService.search(query_text=kw, filter_dict=filter_dict, top_k=50)
            
            for match in matches:
                metadata = match.get("metadata", {})
                term = metadata.get("generic_name", "").upper()
                if not term:
                    term = metadata.get("active_ingredient", "").upper()
                if not term:
                    continue
                    
                parts = re.split(r",\s*| AND ", term)
                for part in parts:
                    part = part.strip()
                    part_clean = re.sub(r"\s+\d+.*$", "", part).strip()
                    if part_clean and len(part_clean) > 2:
                        ingredient_counts[part_clean] = ingredient_counts.get(part_clean, 0) + 1

        sorted_ingrs = sorted(ingredient_counts.items(), key=lambda x: x[1], reverse=True)
        top_ingrs = [ingr for ingr, _ in sorted_ingrs[:5]]
        return top_ingrs

    @staticmethod
    @sync_to_async
    def get_dur_by_ingr(ingr_text):
        """제품 검색 시 성분 텍스트로 한국 DUR 조회"""
        from drug.models import DurMaster

        if not ingr_text:
            return []

        query = Q()
        for i in ingr_text.replace(",", "/").split("/"):
            target = i.strip().lower()
            if len(target) > 1:
                query |= Q(ingr_eng_name__icontains=target)

        durs = list(DurMaster.objects.filter(query))

        return [
            {
                "type": d.dur_type,
                "ingr_name": d.ingr_kor_name,
                "warning_msg": d.prohbt_content or d.remark,
                "severity": d.critical_value,
            }
            for d in durs
        ]

    @classmethod
    async def get_warnings_by_ingredient(cls, ingr_name: str):
        filter_dict = {
            "generic_name": {"$eq": ingr_name.upper()},
            "source_section": {"$eq": "warnings"}
        }
        try:
            matches = await PineconeService.search(query_text=ingr_name, filter_dict=filter_dict, top_k=1)
            if matches:
                metadata = matches[0].get("metadata", {})
                return metadata.get("chunk_text", "No warning found.")
        except Exception as e:
            logger.warning(f"Error fetching warnings for '{ingr_name}': {e}")
        return None

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """영어 성분명 리스트를 받아 KR DUR 및 FDA Warning 정보를 병합"""
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        # 배치 임베딩 사전 캐시 (CPU 경합 방지: 5× to_thread → 1× to_thread)
        await PineconeService.prefetch_embeddings(unique_ingrs)

        async def fetch_info(ingr):
            durs, warn = await asyncio.gather(
                cls._get_kr_durs_async(ingr),
                cls.get_warnings_by_ingredient(ingr)
            )

            return {
                "ingredient": ingr,
                "kr_durs": durs,
                "fda_warning": warn,
            }

        enriched_data = await asyncio.gather(*[fetch_info(ingr) for ingr in unique_ingrs])
        return list(enriched_data)

    @classmethod
    def compare_dosage_and_warn(
        cls, fda_active_ingredient_text: str, kr_dosage_mg: float
    ) -> dict:
        """
        FDA의 active_ingredient 텍스트에서 mg 단위를 추출하여 한국 처방량과 비교
        """
        warning_msg = None
        us_dosage_mg = None

        match = re.search(
            r"(\d+(?:\.\d+)?)\s*mg", fda_active_ingredient_text, re.IGNORECASE
        )
        if match:
            try:
                us_dosage_mg = float(match.group(1))
            except ValueError:
                pass

        if us_dosage_mg is not None and kr_dosage_mg > 0:
            diff_ratio = us_dosage_mg / kr_dosage_mg
            if diff_ratio >= 1.5:
                warning_msg = f"주의: 미국 제품의 함량({us_dosage_mg}mg)이 한국 기준({kr_dosage_mg}mg)보다 1.5배 이상 높습니다. 복용 전 약사와 상담하세요."
            elif diff_ratio <= 0.5:
                warning_msg = f"주의: 미국 제품의 함량({us_dosage_mg}mg)이 한국 기준({kr_dosage_mg}mg)보다 0.5배 이하로 낮아 권장 효과에 미달할 수 있습니다."
            else:
                warning_msg = f"미국 제품의 함량({us_dosage_mg}mg)은 한국 처방 기준({kr_dosage_mg}mg)과 유사한 수준입니다."
        else:
            warning_msg = "함량(mg) 정보를 명확히 추출하지 못했거나 기준량이 입력되지 않아 비교할 수 없습니다. 제조사 라벨을 반드시 확인하세요."

        return {
            "us_dosage_mg": us_dosage_mg,
            "kr_dosage_mg": kr_dosage_mg,
            "warning": warning_msg,
        }

    @classmethod
    async def _get_kr_durs_async(cls, ingr_name):
        """Su pabase API를 통한 DUR 정보 조회 및 포맷팅"""
        from services.supabase_service import SupabaseService
        return await SupabaseService._get_kr_durs_supabase(ingr_name)
