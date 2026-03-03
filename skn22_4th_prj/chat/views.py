import os
import logging
import asyncio
from asgiref.sync import sync_to_async
from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from graph_agent.builder_v2 import build_graph
from services.user_service import UserService
from services.drug_service import DrugService

logger = logging.getLogger(__name__)


def home(request):
    """홈 화면 - 세션에서 Supabase 유저 정보 확인"""
    user = request.session.get("supabase_user")
    return render(request, "index.html", {"user": user})


async def smart_search(request):
    """지능형 RAG 검색 (LangGraph 기반) - Supabase 세션 버전"""
    query = request.GET.get("q") or request.POST.get("q")
    if not query:
        return HttpResponse(
            "<script>alert('검색어를 입력하세요.'); history.back();</script>"
        )

    logger.info(f"LangGraph User Query: {query}")

    user_profile_data = None
    user_info = request.session.get("supabase_user")
    
    if user_info:
        try:
            profile = await UserService.get_profile(user_info)
            if profile:
                user_profile_data = {
                    "current_medications": profile.current_medications,
                    "allergies": profile.allergies,
                    "chronic_diseases": profile.chronic_diseases,
                }
        except Exception as e:
            logger.error(f"Error fetching user profile from Supabase: {e}")

    inputs = {"query": query, "user_profile": user_profile_data}
    try:
        graph = build_graph()
        result = await graph.ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return render(
            request, "error.html", {"message": f"처리 중 오류가 발생했습니다: {str(e)}"}
        )

    category = result.get("category")
    final_answer = result.get("final_answer", "")

    if category == "symptom_recommendation":
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "ingredients_data": result.get("ingredients_data", []),
                "dur_details": result.get("dur_data", []),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    elif category == "product_request":
        fda = result.get("drug_data")
        dur = result.get("dur_data", [])

        if not fda:
            return render(
                request,
                "error.html",
                {"message": final_answer or f"'{query}' 관련 정보를 찾을 수 없습니다."},
            )

        return render(
            request,
            "search_result.html",
            {
                "drug_name": fda.get("brand_name", query),
                "ingredients": fda.get("active_ingredients"),
                "us_guideline": fda,
                "kr_dur": dur,
                "dur_count": len(dur),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    elif category == "general_medical":
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "dur_details": [],
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    else:
        return render(
            request,
            "error.html",
            {"message": final_answer or "요청을 처리할 수 없습니다."},
        )


async def pharmacy_api(request):
    lat = float(request.GET.get("lat", 0))
    lng = float(request.GET.get("lng", 0))
    from services.map_service import MapService

    try:
        results = await MapService.find_nearby_pharmacies(lat, lng)
        return JsonResponse({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Error fetching pharmacies: {e}")
        return JsonResponse({"status": "error", "message": str(e)})
