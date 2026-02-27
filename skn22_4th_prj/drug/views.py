import asyncio
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import UnifiedDrugInfo
from django.db.models import Q
from services.supabase_service import SupabaseService
from services.map_service import MapService
from services.drug_service import DrugService

logger = logging.getLogger(__name__)


class DrugSearchView(APIView):
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if not query:
            return Response([])

        results = UnifiedDrugInfo.objects.filter(
            Q(item_name__icontains=query) | Q(entp_name__icontains=query)
        ).values("item_name", "entp_name")[:20]

        return Response(list(results))


class UsRoadmapView(APIView):
    async def get(self, request):
        ingredients = request.query_params.getlist("ingredients")
        kr_dosage_mg = float(request.query_params.get("kr_dosage_mg", 0.0))

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
