import re

from django.http import JsonResponse
from django.shortcuts import redirect, render

from services.supabase_service import SupabaseService
from services.user_service import UserService

FOOD_ALLERGY_DETAIL_CODES = {"T78.1", "T78.0", "Z91.0"}
FOOD_KEYWORDS = [
    "음식",
    "식품",
    "먹",
    "섭취",
    "땅콩",
    "복숭아",
    "견과",
    "새우",
    "게",
    "우유",
    "계란",
    "밀",
    "콩",
    '갑각류'
]


def _parse_medication_names(raw_text: str):
    tokens = re.split(r"[,/\n;]+", str(raw_text or ""))
    names = []
    seen = set()
    for token in tokens:
        name = token.strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _parse_text_tokens(raw_text: str):
    return _parse_medication_names(raw_text)


def _format_kcd_items(items: list):
    displays = []
    for item in items or []:
        name = str((item or {}).get("kcd_name") or "").strip()
        code = str((item or {}).get("kcd_code") or "").strip().upper()
        if name and code:
            displays.append(f"{name} [{code}]")
    return ", ".join(displays)


def _contains_food_keyword(tokens: list[str]) -> bool:
    for token in tokens or []:
        lowered = str(token or "").strip().lower()
        if not lowered:
            continue
        for kw in FOOD_KEYWORDS:
            if kw in lowered:
                return True
    return False


def _build_food_code_guide_message(unresolved_tokens: list[str]):
    preview = ", ".join((unresolved_tokens or [])[:5])
    return (
        "음식 알레르기 항목은 KCD 코드 기반으로 저장해야 합니다. "
        "권장 코드: T78.1(기타 음식물 유해반응), T78.0(음식 유해작용 아나필락시스), Z91.0(식품 알레르기 개인력). "
        f"확인 필요: {preview} | 입력 예시: T78.1 선택 후 `상세정보: 땅콩`"
    )


def _split_allergy_detail(raw_text: str):
    text = str(raw_text or "").strip()
    if not text:
        return "", ""

    marker = re.search(r"\b상세정보\s*:\s*", text)
    if not marker:
        return text, ""

    base = text[: marker.start()].strip(" |,")
    detail = text[marker.end() :].strip()
    return base, detail


def _build_kcd_error_message(field_label: str, unresolved_info: dict):
    source_error = (unresolved_info or {}).get("source_error")
    if source_error:
        if source_error == "kcd_source_missing":
            return (
                "KCD 데이터 소스(kcd_info)가 구성되어 있지 않아 저장할 수 없습니다. "
                "운영 테이블/컬럼 구성을 먼저 확인해주세요."
            )
        return (
            "KCD 데이터 조회 중 오류가 발생해 저장할 수 없습니다. "
            "잠시 후 다시 시도하거나 운영 설정을 확인해주세요."
        )

    ambiguous = (unresolved_info or {}).get("ambiguous") or []
    unmatched = (unresolved_info or {}).get("unmatched") or []
    unresolved = (unresolved_info or {}).get("unresolved") or []

    if field_label == "알레르기":
        merged = []
        merged.extend(ambiguous)
        merged.extend(unmatched)
        merged.extend(unresolved)
        if _contains_food_keyword(merged):
            return _build_food_code_guide_message(merged)

    if ambiguous:
        preview = ", ".join(ambiguous[:5])
        return (
            f"{field_label} 항목 중 KCD 매핑이 모호한 값이 있습니다. "
            f"검색 결과에서 정확히 선택해주세요: {preview}"
        )

    if unmatched:
        preview = ", ".join(unmatched[:5])
        return (
            f"{field_label} 항목 중 KCD와 매칭되지 않는 값이 있습니다. "
            f"검색 결과에서 다시 선택해주세요: {preview}"
        )

    if unresolved:
        preview = ", ".join(unresolved[:5])
        return (
            f"{field_label}은 KCD 코드가 확인된 항목만 저장할 수 있습니다. "
            f"확인 필요: {preview}"
        )

    return f"{field_label} 처리 중 오류가 발생했습니다."


async def _normalize_profile_inputs(
    current_medications: str,
    allergies: str,
    chronic_diseases: str,
    food_allergy_detail: str = "",
):
    raw_meds = _parse_text_tokens(current_medications)
    valid_meds, invalid_meds = await SupabaseService.resolve_valid_drug_names(raw_meds)
    if invalid_meds:
        invalid_preview = ", ".join(invalid_meds[:5])
        return None, (
            "복용 중인 약은 검색에서 선택한 허가 의약품명만 저장할 수 있습니다. "
            f"확인 필요: {invalid_preview}"
        )

    async def resolve_kcd_field(raw_text: str, field_label: str):
        terms = _parse_text_tokens(raw_text)
        if not terms:
            return "", [], None

        resolved, unresolved, kcd_ready, info = await SupabaseService.resolve_kcd_terms(terms)
        if not kcd_ready or unresolved:
            info = info or {}
            info["unresolved"] = unresolved
            return None, [], _build_kcd_error_message(field_label, info)

        return _format_kcd_items(resolved), resolved, None

    normalized_diseases, _, disease_error = await resolve_kcd_field(
        chronic_diseases, "기저질환"
    )
    if disease_error:
        return None, disease_error

    normalized_allergies, resolved_allergies, allergy_error = await resolve_kcd_field(
        allergies, "알레르기"
    )
    if allergy_error:
        return None, allergy_error

    detail = str(food_allergy_detail or "").strip()
    if detail:
        selected_codes = {
            str((item or {}).get("kcd_code") or "").strip().upper()
            for item in (resolved_allergies or [])
        }
        if not selected_codes.intersection(FOOD_ALLERGY_DETAIL_CODES):
            return None, (
                "음식 알레르기 상세정보는 T78.1, T78.0, Z91.0 코드 선택 시에만 입력할 수 있습니다. "
                "먼저 해당 코드를 선택한 뒤 `상세정보:`를 입력해주세요."
            )
        normalized_allergies = f"{normalized_allergies} | 상세정보: {detail}"

    main_ingr_eng = await SupabaseService.get_main_ingr_eng_for_drugs(valid_meds)
    normalized = {
        "current_medications": ", ".join(valid_meds),
        "allergies": normalized_allergies,
        "chronic_diseases": normalized_diseases,
        "applied_allergies": normalized_allergies,
        "applied_chronic_diseases": normalized_diseases,
        "food_allergy_detail": detail,
        "main_ingr_eng": main_ingr_eng,
    }
    return normalized, None


async def register_view(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip()
        password = request.POST.get("password")
        password_confirm = request.POST.get("password_confirm")

        if password != password_confirm:
            return JsonResponse(
                {"status": "error", "message": "비밀번호가 서로 일치하지 않습니다."},
                status=400,
            )

        current_medications = (request.POST.get("current_medications") or "").strip()
        is_pregnant = request.POST.get("is_pregnant") == "on"

        has_disease = request.POST.get("has_disease") == "on"
        chronic_diseases = (
            (request.POST.get("chronic_diseases") or "").strip() if has_disease else ""
        )

        has_allergy = request.POST.get("has_allergy") == "on"
        allergies = (request.POST.get("allergies") or "").strip() if has_allergy else ""
        food_allergy_detail = (
            (request.POST.get("food_allergy_detail") or "").strip() if has_allergy else ""
        )

        try:
            normalized, normalize_error = await _normalize_profile_inputs(
                current_medications=current_medications,
                allergies=allergies,
                chronic_diseases=chronic_diseases,
                food_allergy_detail=food_allergy_detail,
            )
            if normalize_error:
                return JsonResponse(
                    {"status": "error", "message": normalize_error},
                    status=400,
                )

            user, error = await SupabaseService.auth_sign_up(email, password)
            if user:
                profile = await SupabaseService.update_user_profile(
                    user.id,
                    normalized["current_medications"],
                    normalized["allergies"],
                    normalized["chronic_diseases"],
                    is_pregnant,
                    normalized["main_ingr_eng"],
                    normalized["applied_allergies"],
                    normalized["applied_chronic_diseases"],
                    normalized["food_allergy_detail"],
                )
                if not profile:
                    # Prevent partially-created accounts (auth only, no profile).
                    await SupabaseService.auth_delete_user(str(user.id))
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": (
                                "회원가입은 완료됐지만 프로필 저장에 실패했습니다. "
                                "SUPABASE_SERVICE_ROLE_KEY 또는 user_profile RLS 정책을 확인해주세요."
                            ),
                        },
                        status=500,
                    )
                return JsonResponse({"status": "success", "redirect": "/auth/login/"})

            if error == "exists":
                return JsonResponse(
                    {
                        "status": "error",
                        "code": "user_exists",
                        "message": "이미 존재하는 이메일입니다.",
                    },
                    status=400,
                )

            return JsonResponse(
                {"status": "error", "message": f"회원가입에 실패했습니다: {error}"},
                status=400,
            )
        except Exception as e:
            return JsonResponse(
                {"status": "error", "message": f"시스템 오류: {str(e)}"},
                status=500,
            )

    return render(request, "register.html")


async def login_view(request):
    if "supabase_user" in request.session:
        return redirect("chat:home")

    if request.method == "POST":
        email = request.POST.get("email")
        password = request.POST.get("password")
        user, session = await SupabaseService.auth_sign_in(email, password)

        if user and session:
            request.session["supabase_user"] = {
                "id": user.id,
                "email": user.email,
                "display_name": email.split("@")[0],
            }
            return redirect("chat:home")

        return render(
            request,
            "login.html",
            {"error": "이메일 또는 비밀번호가 올바르지 않습니다."},
        )

    return render(request, "login.html")


def logout_view(request):
    if "supabase_user" in request.session:
        del request.session["supabase_user"]
    return redirect("chat:home")


async def kcd_search_view(request):
    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse([], safe=False)

    results = await SupabaseService.search_kcd(query_text=query, limit=20)
    return JsonResponse(results, safe=False)


async def profile_view(request):
    user_info = request.session.get("supabase_user")
    if not user_info:
        return redirect("users:login")

    profile = await UserService.get_profile(user_info)
    message = None
    error = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_profile":
            current_medications = (request.POST.get("current_medications") or "").strip()
            allergies = (request.POST.get("allergies") or "").strip()
            chronic_diseases = (request.POST.get("chronic_diseases") or "").strip()
            food_allergy_detail = (request.POST.get("food_allergy_detail") or "").strip()
            is_pregnant = request.POST.get("is_pregnant") == "on"

            normalized, normalize_error = await _normalize_profile_inputs(
                current_medications=current_medications,
                allergies=allergies,
                chronic_diseases=chronic_diseases,
                food_allergy_detail=food_allergy_detail,
            )
            if normalize_error:
                error = normalize_error
                allergy_preview = allergies
                if food_allergy_detail:
                    allergy_preview = (
                        f"{allergies} | 상세정보: {food_allergy_detail}"
                        if allergies
                        else f"상세정보: {food_allergy_detail}"
                    )
                return render(
                    request,
                    "profile.html",
                    {
                        "user": user_info,
                        "message": message,
                        "error": error,
                        "meds_display": current_medications,
                        "meds_value": current_medications,
                        "allergy_display": allergy_preview,
                        "allergy_value": allergies,
                        "allergy_food_detail": food_allergy_detail,
                        "disease_display": chronic_diseases,
                        "disease_value": chronic_diseases,
                        "is_pregnant": is_pregnant,
                        "pregnant_checked": "checked" if is_pregnant else "",
                    },
                )

            updated_profile = await UserService.update_profile(
                user_info,
                normalized["current_medications"],
                normalized["allergies"],
                normalized["chronic_diseases"],
                is_pregnant,
                normalized["main_ingr_eng"],
                normalized["applied_allergies"],
                normalized["applied_chronic_diseases"],
                normalized["food_allergy_detail"],
            )
            if updated_profile:
                profile = updated_profile
                message = "건강 정보가 성공적으로 저장되었습니다."
            else:
                error = "정보 저장 중 오류가 발생했습니다."

        elif action == "update_password":
            new_password = request.POST.get("new_password")
            confirm_password = request.POST.get("confirm_password")

            if not new_password or new_password != confirm_password:
                error = "비밀번호가 일치하지 않거나 입력되지 않았습니다."
            else:
                _, auth_error = await SupabaseService.auth_update_password(new_password)
                if auth_error:
                    error = f"비밀번호 변경 실패: {auth_error}"
                else:
                    message = "비밀번호가 성공적으로 변경되었습니다."

        elif action == "delete_account":
            success, delete_error = await UserService.delete_account(user_info)
            if success:
                if "supabase_user" in request.session:
                    del request.session["supabase_user"]
                return redirect("users:login")
            error = f"회원 탈퇴 실패: {delete_error}"

    no_info = "입력된 정보가 없습니다."
    meds_val = getattr(profile, "current_medications", "") or ""
    allergy_val = getattr(profile, "allergies", "") or ""
    disease_val = getattr(profile, "chronic_diseases", "") or ""
    pregnant_val = getattr(profile, "is_pregnant", False)

    allergy_kcd_val, parsed_food_detail = _split_allergy_detail(allergy_val)
    allergy_food_detail = getattr(profile, "food_allergy_detail", "") or parsed_food_detail

    return render(
        request,
        "profile.html",
        {
            "user": user_info,
            "message": message,
            "error": error,
            "meds_display": meds_val if meds_val.strip() else no_info,
            "meds_value": meds_val,
            "allergy_display": allergy_val if allergy_val.strip() else no_info,
            "allergy_value": allergy_kcd_val,
            "allergy_food_detail": allergy_food_detail,
            "disease_display": disease_val if disease_val.strip() else no_info,
            "disease_value": disease_val,
            "is_pregnant": pregnant_val,
            "pregnant_checked": "checked" if pregnant_val else "",
        },
    )
