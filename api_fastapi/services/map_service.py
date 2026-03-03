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
        """
        # Google Maps Nearby Search API 활용 (현재 사용자 요청으로 주석 처리됨)
        # api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        # url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        # params = {
        #     "location": f"{lat},{lng}",
        #     "radius": 1500, # 1.5km
        #     "type": "pharmacy",
        #     "key": api_key,
        #     "language": "ko"
        # }
        # async with httpx.AsyncClient() as client:
        #     resp = await client.get(url, params=params)
        #     return resp.json().get("results", [])
        """
        return []

    @classmethod
    async def get_us_otc_products_by_ingredient(cls, ingredient: str):
        """
        특정 주성분이 포함된 미국 내 가용 OTC 제품명(Brand Name) 및 기전 전수 리스트업
        """
        # Search using generic_name or substance_name or active_ingredient
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
                metadata = match.get('metadata', {})
                brand_name = metadata.get('brand_name', '')
                if not brand_name:
                    continue

                purpose = metadata.get('purpose', "Description not available.")
                active_ingr = metadata.get('active_ingredient', "Unknown")

                products_info.append({
                    "brand_name": brand_name,
                    "purpose": purpose,
                    "active_ingredient": active_ingr
                })

            # 중복 제거 (brand_name 기준)
            unique_products = {prod['brand_name']: prod for prod in products_info}.values()
            sorted_products = sorted(list(unique_products), key=lambda x: x['brand_name'])

            return {
                "ingredient": ingredient,
                "products": sorted_products,
                "count": len(sorted_products)
            }
        except Exception as e:
            logger.error(f"Error fetching products for '{ingredient}' via Pinecone: {e}")
            return {"ingredient": ingredient, "products": [], "error": str(e)}

    @classmethod
    async def find_optimal_us_products(cls, ingredients: list):
        """
        복합 주성분 제품(Combination Drugs) 분해 및 듀얼 매치 알고리즘
        1단계: Full Match (모든 성분이 포함된 복합제 검색)
        2단계: Component Match (없을 경우, 개별 성분별 단일제 큐레이션)
        """
        if not ingredients:
            return {"match_type": "NONE", "recommendations": []}

        # Full Match 검색 시도 (단순화를 위해 각 성분이 모두 포함되는지 AND 연산 필터 생성)
        # However, metadata values are scalar, so checking if multiple values exist in a single record
        # might require regex or text search if they are flat strings. E.g. active_ingredient contains both.
        # Since active_ingredient is a string, we can rely on semantic search + checking metadata locally.
        query_text = " and ".join(ingredients)
        
        try:
             # We query with the combined string and take top 20, then filter locally for full match
             matches = await PineconeService.search(query_text=query_text, top_k=20)
             
             products = []
             for match in matches:
                 metadata = match.get('metadata', {})
                 # We need to find products where ALL ingredients are present in generic_name or substance_name or active_ingredient
                 text_to_search = str(metadata.get('generic_name', '')) + " " + str(metadata.get('substance_name', '')) + " " + str(metadata.get('active_ingredient', ''))
                 text_to_search = text_to_search.lower()
                 
                 has_all = all(ingr.lower() in text_to_search for ingr in ingredients)
                 
                 if has_all:
                     brand_name = metadata.get('brand_name', 'Unknown')
                     if brand_name == 'Unknown': continue
                     
                     products.append({
                        "brand_name": brand_name, 
                        "purpose": metadata.get('purpose', 'No purpose specified.'),
                        "active_ingredient": metadata.get('active_ingredient', 'Unknown')
                     })
                     
             # 중복 제거
             unique_products = {prod['brand_name']: prod for prod in products}.values()
             products = list(unique_products)[:10]

             if products:
                 return {
                     "match_type": "FULL_MATCH",
                     "description": "모든 성분이 일치하는 미국 복합제 우선 추천",
                     "recommendations": products
                 }
        except Exception as e:
            logger.error(f"Full match search error: {e}")
        
        # Component Match (Full Match 실패 또는 데이터 부족 시 개별 검색)
        component_recommendations = await asyncio.gather(
            *[cls.get_us_otc_products_by_ingredient(ingr) for ingr in ingredients]
        )
            
        return {
            "match_type": "COMPONENT_MATCH",
            "description": "완전 일치 복합제가 없어 각 성분별 단일제 그룹을 큐레이션하여 추천합니다.",
            "recommendations": component_recommendations
        }

    @classmethod
    def generate_pharmacist_card(cls, ingredients: list, dosage_form: str = "Tablet/Capsule"):
        """
        약사 소통 브릿지 (Pharmacist Comm. Card) 생성
        """
        ingr_str = ", ".join(ingredients)
        
        card = {
            "title": "Pharmacist Communication Card",
            "active_ingredients": ingredients,
            "desired_dosage_form": dosage_form,
            "english_guide": [
                f"Hello, I am looking for an OTC product containing these active ingredients: {ingr_str}.",
                f"I prefer the '{dosage_form}' form if available.",
                "Could you please recommend the closest match you have in stock?"
            ]
        }
        return card