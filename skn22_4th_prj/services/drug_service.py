import httpx
import logging
from asgiref.sync import sync_to_async
from django.db.models import Q
import asyncio
import re

logger = logging.getLogger(__name__)


class DrugService:
    FDA_BASE_URL = "https://api.fda.gov/drug/label.json"
    FDA_OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'

    # 성분명 매핑 테이블 (FDA generic_name -> KR DUR ingr_eng_name)
    MANUAL_INGR_MAPPING = {
        "DIVALPROEX SODIUM": "VALPROIC ACID",
        "DIVALPROEX": "VALPROIC ACID",
        # 필요 시 추가
    }

    @classmethod
    async def search_fda(cls, name: str):
        """
        특정 제품명으로 FDA 정보 검색 (비동기)
        상세 정보(적응증, 경고, 용법)를 포함하여 반환
        """
        params = {
            "search": f'(openfda.brand_name:"{name}"+OR+openfda.generic_name:"{name}")+AND+{cls.FDA_OTC_FILTER}',
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(cls.FDA_BASE_URL, params=params)
                if res.status_code != 200:
                    return None

                data = res.json().get("results", [])
                if not data:
                    return None

                result = data[0]
                openfda = result.get("openfda", {})

                # 성분명 추출 (generic_name, substance_name 모두 포함)
                generic_names = openfda.get("generic_name", [])
                substance_names = openfda.get("substance_name", [])

                combined_ingrs = list(set(generic_names + substance_names))

                if not combined_ingrs:
                    combined_ingrs = result.get("active_ingredient", [])

                ingr_text = (
                    ", ".join(combined_ingrs)
                    if isinstance(combined_ingrs, list)
                    else str(combined_ingrs)
                )

                return {
                    "brand_name": name,
                    "active_ingredients": ingr_text or "Ingredient Not Found",
                    "ingredients": ingr_text,  # 호환성을 위해 유지
                    "indications": result.get(
                        "indications_and_usage", ["Indications not provided"]
                    )[0],
                    "warnings": result.get("warnings", ["Warnings not provided"])[0],
                    "dosage": result.get(
                        "dosage_and_administration", ["Dosage info not provided"]
                    )[0],
                }
            except Exception as e:
                logger.error(f"Error searching FDA: {e}")
                return None

    @classmethod
    async def get_ingrs_from_fda_by_symptoms(cls, keywords: list):
        """영어 증상 키워드로 FDA 관련 성분명 추출"""
        all_ingrs = set()

        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = []
            for kw in keywords:
                url = (
                    f"{cls.FDA_BASE_URL}"
                    f'?search=indications_and_usage:"{kw}"'
                    f"+AND+{cls.FDA_OTC_FILTER}"
                    f"&count=openfda.generic_name.exact"
                )
                tasks.append(client.get(url))

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for res in responses:
                if isinstance(res, httpx.Response) and res.status_code == 200:
                    try:
                        results = res.json().get("results", [])
                        for item in results[:20]:
                            term = item.get("term", "").upper()
                            if not term:
                                continue
                            parts = re.split(r",\s*| AND ", term)
                            for part in parts:
                                part = part.strip()
                                part_clean = re.sub(r"\s+\d+.*$", "", part).strip()
                                if part_clean and len(part_clean) > 2:
                                    all_ingrs.add(part_clean)
                    except Exception as e:
                        logger.warning(f"[FDA count parse error]: {e}")
                        continue

        return list(all_ingrs)

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
    async def get_fda_warnings_by_ingr(cls, ingr_name: str):
        params = {
            "search": f'openfda.generic_name:"{ingr_name}"+AND+{cls.FDA_OTC_FILTER}',
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                res = await client.get(cls.FDA_BASE_URL, params=params)
                if res.status_code == 200:
                    data = res.json().get("results", [])
                    if data:
                        return data[0].get("warnings", ["No FDA warning found."])[0]
            except Exception as e:
                logger.warning(f"Error fetching FDA warnings for '{ingr_name}': {e}")
        return None

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """영어 성분명 리스트를 받아 KR DUR 및 FDA Warning 정보를 병합"""
        enriched_data = []
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        for ingr in unique_ingrs:
            durs = await cls._get_kr_durs_async(ingr)
            fda_warn = await cls.get_fda_warnings_by_ingr(ingr)

            if fda_warn:
                from services.ai_service_v2 import AIService

                summary = await AIService.summarize_fda_warning(fda_warn)
                if summary:
                    fda_warn = summary

            enriched_data.append(
                {"ingredient": ingr, "kr_durs": durs, "fda_warning": fda_warn}
            )

        return enriched_data

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
        from drug.models import DurMaster
        from django.db.models import Q

        if not ingr_name:
            return []
        target_name = ingr_name.strip().lower()
        if not target_name:
            return []

        SYNONYMS = {
            "acetaminophen": ["acetaminophen", "paracetamol"],
            "paracetamol": ["acetaminophen", "paracetamol"],
            "aspirin": ["aspirin", "acetylsalicylic acid"],
            "ibuprofen": ["ibuprofen"],
            "naproxen": ["naproxen"],
            "diphenhydramine": ["diphenhydramine"],
        }

        search_candidates = {target_name}
        if target_name in SYNONYMS:
            search_candidates.update(SYNONYMS[target_name])

        first_word = target_name.split()[0]
        if len(first_word) > 3:
            search_candidates.add(first_word)

        q_obj = Q()
        for cand in search_candidates:
            q_obj |= Q(ingr_eng_name__icontains=cand)
            q_obj |= Q(ingr_kor_name__icontains=cand)

        durs_qs = DurMaster.objects.filter(q_obj).distinct()
        durs_list = await sync_to_async(list)(durs_qs)

        if not durs_list and len(target_name) > 2:
            from services.ai_service_v2 import AIService

            ai_synonyms = await AIService.get_synonyms(ingr_name)
            if ai_synonyms:
                q_retry = Q()
                for syn in ai_synonyms:
                    q_retry |= Q(ingr_eng_name__icontains=syn)
                    q_retry |= Q(ingr_kor_name__icontains=syn)
                durs_retry_qs = DurMaster.objects.filter(q_retry).distinct()
                durs_list = await sync_to_async(list)(durs_retry_qs)

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
        for d in durs_list:
            kor_type = DUR_TYPE_KOR_MAP.get(d.dur_type, d.dur_type)
            content = (d.prohbt_content or d.remark or "").strip()
            if not content:
                continue
            if kor_type not in grouped_results:
                grouped_results[kor_type] = {
                    "type": kor_type,
                    "kor_name": d.ingr_kor_name,
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
