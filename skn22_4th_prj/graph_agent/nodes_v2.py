import logging
import asyncio
from .state import AgentState
from services.ai_service_v2 import AIService
from services.drug_service import DrugService

logger = logging.getLogger(__name__)


async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keywords"""
    query = state["query"]

    cache_key = await AIService.normalize_symptom_query(query)

    logger.info(f"Classifying query (cache_key: {cache_key})")
    intent = await AIService.classify_intent(query)

    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")

    return {
        "category": category,
        "keyword": keyword,
        "symptom": query if category == "symptom_recommendation" else None,
        "cache_key": cache_key if category == "symptom_recommendation" else None,
        "is_cached": False,
    }


async def retrieve_fda_node(state: AgentState) -> AgentState:
    """Retrieve FDA data based on category"""
    category = state["category"]
    keyword = state["keyword"]
    query = state["query"]

    fda_data = None

    if category == "symptom_recommendation":
        eng_kw = [keyword] if keyword and keyword != "none" else ["pain"]
        fda_ingrs = await DrugService.get_ingrs_from_fda_by_symptoms(eng_kw)

        if not fda_ingrs:
            logger.info(
                f"FDA search failed for '{keyword}'. Requesting AI symptom synonyms."
            )
            synonyms = await AIService.get_symptom_synonyms(keyword or query)
            if synonyms:
                logger.info(f"AI suggested synonyms: {synonyms}. Retrying FDA search.")
                fda_ingrs = await DrugService.get_ingrs_from_fda_by_symptoms(synonyms)

            if not fda_ingrs:
                logger.info(
                    "FDA search with synonyms failed. Requesting AI ingredient recommendation."
                )
                fda_ingrs = await AIService.recommend_ingredients_for_symptom(
                    keyword or query
                )
                logger.info(f"AI recommended ingredients: {fda_ingrs}")

        fda_data = fda_ingrs

    elif category == "product_request":
        target = keyword if keyword and keyword != "none" else query
        fda_data = await DrugService.search_fda(target)

    return {"fda_data": fda_data}


async def retrieve_dur_node(state: AgentState) -> AgentState:
    """Retrieve DUR data based on FDA ingredients"""
    category = state["category"]
    fda_data = state["fda_data"]

    if not fda_data:
        return {"dur_data": []}

    dur_data = []

    if category == "symptom_recommendation" and isinstance(fda_data, list):
        dur_data = await DrugService.get_enriched_dur_info(fda_data)

    elif category == "product_request" and isinstance(fda_data, dict):
        ingrs = fda_data.get("active_ingredients", "")
        dur_data = await DrugService.get_dur_by_ingr(ingrs)

    return {"dur_data": dur_data}


async def generate_symptom_answer_node(state: AgentState) -> AgentState:
    """Generate per-ingredient safety guidance and fetch OTC product names"""
    symptom = state["symptom"]
    dur_data = state["dur_data"]
    fda_data = state.get("fda_data", [])

    if state.get("is_cached", False):
        return {
            "final_answer": state.get("final_answer", ""),
            "dur_data": dur_data,
            "fda_data": fda_data,
            "ingredients_data": state.get("ingredients_data", []),
        }

    # DUR 데이터가 없으면 일반 AI 답변으로 폴백
    if not dur_data:
        fallback_query = (
            f"The user asked about '{symptom}' but I couldn't find specific drugs in the FDA/DUR database. "
            f"Please provide general medical advice or common over-the-counter ingredients for this symptom. "
            f"(User query: {state['query']})"
        )
        answer = await AIService.generate_general_answer(fallback_query)
        prefix = "해당 증상에 대한 FDA/DUR 기반의 정확한 의약품 정보는 찾을 수 없었지만, 일반적인 정보를 안내해 드립니다.\n\n"
        return {"final_answer": prefix + answer, "ingredients_data": []}

    # AI에게 성분별 안전 여부 판단 요청
    ai_result = await AIService.generate_symptom_answer(
        symptom, dur_data, state.get("user_profile")
    )

    if not isinstance(ai_result, dict):
        # 예외적 폴백
        return {
            "final_answer": str(ai_result),
            "dur_data": dur_data,
            "ingredients_data": [],
        }

    summary = ai_result.get("summary", "")
    ai_ingredients = ai_result.get("ingredients", [])

    logger.info(
        f"AI classified {len(ai_ingredients)} ingredients for symptom '{symptom}'"
    )

    # DUR 상세 데이터를 성분명 기준으로 인덱싱
    dur_map = {item["ingredient"].upper(): item for item in dur_data}

    # 안전 성분의 제품명을 병렬로 조회
    safe_names = [
        ing["name"].upper() for ing in ai_ingredients if ing.get("can_take", False)
    ]

    async def fetch_products(ingr_name: str):
        from services.map_service import MapService

        try:
            result = await MapService.get_us_otc_products_by_ingredient(ingr_name)
            return ingr_name, result.get("products", [])
        except Exception as e:
            logger.warning(f"Failed to fetch products for '{ingr_name}': {e}")
            return ingr_name, []

    product_results = await asyncio.gather(*[fetch_products(n) for n in safe_names])
    products_map = dict(product_results)

    # 최종 ingredients_data 조립
    ingredients_data = []
    for ing in ai_ingredients:
        name = ing.get("name", "").upper()
        dur_item = dur_map.get(name, {})

        entry = {
            "name": name,
            "can_take": ing.get("can_take", True),
            "reason": ing.get("reason", ""),
            "dur_warning_types": ing.get("dur_warning_types", []),
            "kr_durs": dur_item.get("kr_durs", []),
            "fda_warning": dur_item.get("fda_warning", None),
            "products": (
                products_map.get(name, []) if ing.get("can_take", False) else []
            ),
        }
        ingredients_data.append(entry)

    return {
        "final_answer": summary,
        "dur_data": dur_data,
        "fda_data": fda_data,
        "ingredients_data": ingredients_data,
    }


async def generate_product_answer_node(state: AgentState) -> AgentState:
    """Generate answer for product queries"""
    fda_data = state["fda_data"]
    dur_data = state["dur_data"]

    if not fda_data:
        return {"final_answer": "해당 의약품 정보를 찾을 수 없습니다."}

    brand_name = fda_data.get("brand_name")
    indications = fda_data.get("indications")

    answer = f"**{brand_name}** 정보입니다.\n\n**효능/효과**:\n{indications}\n\n**DUR/주의사항**:\n"
    for d in dur_data:
        answer += f"- {d['ingr_name']} ({d['type']}): {d['warning_msg']}\n"

    return {"final_answer": answer}


async def generate_general_answer_node(state: AgentState) -> AgentState:
    """Generate answer for general medical queries"""
    answer = await AIService.generate_general_answer(state["query"])
    return {"final_answer": answer}


async def generate_error_node(state: AgentState) -> AgentState:
    """Handle invalid queries"""
    return {
        "final_answer": "죄송합니다. 질문을 이해하지 못하거나 의약품과 관련이 없는 질문입니다."
    }
