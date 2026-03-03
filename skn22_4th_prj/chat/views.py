import os
import logging
import asyncio
import json
import re
from collections import Counter
from functools import lru_cache

from django.shortcuts import render, redirect
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
        return "다른 성분과의 병용이 금지되는 항목으로 안내되고 있습니다."
    if is_combined and is_caution:
        return "다른 성분과 병용 시 이상반응 가능성이 있다고 안내되고 있습니다."
    if "pregnan" in t_lower or "임부" in t or "임신" in t:
        return "임신 중 사용 금지 또는 주의가 필요한 항목으로 안내되고 있습니다."
    if "elder" in t_lower or "geriatric" in t_lower or "노인" in t or "고령" in t:
        return "고령자에서 주의가 필요한 항목으로 안내되고 있습니다."
    if (
        "adolescent" in t_lower
        or "pediatric" in t_lower
        or "age" in t_lower
        or "연령" in t
        or "청소년" in t
        or "소아" in t
    ):
        return "연령 기준에 따른 사용 제한 또는 주의가 필요한 항목으로 안내되고 있습니다."
    if "dose" in t_lower or "용량" in t:
        return "권장 용량 범위를 준수해야 하는 항목으로 안내되고 있습니다."
    if "duration" in t_lower or "기간" in t:
        return "권장 투여 기간을 초과하지 않도록 안내되고 있습니다."
    if (
        "disease" in t_lower
        or "condition" in t_lower
        or "질환" in t
        or "kidney" in t_lower
        or "liver" in t_lower
        or "신장" in t
        or "간" in t
    ):
        return "기저 질환 여부에 따라 사용 주의가 필요한 항목으로 안내되고 있습니다."
    if is_contra:
        return "금기 항목으로 안내되고 있어 전문가 확인을 권고합니다."
    if is_caution:
        return "주의 항목으로 안내되고 있어 전문가 확인을 권고합니다."
    return "개인 상태에 따라 적용 기준이 달라질 수 있는 항목입니다."


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
            f"{ingredient}: DUR 기준 '{entry['type']}' 항목으로 안내되고 있습니다. "
            f"{guidance} 실제 적용 여부는 의사 또는 약사 상담을 통해 확인하시길 권고합니다."
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


_EMPTY_PROFILE_TOKENS = {"", "none", "없음", "없어요", "n/a", "na", "x"}
_SYMPTOM_KR_TO_EN = {
    "두통": "headache",
    "편두통": "migraine",
    "알레르기": "allergy",
    "기침": "cough",
    "감기": "common cold",
    "발열": "fever",
    "소화불량": "indigestion",
    "복통": "abdominal pain",
    "통증": "pain",
    "염좌": "sprain",
    "찰과상": "abrasion",
    "상처": "wound",
    "화상": "burn",
    "곤충교상": "insect bite",
}


def _to_profile_display(value: str, fallback: str = "Not provided") -> str:
    token = str(value or "").strip()
    if not token:
        return fallback
    if token.lower() in _EMPTY_PROFILE_TOKENS:
        return fallback
    return token


def _to_english_symptom(symptom: str, symptom_term: str = "", symptom_keyword: str = "") -> str:
    candidates = [symptom_term, symptom_keyword, symptom]

    for raw in candidates:
        token = str(raw or "").strip()
        if not token:
            continue

        # Already English/plain text.
        if re.search(r"[A-Za-z]", token) and not re.search(r"[가-힣]", token):
            return token

        # Exact mapping.
        if token in _SYMPTOM_KR_TO_EN:
            return _SYMPTOM_KR_TO_EN[token]

        # Phrase contains known Korean symptom term.
        for kr_term, en_term in _SYMPTOM_KR_TO_EN.items():
            if kr_term in token:
                return en_term

    return "unspecified symptom"


def _contains_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", str(text or "")))


async def _translate_profile_fields_to_english(meds: str, allergies: str, diseases: str):
    values = {
        "meds": str(meds or "").strip(),
        "allergies": str(allergies or "").strip(),
        "diseases": str(diseases or "").strip(),
    }
    translatable = {
        key: value
        for key, value in values.items()
        if value and value != "Not provided" and _contains_hangul(value)
    }
    if not translatable:
        return values

    try:
        from services.ai_service_v2 import AIService

        client = AIService.get_client()
        if not client:
            return values

        prompt = (
            "Translate the following user medical profile fields into clear English.\n"
            "Rules:\n"
            "- Preserve medicine names, strengths, bracketed codes, and parentheses.\n"
            "- Keep factual content intact.\n"
            "- If a field is already in English, keep it as-is.\n"
            "Return ONLY JSON with keys: meds, allergies, diseases."
        )
        res = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a medical translator."},
                {
                    "role": "user",
                    "content": f"{prompt}\n\n{json.dumps(values, ensure_ascii=False)}",
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = json.loads(res.choices[0].message.content or "{}")
        if isinstance(payload, dict):
            for key in ("meds", "allergies", "diseases"):
                translated = str(payload.get(key) or "").strip()
                if translated:
                    values[key] = translated
    except Exception as e:
        logger.warning(f"profile field translation failed: {e}")

    return values


async def _build_consultation_note(
    symptom: str,
    user_profile: dict,
    symptom_term: str = "",
    symptom_keyword: str = "",
):
    profile = user_profile if isinstance(user_profile, dict) else {}
    meds = _to_profile_display(profile.get("current_medications"))
    allergies = _to_profile_display(profile.get("allergies"))
    diseases = _to_profile_display(profile.get("chronic_diseases"))
    translated = await _translate_profile_fields_to_english(meds, allergies, diseases)
    meds = translated["meds"]
    allergies = translated["allergies"]
    diseases = translated["diseases"]
    pregnancy = "Yes" if bool(profile.get("is_pregnant")) else "No"
    symptom_text = _to_english_symptom(
        symptom=symptom,
        symptom_term=symptom_term,
        symptom_keyword=symptom_keyword,
    )

    memo_text = (
        f"Hello, I would like consultation for the symptom '{symptom_text}'.\n"
        f"- Current medications: {meds}\n"
        f"- Allergies: {allergies}\n"
        f"- Chronic conditions: {diseases}\n"
        f"- Pregnancy/Breastfeeding: {pregnancy}\n\n"
        "Please advise which OTC options are appropriate, which ingredients to avoid,\n"
        "and confirm dose, duration, and interaction risks."
    )

    return {
        "symptom": symptom_text,
        "meds": meds,
        "allergies": allergies,
        "diseases": diseases,
        "pregnancy": pregnancy,
        "memo_text": memo_text,
    }

def home(request):
    user = request.session.get("supabase_user")
    return render(
        request,
        "index.html",
        {
            "user": user,
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
        },
    )


def symptom_products_page(request):
    payload = request.session.get("last_symptom_result")
    if not isinstance(payload, dict):
        return redirect("chat:home")

    symptom = str(payload.get("symptom") or "").strip()
    ingredients_data = payload.get("ingredients_data", [])
    if not isinstance(ingredients_data, list):
        ingredients_data = []

    return render(
        request,
        "symptom_products_page.html",
        {
            "symptom": symptom,
            "answer": payload.get("answer", ""),
            "ingredients_data": ingredients_data,
        },
    )


async def smart_search(request):
    query = request.GET.get("q") or request.POST.get("q")
    if not query:
        return HttpResponse("<script>alert('寃?됱뼱瑜??낅젰?섏꽭??); history.back();</script>")

    logger.info(f"LangGraph User Query: {query}")

    user_info = request.session.get("supabase_user")
    inputs = {"query": query, "user_info": user_info}

    try:
        result = await get_graph().ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return render(request, "error.html", {"message": f"泥섎━ 以??ㅻ쪟媛 諛쒖깮?덉뒿?덈떎: {str(e)}"})

    category = result.get("category")
    final_answer = result.get("final_answer", "")

    if category == "symptom_recommendation":
        dur_data = result.get("dur_data", [])
        ingredients_data = result.get("ingredients_data", [])
        consultation_note = await _build_consultation_note(
            symptom=query,
            user_profile=result.get("user_profile") or {},
            symptom_term=result.get("symptom_term") or "",
            symptom_keyword=result.get("keyword") or "",
        )
        request.session["last_symptom_result"] = {
            "symptom": query,
            "answer": final_answer,
            "ingredients_data": ingredients_data,
            "dur_details": dur_data,
            "consultation_note": consultation_note,
        }
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "ingredients_data": ingredients_data,
                "dur_details": dur_data,
                "dur_summary": _build_dur_summary(dur_data),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
                "consultation_note": consultation_note,
            },
        )

    if category == "product_request":
        fda = result.get("fda_data")
        dur = result.get("dur_data", [])

        if not fda:
            return render(
                request,
                "general_result.html",
                {
                    "query": query,
                    "answer": final_answer
                    or f"Supabase에서 '{query}' 제품 정보를 찾지 못했습니다.",
                },
            )

        return render(
            request,
            "search_result.html",
            {
                "drug_name": fda.get("brand_name", query),
                "ingredients": fda.get("active_ingredients"),
                "search_query": query,
                "matched_query": result.get("keyword") or fda.get("brand_name", ""),
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
            "general_result.html",
            {
                "query": query,
                "answer": final_answer,
            },
        )

    return render(
        request,
        "error.html",
        {"message": final_answer or "?붿껌??泥섎━?????놁뒿?덈떎."},
    )


async def pharmacy_api(request):
    try:
        lat = float(request.GET.get("lat", 0))
        lng = float(request.GET.get("lng", 0))
        radius_m = int(request.GET.get("radius", 3000) or 3000)
        limit = int(request.GET.get("limit", 10) or 10)
    except (TypeError, ValueError):
        return JsonResponse(
            {"status": "error", "message": "Invalid coordinates or query params"},
            status=400,
        )

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JsonResponse(
            {"status": "error", "message": "Coordinates out of range"},
            status=400,
        )

    from services.map_service import MapService

    try:
        results = await MapService.find_nearby_pharmacies(
            lat=lat,
            lng=lng,
            radius_m=radius_m,
            limit=limit,
        )
        return JsonResponse({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Error fetching pharmacies: {e}")
        return JsonResponse({"status": "error", "message": str(e)})


async def symptom_products_api(request):
    raw = request.GET.get("ingredients", "").strip()
    symptom = (request.GET.get("symptom") or "").strip()
    debug_mode = str(request.GET.get("debug") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    ingredients = []
    seen_ingredients = set()
    for token in raw.split(","):
        ingredient = str(token or "").strip().upper()
        if not ingredient or ingredient in seen_ingredients:
            continue
        seen_ingredients.add(ingredient)
        ingredients.append(ingredient)
    if not ingredients:
        return JsonResponse({"status": "error", "message": "ingredients is required"}, status=400)

    from services.map_service import MapService
    from services.drug_service import DrugService
    from services.ai_service_v2 import AIService

    semaphore = asyncio.Semaphore(3)
    target_visible_products = 3
    candidate_fetch_limit = 10
    max_extra_component_lookups = 24
    max_excluded_reason_items = 4
    from services.user_service import UserService
    from services.ingredient_utils import canonicalize_ingredient_name

    def _is_combined_contra_text(text: str) -> bool:
        token = str(text or "").strip().lower()
        if not token:
            return False
        return (
            "병용금기" in token
            or "contraindicated combination" in token
            or ("contra" in token and "combined" in token)
        )

    def _is_pregnancy_text(text: str) -> bool:
        token = str(text or "").strip().lower()
        if not token:
            return False
        return (
            "임부" in token
            or "임신" in token
            or "수유" in token
            or "pregnan" in token
            or "lactat" in token
            or "breastfeeding" in token
        )

    def _extract_combined_partner_tokens(warning_text: str) -> list:
        raw = str(warning_text or "")
        if not raw:
            return []

        patterns = [
            r"병용금기\s*성분\s*:\s*([^\n]+)",
            r"contraindicated\s*(?:with)?\s*:\s*([^\n]+)",
        ]
        captured = ""
        for pattern in patterns:
            matched = re.search(pattern, raw, flags=re.IGNORECASE)
            if matched:
                captured = str(matched.group(1) or "").strip()
                break
        if not captured:
            return []

        chunks = re.split(r"[,/;|+]", captured)
        tokens = []
        seen = set()
        for chunk in chunks:
            token = str(chunk or "").strip().upper()
            token = re.sub(r"\([^)]*\)", "", token).strip()
            if not token:
                continue
            token = canonicalize_ingredient_name(token)
            token = str(token or "").strip().upper()
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _build_user_med_ingredient_set(profile: dict) -> set:
        sources = [
            str((profile or {}).get("main_ingr_eng") or "").strip(),
            str((profile or {}).get("current_medications") or "").strip(),
        ]
        token_set = set()
        for source in sources:
            if not source:
                continue
            pieces = re.split(r"[,/;|\n+]+", source)
            for piece in pieces:
                token = str(piece or "").strip().upper()
                token = re.sub(r"\([^)]*\)", "", token).strip()
                if not token:
                    continue
                token = canonicalize_ingredient_name(token)
                token = str(token or "").strip().upper()
                if len(token) >= 2:
                    token_set.add(token)
        return token_set

    user_profile = {}
    user_med_ingredients = set()
    user_is_pregnant = False
    try:
        user_info = request.session.get("supabase_user")
        if isinstance(user_info, dict) and user_info.get("id"):
            profile = await UserService.get_profile(user_info)
            user_profile = {
                "current_medications": str(
                    getattr(profile, "current_medications", "") or ""
                ).strip(),
                "main_ingr_eng": str(getattr(profile, "main_ingr_eng", "") or "").strip(),
                "is_pregnant": bool(getattr(profile, "is_pregnant", False)),
            }
            user_med_ingredients = _build_user_med_ingredient_set(user_profile)
            user_is_pregnant = bool(user_profile.get("is_pregnant"))
    except Exception as e:
        logger.warning(f"failed to load user profile in symptom_products_api: {e}")

    async def fetch_one(ingr):
        async with semaphore:
            diagnostics = {"ingredient": ingr}
            try:
                products_kwargs = {"limit": candidate_fetch_limit}
                if symptom:
                    products_kwargs["symptom"] = symptom

                products_task = asyncio.create_task(
                    MapService.get_us_otc_products_by_ingredient(
                        ingr, **products_kwargs
                    )
                )
                warning_task = asyncio.create_task(
                    DrugService.get_fda_warnings_by_ingr(ingr)
                )
                products_res, us_warning_raw = await asyncio.gather(
                    products_task,
                    warning_task,
                    return_exceptions=True,
                )
                if isinstance(products_res, Exception):
                    diagnostics["product_error"] = str(products_res)
                    products_res = {"products": [], "diagnostics": {"ingredient": ingr}}
                if isinstance(us_warning_raw, Exception):
                    diagnostics["warning_error"] = str(us_warning_raw)
                    us_warning_raw = None

                product_diagnostics = (
                    products_res.get("diagnostics", {})
                    if isinstance(products_res.get("diagnostics"), dict)
                    else {}
                )
                diagnostics = {**product_diagnostics, **diagnostics}
                return {
                    "ingredient": ingr,
                    "products": products_res.get("products", []),
                    "us_warning_raw": us_warning_raw,
                    "diagnostics": diagnostics,
                }
            except Exception as e:
                logger.warning(f"symptom_products_api failed for '{ingr}': {e}")
                return {
                    "ingredient": ingr,
                    "products": [],
                    "us_warning_raw": None,
                    "diagnostics": {**diagnostics, "error": str(e)},
                }

    async def attach_other_component_dur_guidance(payload_items):
        """Analyze other active components with KR DUR and filter risky products."""
        unique_components = set()
        for payload in payload_items or []:
            for product in payload.get("products") or []:
                for token in product.get("other_active_ingredients") or []:
                    name = str(token or "").strip().upper()
                    if name:
                        unique_components.add(name)

        extra_dur_map = {}
        if unique_components:
            capped_components = sorted(unique_components)[:max_extra_component_lookups]
            extra_dur_data = await DrugService.get_kr_dur_info(capped_components)
            for row in extra_dur_data or []:
                name = str((row or {}).get("ingredient") or "").strip().upper()
                kr_durs = (
                    (row or {}).get("kr_durs")
                    if isinstance((row or {}).get("kr_durs"), list)
                    else []
                )
                if not name or not kr_durs:
                    continue
                all_types = []
                hard_block_types = []
                risk_summary = ""
                matched_partners = []
                for dur in kr_durs:
                    if not isinstance(dur, dict):
                        continue
                    dur_type = str(dur.get("type") or "").strip()
                    warning_text = str(dur.get("warning") or "").strip()
                    if dur_type:
                        all_types.append(dur_type)
                    if not risk_summary:
                        risk_summary = warning_text

                    merged_text = f"{dur_type} {warning_text}"

                    if _is_pregnancy_text(merged_text):
                        if user_is_pregnant:
                            hard_block_types.append(dur_type or "임부금기")
                            if warning_text:
                                risk_summary = warning_text
                        continue

                    if _is_combined_contra_text(merged_text):
                        if user_med_ingredients:
                            partner_tokens = _extract_combined_partner_tokens(warning_text)
                            matched = [p for p in partner_tokens if p in user_med_ingredients]
                            if matched:
                                hard_block_types.append(dur_type or "병용금기")
                                matched_partners.extend(matched[:3])
                                risk_summary = (
                                    f"현재 복용 성분과 병용금기 성분이 일치합니다: "
                                    f"{', '.join(matched[:3])}"
                                )
                        # If current meds are empty or no direct match, do not hard-block.
                        continue

                all_types = sorted(set([x for x in all_types if x]))
                hard_block_types = sorted(set([x for x in hard_block_types if x]))
                matched_partners = sorted(set([x for x in matched_partners if x]))
                if len(risk_summary) > 140:
                    risk_summary = risk_summary[:137].rstrip() + "..."

                extra_dur_map[name] = {
                    "has_dur_risk": bool(hard_block_types),
                    "dur_risk_types": hard_block_types[:3] if hard_block_types else all_types[:3],
                    "dur_risk_summary": risk_summary,
                    "dur_all_types": all_types[:5],
                    "matched_partners": matched_partners[:3],
                }
        for payload in payload_items or []:
            products = (
                payload.get("products")
                if isinstance(payload.get("products"), list)
                else []
            )
            safe_products = []
            excluded_products = []

            for product in products:
                components = (
                    product.get("other_active_components")
                    if isinstance(product.get("other_active_components"), list)
                    else []
                )
                enriched_components = []
                risk_components = []

                for comp in components:
                    if not isinstance(comp, dict):
                        continue
                    comp_name = str(comp.get("name") or "").strip().upper()
                    meta = extra_dur_map.get(comp_name, {})
                    has_risk = bool(meta.get("has_dur_risk"))
                    if has_risk:
                        risk_types = list(meta.get("dur_risk_types", []))
                        matched = [x for x in (meta.get("matched_partners") or []) if x]
                        if matched:
                            risk_types.append(f"복용약매칭:{', '.join(matched[:3])}")
                        risk_components.append(
                            {
                                "name": comp_name,
                                "types": risk_types,
                                "summary": meta.get("dur_risk_summary", ""),
                                "matched_partners": meta.get("matched_partners", []),
                            }
                        )

                    enriched_components.append(
                        {
                            **comp,
                            "has_dur_risk": has_risk,
                            "dur_risk_types": meta.get("dur_risk_types", []),
                            "dur_risk_summary": meta.get("dur_risk_summary", ""),
                        }
                    )

                if enriched_components:
                    product["other_active_components"] = enriched_components

                if risk_components:
                    preview_names = ", ".join([c["name"] for c in risk_components[:3]])
                    product["has_other_active_dur_risk"] = True
                    product["other_active_dur_notice"] = (
                        f"추가 주성분 금기(DUR) 성분으로 제외: {preview_names}"
                    )
                    excluded_products.append(
                        {
                            "brand_name": str(product.get("brand_name") or "Unknown Product"),
                            "risk_components": risk_components[:3],
                            "reason": product["other_active_dur_notice"],
                        }
                    )
                    continue

                product["has_other_active_dur_risk"] = False
                safe_products.append(product)

            payload["products"] = safe_products[:target_visible_products]
            payload["excluded_products_due_to_other_component_dur"] = excluded_products[
                :max_excluded_reason_items
            ]
            payload["other_component_dur_filtered_count"] = len(excluded_products)

            if excluded_products:
                if payload["products"]:
                    payload["other_component_dur_notice"] = (
                        f"추가 주성분 금기(DUR) 제품 {len(excluded_products)}개를 제외하고 "
                        f"복용 가능한 후보 {len(payload['products'])}개를 추천합니다."
                    )
                else:
                    payload["other_component_dur_notice"] = (
                        "후보 제품이 추가 주성분 금기(DUR)로 제외되었습니다."
                    )

            diagnostics = payload.get("diagnostics")
            if not isinstance(diagnostics, dict):
                diagnostics = {}
            diagnostics["other_component_dur_filtered"] = len(excluded_products)
            diagnostics["other_component_dur_kept"] = len(payload["products"])
            payload["diagnostics"] = diagnostics

    items = await asyncio.gather(*[fetch_one(ingr) for ingr in ingredients])
    extra_component_task = asyncio.create_task(attach_other_component_dur_guidance(items))

    raw_warning_map = {
        item["ingredient"]: item.get("us_warning_raw")
        for item in items
        if item.get("ingredient") and item.get("us_warning_raw")
    }
    summarized_map = await AIService.bulk_summarize_fda_warnings(raw_warning_map)
    await extra_component_task

    for item in items:
        ingredient = item.get("ingredient")
        item["us_warning"] = summarized_map.get(ingredient) if ingredient else None
        item.pop("us_warning_raw", None)

    with_products = []
    empty_products = []
    for item in items:
        ingredient = str(item.get("ingredient") or "")
        products = item.get("products") or []
        if products:
            with_products.append(ingredient)
        else:
            empty_products.append(ingredient)

    logger.warning(
        "symptom_products_api summary: symptom='%s' requested=%d with_products=%d without_products=%d max_visible=3",
        symptom,
        len(ingredients),
        len(with_products),
        len(empty_products),
    )
    logger.warning(
        "symptom_products_api requested ingredients: %s",
        ", ".join(ingredients) if ingredients else "(none)",
    )
    logger.warning(
        "symptom_products_api ingredients with products: %s",
        ", ".join(with_products) if with_products else "(none)",
    )
    if empty_products:
        logger.warning(
            "symptom_products_api ingredients without products: %s",
            ", ".join(empty_products),
        )
    if debug_mode:
        diagnostics = [
            {
                "ingredient": item.get("ingredient"),
                "product_count": len(item.get("products") or []),
                "diagnostics": item.get("diagnostics", {}),
            }
            for item in items
        ]
        logger.warning(
            "symptom_products_api diagnostics: %s",
            json.dumps(diagnostics, ensure_ascii=False),
        )

    response_payload = {"status": "success", "items": items}
    if debug_mode:
        response_payload["diagnostics"] = [
            {
                "ingredient": item.get("ingredient"),
                "product_count": len(item.get("products") or []),
                "diagnostics": item.get("diagnostics", {}),
            }
            for item in items
        ]
    return JsonResponse(response_payload)




