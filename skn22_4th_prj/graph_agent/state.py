from typing import TypedDict, List, Optional, Any


class AgentState(TypedDict):
    query: str
    category: str  # 'symptom_recommendation', 'product_request', 'general_medical', 'invalid'
    keyword: str  # Extracted keyword for search
    symptom: Optional[str]
    fda_data: Optional[Any]  # dict(product) or list(ingredients)
    dur_data: Optional[List[dict]]
    final_answer: Optional[str]
    user_profile: Optional[dict]
    user_info: Optional[dict]
    ingredients_data: Optional[List[dict]]  # Symptom response payload for template cards

    # Intermediate fields for symptom pipeline
    all_ingredient_candidates: Optional[List[str]]
    ingredient_candidates: Optional[List[str]]
    backup_ingredient_candidates: Optional[List[str]]
    ingredient_efficacy_map: Optional[dict]
    symptom_term: Optional[str]
    products_map: Optional[dict]

    # Caching fields
    cache_key: Optional[str]
    is_cached: Optional[bool]
