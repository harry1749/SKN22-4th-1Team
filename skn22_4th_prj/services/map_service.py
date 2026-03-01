import asyncio
import logging
import re

import httpx

from services.ai_service_v2 import AIService
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class MapService:
    _FDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
    _OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'

    @classmethod
    async def find_nearby_pharmacies(cls, lat: float, lng: float):
        return []

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
    def _to_product_payload(cls, item: dict) -> dict:
        openfda = item.get("openfda") or {}
        brand_name = (openfda.get("brand_name") or ["Unknown"])[0]
        purpose = (item.get("purpose") or ["No purpose specified."])[0]

        return {
            "brand_name": brand_name,
            "purpose": purpose,
            "active_ingredient": cls._extract_active_ingredient_text(item),
        }

    @classmethod
    def _contains_ingredient(cls, text: str, ingredient: str) -> bool:
        if not text or not ingredient:
            return False
        return ingredient.upper() in text.upper()

    @classmethod
    async def get_us_otc_products_by_ingredient(cls, ingredient: str, limit: int = 5):
        """Fetch OTC products containing the given ingredient from openFDA."""
        normalized_ingredient = cls._normalize_ingredient(ingredient)
        if not normalized_ingredient:
            return {"ingredient": ingredient, "products": [], "count": 0}

        url = (
            f"{cls._FDA_LABEL_URL}"
            f'?search=(openfda.substance_name:"{normalized_ingredient}"'
            f'+OR+openfda.generic_name:"{normalized_ingredient}")+AND+{cls._OTC_FILTER}'
            f"&limit=100"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    return {
                        "ingredient": normalized_ingredient,
                        "products": [],
                        "message": "FDA API에서 제품을 찾지 못했습니다.",
                    }

                data = response.json().get("results", [])
                products_info = []
                for item in data:
                    openfda = item.get("openfda") or {}
                    if not openfda.get("brand_name"):
                        continue
                    products_info.append(cls._to_product_payload(item))

                unique_products = {
                    (
                        (prod.get("brand_name") or "").strip().upper(),
                        (prod.get("active_ingredient") or "").strip().upper(),
                    ): prod
                    for prod in products_info
                }.values()

                sorted_products = sorted(
                    list(unique_products), key=lambda x: x.get("brand_name", "")
                )[: max(limit, 1)]

                return {
                    "ingredient": normalized_ingredient,
                    "products": sorted_products,
                    "count": len(sorted_products),
                }
            except Exception as e:
                logger.error(
                    f"Error fetching FDA products for '{normalized_ingredient}': {e}"
                )
                return {
                    "ingredient": normalized_ingredient,
                    "products": [],
                    "error": str(e),
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
                    products = [cls._to_product_payload(item) for item in results]
                    products = list(
                        {
                            (
                                (p.get("brand_name") or "").strip().upper(),
                                (p.get("active_ingredient") or "").strip().upper(),
                            ): p
                            for p in products
                        }.values()
                    )[:10]

                    if products:
                        try:
                            purposes_to_translate = [p["purpose"] for p in products]
                            translated_purposes = await AIService.translate_purposes(
                                purposes_to_translate
                            )
                            for i, prod in enumerate(products):
                                if i < len(translated_purposes):
                                    prod["purpose"] = translated_purposes[i]
                        except Exception as translate_error:
                            logger.warning(
                                "Purpose translation failed, returning English purposes: %s",
                                translate_error,
                            )

                    return {
                        "match_type": "FULL_MATCH",
                        "description": "요청한 모든 성분이 포함된 OTC 제품을 찾았습니다.",
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
                key = (
                    (prod.get("brand_name") or "").strip().upper(),
                    (prod.get("active_ingredient") or "").strip().upper(),
                )
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
            key=lambda x: (-x.get("match_count", 0), x.get("brand_name", ""))
        )

        for rec in component_recommendations:
            rec["products"] = rec.get("products", [])[:5]

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
        card = {
            "title": "약사 상담 카드",
            "active_ingredients": ingredients,
            "desired_dosage_form": dosage_form,
            "english_guide": [
                f"다음 성분이 포함된 OTC 제품을 찾고 있습니다: {ingr_str}",
                f"가능하면 '{dosage_form}' 제형을 선호합니다.",
                "재고 중 가장 가까운 제품을 추천해 주세요.",
            ],
        }
        return card
