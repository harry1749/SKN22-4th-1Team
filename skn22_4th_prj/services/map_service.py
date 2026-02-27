import os
import logging
import httpx
import re
import asyncio
from services.ai_service_v2 import AIService

logger = logging.getLogger(__name__)


class MapService:
    @classmethod
    async def find_nearby_pharmacies(cls, lat: float, lng: float):
        return []

    @classmethod
    async def get_us_otc_products_by_ingredient(cls, ingredient: str):
        """특정 주성분이 포함된 미국 내 가용 OTC 제품명(Brand Name) 및 기전 리스트업"""
        OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
        url = (
            f"https://api.fda.gov/drug/label.json"
            f'?search=(openfda.substance_name:"{ingredient}"+OR+openfda.generic_name:"{ingredient}")'
            f"+AND+{OTC_FILTER}"
            f"&limit=50"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    return {
                        "ingredient": ingredient,
                        "products": [],
                        "message": "FDA API에서 제품을 찾을 수 없습니다.",
                    }

                data = response.json().get("results", [])

                products_info = []
                for item in data:
                    openfda = item.get("openfda", {})
                    brand_name = openfda.get("brand_name", [])
                    if not brand_name:
                        continue

                    purpose = item.get("purpose", ["Description not available."])[0]
                    active_ingr = item.get("active_ingredient", ["Unknown"])[0]

                    products_info.append(
                        {
                            "brand_name": brand_name[0],
                            "purpose": purpose,
                            "active_ingredient": active_ingr,
                        }
                    )

                unique_products = {
                    prod["brand_name"]: prod for prod in products_info
                }.values()
                sorted_products = sorted(
                    list(unique_products), key=lambda x: x["brand_name"]
                )[:5]

                return {
                    "ingredient": ingredient,
                    "products": sorted_products,
                    "count": len(sorted_products),
                }
            except Exception as e:
                logger.error(f"Error fetching FDA products for '{ingredient}': {e}")
                return {"ingredient": ingredient, "products": [], "error": str(e)}

    @classmethod
    async def find_optimal_us_products(cls, ingredients: list):
        if not ingredients:
            return {"match_type": "NONE", "recommendations": []}

        OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
        search_query = "+AND+".join(
            [
                f'(openfda.substance_name:"{ingr}"+OR+openfda.generic_name:"{ingr}")'
                for ingr in ingredients
            ]
        )
        url = f"https://api.fda.gov/drug/label.json?search={search_query}+AND+{OTC_FILTER}&limit=10"

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url)
                if res.status_code == 200 and res.json().get("results"):
                    results = res.json().get("results", [])
                    products = []
                    for item in results:
                        brand_name = item.get("openfda", {}).get(
                            "brand_name", ["Unknown"]
                        )[0]
                        purpose = item.get("purpose", ["No purpose specified."])[0]
                        active_ingr = item.get("active_ingredient", ["Unknown"])[0]
                        products.append(
                            {
                                "brand_name": brand_name,
                                "purpose": purpose,
                                "active_ingredient": active_ingr,
                            }
                        )

                    purposes_to_translate = [p["purpose"] for p in products]
                    translated_purposes = await AIService.translate_purposes(
                        purposes_to_translate
                    )

                    for i, prod in enumerate(products):
                        if i < len(translated_purposes):
                            prod["purpose"] = translated_purposes[i]

                    return {
                        "match_type": "FULL_MATCH",
                        "description": "모든 성분이 일치하는 미국 복합제 우선 추천",
                        "recommendations": products,
                    }
            except Exception as e:
                logger.error(f"Full match search error: {e}")

        component_recommendations = await asyncio.gather(
            *[cls.get_us_otc_products_by_ingredient(ingr) for ingr in ingredients]
        )

        return {
            "match_type": "COMPONENT_MATCH",
            "description": "완전 일치 복합제가 없어 각 성분별 단일제 그룹을 큐레이션하여 추천합니다.",
            "recommendations": component_recommendations,
        }

    @classmethod
    def generate_pharmacist_card(
        cls, ingredients: list, dosage_form: str = "Tablet/Capsule"
    ):
        ingr_str = ", ".join(ingredients)
        card = {
            "title": "Pharmacist Communication Card",
            "active_ingredients": ingredients,
            "desired_dosage_form": dosage_form,
            "english_guide": [
                f"Hello, I am looking for an OTC product containing these active ingredients: {ingr_str}.",
                f"I prefer the '{dosage_form}' form if available.",
                "Could you please recommend the closest match you have in stock?",
            ],
        }
        return card
