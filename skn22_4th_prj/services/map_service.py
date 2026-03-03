import os
import logging
import httpx
import re
import asyncio
from .pinecone_service import PineconeService

logger = logging.getLogger(__name__)


class MapService:
    @classmethod
    async def find_nearby_pharmacies(cls, lat: float, lng: float):
        return []

    @classmethod
    async def get_us_otc_products_by_ingredient(cls, ingredient: str):
        """특정 주성분이 포함된 미국 내 가용 OTC 제품명(Brand Name) 및 기전 전수 리스트업"""
        
        filter_dict = {
            "$or": [
                {"substance_name": {"$eq": ingredient.upper()}},
                {"generic_name": {"$eq": ingredient.upper()}},
                {"active_ingredient": {"$in": [ingredient.upper()]}}
            ]
        }
        
        try:
            matches = await PineconeService.search(query_text=ingredient, filter_dict=filter_dict, top_k=5)

            if not matches:
                return {"ingredient": ingredient, "products": [], "message": "Pinecone 검색 결과에서 제품을 찾을 수 없습니다."}

            products_info = []
            for match in matches:
                metadata = match.get("metadata", {})
                brand_name = metadata.get("brand_name", "")
                if not brand_name:
                    continue

                purpose = metadata.get("purpose", "Description not available.")
                active_ingr = metadata.get("active_ingredient", "Unknown")

                products_info.append({
                    "brand_name": brand_name,
                    "purpose": purpose,
                    "active_ingredient": active_ingr,
                })

            unique_products = {prod["brand_name"]: prod for prod in products_info}.values()
            sorted_products = sorted(list(unique_products), key=lambda x: x["brand_name"])

            return {
                "ingredient": ingredient,
                "products": sorted_products,
                "count": len(sorted_products),
            }
        except Exception as e:
            logger.error(f"Error fetching products for '{ingredient}' via Pinecone: {e}")
            return {"ingredient": ingredient, "products": [], "error": str(e)}

    @classmethod
    async def find_optimal_us_products(cls, ingredients: list):
        """복합 주성분 제품 분해 및 듀얼 매치 알고리즘"""
        if not ingredients:
            return {"match_type": "NONE", "recommendations": []}

        query_text = " and ".join(ingredients)
        
        try:
             matches = await PineconeService.search(query_text=query_text, top_k=20)
             
             products = []
             for match in matches:
                 metadata = match.get("metadata", {})
                 text_to_search = str(metadata.get('generic_name', '')) + " " + str(metadata.get('substance_name', '')) + " " + str(metadata.get('active_ingredient', ''))
                 text_to_search = text_to_search.lower()
                 
                 has_all = all(ingr.lower() in text_to_search for ingr in ingredients)
                 
                 if has_all:
                     brand_name = metadata.get("brand_name", "Unknown")
                     if brand_name == 'Unknown': continue
                     
                     products.append({
                        "brand_name": brand_name, 
                        "purpose": metadata.get("purpose", "No purpose specified."),
                        "active_ingredient": metadata.get("active_ingredient", "Unknown")
                     })
                     
             unique_products = {prod["brand_name"]: prod for prod in products}.values()
             products = list(unique_products)[:10]

             if products:
                 return {
                     "match_type": "FULL_MATCH",
                     "description": "모든 성분이 일치하는 미국 복합제 우선 추천",
                     "recommendations": products
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
