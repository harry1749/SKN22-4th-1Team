import logging
import asyncio
from .state import AgentState
from services.ai_service_v2 import AIService
from services.drug_service import DrugService

logger = logging.getLogger(__name__)


async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keywords"""
    query = state["query"]

    cache_key_task = AIService.normalize_symptom_query(query)
    intent_task = AIService.classify_intent(query)
    
    cache_key, intent = await asyncio.gather(cache_key_task, intent_task)

    logger.info(f"Classifying query (cache_key: {cache_key})")

    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")

    return {
        "category": category,
        "keyword": keyword,
        "symptom": query if category == "symptom_recommendation" else None,
        "cache_key": cache_key if category == "symptom_recommendation" else None,
        "is_cached": False,
    }


async def retrieve_data_node(state: AgentState) -> AgentState:
    """Pinecone 벡터 검색 + DUR 조회를 단일 스텝으로 처리 (지연 최적화)"""
    category = state["category"]
    keyword = state["keyword"]
    query = state["query"]

    drug_data = None
    dur_data = []

    # ── Step 1: Pinecone 벡터 검색 ──
    if category == "symptom_recommendation":
        if keyword and keyword != "none":
            eng_kw = [keyword]
        else:
            # cache_key = "paresthesia_moderate_none" → "paresthesia" 추출
            cache_key = state.get("cache_key", "")
            symptom_term = cache_key.split("_")[0] if cache_key else ""
            eng_kw = [symptom_term] if symptom_term and symptom_term != "none" else ["pain"]
        drug_ingrs = await DrugService.get_ingredients_by_symptoms(eng_kw)

        if not drug_ingrs:
            logger.info(
                f"Pinecone search failed for '{keyword}'. Requesting AI ingredient recommendation."
            )
            drug_ingrs = await AIService.recommend_ingredients_for_symptom(
                keyword or query
            )
            logger.info(f"AI recommended ingredients: {drug_ingrs}")

        drug_data = drug_ingrs

    elif category == "product_request":
        target = keyword if keyword and keyword != "none" else query
        drug_data = await DrugService.search_drug(target)

    # ── Step 2: DUR 조회 (벡터 검색 결과에 의존) ──
    if drug_data:
        if category == "symptom_recommendation" and isinstance(drug_data, list):
            dur_data = await DrugService.get_enriched_dur_info(drug_data)
        elif category == "product_request" and isinstance(drug_data, dict):
            ingrs = drug_data.get("active_ingredients", "")
            dur_data = await DrugService.get_dur_by_ingr(ingrs)

    return {"drug_data": drug_data, "dur_data": dur_data}


async def generate_symptom_answer_node(state: AgentState) -> AgentState:
    """Generate per-ingredient safety guidance and fetch OTC product names"""
    symptom = state["symptom"]
    dur_data = state["dur_data"]
    drug_data = state.get("drug_data", [])

    if state.get("is_cached", False):
        return {
            "final_answer": state.get("final_answer", ""),
            "dur_data": dur_data,
            "drug_data": drug_data,
            "ingredients_data": state.get("ingredients_data", []),
        }

    # DUR 데이터가 없으면 일반 AI 답변으로 폴백
    if not dur_data:
        fallback_query = (
            f"The user asked about '{symptom}' but I couldn't find specific drugs in the DUR database. "
            f"Please provide general medical advice or common over-the-counter ingredients for this symptom. "
            f"(User query: {state['query']})"
        )
        answer = await AIService.generate_general_answer(fallback_query)
        prefix = "해당 증상에 대한 DUR 기반의 정확한 의약품 정보는 찾을 수 없었지만, 일반적인 정보를 안내해 드립니다.\n\n"
        return {"final_answer": prefix + answer, "ingredients_data": []}

    # ── 병렬 실행: AI 안전성 판단 + 모든 성분 제품 조회 ──
    # 모든 성분명 추출 (AI 판단 전에 제품 조회 미리 시작)
    all_ingredient_names = [item["ingredient"].upper() for item in dur_data]

    async def fetch_products(ingr_name: str):
        from services.map_service import MapService
        try:
            result = await MapService.get_us_otc_products_by_ingredient(ingr_name)
            return ingr_name, result.get("products", [])
        except Exception as e:
            logger.warning(f"Failed to fetch products for '{ingr_name}': {e}")
            return ingr_name, []

    # AI 판단과 제품 조회를 동시에 실행
    ai_task = AIService.generate_symptom_answer(
        symptom, dur_data, state.get("user_profile")
    )
    product_tasks = [fetch_products(n) for n in all_ingredient_names]

    results = await asyncio.gather(ai_task, *product_tasks)
    ai_result = results[0]
    product_results = results[1:]
    products_map = dict(product_results)

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

    # 최종 ingredients_data 조립 (safe 성분만 products 포함)
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
        "drug_data": drug_data,
        "ingredients_data": ingredients_data,
    }


async def generate_product_answer_node(state: AgentState) -> AgentState:
    """Generate answer for product queries"""
    drug_data = state["drug_data"]
    dur_data = state["dur_data"]

    if not drug_data:
        return {"final_answer": "해당 의약품 정보를 찾을 수 없습니다."}

    brand_name = drug_data.get("brand_name")
    indications = drug_data.get("indications")

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
