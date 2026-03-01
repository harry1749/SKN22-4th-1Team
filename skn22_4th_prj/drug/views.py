import asyncio
import logging
import re

try:
    from rest_framework.views import APIView
    from rest_framework.response import Response
except ModuleNotFoundError:
    from django.http import JsonResponse
    from django.views import View

    class APIView(View):
        pass

    def Response(data, status=200):
        return JsonResponse(data, status=status, safe=not isinstance(data, list))

from services.supabase_service import SupabaseService
from services.map_service import MapService
from services.drug_service import DrugService
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class DrugSearchView(APIView):
    async def get(self, request):
        params = getattr(request, "query_params", request.GET)
        query = params.get("q", "").strip()
        if not query:
            return Response([])

        from services.supabase_service import SupabaseService
        # Supabase API를 통한 약품 검색
        results = await SupabaseService.search_drugs(query)

        return Response(results)


class UsRoadmapView(APIView):
    @staticmethod
    def _normalize_ingredients(raw_ingredients):
        parsed = []
        seen = set()
        for raw in raw_ingredients or []:
            if not raw:
                continue
            chunks = re.split(r",|/|;|\bAND\b|\bWITH\b|\+", str(raw), flags=re.IGNORECASE)
            for chunk in chunks:
                token = chunk.strip().upper()
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
                parsed.append(token)
        return parsed

    async def get(self, request):
        params = getattr(request, "query_params", request.GET)
        ingredients = self._normalize_ingredients(params.getlist("ingredients"))
        kr_dosage_mg = float(params.get("kr_dosage_mg", 0.0))

        if not ingredients:
            return Response({"error": "ingredients are required"}, status=400)

        sorted_ingrs = sorted(
            [ingr.strip().upper() for ingr in ingredients if ingr.strip()]
        )
        ingrs_str = "_".join(sorted_ingrs)
        cache_key = f"roadmap_{kr_dosage_mg}_{ingrs_str}"

        try:
            cached_data = await SupabaseService.get_roadmap_cache(cache_key)
            if cached_data:
                return Response(
                    {
                        "requested_ingredients": ingredients,
                        "mapping_result": cached_data.get("mapping_result", {}),
                        "pharmacist_card": cached_data.get("pharmacist_card", {}),
                        "dosage_warnings": cached_data.get("dosage_warnings", []),
                    }
                )
        except Exception as e:
            logger.error(f"[Roadmap Cache Read Error]: {e}")

        try:
            mapping_result = await MapService.find_optimal_us_products(ingredients)
            pharmacist_card = MapService.generate_pharmacist_card(ingredients)
            dosage_warnings = []

            if kr_dosage_mg > 0 and mapping_result.get("recommendations"):
                match_type = mapping_result.get("match_type")
                if match_type == "FULL_MATCH":
                    for rec in mapping_result["recommendations"]:
                        active_ingr = rec.get("active_ingredient", "")
                        warn_info = DrugService.compare_dosage_and_warn(
                            active_ingr, kr_dosage_mg
                        )
                        if warn_info.get("us_dosage_mg") is not None:
                            dosage_warnings.append(
                                {
                                    "brand_name": rec.get("brand_name"),
                                    "warning_info": warn_info,
                                }
                            )
                elif match_type == "COMPONENT_MATCH":
                    recs = mapping_result.get("recommendations", [])
                    if recs:
                        first_ingr_recs = recs[0].get("products", [])
                        for rec in first_ingr_recs[:3]:
                            active_ingr = rec.get("active_ingredient", "")
                            warn_info = DrugService.compare_dosage_and_warn(
                                active_ingr, kr_dosage_mg
                            )
                            if warn_info.get("us_dosage_mg") is not None:
                                dosage_warnings.append(
                                    {
                                        "brand_name": rec.get("brand_name"),
                                        "warning_info": warn_info,
                                    }
                                )

            # Save to Cache
            asyncio.create_task(
                SupabaseService.set_roadmap_cache(
                    query_text=cache_key,
                    mapping_result=mapping_result,
                    pharmacist_card=pharmacist_card,
                    dosage_warnings=dosage_warnings,
                )
            )

            return Response(
                {
                    "requested_ingredients": ingredients,
                    "mapping_result": mapping_result,
                    "pharmacist_card": pharmacist_card,
                    "dosage_warnings": dosage_warnings,
                }
            )
        except Exception as e:
            logger.error(f"Error generating US Roadmap: {e}")
            return Response({"error": str(e)}, status=500)
