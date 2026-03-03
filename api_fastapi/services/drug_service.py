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
        # Search by brand_name or generic_name
        # Assuming our Pinecone index has these fields in metadata
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

            metadata = matches[0].get('metadata', {})

            generic_name = metadata.get('generic_name', "")
            substance_name = metadata.get('substance_name', "")
            active_ingredient = metadata.get('active_ingredient', "")

            combined_ingrs = list(set(filter(None, [generic_name, substance_name, active_ingredient])))
            ingr_text = ", ".join(combined_ingrs)

            return {
                "brand_name": metadata.get('brand_name', name),
                "active_ingredients": ingr_text or "Ingredient Not Found",
                "ingredients": ingr_text,
                "indications": metadata.get('indications_and_usage', "Indications not provided"),
                "warnings": metadata.get('warnings', "Warnings not provided"),
                "dosage": metadata.get('dosage_and_administration', "Dosage info not provided")
            }
        except Exception as e:
            logger.error(f"Error searching drug via Pinecone: {e}")
            return None

    @classmethod
    async def get_ingredients_by_symptoms(cls, keywords: list):
        """
        영어 증상 키워드로 Pinecone 벡터 검색을 통해 관련 성분명 추출 (비동기 + 병렬 처리)
        유사도 검색으로 상위 N(50)개의 결과를 가져온 후 코드 레벨에서 성분별 빈도를 집계(Aggregating)
        """
        all_ingrs = set()
        ingredient_counts = {}
        
        for kw in keywords:
            # Filter by indications section if available
            filter_dict = {"source_section": "indications_and_usage"}
            matches = await PineconeService.search(query_text=kw, filter_dict=filter_dict, top_k=50)
            
            for match in matches:
                metadata = match.get('metadata', {})
                term = metadata.get('generic_name', '').upper()
                if not term:
                    term = metadata.get('active_ingredient', '').upper()
                if not term:
                    continue
                    
                parts = re.split(r',\s*| AND ', term)
                for part in parts:
                    part = part.strip()
                    part_clean = re.sub(r'\s+\d+.*$', '', part).strip()
                    if part_clean and len(part_clean) > 2:
                        # count aggregate
                        ingredient_counts[part_clean] = ingredient_counts.get(part_clean, 0) + 1
        
        # Sort by frequency and get top 20
        sorted_ingrs = sorted(ingredient_counts.items(), key=lambda x: x[1], reverse=True)
        top_ingrs = [ingr for ingr, _ in sorted_ingrs[:20]]
        return top_ingrs

    @staticmethod
    @sync_to_async
    def get_dur_by_ingr(ingr_text):
        """제품 검색 시 성분 텍스트로 한국 DUR 조회"""
        from drugs.models import DurMaster
        if not ingr_text:
            return []
            
        query = Q()
        for i in ingr_text.replace(',', '/').split('/'):
            target = i.strip().lower()
            if len(target) > 1:
                query |= Q(ingr_eng_name__icontains=target)
        
        # 쿼리셋 평가를 위해 list()로 변환
        durs = list(DurMaster.objects.filter(query))
        
        return [{
            "type": d.dur_type,
            "ingr_name": d.ingr_kor_name,
            "warning_msg": d.prohbt_content or d.remark,
            "severity": d.critical_value
        } for d in durs]

    @classmethod
    async def get_warnings_by_ingredient(cls, ingr_name: str):
        """
        성분명으로 경고(Warnings) 정보 조회
        """
        filter_dict = {
            "generic_name": {"$eq": ingr_name.upper()},
            "source_section": {"$eq": "warnings"}
        }

        try:
            matches = await PineconeService.search(query_text=ingr_name, filter_dict=filter_dict, top_k=1)
            if matches:
                metadata = matches[0].get('metadata', {})
                return metadata.get('chunk_text', "No warning found.")
        except Exception as e:
            logger.warning(f"Error fetching warnings for '{ingr_name}': {e}")
        return None

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """
        영어 성분명 리스트를 받아 KR DUR 및 FDA Warning 정보를 병합하여 반환
        """
        # 1. 고유 성분명으로 정리
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        # 배치 임베딩 사전 캐시 (CPU 경합 방지: 5× to_thread → 1× to_thread)
        await PineconeService.prefetch_embeddings(unique_ingrs)

        async def fetch_info(ingr):
            durs, warn = await asyncio.gather(
                cls._get_kr_durs_async(ingr),
                cls.get_warnings_by_ingredient(ingr)
            )
            return {"ingredient": ingr, "kr_durs": durs, "fda_warning": warn}

        enriched_data = await asyncio.gather(*[fetch_info(ingr) for ingr in unique_ingrs])
        return list(enriched_data)

    @classmethod
    async def _get_kr_durs_async(cls, ingr_name):
        """비동기 문맥에서 DB 호출을 위한 헬퍼 (Robust Search with Lazy LLM)"""
        from drugs.models import DurMaster
        from django.db.models import Q
        
        if not ingr_name: return []
        
        # 1. Cleaning
        target_name = ingr_name.strip().lower()
        if not target_name: return []

        # 2. Synonym Mapping (Common miss-matches)
        SYNONYMS = {
            "acetaminophen": ["acetaminophen", "paracetamol"],
            "paracetamol": ["acetaminophen", "paracetamol"],
            "aspirin": ["aspirin", "acetylsalicylic acid"],
            "ibuprofen": ["ibuprofen"],
            "naproxen": ["naproxen"],
            "diphenhydramine": ["diphenhydramine"],
        }
        
        search_candidates = set()
        search_candidates.add(target_name)
        
        # Add synonyms
        if target_name in SYNONYMS:
            search_candidates.update(SYNONYMS[target_name])
            
        # Add first word
        first_word = target_name.split()[0]
        if len(first_word) > 3:
            search_candidates.add(first_word)

        logger.debug(f"Search candidates for '{ingr_name}': {search_candidates}")

        # 3. Construct Query
        q_obj = Q()
        for cand in search_candidates:
            q_obj |= Q(ingr_eng_name__icontains=cand)
            q_obj |= Q(ingr_kor_name__icontains=cand)

        # Sync code to create queryset is fine
        durs_qs = DurMaster.objects.filter(q_obj).distinct()
        
        # Async execution of DB query
        durs_list = await sync_to_async(list)(durs_qs)
        
        # [Lazy LLM Expansion]
        if not durs_list and len(target_name) > 2:
            from services.ai_service import AIService
            logger.debug(f"No direct DUR match for '{target_name}'. Requesting AI synonyms...")
            
            ai_synonyms = await AIService.get_synonyms(ingr_name)
            logger.debug(f"AI Synonyms for '{ingr_name}': {ai_synonyms}")
            
            if ai_synonyms:
                q_retry = Q()
                for syn in ai_synonyms:
                    q_retry |= Q(ingr_eng_name__icontains=syn)
                    q_retry |= Q(ingr_kor_name__icontains=syn)
                
                durs_retry_qs = DurMaster.objects.filter(q_retry).distinct()
                durs_list = await sync_to_async(list)(durs_retry_qs)



        # [Dedup & Translation]
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

        # Group by type to remove duplicates and combine messages
        grouped_results = {}
        
        for d in durs_list:
            d_type = d.dur_type
            kor_type = DUR_TYPE_KOR_MAP.get(d_type, d_type) # Fallback to original if not mapped
            content = (d.prohbt_content or d.remark or "").strip()
            
            if not content: continue
            
            if kor_type not in grouped_results:
                grouped_results[kor_type] = {
                    "type": kor_type, # Use localized name
                    "original_type": d_type,
                    "kor_name": d.ingr_kor_name,
                    "warnings": set() # Use set for dedup content
                }
            
            grouped_results[kor_type]["warnings"].add(content)

        results = []
        for key, val in grouped_results.items():
            # Combine unique warnings into one string
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append({
                "type": val["type"],
                "kor_name": val["kor_name"],
                "warning": combined_warning
            })
            
        logger.debug(f"Found {len(results)} DUR categories for '{ingr_name}' (after dedup/translation).")
        return results



    @staticmethod
    @sync_to_async
    def search_eyak_drug(keyword: str):
        """
        DrugPermitInfo에서 제품명 또는 업체명으로 약품 검색 (사용자 확인: 데이터 존재함)
        """
        from drugs.models import DrugPermitInfo
        
        # 검색어 공백 제거
        keyword = keyword.strip()

        # 최대 100개까지 반환 (스크롤 고려)
        if keyword:
            results = DrugPermitInfo.objects.filter(
                Q(item_name__icontains=keyword) | 
                Q(entp_name__icontains=keyword)
            )[:100]
        else:
            # 검색어 없으면 상위 100개 반환 (전체 보기)
            results = DrugPermitInfo.objects.all()[:100]

        return [{
            "item_seq": item.item_seq,
            "item_name": item.item_name,
            "entp_name": item.entp_name
        } for item in results]

    @classmethod
    async def get_us_mapping(cls, ingredient_name: str):
        """
        Pinecone에서 해당 성분명으로 브랜드 등 정보 검색
        """
        filter_dict = {
            "substance_name": {"$eq": ingredient_name.upper()}
        }
        
        try:
            matches = await PineconeService.search(query_text=ingredient_name, filter_dict=filter_dict, top_k=3)
            if not matches:
                return {"error": "미국 내 해당 성분 의약품을 찾을 수 없습니다."}
                
            results = []
            for match in matches:
                metadata = match.get('metadata', {})
                results.append({
                    "brand_name": metadata.get("brand_name", "N/A"),
                    "dosage_form": metadata.get("dosage_form", "N/A"), # Assuming dosage form might exist, fallback N/A
                    "warnings": metadata.get("chunk_text", "N/A")[:200] if metadata.get("source_section") == "warnings" else "N/A"
                })
            return results
        except Exception as e:
            logger.error(f"Error getting US mapping for '{ingredient_name}': {e}")
            return {"error": "미국 내 해당 성분 의약품을 찾을 수 없습니다."}

    @classmethod
    def compare_dosage_and_warn(cls, fda_active_ingredient_text: str, kr_dosage_mg: float) -> dict:
        """
        FDA의 active_ingredient 텍스트에서 mg 단위를 추출하여 한국 처방량과 비교
        fda_active_ingredient_text 예: "ACETAMINOPHEN 500mg" 또는 "Ibuprofen 200 mg"
        kr_dosage_mg 예: 300.0 (한국 기준 함량)
        """
        warning_msg = None
        us_dosage_mg = None
        
        # 정규식을 이용해 mg 수치 추출 (예: 500 mg, 500.0mg 등)
        match = re.search(r'(\d+(?:\.\d+)?)\s*mg', fda_active_ingredient_text, re.IGNORECASE)
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
            "warning": warning_msg
        }