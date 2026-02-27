from langgraph.graph import StateGraph, END
from .state import AgentState
from .nodes_v2 import (
    classify_node,
    retrieve_fda_node,
    retrieve_dur_node,
    generate_symptom_answer_node,
    generate_product_answer_node,
    generate_general_answer_node,
    generate_error_node,
)


def build_graph():
    """
    Build and compile the LangGraph workflow V2 for drug information
    (Optimized with parallel logic where applicable & V2 prompt nodes)
    """
    workflow = StateGraph(AgentState)

    # Add Nodes
    workflow.add_node("classify", classify_node)

    # [V2 최적화] FDA 조차도 비동기 내부에서 모아치기 처리가 가능하도록 변경
    workflow.add_node("retrieve_fda", retrieve_fda_node)
    workflow.add_node("retrieve_dur", retrieve_dur_node)

    workflow.add_node("answer_symptom", generate_symptom_answer_node)
    workflow.add_node("answer_product", generate_product_answer_node)
    workflow.add_node("answer_general", generate_general_answer_node)
    workflow.add_node("answer_error", generate_error_node)

    # Set Entry Point
    workflow.set_entry_point("classify")

    # Define Routing Logic
    def route_query(state: AgentState):
        # [V2 Cache] 캐시 적중 시 API 페치 우회
        if state.get("is_cached", False):
            return "cached_symptom"

        category = state["category"]
        if category == "symptom_recommendation":
            return "indication"
        elif category == "product_request":
            return "product"
        elif category == "general_medical":
            return "general"
        else:  # invalid, etc
            return "error"

    # Add Conditional Edges from Classifier
    workflow.add_conditional_edges(
        "classify",
        route_query,
        {
            "cached_symptom": "answer_symptom",
            "indication": "retrieve_fda",
            "product": "retrieve_fda",
            "general": "answer_general",
            "error": "answer_error",
        },
    )

    # Linear flow for retrievals (FDA -> DUR)
    workflow.add_edge("retrieve_fda", "retrieve_dur")

    # Route after DUR retrieval to appropriate answer generator
    def route_answer_generation(state: AgentState):
        category = state["category"]
        if category == "symptom_recommendation":
            return "answer_symptom"
        elif category == "product_request":
            return "answer_product"
        return "answer_error"  # Should not happen

    workflow.add_conditional_edges(
        "retrieve_dur",
        route_answer_generation,
        {
            "answer_symptom": "answer_symptom",
            "answer_product": "answer_product",
            "answer_error": "answer_error",
        },
    )

    # End edges
    workflow.add_edge("answer_symptom", END)
    workflow.add_edge("answer_product", END)
    workflow.add_edge("answer_general", END)
    workflow.add_edge("answer_error", END)

    return workflow.compile()
