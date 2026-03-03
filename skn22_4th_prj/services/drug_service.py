import httpx
import logging
from asgiref.sync import sync_to_async
from django.db.models import Q
import asyncio
import re
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class DrugService:
    FDA_BASE_URL = "https://api.fda.gov/drug/label.json"
    FDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"
    FDA_OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
    FDA_WARNING_CONCURRENCY = 3
    NDC_MARKETING_CACHE = {}
    NDC_LOOKUP_SEMAPHORE = asyncio.Semaphore(6)

    # 성분명 매핑 테이블 (FDA generic_name -> KR DUR ingr_eng_name)
    MANUAL_INGR_MAPPING = {
        "DIVALPROEX SODIUM": "VALPROIC ACID",
        "DIVALPROEX": "VALPROIC ACID",
        # 필요 시 추가
    }

    @classmethod
    def _normalize_ingredient_tokens(cls, values):
        tokens = []
        seen = set()
        for value in values or []:
            raw = str(value or "").upper()
            if not raw:
                continue
            parts = re.split(r",|/|;|\bAND\b|\bWITH\b|\+", raw, flags=re.IGNORECASE)
            for part in parts:
                token = part.strip()
                if not token:
                    continue
                token = re.sub(r"\([^)]*\)", " ", token)
                token = re.sub(
                    r"\b\d+(?:\.\d+)?\s*(MG|MCG|G|ML|%)\b",
                    " ",
                    token,
                    flags=re.IGNORECASE,
                )
                token = re.sub(r"[^A-Z0-9\s\-]", " ", token)
                token = re.sub(r"\s+", " ", token).strip()
                if not token:
                    continue
                token = canonicalize_ingredient_name(token)
                if not token or token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
        return tokens

    @classmethod
    def _extract_product_ndc_from_openfda(cls, openfda: dict) -> str:
        product_ndc_list = (openfda or {}).get("product_ndc") or []
        if isinstance(product_ndc_list, list) and product_ndc_list:
            value = str(product_ndc_list[0] or "").strip()
            if value:
                return value

        package_ndc_list = (openfda or {}).get("package_ndc") or []
        if isinstance(package_ndc_list, list) and package_ndc_list:
            raw = str(package_ndc_list[0] or "").strip()
            if raw:
                parts = raw.split("-")
                if len(parts) >= 2:
                    return f"{parts[0]}-{parts[1]}"
                return raw
        return ""

    @classmethod
    def _is_homeopathic_marketing_category(cls, category: str) -> bool:
        return "HOMEOPATHIC" in str(category or "").upper()

    @classmethod
    async def _get_marketing_category_by_ndc(
        cls, product_ndc: str, client: httpx.AsyncClient
    ) -> str:
        key = str(product_ndc or "").strip()
        if not key:
            return ""
        if key in cls.NDC_MARKETING_CACHE:
            return cls.NDC_MARKETING_CACHE.get(key) or ""

        query = f'product_ndc:"{key}"'
        url = f"{cls.FDA_NDC_URL}?search={query}&limit=1"
        async with cls.NDC_LOOKUP_SEMAPHORE:
            try:
                res = await client.get(url)
                if res.status_code != 200:
                    cls.NDC_MARKETING_CACHE[key] = ""
                    return ""
                results = res.json().get("results", [])
                category = str((results[0] or {}).get("marketing_category") or "") if results else ""
                cls.NDC_MARKETING_CACHE[key] = category
                return category
            except Exception:
                cls.NDC_MARKETING_CACHE[key] = ""
                return ""

    @classmethod
    async def search_fda(cls, name: str):
        """
        특정 제품명으로 FDA 정보 검색 (비동기)
        상세 정보(적응증, 경고, 용법)를 포함하여 반환
        """
        url = (
            f"{cls.FDA_BASE_URL}"
            f'?search=(openfda.brand_name:"{name}"+OR+openfda.generic_name:"{name}")'
            f"+AND+{cls.FDA_OTC_FILTER}"
            f"&limit=1"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url)
                if res.status_code != 200:
                    return None

                data = res.json().get("results", [])
                if not data:
                    return None

                result = data[0]
                openfda = result.get("openfda", {})
                product_ndc = cls._extract_product_ndc_from_openfda(openfda)
                if product_ndc:
                    category = await cls._get_marketing_category_by_ndc(
                        product_ndc, client
                    )
                    if cls._is_homeopathic_marketing_category(category):
                        logger.info(
                            "Excluded homeopathic product from search_fda: %s (%s)",
                            name,
                            category,
                        )
                        return None

                # 성분명 추출 (generic_name, substance_name 모두 포함)
                generic_names = openfda.get("generic_name", [])
                substance_names = openfda.get("substance_name", [])

                combined_ingrs = cls._normalize_ingredient_tokens(
                    generic_names + substance_names
                )

                if not combined_ingrs:
                    combined_ingrs = cls._normalize_ingredient_tokens(
                        result.get("active_ingredient", [])
                    )

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
    async def get_ingrs_from_fda_by_symptoms(
        cls,
        keywords: list,
        max_terms_per_keyword: int = 50,
        top_n: int = 10,
    ):
        """영어 증상 키워드로 FDA OTC 라벨에서 후보 성분을 집계한다."""
        ingredient_counts = {}

        normalized_keywords = []
        seen_keywords = set()
        for raw_kw in keywords or []:
            kw = str(raw_kw or "").strip().lower()
            if not kw or kw in seen_keywords:
                continue
            seen_keywords.add(kw)
            normalized_keywords.append(kw)

        if not normalized_keywords:
            return []

        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = []
            for kw in normalized_keywords:
                url = (
                    f"{cls.FDA_BASE_URL}"
                    f'?search=indications_and_usage:"{kw}"'
                    f"+AND+{cls.FDA_OTC_FILTER}"
                    f"&count=openfda.generic_name.exact"
                )
                tasks.append(client.get(url))

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for idx, res in enumerate(responses):
                if not isinstance(res, httpx.Response) or res.status_code != 200:
                    continue
                try:
                    results = res.json().get("results", [])
                    for item in results[: max(max_terms_per_keyword, 1)]:
                        term = str(item.get("term") or "").upper()
                        count = int(item.get("count") or 0)
                        if not term or count <= 0:
                            continue

                        parts = re.split(
                            r",|/|;|\bAND\b|\bWITH\b|\+",
                            term,
                            flags=re.IGNORECASE,
                        )
                        for part in parts:
                            token = str(part or "").strip()
                            if not token:
                                continue
                            token = re.sub(r"^(AND|WITH)\s+", "", token, flags=re.I)
                            token = re.sub(
                                r"\b\d+(?:\.\d+)?\s*(MG|MCG|G|ML|%)\b",
                                " ",
                                token,
                                flags=re.I,
                            )
                            token = re.sub(r"[^A-Z0-9\s\-]", " ", token)
                            token = re.sub(r"\s+", " ", token).strip()
                            token = canonicalize_ingredient_name(token)
                            if not token or len(token) < 3:
                                continue
                            ingredient_counts[token] = (
                                ingredient_counts.get(token, 0) + count
                            )
                except Exception as e:
                    kw = (
                        normalized_keywords[idx]
                        if idx < len(normalized_keywords)
                        else "unknown"
                    )
                    logger.warning(
                        f"[FDA count parse error] keyword='{kw}' error={e}"
                    )
                    continue

        sorted_ingrs = sorted(ingredient_counts.items(), key=lambda x: x[1], reverse=True)
        top_ingrs = [ingr for ingr, _ in sorted_ingrs[: max(top_n, 1)]]
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

        seen = set()
        results = []
        for d in durs:
            dur_type = d.dur_type
            warning_msg = d.prohbt_content or d.remark
            
            # Create a unique key for deduplication
            key = (dur_type, warning_msg)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "type": dur_type,
                    "ingr_name": d.ingr_kor_name,
                    "warning_msg": warning_msg,
                    "severity": d.critical_value,
                }
            )
        return results

    @classmethod
    async def get_fda_warnings_by_ingr(cls, ingr_name: str, client: httpx.AsyncClient = None):
        url = (
            f"{cls.FDA_BASE_URL}"
            f'?search=openfda.generic_name:"{ingr_name}"+AND+{cls.FDA_OTC_FILTER}'
            f"&limit=1"
        )
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=5.0)

        try:
            res = await client.get(url)
            if res.status_code == 200:
                data = res.json().get("results", [])
                if data:
                    return data[0].get("warnings", ["No FDA warning found."])[0]
        except Exception as e:
            logger.warning(f"Error fetching FDA warnings for '{ingr_name}': {e}")
        finally:
            if owns_client:
                await client.aclose()
        return None

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """영어 성분명 리스트를 받아 KR DUR 및 FDA Warning 정보를 병합"""
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        async with httpx.AsyncClient(timeout=5.0) as shared_client:
            warning_semaphore = asyncio.Semaphore(cls.FDA_WARNING_CONCURRENCY)

            async def fetch_warning_with_limit(ingr: str):
                async with warning_semaphore:
                    return await cls.get_fda_warnings_by_ingr(
                        ingr, client=shared_client
                    )

            async def fetch_info(ingr):
                durs, fda_warn = await asyncio.gather(
                    cls._get_kr_durs_async(ingr),
                    fetch_warning_with_limit(ingr),
                )

                return {
                    "ingredient": ingr,
                    "kr_durs": durs,
                    "fda_warning": fda_warn,
                }

            enriched_data = await asyncio.gather(
                *[fetch_info(ingr) for ingr in unique_ingrs]
            )
        return list(enriched_data)

    @classmethod
    async def get_kr_dur_info(cls, ingr_list: list):
        """영어 성분명 리스트를 받아 KR DUR만 조회 (초기 응답 가속용)."""
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        async def fetch_kr_only(ingr):
            durs = await cls._get_kr_durs_async(ingr)
            return {
                "ingredient": ingr,
                "kr_durs": durs,
                "fda_warning": None,
            }

        return list(await asyncio.gather(*[fetch_kr_only(ingr) for ingr in unique_ingrs]))

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
