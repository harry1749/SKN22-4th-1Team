from typing import TypedDict, List, Optional, Any


class AgentState(TypedDict):
    query: str
    category: (
        str  # 'symptom_recommendation', 'product_request', 'general_medical', 'invalid'
    )
    keyword: str  # Extracted keyword for search
    symptom: Optional[str]
    fda_data: Optional[Any]  # dict(product) or list(ingredients)
    dur_data: Optional[List[dict]]
    final_answer: Optional[str]
    user_profile: Optional[dict]
    ingredients_data: Optional[List[dict]]  # 성분별 안전·DUR·제품명 정보 (symptom 전용)

    # Caching fields
    cache_key: Optional[str]
    is_cached: Optional[bool]
