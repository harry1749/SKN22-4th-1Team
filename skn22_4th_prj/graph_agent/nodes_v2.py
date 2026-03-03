import logging
import asyncio
import re
from .state import AgentState
from services.ai_service_v2 import AIService
from services.drug_service import DrugService
from services.user_service import UserService
from services.supabase_service import SupabaseService
from services.map_service import MapService
from services.ingredient_utils import (
    canonicalize_ingredient_name,
    canonicalize_ingredient_list,
)

logger = logging.getLogger(__name__)


SYMPTOM_TO_FDA_TERMS = {
    "두통": ["headache", "pain relief"],
    "편두통": ["migraine", "headache"],
    "알레르기": ["allergy", "allergic reaction", "antihistamine"],
    "기침": ["cough", "cold", "nasal congestion", "sinus congestion"],
    "감기": ["cold"],
    "발열": ["fever"],
    "소화불량": ["indigestion"],
    "복통": ["stomachache", "abdominal pain"],
    "염좌": ["sprain"],
    "찰과상": ["wound", "skin abrasion"],
    "화상": ["burn"],
    "곤충교상": ["insect bite"],
}

_EMPTY_PROFILE_TOKENS = {"none", "없음", "없어요", "n/a", "na", "x"}

_INGREDIENT_BENEFIT_OVERRIDES = {
    "ACETAMINOPHEN": "통증·발열 완화",
    "IBUPROFEN": "통증·염증·발열 완화",
    "NAPROXEN": "통증·염증 완화",
    "ASPIRIN": "통증·발열 완화",
    "DEXTROMETHORPHAN": "기침 완화",
    "GUAIFENESIN": "가래 배출 도움",
    "LORATADINE": "알레르기 증상 완화",
    "CETIRIZINE": "알레르기 증상 완화",
    "DIPHENHYDRAMINE": "알레르기·콧물 완화",
    "PHENYLEPHRINE": "코막힘 완화",
    "PSEUDOEPHEDRINE": "코막힘 완화",
    "FAMOTIDINE": "속쓰림·위산 완화",
    "OMEPRAZOLE": "위산 과다 완화",
    "LANSOPRAZOLE": "위산 과다 완화",
    "BISMUTH SUBSALICYLATE": "복통·설사 완화",
    "LOPERAMIDE": "설사 완화",
    "MECLIZINE": "멀미·어지럼 완화",
    "DIMENHYDRINATE": "멀미·구역 완화",
}

_SYMPTOM_BRIEF_MAP = {
    "두통": "두통·통증 완화",
    "편두통": "편두통 완화",
    "알레르기": "알레르기 증상 완화",
    "기침": "기침 완화",
    "감기": "감기 증상 완화",
    "발열": "발열 완화",
    "소화불량": "소화불량 완화",
    "복통": "복통 완화",
    "염좌": "근육·관절 통증 완화",
    "찰과상": "상처 소독·보호",
    "화상": "화상 부위 통증 완화",
    "곤충교상": "가려움·염증 완화",
}

_EFFICACY_KEYWORD_LABELS = [
    (("진통", "통증", "pain"), "통증 완화"),
    (("해열", "발열", "fever"), "발열 완화"),
    (("소염", "염증", "anti-inflammatory", "inflammation"), "염증 완화"),
    (("기침", "진해", "cough"), "기침 완화"),
    (("가래", "거담", "sputum", "expectoration"), "가래 배출 도움"),
    (("코막힘", "비충혈", "nasal congestion", "decongest"), "코막힘 완화"),
    (("콧물", "재채기", "allergy", "antihist"), "알레르기 증상 완화"),
    (("속쓰림", "위산", "제산", "heartburn", "acid"), "속쓰림·위산 완화"),
    (("소화", "indigestion"), "소화불량 완화"),
    (("복통", "abdominal"), "복통 완화"),
    (("설사", "diarrhea"), "설사 완화"),
    (("변비", "constipation"), "변비 완화"),
    (("멀미", "구역", "nausea", "motion sickness"), "멀미·구역 완화"),
    (("가려움", "itch"), "가려움 완화"),
    (("상처", "wound"), "상처 소독·보호"),
]

_SYMPTOM_INGREDIENT_EXCLUDE = {
    "두통": {
        "DEXTROMETHORPHAN",
        "GUAIFENESIN",
        "PSEUDOEPHEDRINE",
        "PHENYLEPHRINE",
    },
    "편두통": {
        "DEXTROMETHORPHAN",
        "GUAIFENESIN",
        "PSEUDOEPHEDRINE",
        "PHENYLEPHRINE",
    },
}


def _to_fda_symptom_terms(symptom_term: str):
    token = str(symptom_term or "").strip().lower()
    if not token:
        return []
    return SYMPTOM_TO_FDA_TERMS.get(token, [])


def _merge_unique_terms(*groups):
    merged = []
    seen = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for value in group:
            token = str(value or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            merged.append(token)
    return merged


def _is_excluded_ingredient_for_symptom(symptom_term: str, ingredient_name: str) -> bool:
    symptom_key = str(symptom_term or "").strip()
    if not symptom_key:
        return False
    excluded = _SYMPTOM_INGREDIENT_EXCLUDE.get(symptom_key) or set()
    if not excluded:
        return False
    normalized = canonicalize_ingredient_name(ingredient_name or "")
    normalized = str(normalized or "").strip().upper()
    return bool(normalized and normalized in excluded)


def _has_user_risk_profile(user_profile):
    if not isinstance(user_profile, dict):
        return False

    if _to_bool(user_profile.get("is_pregnant")):
        return True

    for key in ("current_medications", "allergies", "chronic_diseases"):
        value = str(user_profile.get(key) or "").strip().lower()
        if value and value not in _EMPTY_PROFILE_TOKENS:
            return True
    return False


def _to_profile_text(value):
    text = str(value or "").strip()
    return text if text else "\uc785\ub825 \uc5c6\uc74c"


def _looks_mojibake(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True

    if "??" in value or "\ufffd" in value:
        return True

    cjk = len(re.findall(r"[\u4E00-\u9FFF]", value))
    hangul = len(re.findall(r"[\uAC00-\uD7A3]", value))
    ascii_alpha = len(re.findall(r"[A-Za-z]", value))
    if cjk >= 3 and hangul == 0 and ascii_alpha < 3:
        return True

    suspicious_tokens = ("?", "?", "?", "?", "?", "?", "??", "?")
    if any(token in value for token in suspicious_tokens) and hangul < 2:
        return True

    return False


def _fallback_reason(can_take: bool, warning_types) -> str:
    warnings = [str(x).strip() for x in (warning_types or []) if str(x).strip()]
    if can_take is False:
        return (
            "DUR \uc815\ubcf4\uc0c1 \ubcf5\uc6a9\ud558\uba74 "
            "\uc704\ud5d8\ud558\ub2e4\uace0 \uc548\ub0b4\ub418\uace0 \uc788\uc2b5\ub2c8\ub2e4."
        )
    if warnings:
        return (
            f"DUR \uc8fc\uc758 \ud56d\ubaa9({', '.join(warnings[:3])}) "
            "\uae30\uc900\uc73c\ub85c \uc8fc\uc758\uac00 \ud544\uc694\ud55c \uc131\ubd84\uc785\ub2c8\ub2e4."
        )
    return (
        "\uac1c\uc778 \uac74\uac15\uc815\ubcf4(\ubcf5\uc6a9\uc57d/\uc54c\ub808\ub974\uae30/\uae30\uc800\uc9c8\ud658) "
        "\uae30\uc900\uc5d0\uc11c \uc77c\ubc18 \ubcf5\uc6a9 \uac00\ub2a5\uc73c\ub85c \uc548\ub0b4\ub429\ub2c8\ub2e4."
    )


def _profile_value(user_profile, key: str) -> str:
    if not isinstance(user_profile, dict):
        return ""
    return str(user_profile.get(key) or "").strip()


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    return token in {"true", "1", "yes", "y", "on"}


def _has_profile_value(user_profile, key: str) -> bool:
    value = _profile_value(user_profile, key).lower()
    return bool(value and value not in _EMPTY_PROFILE_TOKENS)


def _collect_warning_types(dur_item: dict) -> list:
    if not isinstance(dur_item, dict):
        return []
    seen = set()
    warning_types = []
    for row in dur_item.get("kr_durs", []) or []:
        if not isinstance(row, dict):
            continue
        dur_type = str(row.get("type") or "").strip()
        if not dur_type:
            continue
        key = dur_type.lower()
        if key in seen:
            continue
        seen.add(key)
        warning_types.append(dur_type)
    return warning_types


def _collect_warning_excerpt(dur_item: dict, max_len: int = 160) -> str:
    if not isinstance(dur_item, dict):
        return ""
    for row in dur_item.get("kr_durs", []) or []:
        if not isinstance(row, dict):
            continue
        warning = str(row.get("warning") or "").strip()
        if not warning:
            continue
        if len(warning) > max_len:
            return warning[: max_len - 3].rstrip() + "..."
        return warning
    return ""


def _summarize_efficacy_text(
    ingredient_name: str,
    efficacy_text: str,
    symptom_term: str = "",
) -> str:
    name = canonicalize_ingredient_name(ingredient_name or "")
    name = str(name or "").strip().upper()
    if name and name in _INGREDIENT_BENEFIT_OVERRIDES:
        return _INGREDIENT_BENEFIT_OVERRIDES[name]

    source = re.sub(r"\s+", " ", str(efficacy_text or "")).strip().lower()
    labels = []
    seen = set()
    for keywords, label in _EFFICACY_KEYWORD_LABELS:
        if any(keyword in source for keyword in keywords):
            if label not in seen:
                seen.add(label)
                labels.append(label)
        if len(labels) >= 2:
            break
    if labels:
        return " · ".join(labels)

    symptom_key = str(symptom_term or "").strip()
    if symptom_key in _SYMPTOM_BRIEF_MAP:
        return _SYMPTOM_BRIEF_MAP[symptom_key]

    return "해당 증상 완화 목적 성분"


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
        token = str(chunk or "").strip().lower()
        token = re.sub(r"\([^)]*\)", "", token).strip()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _evaluate_profile_risk_for_ingredient(
    dur_item: dict,
    user_profile: dict,
    has_user_risk: bool,
):
    warning_types = _collect_warning_types(dur_item)
    if not has_user_risk:
        return True, warning_types, _fallback_reason(True, warning_types)

    meds = _profile_value(user_profile, "current_medications")
    is_pregnant = _to_bool((user_profile or {}).get("is_pregnant"))
    has_meds = _has_profile_value(user_profile, "current_medications")

    rows = dur_item.get("kr_durs", []) if isinstance(dur_item, dict) else []
    block_reasons = []
    caution_notes = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        type_text = str(row.get("type") or "").strip()
        warning_text = str(row.get("warning") or "").strip()
        merged = f"{type_text} {warning_text}".lower()

        is_pregnancy_rule = any(
            token in merged
            for token in ("임부", "임신", "수유", "pregnan", "lactat", "breastfeeding")
        )
        is_combined_contra = any(
            token in merged
            for token in ("병용금기", "combined", "contraindicated combination")
        )

        if is_pregnancy_rule:
            if is_pregnant:
                block_reasons.append("임신/수유 중 사용 금기(임부금기) 항목으로 확인되었습니다.")
            else:
                caution_notes.append("임부/수유부 대상 금기 항목이 있어 해당 조건에서는 복용 위험입니다.")
            continue

        if is_combined_contra:
            if has_meds:
                med_text = meds.lower()
                partner_tokens = _extract_combined_partner_tokens(warning_text)
                matched_partner = next((p for p in partner_tokens if p and p in med_text), "")
                if matched_partner:
                    block_reasons.append(
                        f"현재 복용 중인 약({meds})과 병용금기 성분({matched_partner})이 일치합니다."
                    )
                else:
                    caution_notes.append(
                        "병용금기 항목이 있으나 현재 복용약과의 직접 일치 근거가 확인되지 않아 주의 안내로 표시합니다."
                    )
            else:
                caution_notes.append("병용금기 항목이 있으나 현재 복용약 정보가 없어 주의 안내로 표시합니다.")
            continue

        if type_text:
            caution_notes.append(f"DUR '{type_text}' 항목으로 복용 시 주의가 필요합니다.")

    warning_excerpt = _collect_warning_excerpt(dur_item)
    if block_reasons:
        joined = " ".join(dict.fromkeys(block_reasons))
        if warning_types:
            joined += f" (DUR 유형: {', '.join(warning_types[:3])})"
        if warning_excerpt:
            joined += f" 근거 문구: {warning_excerpt}"
        return False, warning_types, joined

    if warning_types:
        caution_summary = " ".join(dict.fromkeys(caution_notes[:2])) if caution_notes else ""
        if caution_summary:
            return (
                True,
                warning_types,
                f"{caution_summary} (DUR 유형: {', '.join(warning_types[:3])})",
            )
        return (
            True,
            warning_types,
            f"입력한 건강정보와 직접 충돌 근거는 확인되지 않았지만 DUR 주의 항목({', '.join(warning_types[:3])})이 있어 복용 전 전문가 확인을 권장합니다.",
        )
    return True, warning_types, _fallback_reason(True, warning_types)


def _build_profile_reflection_tail(user_profile, ingredients_data):
    if not isinstance(user_profile, dict):
        return ""

    meds = _to_profile_text(user_profile.get("current_medications"))
    allergies = _to_profile_text(user_profile.get("allergies"))
    diseases = _to_profile_text(user_profile.get("chronic_diseases"))
    pregnancy = "예" if _to_bool(user_profile.get("is_pregnant")) else "아니오"

    blocked = []
    caution = []
    for ing in ingredients_data or []:
        if not isinstance(ing, dict):
            continue
        name = str(ing.get("name") or "").strip()
        if not name:
            continue
        can_take = ing.get("can_take", True)
        warnings = ing.get("dur_warning_types") or []
        if can_take is False:
            blocked.append(name)
        elif warnings:
            caution.append(name)

    if blocked:
        reflection = (
            f"\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc73c\ub85c "
            f"\ubcf5\uc6a9 \uc81c\ud55c \uc131\ubd84\uc774 \ud655\uc778\ub418\uc5c8\uc2b5\ub2c8\ub2e4: "
            f"{', '.join(blocked[:5])}"
        )
    elif caution:
        reflection = (
            f"\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc73c\ub85c "
            f"\uc8fc\uc758\uac00 \ud544\uc694\ud55c \uc131\ubd84\uc774 \ud655\uc778\ub418\uc5c8\uc2b5\ub2c8\ub2e4: "
            f"{', '.join(caution[:5])}"
        )
    else:
        reflection = (
            "\uc785\ub825\ud55c \uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc5d0\uc11c "
            "\uc911\ub300\ud55c \ubcf5\uc6a9 \uc81c\ud55c \uc131\ubd84\uc740 "
            "\ud655\uc778\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4."
        )

    return (
        "\n\n[\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \ubc18\uc601 \uc694\uc57d]\n"
        f"- \ubcf5\uc6a9 \uc911\uc778 \uc57d: {meds}\n"
        f"- \uc54c\ub808\ub974\uae30: {allergies}\n"
        f"- \uae30\uc800\uc9c8\ud658: {diseases}\n"
        f"- 임신/수유 여부: {pregnancy}\n"
        f"- \ubc18\uc601 \uacb0\uacfc: {reflection}"
    )

def _normalize_ai_ingredients(ai_ingredients, dur_data):
    """Build stable output entries from preselected DUR ingredients.

    Ingredient validity is decided upstream. Here we only normalize output fields.
    """
    if not isinstance(dur_data, list):
        return []

    ordered_names = []
    for item in dur_data:
        if not isinstance(item, dict):
            continue
        name = canonicalize_ingredient_name(item.get("ingredient"))
        if name and name not in ordered_names:
            ordered_names.append(name)

    allowed = set(ordered_names)
    normalized_map = {}

    if isinstance(ai_ingredients, list):
        for ing in ai_ingredients:
            if not isinstance(ing, dict):
                continue
            name = canonicalize_ingredient_name(ing.get("name"))
            if not name or name not in allowed:
                continue
            if name in normalized_map:
                continue
            warning_types = ing.get("dur_warning_types")
            if not isinstance(warning_types, list):
                warning_types = []
            can_take_raw = ing.get("can_take", True)
            if isinstance(can_take_raw, bool):
                can_take = can_take_raw
            elif isinstance(can_take_raw, str):
                can_take = can_take_raw.strip().lower() in ("true", "1", "yes", "y")
            else:
                can_take = bool(can_take_raw)
            normalized_map[name] = {
                "name": name,
                "can_take": can_take,
                "reason": str(ing.get("reason") or ""),
                "dur_warning_types": [str(x) for x in warning_types if isinstance(x, str)],
            }

    # Fill missing ingredients with neutral defaults to keep output stable.
    for name in ordered_names:
        if name not in normalized_map:
            normalized_map[name] = {
                "name": name,
                "can_take": True,
                "reason": "개별 복용 판정 정보가 없어 일반 주의 안내를 제공합니다.",
                "dur_warning_types": [],
            }

    return [normalized_map[name] for name in ordered_names]


async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keyword."""
    query = state["query"]
    intent = await AIService.classify_intent(query)

    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")
    query_l = str(query or "").strip().lower()

    # Heuristic safeguard: allergy-like symptom queries must stay on symptom path.
    if any(token in query_l for token in ["알레르기", "allergy", "allergic"]):
        category = "symptom_recommendation"
        if not keyword or keyword == "none":
            keyword = "알레르기"

    return {
        "category": category,
        "keyword": keyword,
        "symptom": query if category == "symptom_recommendation" else None,
        "cache_key": None,
        "is_cached": False,
    }


async def retrieve_data_node(state: AgentState) -> AgentState:
    """
    Symptom path: symptom classification -> DB search -> ingredient extraction.
    Product path: product search on FDA.
    """
    category = state["category"]
    keyword = state["keyword"]
    query = state["query"]

    user_profile_data = state.get("user_profile")
    if category == "symptom_recommendation" and not user_profile_data:
        user_info = state.get("user_info")
        if user_info:
            try:
                profile = await UserService.get_profile(user_info)
                if profile:
                    applied_allergies = (
                        getattr(profile, "applied_allergies", None) or profile.allergies
                    )
                    applied_chronic_diseases = (
                        getattr(profile, "applied_chronic_diseases", None)
                        or profile.chronic_diseases
                    )
                    food_allergy_detail = (
                        str(getattr(profile, "food_allergy_detail", "") or "").strip()
                    )
                    if food_allergy_detail and "상세정보:" not in str(applied_allergies or ""):
                        applied_allergies = (
                            f"{applied_allergies} | 상세정보: {food_allergy_detail}"
                            if applied_allergies
                            else f"상세정보: {food_allergy_detail}"
                        )
                    user_profile_data = {
                        "current_medications": profile.current_medications,
                        "allergies": applied_allergies,
                        "chronic_diseases": applied_chronic_diseases,
                        "is_pregnant": bool(getattr(profile, "is_pregnant", False)),
                    }
            except Exception as e:
                logger.error(f"Error fetching user profile from Supabase: {e}")

    if category == "symptom_recommendation":
        # Fast path: avoid extra LLM hop for canonicalization to reduce first-page latency.
        db_symptom_term = (keyword or query).strip()
        for known in SYMPTOM_TO_FDA_TERMS.keys():
            if known and known in str(query):
                db_symptom_term = known
                break

        ranked_ingredients = await SupabaseService.search_ingredient_scores_by_symptom(
            keyword=db_symptom_term,
            raw_query=query,
            max_rows=5000,
        )
        all_ingredients = [item["ingredient"] for item in ranked_ingredients]
        ingredient_efficacy_map = {}
        for item in ranked_ingredients:
            name = canonicalize_ingredient_name(item.get("ingredient"))
            if not name:
                continue
            name_key = str(name).strip().upper()
            if not name_key:
                continue
            efficacy_text = str(item.get("sample_efficacy") or "").strip()
            if efficacy_text and name_key not in ingredient_efficacy_map:
                ingredient_efficacy_map[name_key] = efficacy_text
        eng_kw = _to_fda_symptom_terms(db_symptom_term)
        if not eng_kw:
            eng_kw = _to_fda_symptom_terms(keyword)
        if not eng_kw:
            eng_kw = ["pain"]
        search_terms = list(eng_kw)
        fda_candidates = []
        should_query_fda_candidates = not ranked_ingredients or len(ranked_ingredients) < 5
        if should_query_fda_candidates:
            logger.info(
                "FDA symptom ingredient terms: %s",
                ", ".join(search_terms),
            )
            fda_candidates = await DrugService.get_ingrs_from_fda_by_symptoms(
                search_terms
            )
            fda_candidates = canonicalize_ingredient_list(fda_candidates)
            fda_candidates = [
                token
                for token in fda_candidates
                if not _is_excluded_ingredient_for_symptom(db_symptom_term, token)
            ]
            if not fda_candidates:
                synonyms = await AIService.get_symptom_synonyms(keyword or query)
                if synonyms:
                    search_terms = _merge_unique_terms(eng_kw, synonyms)
                    fda_candidates = await DrugService.get_ingrs_from_fda_by_symptoms(
                        search_terms
                    )
                    fda_candidates = canonicalize_ingredient_list(fda_candidates)
                    fda_candidates = [
                        token
                        for token in fda_candidates
                        if not _is_excluded_ingredient_for_symptom(db_symptom_term, token)
                    ]

        if ranked_ingredients:
            merged_scores = {}
            for item in ranked_ingredients:
                name = canonicalize_ingredient_name(item.get("ingredient"))
                if not name:
                    continue
                if _is_excluded_ingredient_for_symptom(db_symptom_term, name):
                    continue
                merged_scores[name] = merged_scores.get(name, 0) + int(item.get("score", 0) or 0)
            scored_candidates = [
                {"ingredient": name, "score": score}
                for name, score in sorted(merged_scores.items(), key=lambda x: (-x[1], x[0]))
            ]
        else:
            scored_candidates = []

        # Primary selection:
        # Let LLM choose direct symptom-relief ingredients from DB-extracted candidates.
        selected_ingredients = []
        if scored_candidates:
            selected_ingredients = await AIService.select_direct_symptom_ingredients(
                symptom=db_symptom_term or keyword or query,
                candidates=scored_candidates,
                top_n=5,
            )
            selected_ingredients = canonicalize_ingredient_list(selected_ingredients)[:5]
            selected_ingredients = [
                token
                for token in selected_ingredients
                if not _is_excluded_ingredient_for_symptom(db_symptom_term, token)
            ]
            if len(selected_ingredients) < 5 and fda_candidates:
                for token in fda_candidates:
                    if token in selected_ingredients:
                        continue
                    selected_ingredients.append(token)
                    if len(selected_ingredients) >= 5:
                        break
            logger.info(
                "Symptom direct ingredient selection via LLM: selected=%d from_candidates=%d",
                len(selected_ingredients),
                len(scored_candidates),
            )

        if not selected_ingredients:
            logger.info(
                f"DB symptom search returned no ingredients for '{db_symptom_term}'. "
                "Falling back to FDA symptom ingredient search."
            )
            all_ingredients = list(fda_candidates)

            if not all_ingredients:
                all_ingredients = await AIService.recommend_ingredients_for_symptom(
                    keyword or query
                )

            all_ingredients = canonicalize_ingredient_list(all_ingredients)
            all_ingredients = [
                token
                for token in all_ingredients
                if not _is_excluded_ingredient_for_symptom(db_symptom_term, token)
            ]
            selected_ingredients = all_ingredients[:5]
        else:
            all_ingredients = canonicalize_ingredient_list(
                [item["ingredient"] for item in scored_candidates] + list(fda_candidates)
            )
            all_ingredients = [
                token
                for token in all_ingredients
                if not _is_excluded_ingredient_for_symptom(db_symptom_term, token)
            ]

        fda_ingredients = selected_ingredients[:5]
        backup_ingredients = selected_ingredients[5:10]

        logger.info(
            f"Symptom raw='{query}', db_term='{db_symptom_term}', keyword='{keyword}' "
            f"ingredients extracted={len(all_ingredients)}, "
            f"primary_targets={len(fda_ingredients)}, backup_targets={len(backup_ingredients)}"
        )
        logger.info(
            "Symptom selected ingredients (top10): %s",
            ", ".join(selected_ingredients) if selected_ingredients else "(none)",
        )
        logger.info(
            "Symptom FDA candidates (top10): %s",
            ", ".join(fda_candidates[:10]) if fda_candidates else "(none)",
        )

        return {
            "all_ingredient_candidates": all_ingredients,
            "ingredient_candidates": selected_ingredients,
            "backup_ingredient_candidates": backup_ingredients,
            "ingredient_efficacy_map": ingredient_efficacy_map,
            "symptom_term": db_symptom_term,
            "fda_data": fda_ingredients,
            "user_profile": user_profile_data,
        }

    if category == "product_request":
        primary_target = keyword if keyword and keyword != "none" else query
        normalized_target = await AIService.normalize_product_keyword(
            query=query,
            hint_keyword=primary_target,
        )

        candidates = []
        seen = set()
        for candidate in [primary_target, normalized_target, query]:
            token = str(candidate or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(token)

        fda_data = await SupabaseService.get_product_profile(candidates)
        return {
            "fda_data": fda_data,
            "user_profile": user_profile_data,
        }

    return {"user_profile": user_profile_data}


async def retrieve_fda_products_node(state: AgentState) -> AgentState:
    # Deprecated path: product lookup moved to answer_symptom for can_take=true ingredients only.
    return {"products_map": {}}


async def retrieve_dur_node(state: AgentState) -> AgentState:
    """Extract KR/US DUR data after product lookup."""
    category = state["category"]

    if category == "symptom_recommendation":
        ingredients = state.get("ingredient_candidates") or []
        if not ingredients:
            return {"dur_data": []}
        # Initial response: KR DUR only.
        # US warning and product details are loaded asynchronously from a follow-up API.
        dur_data = await DrugService.get_kr_dur_info(ingredients)
        return {"dur_data": dur_data}

    if category == "product_request":
        fda_data = state.get("fda_data")
        if not fda_data or not isinstance(fda_data, dict):
            return {"dur_data": []}
        ingredient_list = fda_data.get("ingredient_list") or []
        if not ingredient_list:
            ingredient_list = fda_data.get("active_ingredients", "")
        dur_data = await SupabaseService.get_product_dur_by_ingredients(ingredient_list)
        return {"dur_data": dur_data}

    return {"dur_data": []}


async def generate_symptom_answer_node(state: AgentState) -> AgentState:
    """Generate final symptom response using DUR + FDA product lookup result."""
    symptom = state["symptom"]
    dur_data = state.get("dur_data") or []
    fda_data = state.get("fda_data", [])
    products_map = {}

    if not dur_data:
        fallback_query = (
            f"The user asked about '{symptom}' but I couldn't find specific drugs in the DB/FDA/DUR flow. "
            f"Please provide general medical advice or common over-the-counter ingredients for this symptom. "
            f"(User query: {state['query']})"
        )
        answer = await AIService.generate_general_answer(fallback_query)
        prefix = "해당 증상에 대한 DB/FDA/DUR 기반 정보를 찾기 어려워 일반 가이드를 제공합니다.\n\n"
        return {"final_answer": prefix + answer, "ingredients_data": []}

    summary = (
        f"입력 증상 '{symptom}'에 대해 한국 DUR 성분 정보를 우선 분석했습니다. "
        "사용자 건강정보(복용약/알레르기/기저질환)가 있으면 복용 가능/주의 여부를 함께 반영합니다."
    )
    has_user_risk = _has_user_risk_profile(state.get("user_profile"))
    dur_map = {item["ingredient"].upper(): item for item in dur_data}

    # Keep rendering order aligned with DB-ranked ingredients (1~10).
    # Show all analyzed ingredients so blocked ingredients and reasons are visible.
    ranked_ingredients = state.get("ingredient_candidates") or []
    ordered_names = [str(x).strip().upper() for x in ranked_ingredients if str(x).strip()]
    if not ordered_names:
        ordered_names = [str(item.get("ingredient") or "").strip().upper() for item in dur_data]
        ordered_names = [x for x in ordered_names if x]

    safe_ingredients = []
    blocked_ingredients = []
    efficacy_map = state.get("ingredient_efficacy_map") or {}
    symptom_term = str(state.get("symptom_term") or "").strip()
    for name in ordered_names:
        dur_item = dur_map.get(name, {})
        can_take, warning_types, reason = _evaluate_profile_risk_for_ingredient(
            dur_item=dur_item,
            user_profile=state.get("user_profile") or {},
            has_user_risk=has_user_risk,
        )
        reason = str(reason or "").strip()
        if _looks_mojibake(reason):
            reason = _fallback_reason(can_take, warning_types)
        if can_take is False:
            risk_prefix = (
                "DUR \uc815\ubcf4\uc0c1 \ubcf5\uc6a9\ud558\uba74 "
                "\uc704\ud5d8\ud558\ub2e4\uace0 \uc548\ub0b4\ub418\uace0 \uc788\uc2b5\ub2c8\ub2e4."
            )
            if not reason:
                reason = risk_prefix
            elif risk_prefix not in reason:
                reason = f"{risk_prefix} {reason}"
        elif not reason:
            reason = _fallback_reason(can_take, warning_types)
        efficacy_brief = _summarize_efficacy_text(
            ingredient_name=name,
            efficacy_text=efficacy_map.get(name),
            symptom_term=symptom_term,
        )
        entry = {
            "name": name,
            "can_take": can_take,
            "efficacy_brief": efficacy_brief,
            "reason": reason,
            "dur_warning_types": warning_types,
            "kr_durs": dur_item.get("kr_durs", []),
            "fda_warning": dur_item.get("fda_warning", None),
            "products": products_map.get(name, []),
        }
        if can_take is False:
            blocked_ingredients.append(entry)
        else:
            safe_ingredients.append(entry)

    # UX priority:
    # - Keep up to 10 actionable ingredients as product-page candidates.
    # - The product page displays up to 5 cards and backfills from this candidate pool.
    # - Keep blocked ingredients for safety explanation.
    max_safe_candidates = 10
    ingredients_data = safe_ingredients[:max_safe_candidates] + blocked_ingredients

    profile_tail = _build_profile_reflection_tail(state.get("user_profile"), ingredients_data)
    final_answer = summary + profile_tail if profile_tail else summary

    return {
        "final_answer": final_answer,
        "dur_data": dur_data,
        "fda_data": fda_data,
        "ingredients_data": ingredients_data,
    }


async def generate_product_answer_node(state: AgentState) -> AgentState:
    """Generate answer for product queries."""
    fda_data = state.get("fda_data")
    dur_data = state.get("dur_data") or []

    if not fda_data:
        fallback_query = str(state.get("query") or "").strip()
        answer = await AIService.generate_web_search_answer(fallback_query)
        return {"final_answer": answer}

    brand_name = fda_data.get("brand_name")
    indications = fda_data.get("indications")

    answer = f"**{brand_name}** 정보입니다.\n\n**효능/효과**:\n{indications}\n\n**DUR/주의사항**:\n"
    for d in dur_data:
        answer += f"- {d['ingr_name']} ({d['type']}): {d['warning_msg']}\n"

    return {"final_answer": answer}


async def generate_general_answer_node(state: AgentState) -> AgentState:
    answer = await AIService.generate_general_answer(state["query"])
    return {"final_answer": answer}


async def generate_error_node(state: AgentState) -> AgentState:
    return {
        "final_answer": "질문을 이해하지 못했거나 의약품과 관련 없는 요청입니다."

    }
