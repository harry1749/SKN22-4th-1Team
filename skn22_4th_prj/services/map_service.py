import asyncio
import logging
import math
import os
import re
from urllib.parse import quote_plus

import httpx

from services.ai_service_v2 import AIService
from services.amazon_rank_service import AmazonRankService
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class MapService:
    _FDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
    _FDA_NDC_URL = "https://api.fda.gov/drug/ndc.json"
    _GOOGLE_PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    _GOOGLE_PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    _OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    _OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
    _NDC_MARKETING_CACHE = {}
    _NDC_LOOKUP_SEMAPHORE = asyncio.Semaphore(6)
    _SYMPTOM_TO_FDA_TERM = {
        "두통": "headache",
        "편두통": "migraine",
        "소화불량": "indigestion",
        "기침": "cough",
        "감기": "cold",
        "발열": "fever",
        "통증": "pain",
        "염좌": "sprain",
        "찰과상": "wound",
        "상처": "wound",
        "화상": "burn",
        "곤충교상": "insect bite",
    }
    _DEFAULT_KR_SUMMARY = (
        "증상 완화를 위한 일반의약품으로 안내합니다. "
        "복용 전 용법/용량과 주의사항을 확인하세요."
    )
    _BENEFIT_RULES = [
        ("진통", "통증 완화"),
        ("통증", "통증 완화"),
        ("해열", "발열 완화"),
        ("발열", "발열 완화"),
        ("소염", "염증 완화"),
        ("염증", "염증 완화"),
        ("기침", "기침 완화"),
        ("코막힘", "코막힘 완화"),
        ("비염", "비염 증상 완화"),
        ("알레르기", "알레르기 증상 완화"),
        ("속쓰림", "위산/속쓰림 완화"),
        ("소화", "소화 불편 완화"),
        ("복통", "복부 통증 완화"),
        ("설사", "설사 증상 완화"),
        ("감기", "감기 증상 완화"),
        ("pain", "통증 완화"),
        ("analgesic", "통증 완화"),
        ("fever", "발열 완화"),
        ("antipyretic", "발열 완화"),
        ("anti-inflammatory", "염증 완화"),
        ("inflammation", "염증 완화"),
        ("cough", "기침 완화"),
        ("allergy", "알레르기 증상 완화"),
        ("rhinitis", "비염 증상 완화"),
        ("cold", "감기 증상 완화"),
        ("indigestion", "소화 불편 완화"),
        ("heartburn", "위산/속쓰림 완화"),
        ("diarrhea", "설사 증상 완화"),
        ("sleep", "수면 보조"),
    ]
    _VET_TOKEN_PATTERN = re.compile(
        r"\b(vet|veterinary|animal|pet|pets)\b",
        flags=re.IGNORECASE,
    )
    _VET_KR_KEYWORDS = ("동물", "반려", "애완", "수의")

    @classmethod
    async def find_nearby_pharmacies(
        cls, lat: float, lng: float, radius_m: int = 3000, limit: int = 10
    ):
        try:
            lat_v = float(lat)
            lng_v = float(lng)
        except Exception:
            return []

        if not (-90 <= lat_v <= 90 and -180 <= lng_v <= 180):
            return []
        radius_v = int(max(500, min(int(radius_m or 3000), 10000)))
        limit_v = int(max(1, min(int(limit or 10), 30)))

        google_key = str(os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
        google_results = []
        if google_key:
            try:
                google_results = await cls._find_nearby_pharmacies_google(
                    lat=lat_v,
                    lng=lng_v,
                    api_key=google_key,
                    radius_m=radius_v,
                    limit=limit_v,
                )
            except Exception as e:
                logger.warning(f"Google nearby pharmacy lookup failed: {e}")
                google_results = []
        if google_results:
            return google_results

        try:
            return await cls._find_nearby_pharmacies_osm(
                lat=lat_v,
                lng=lng_v,
                radius_m=radius_v,
                limit=limit_v,
            )
        except Exception as e:
            logger.warning(f"OSM nearby pharmacy lookup failed: {e}")
            return []

    @staticmethod
    def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        r = 6371000.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lng2 - lng1)
        a = (math.sin(dp / 2) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dl / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return r * c

    @classmethod
    def _is_veterinary_or_pet_place(
        cls,
        name: str = "",
        address: str = "",
        extra_tokens=None,
        extra_text: str = "",
    ) -> bool:
        token_values = []
        for value in extra_tokens or []:
            text = str(value or "").strip()
            if text:
                token_values.append(text)

        merged = " ".join(
            [str(name or "").strip(), str(address or "").strip(), str(extra_text or "").strip(), " ".join(token_values)]
        ).strip()
        if not merged:
            return False

        lowered = merged.lower()
        if cls._VET_TOKEN_PATTERN.search(lowered):
            return True
        if any(keyword in merged for keyword in cls._VET_KR_KEYWORDS):
            return True

        token_set = {x.lower() for x in token_values}
        if {"veterinary", "veterinary_care", "animal_hospital", "pet_store"}.intersection(token_set):
            return True
        return False

    @classmethod
    async def _find_nearby_pharmacies_google(
        cls, lat: float, lng: float, api_key: str, radius_m: int = 3000, limit: int = 10
    ) -> list:
        params = {
            "location": f"{lat},{lng}",
            "radius": int(max(radius_m, 500)),
            "type": "pharmacy",
            "language": "en",
            "key": api_key,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(cls._GOOGLE_PLACES_NEARBY_URL, params=params)
            if res.status_code != 200:
                raise RuntimeError(f"google_places_http_{res.status_code}")
            payload = res.json()
            status = str(payload.get("status") or "")
            if status not in {"OK", "ZERO_RESULTS"}:
                message = str(payload.get("error_message") or status or "unknown_error")
                raise RuntimeError(message)

            raw = payload.get("results") or []
            items = []
            for row in raw:
                if not isinstance(row, dict):
                    continue
                geo = (row.get("geometry") or {}).get("location") or {}
                item_lat = geo.get("lat")
                item_lng = geo.get("lng")
                if item_lat is None or item_lng is None:
                    continue
                name = str(row.get("name") or "Pharmacy").strip()
                address = str(row.get("vicinity") or row.get("formatted_address") or "").strip()
                types = row.get("types") or []
                if cls._is_veterinary_or_pet_place(
                    name=name,
                    address=address,
                    extra_tokens=types,
                ):
                    continue

                items.append(
                    {
                        "name": name or "Pharmacy",
                        "address": address,
                        "vicinity": row.get("vicinity") or "",
                        "formatted_address": row.get("formatted_address") or row.get("vicinity") or "",
                        "lat": item_lat,
                        "lng": item_lng,
                        "place_id": row.get("place_id") or "",
                        "phone": "",
                        "open_now": (row.get("opening_hours") or {}).get("open_now"),
                        "opening_hours_text": "",
                        "rating": row.get("rating"),
                        "user_ratings_total": row.get("user_ratings_total"),
                        "source": "google_places",
                    }
                )

            trimmed = items[: max(limit, 1)]
            semaphore = asyncio.Semaphore(5)

            async def _enrich(item: dict):
                place_id = str(item.get("place_id") or "").strip()
                if not place_id:
                    return
                async with semaphore:
                    details = await cls._fetch_google_place_details(client, place_id, api_key)
                if not isinstance(details, dict):
                    return
                if details.get("phone"):
                    item["phone"] = details.get("phone")
                if details.get("opening_hours_text"):
                    item["opening_hours_text"] = details.get("opening_hours_text")
                if isinstance(details.get("open_now"), bool):
                    item["open_now"] = details.get("open_now")

            await asyncio.gather(*[_enrich(item) for item in trimmed])
            return trimmed

    @classmethod
    async def _fetch_google_place_details(
        cls, client: httpx.AsyncClient, place_id: str, api_key: str
    ) -> dict:
        if not place_id or not api_key:
            return {}
        params = {
            "place_id": place_id,
            "fields": "opening_hours,formatted_phone_number",
            "language": "en",
            "key": api_key,
        }
        try:
            res = await client.get(cls._GOOGLE_PLACE_DETAILS_URL, params=params)
            if res.status_code != 200:
                return {}
            payload = res.json()
            status = str(payload.get("status") or "")
            if status not in {"OK", "ZERO_RESULTS"}:
                return {}
            result = payload.get("result") or {}
            opening = result.get("opening_hours") or {}
            weekday_text = opening.get("weekday_text") or []
            opening_hours_text = " / ".join(
                [str(x).strip() for x in weekday_text if str(x).strip()]
            )
            open_now = opening.get("open_now")
            phone = str(result.get("formatted_phone_number") or "").strip()
            return {
                "phone": phone,
                "open_now": bool(open_now) if isinstance(open_now, bool) else None,
                "opening_hours_text": opening_hours_text,
            }
        except Exception as e:
            logger.debug(f"Google place details lookup failed for {place_id}: {e}")
            return {}

    @classmethod
    async def _find_nearby_pharmacies_osm(
        cls, lat: float, lng: float, radius_m: int = 3000, limit: int = 10
    ) -> list:
        radius = int(max(radius_m, 500))
        query = (
            "[out:json][timeout:20];"
            "("
            f'node["amenity"="pharmacy"](around:{radius},{lat},{lng});'
            f'way["amenity"="pharmacy"](around:{radius},{lat},{lng});'
            f'relation["amenity"="pharmacy"](around:{radius},{lat},{lng});'
            ");"
            "out center tags;"
        )

        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.post(cls._OSM_OVERPASS_URL, data={"data": query})
            if res.status_code != 200:
                raise RuntimeError(f"osm_overpass_http_{res.status_code}")
            payload = res.json()

        elements = payload.get("elements") or []
        parsed = []
        for row in elements:
            if not isinstance(row, dict):
                continue

            center = row.get("center") or {}
            item_lat = row.get("lat", center.get("lat"))
            item_lng = row.get("lon", center.get("lon"))
            if item_lat is None or item_lng is None:
                continue

            tags = row.get("tags") or {}
            name = str(tags.get("name") or tags.get("brand") or "Pharmacy").strip()
            if not name:
                name = "Pharmacy"

            house = str(tags.get("addr:housenumber") or "").strip()
            street = str(tags.get("addr:street") or "").strip()
            city = str(tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village") or "").strip()
            postcode = str(tags.get("addr:postcode") or "").strip()
            address_parts = [x for x in [house, street, city, postcode] if x]
            address = ", ".join(address_parts)
            if cls._is_veterinary_or_pet_place(
                name=name,
                address=address,
                extra_tokens=[tags.get("healthcare"), tags.get("amenity"), tags.get("shop")],
                extra_text=f"{tags.get('operator') or ''} {tags.get('description') or ''}",
            ):
                continue

            parsed.append(
                {
                    "name": name,
                    "address": address,
                    "vicinity": address,
                    "formatted_address": address,
                    "lat": item_lat,
                    "lng": item_lng,
                    "phone": str(tags.get("phone") or tags.get("contact:phone") or "").strip(),
                    "opening_hours_text": str(tags.get("opening_hours") or "").strip(),
                    "distance_m": round(cls._distance_meters(lat, lng, float(item_lat), float(item_lng))),
                    "source": "osm_overpass",
                }
            )

        parsed.sort(key=lambda x: x.get("distance_m", 10**9))
        return parsed[: max(limit, 1)]

    @classmethod
    def _normalize_ingredient(cls, raw_value: str) -> str:
        value = str(raw_value or "").strip().upper()
        if not value:
            return ""

        value = re.sub(r"\([^)]*\)", " ", value)
        value = re.sub(
            r"\b\d+(?:\.\d+)?\s*(MG|MCG|G|ML|%)\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"[^A-Z0-9\s\-]", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return ""
        return canonicalize_ingredient_name(value)

    @classmethod
    def _normalize_ingredient_list(cls, ingredients: list) -> list:
        normalized = []
        seen = set()
        for raw in ingredients or []:
            token = cls._normalize_ingredient(raw)
            if not token or token in seen:
                continue
            seen.add(token)
            normalized.append(token)
        return normalized

    @classmethod
    def _normalize_identity_text(cls, value: str) -> str:
        return " ".join(str(value or "").strip().upper().split())

    @classmethod
    def _product_identity_key(cls, product: dict):
        brand = cls._normalize_identity_text((product or {}).get("brand_name"))
        manufacturer = cls._normalize_identity_text(
            (product or {}).get("manufacturer_name")
        )
        active = cls._normalize_identity_text((product or {}).get("active_ingredient"))

        if manufacturer and manufacturer != "UNKNOWN MANUFACTURER":
            return (brand, manufacturer)
        return (brand, active)

    @classmethod
    def _dedupe_products(cls, products: list) -> list:
        unique = {}
        for product in products or []:
            if not isinstance(product, dict):
                continue
            key = cls._product_identity_key(product)
            if key in unique:
                continue
            unique[key] = product
        return list(unique.values())

    @classmethod
    def _extract_active_ingredient_text(cls, item: dict) -> str:
        active_values = item.get("active_ingredient") or []
        if isinstance(active_values, list):
            parts = [str(v).strip() for v in active_values if str(v).strip()]
            if parts:
                return " | ".join(parts)
        elif isinstance(active_values, str) and active_values.strip():
            return active_values.strip()

        generic_values = (item.get("openfda") or {}).get("generic_name") or []
        if isinstance(generic_values, list):
            parts = [str(v).strip() for v in generic_values if str(v).strip()]
            if parts:
                return ", ".join(parts)

        return "Unknown"

    @classmethod
    def _split_ingredient_tokens_from_text(cls, text: str) -> list:
        raw = str(text or "").strip()
        if not raw:
            return []

        parts = re.split(r"\||/|,|;|\bAND\b|\bWITH\b|\+", raw, flags=re.IGNORECASE)
        tokens = []
        seen = set()
        for part in parts:
            token = cls._normalize_ingredient(part)
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @classmethod
    def _split_ingredient_tokens_from_values(cls, values) -> list:
        tokens = []
        seen = set()
        for raw in values or []:
            parts = re.split(r"\||/|,|;|\bAND\b|\bWITH\b|\+", str(raw), flags=re.IGNORECASE)
            for part in parts:
                token = cls._normalize_ingredient(part)
                if not token or token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
        return tokens

    @classmethod
    def _extract_product_ingredient_tokens(cls, item: dict) -> list:
        openfda = item.get("openfda") or {}
        tokens = cls._split_ingredient_tokens_from_values(openfda.get("substance_name") or [])
        if not tokens:
            tokens = cls._split_ingredient_tokens_from_values(openfda.get("generic_name") or [])
        if tokens:
            return tokens

        active_values = item.get("active_ingredient") or []
        if isinstance(active_values, str):
            active_values = [active_values]
        return cls._split_ingredient_tokens_from_values(active_values)

    @classmethod
    def _extract_product_ndc(cls, item: dict) -> str:
        openfda = item.get("openfda") or {}
        product_ndc_list = openfda.get("product_ndc") or []
        if isinstance(product_ndc_list, list) and product_ndc_list:
            value = str(product_ndc_list[0] or "").strip()
            if value:
                return value

        package_ndc_list = openfda.get("package_ndc") or []
        if isinstance(package_ndc_list, list) and package_ndc_list:
            raw = str(package_ndc_list[0] or "").strip()
            if raw:
                # package_ndc: labeler-product-package -> keep labeler-product
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
        if key in cls._NDC_MARKETING_CACHE:
            return cls._NDC_MARKETING_CACHE.get(key) or ""

        query = f'product_ndc:"{key}"'
        url = f"{cls._FDA_NDC_URL}?search={query}&limit=1"

        async with cls._NDC_LOOKUP_SEMAPHORE:
            try:
                res = await client.get(url)
                if res.status_code != 200:
                    cls._NDC_MARKETING_CACHE[key] = ""
                    return ""
                results = res.json().get("results", [])
                category = str((results[0] or {}).get("marketing_category") or "") if results else ""
                cls._NDC_MARKETING_CACHE[key] = category
                return category
            except Exception as e:
                logger.debug(f"NDC marketing_category lookup failed for {key}: {e}")
                cls._NDC_MARKETING_CACHE[key] = ""
                return ""

    @classmethod
    async def _build_homeopathic_ndc_set(
        cls, items: list, client: httpx.AsyncClient
    ) -> set:
        ndcs = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            ndc = cls._extract_product_ndc(item)
            if ndc:
                ndcs.append(ndc)
        ndcs = sorted(set(ndcs))
        if not ndcs:
            return set()

        categories = await asyncio.gather(
            *[cls._get_marketing_category_by_ndc(ndc, client) for ndc in ndcs]
        )
        return {
            ndc
            for ndc, category in zip(ndcs, categories)
            if cls._is_homeopathic_marketing_category(category)
        }

    @classmethod
    def _infer_benefit_brief_kr(cls, text: str) -> str:
        raw = str(text or "").strip().lower()
        if not raw:
            return "증상 완화 보조"
        for needle, benefit in cls._BENEFIT_RULES:
            if needle in raw:
                return benefit
        return "증상 완화 보조"

    @classmethod
    def _to_product_payload(cls, item: dict) -> dict:
        openfda = item.get("openfda") or {}
        brand_name = (openfda.get("brand_name") or ["Unknown"])[0]
        manufacturer_name = (openfda.get("manufacturer_name") or ["Unknown Manufacturer"])[0]
        purpose = (item.get("purpose") or ["No purpose specified."])[0]
        set_id = str(item.get("set_id") or "").strip()
        product_ndc = cls._extract_product_ndc(item)

        if set_id:
            dailymed_url = (
                f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
            )
        else:
            brand_query = quote_plus(str(brand_name or "").strip())
            dailymed_url = (
                f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?query={brand_query}"
                if brand_query
                else ""
            )
        fda_label_api_url = (
            f'https://api.fda.gov/drug/label.json?search=openfda.product_ndc:"{product_ndc}"&limit=1'
            if product_ndc
            else ""
        )

        return {
            "brand_name": brand_name,
            "manufacturer_name": manufacturer_name,
            "purpose": purpose,
            "summary_kr": "",
            "active_ingredient": cls._extract_active_ingredient_text(item),
            "dailymed_url": dailymed_url,
            "fda_label_api_url": fda_label_api_url,
        }

    @staticmethod
    def _contains_hangul(text: str) -> bool:
        return bool(re.search(r"[가-힣]", str(text or "")))

    @classmethod
    def _fallback_korean_summary(cls, original_text: str = "") -> str:
        if cls._contains_hangul(original_text):
            summary = str(original_text).strip()
        else:
            summary = cls._DEFAULT_KR_SUMMARY
        if len(summary) <= 150:
            return summary
        return summary[:147].rstrip() + "..."

    @classmethod
    async def _attach_korean_summaries(cls, products: list) -> list:
        if not isinstance(products, list) or not products:
            return products

        purposes = [str((p or {}).get("purpose") or "").strip() for p in products]
        translated = []
        try:
            translated = await AIService.translate_purposes(purposes)
        except Exception as e:
            logger.warning(f"Purpose summarization failed: {e}")
            translated = []

        for i, product in enumerate(products):
            summary = ""
            if i < len(translated):
                summary = str(translated[i] or "").strip()
            if not summary or not cls._contains_hangul(summary):
                src = purposes[i] if i < len(purposes) else ""
                summary = cls._fallback_korean_summary(src)
            product["summary_kr"] = summary
        return products

    @classmethod
    async def ensure_mapping_result_summaries(cls, mapping_result: dict) -> dict:
        if not isinstance(mapping_result, dict):
            return mapping_result

        match_type = str(mapping_result.get("match_type") or "").upper()
        if match_type == "FULL_MATCH":
            products = mapping_result.get("recommendations")
            if isinstance(products, list):
                needs = any(not str((p or {}).get("summary_kr") or "").strip() for p in products)
                if needs:
                    await cls._attach_korean_summaries(products)
            return mapping_result

        if match_type == "COMPONENT_MATCH":
            rec_groups = mapping_result.get("recommendations")
            if isinstance(rec_groups, list):
                for group in rec_groups:
                    products = (group or {}).get("products")
                    if not isinstance(products, list):
                        continue
                    needs = any(not str((p or {}).get("summary_kr") or "").strip() for p in products)
                    if needs:
                        await cls._attach_korean_summaries(products)

            cross = mapping_result.get("cross_ingredient_recommendations")
            if isinstance(cross, list):
                needs = any(not str((p or {}).get("summary_kr") or "").strip() for p in cross)
                if needs:
                    await cls._attach_korean_summaries(cross)

        return mapping_result

    @classmethod
    def _contains_ingredient(cls, text: str, ingredient: str) -> bool:
        if not text or not ingredient:
            return False
        return ingredient.upper() in text.upper()

    @classmethod
    def _ingredient_search_variants(cls, ingredient: str) -> list:
        """Build search variants to improve openFDA hit rate (e.g., NAPROXEN SODIUM)."""
        base = cls._normalize_ingredient(ingredient)
        if not base:
            return []

        variants = [base, f"{base} SODIUM", f"{base} POTASSIUM"]
        seen = set()
        ordered = []
        for token in variants:
            key = token.strip().upper()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(key)
        return ordered

    @classmethod
    def _normalize_symptom_for_fda(cls, symptom: str) -> str:
        token = str(symptom or "").strip().lower()
        if not token:
            return ""
        mapped = cls._SYMPTOM_TO_FDA_TERM.get(token)
        if mapped:
            return mapped
        return token

    @classmethod
    def _build_otc_search_query(cls, ingredient_variants: list, symptom: str = "") -> str:
        symptom_term = cls._normalize_symptom_for_fda(symptom)

        ingredient_clauses = []
        for var in ingredient_variants:
            ingredient_clauses.append(f'openfda.substance_name:"{var}"')
            ingredient_clauses.append(f'openfda.generic_name:"{var}"')
            ingredient_clauses.append(f'active_ingredient:"{var}"')

        ingredient_query = f"({' +OR+ '.join(ingredient_clauses)})"
        if symptom_term:
            symptom_query = f'(indications_and_usage:"{symptom_term}")'
            return f"{symptom_query}+AND+{ingredient_query}+AND+{cls._OTC_FILTER}"
        return f"{ingredient_query}+AND+{cls._OTC_FILTER}"

    @classmethod
    async def get_us_otc_products_by_ingredient(
        cls, ingredient: str, limit: int = 3, symptom: str = ""
    ):
        """Fetch OTC products containing the given ingredient from openFDA."""
        normalized_ingredient = cls._normalize_ingredient(ingredient)
        if not normalized_ingredient:
            return {
                "ingredient": ingredient,
                "products": [],
                "count": 0,
                "diagnostics": {
                    "ingredient": ingredient,
                    "normalized_ingredient": normalized_ingredient,
                    "reason": "invalid_ingredient",
                },
            }

        variants = cls._ingredient_search_variants(normalized_ingredient)
        primary_query = cls._build_otc_search_query(variants, symptom=symptom)
        fallback_query = cls._build_otc_search_query([normalized_ingredient], symptom="")
        diagnostics = {
            "ingredient": ingredient,
            "normalized_ingredient": normalized_ingredient,
            "symptom": symptom or "",
            "search_variants": variants,
            "primary_hits": 0,
            "fallback_used": False,
            "fallback_hits": 0,
            "homeopathic_filtered": 0,
            "brand_missing_filtered": 0,
            "ingredient_mismatch_filtered": 0,
            "post_filter_items": 0,
            "deduped_items": 0,
            "final_products": 0,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                primary_url = f"{cls._FDA_LABEL_URL}?search={primary_query}&limit=50"
                response = await client.get(primary_url)
                if response.status_code != 200:
                    data = []
                else:
                    data = response.json().get("results", [])
                diagnostics["primary_hits"] = len(data)

                # Fallback for ingredients whose OTC labels are indexed under active_ingredient text.
                if not data:
                    diagnostics["fallback_used"] = True
                    fallback_url = f"{cls._FDA_LABEL_URL}?search={fallback_query}&limit=50"
                    fallback_res = await client.get(fallback_url)
                    if fallback_res.status_code == 200:
                        data = fallback_res.json().get("results", [])
                    diagnostics["fallback_hits"] = len(data)
                homeopathic_ndcs = await cls._build_homeopathic_ndc_set(data, client)

                products_info = []
                for item in data:
                    openfda = item.get("openfda") or {}
                    if not openfda.get("brand_name"):
                        diagnostics["brand_missing_filtered"] += 1
                        continue
                    item_ndc = cls._extract_product_ndc(item)
                    if item_ndc and item_ndc in homeopathic_ndcs:
                        diagnostics["homeopathic_filtered"] += 1
                        continue
                    ingredient_text = cls._extract_active_ingredient_text(item)
                    generic_text = " ".join(openfda.get("generic_name") or [])
                    substance_text = " ".join(openfda.get("substance_name") or [])
                    searchable = f"{ingredient_text} {generic_text} {substance_text}".upper()
                    if not any(
                        cls._contains_ingredient(searchable, var) for var in variants
                    ):
                        diagnostics["ingredient_mismatch_filtered"] += 1
                        continue
                    payload = cls._to_product_payload(item)
                    payload["_all_ingredient_tokens"] = cls._extract_product_ingredient_tokens(
                        item
                    )
                    products_info.append(payload)
                diagnostics["post_filter_items"] = len(products_info)

                unique_products = cls._dedupe_products(products_info)
                diagnostics["deduped_items"] = len(unique_products)

                sorted_products = sorted(
                    list(unique_products), key=lambda x: x.get("brand_name", "")
                )[: max(limit, 1)]
                sorted_products = await cls._attach_korean_summaries(sorted_products)
                for product in sorted_products:
                    all_tokens = product.get("_all_ingredient_tokens") or []
                    if not all_tokens:
                        all_tokens = cls._split_ingredient_tokens_from_text(
                            product.get("active_ingredient")
                        )

                    extras = [token for token in all_tokens if token != normalized_ingredient]
                    benefit = cls._infer_benefit_brief_kr(
                        product.get("summary_kr") or product.get("purpose")
                    )
                    product["other_active_ingredients"] = extras
                    product["other_active_components"] = [
                        {"name": token, "benefit": benefit} for token in extras
                    ]
                    product.pop("_all_ingredient_tokens", None)
                sorted_products = await AmazonRankService.enrich_and_sort_products(
                    sorted_products
                )
                diagnostics["final_products"] = len(sorted_products)

                return {
                    "ingredient": normalized_ingredient,
                    "products": sorted_products,
                    "count": len(sorted_products),
                    "diagnostics": diagnostics,
                }
            except Exception as e:
                logger.error(
                    f"Error fetching FDA products for '{normalized_ingredient}': {e}"
                )
                return {
                    "ingredient": normalized_ingredient,
                    "products": [],
                    "error": str(e),
                    "diagnostics": diagnostics,
                }

    @classmethod
    async def find_optimal_us_products(cls, ingredients: list):
        normalized_ingredients = cls._normalize_ingredient_list(ingredients)
        if not normalized_ingredients:
            return {"match_type": "NONE", "recommendations": []}

        search_query = "+AND+".join(
            [
                f'(openfda.substance_name:"{ingr}"+OR+openfda.generic_name:"{ingr}")'
                for ingr in normalized_ingredients
            ]
        )
        url = (
            f"{cls._FDA_LABEL_URL}"
            f"?search={search_query}+AND+{cls._OTC_FILTER}"
            f"&limit=20"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url)
                if res.status_code == 200 and res.json().get("results"):
                    results = res.json().get("results", [])
                    homeopathic_ndcs = await cls._build_homeopathic_ndc_set(results, client)
                    filtered_results = []
                    for item in results:
                        ndc = cls._extract_product_ndc(item)
                        if ndc and ndc in homeopathic_ndcs:
                            continue
                        filtered_results.append(item)
                    results = filtered_results
                    products = [cls._to_product_payload(item) for item in results]
                    products = cls._dedupe_products(products)[:10]

                    if products:
                        products = await cls._attach_korean_summaries(products)
                        products = await AmazonRankService.enrich_and_sort_products(
                            products
                        )

                    return {
                        "match_type": "FULL_MATCH",
                        "description": "요청한 모든 성분을 포함한 OTC 제품을 찾았습니다.",
                        "recommendations": products,
                    }
            except Exception as e:
                logger.error(f"Full match search error: {e}")

        component_recommendations = await asyncio.gather(
            *[
                cls.get_us_otc_products_by_ingredient(ingr, limit=20)
                for ingr in normalized_ingredients
            ]
        )

        candidate_map = {}
        for rec in component_recommendations:
            for prod in rec.get("products", []):
                key = cls._product_identity_key(prod)
                if key not in candidate_map:
                    candidate_map[key] = prod

        cross_ingredient_recommendations = []
        for product in candidate_map.values():
            combined_text = (
                f"{product.get('brand_name', '')} "
                f"{product.get('active_ingredient', '')}"
            )
            matched_ingredients = [
                ingr
                for ingr in normalized_ingredients
                if cls._contains_ingredient(combined_text, ingr)
            ]
            if len(matched_ingredients) >= 2:
                cross_ingredient_recommendations.append(
                    {
                        **product,
                        "matched_ingredients": matched_ingredients,
                        "match_count": len(matched_ingredients),
                    }
                )

        cross_ingredient_recommendations.sort(
            key=lambda x: (
                -x.get("match_count", 0),
                x.get("amazon_rank_value")
                if isinstance(x.get("amazon_rank_value"), int)
                else 10**12,
                x.get("brand_name", ""),
            )
        )

        for rec in component_recommendations:
            rec["products"] = rec.get("products", [])[:3]

        return {
            "match_type": "COMPONENT_MATCH",
            "description": "완전 일치 제품이 없어 성분별 대체 후보를 제공합니다.",
            "recommendations": component_recommendations,
            "cross_ingredient_recommendations": cross_ingredient_recommendations[:10],
        }
    @classmethod
    def generate_pharmacist_card(
        cls, ingredients: list, dosage_form: str = "Tablet/Capsule"
    ):
        ingr_str = ", ".join(ingredients)
        return {
            "title": "Pharmacist Consultation Card",
            "active_ingredients": ingredients,
            "desired_dosage_form": dosage_form,
            "english_guide": [
                f"I am looking for an OTC product that contains: {ingr_str}.",
                f"If possible, I prefer this dosage form: {dosage_form}.",
                "Please recommend the closest available options and check interactions or cautions.",
            ],
        }
