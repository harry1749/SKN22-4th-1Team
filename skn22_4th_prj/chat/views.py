import os
import logging
from collections import Counter
from functools import lru_cache

from django.shortcuts import render
from django.http import HttpResponse, JsonResponse

from graph_agent.builder_v2 import build_graph

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()


def _normalize_dur_entries(dur_entries):
    normalized = []
    for item in dur_entries or []:
        if not isinstance(item, dict):
            continue

        dur_type = str(item.get("type") or "주의").strip()
        ingredient = str(
            item.get("ingr_name") or item.get("ingredient") or item.get("name") or ""
        ).strip()
        warning = str(
            item.get("warning_msg") or item.get("warning") or item.get("message") or ""
        ).strip()

        normalized.append(
            {
                "type": dur_type or "주의",
                "ingredient": ingredient,
                "warning": warning,
            }
        )
    return normalized


def _guidance_by_dur_type(dur_type):
    t = (dur_type or "").strip()
    t_lower = t.lower()
    is_combined = ("interaction" in t_lower) or ("combined" in t_lower) or ("병용" in t)
    is_contra = ("contra" in t_lower) or ("금기" in t)
    is_caution = ("caution" in t_lower) or ("주의" in t)

    if is_combined and is_contra:
        return "다른 성분과의 병용이 제한된다고 안내되고 있습니다."
    if is_combined and is_caution:
        return "다른 성분과 병용 시 이상반응 가능성이 있다고 안내되고 있습니다."
    if "pregnan" in t_lower or "임부" in t or "임신" in t:
        return "임신 중 사용 제한 또는 주의가 필요하다고 안내되고 있습니다."
    if "elder" in t_lower or "geriatric" in t_lower or "노인" in t or "고령" in t:
        return "고령자에서 주의가 필요하다고 안내되고 있습니다."
    if (
        "adolescent" in t_lower
        or "pediatric" in t_lower
        or "age" in t_lower
        or "연령" in t
        or "청소년" in t
        or "소아" in t
    ):
        return "연령 기준에 따른 사용 제한 또는 주의가 필요하다고 안내되고 있습니다."
    if "dose" in t_lower or "용량" in t:
        return "권장 용량 범위를 준수해야 한다고 안내되고 있습니다."
    if "duration" in t_lower or "기간" in t:
        return "권장 투여 기간을 넘기지 않도록 안내되고 있습니다."
    if (
        "disease" in t_lower
        or "condition" in t_lower
        or "질환" in t
        or "kidney" in t_lower
        or "liver" in t_lower
        or "신장" in t
        or "간" in t
    ):
        return "기저 질환 여부에 따른 사용 주의가 필요하다고 안내되고 있습니다."
    if is_contra:
        return "금기 항목으로 안내되고 있어 전문가 확인이 권고됩니다."
    if is_caution:
        return "주의 항목으로 안내되고 있어 전문가 확인이 권고됩니다."
    return "개인 상태에 따라 적용 기준이 달라질 수 있다고 안내되고 있습니다."


def _build_dur_summary(dur_entries, limit=5):
    entries = _normalize_dur_entries(dur_entries)
    if not entries:
        return {
            "count": 0,
            "headline": "",
            "type_summary": "",
            "lines": [],
            "has_more": False,
        }

    type_counter = Counter(entry["type"] for entry in entries if entry["type"])
    top_types = ", ".join(
        [f"{dur_type} {count}건" for dur_type, count in type_counter.most_common(3)]
    )

    lines = []
    for entry in entries[:limit]:
        ingredient = entry["ingredient"] or "해당 성분"
        guidance = _guidance_by_dur_type(entry["type"])
        line = (
            f"{ingredient}: DUR 기준상 '{entry['type']}' 항목으로 안내되고 있습니다. "
            f"{guidance} 세부 적용 여부는 의사 또는 약사 상담을 통해 확인이 권고됩니다."
        )
        if entry["warning"]:
            warning = entry["warning"]
            if len(warning) > 100:
                warning = warning[:100].rstrip() + "..."
            line = f"{line} ({warning})"
        lines.append(line)

    return {
        "count": len(entries),
        "headline": f"DUR 안내 항목 {len(entries)}건이 확인되었습니다.",
        "type_summary": top_types,
        "lines": lines,
        "has_more": len(entries) > limit,
    }


def home(request):
    user = request.session.get("supabase_user")
    return render(request, "index.html", {"user": user})


async def smart_search(request):
    query = request.GET.get("q") or request.POST.get("q")
    if not query:
        return HttpResponse("<script>alert('검색어를 입력하세요.'); history.back();</script>")

    logger.info(f"LangGraph User Query: {query}")

    user_info = request.session.get("supabase_user")
    inputs = {"query": query, "user_info": user_info}

    try:
        result = await get_graph().ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return render(request, "error.html", {"message": f"처리 중 오류가 발생했습니다: {str(e)}"})

    category = result.get("category")
    final_answer = result.get("final_answer", "")

    if category == "symptom_recommendation":
        dur_data = result.get("dur_data", [])
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "ingredients_data": result.get("ingredients_data", []),
                "dur_details": dur_data,
                "dur_summary": _build_dur_summary(dur_data),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    if category == "product_request":
        fda = result.get("fda_data")
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
                "dur_summary": _build_dur_summary(dur),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    if category == "general_medical":
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "dur_details": [],
                "dur_summary": _build_dur_summary([]),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

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
