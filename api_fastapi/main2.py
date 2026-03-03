import os
import sys
import django
import logging
import json
from dotenv import load_dotenv

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. Django 초기화 및 환경변수 로드
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(os.path.join(project_root, 'backend_django'))

# .env 파일 명시적 로드
env_path = os.path.join(project_root, '.env')
load_dotenv(env_path)
logger.info(f"Loading .env from: {env_path}")

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

# ------------------------------------------------------------------------------
# Monkey Patching: Replace DrugService with SupabaseService for DUR retrieval
# ------------------------------------------------------------------------------
from services.drug_service import DrugService
from services.supabase_service import SupabaseService

logger.info("Patching DrugService to use Supabase for DUR queries...")
DrugService.get_dur_by_ingr = SupabaseService.get_dur_by_ingr
DrugService.get_enriched_dur_info = SupabaseService.get_enriched_dur_info


from fastapi import FastAPI, Request, HTTPException, APIRouter
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.ai_service_v2 import AIService
from services.auth_service import get_current_user_optional
from services.user_service import UserService
from services.map_service import MapService
from routers import auth_router, user_router, drug_router
from graph_agent.builder_v2 import build_graph

app = FastAPI(title="Global Drug Safety Intelligence")
app.include_router(auth_router.router)
app.include_router(user_router.router)
app.include_router(drug_router.router)

templates = Jinja2Templates(directory=os.path.join(current_dir, "templates"))
router = APIRouter()

@app.on_event("startup")
async def startup_event():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("CRITICAL: OPENAI_API_KEY is missing!")
    else:
        logger.info(f"OPENAI_API_KEY loaded.")
    
    # LangSmith Config
    if os.getenv("LANGSMITH_API_KEY") and not os.getenv("LANGCHAIN_API_KEY"):
        os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGSMITH_API_KEY")
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

    # Initialize LangGraph
    app.state.graph = build_graph()
    logger.info("LangGraph workflow initialized (with Supabase Support).")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    token = request.cookies.get("access_token")
    if token and token.startswith("Bearer "):
        token = token.split(" ")[1]
    user = await get_current_user_optional(token)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/web-search/{drug_name}", response_class=HTMLResponse)
async def product_search(request: Request, drug_name: str):
    """제품명 기반 검색 (웹 화면용) - Supabase 사용"""
    logger.info(f"Searching Pinecone for Product: {drug_name}")
    drug_result = await DrugService.search_drug(drug_name)

    if not drug_result:
        return templates.TemplateResponse("error.html", {
            "request": request, "message": f"'{drug_name}' 정보를 찾을 수 없습니다."
        })

    kr_dur = await DrugService.get_dur_by_ingr(drug_result['active_ingredients'])

    return templates.TemplateResponse("search_result.html", {
        "request": request,
        "drug_name": drug_name,
        "ingredients": drug_result['active_ingredients'],
        "us_guideline": drug_result,
        "kr_dur": kr_dur,
        "dur_count": len(kr_dur),
        "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
    })

@app.get("/smart-search", response_class=HTMLResponse)
async def smart_search(request: Request, q: str):
    """지능형 RAG 검색 (LangGraph 기반) - Supabase 사용"""
    if not q: return HTMLResponse("<script>alert('검색어를 입력하세요.'); history.back();</script>")

    logger.info(f"LangGraph User Query: {q}")

    user_profile_data = None
    try:
        token = request.cookies.get("access_token")
        if token and token.startswith("Bearer "):
            token = token.split(" ")[1]
        user = await get_current_user_optional(token)
        if user:
            profile = await UserService.get_profile(user)
            if profile:
                user_profile_data = {
                    "current_medications": profile.current_medications,
                    "allergies": profile.allergies,
                    "chronic_diseases": profile.chronic_diseases
                }
    except Exception as e:
        logger.error(f"Error fetching user profile: {e}")

    inputs = {"query": q, "user_profile": user_profile_data}
    try:
        # The graph nodes recall DrugService, which is patched.
        result = await app.state.graph.ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return templates.TemplateResponse("error.html", {
            "request": request, "message": f"처리 중 오류가 발생했습니다: {str(e)}"
        })

    category = result.get("category")
    final_answer = result.get("final_answer", "")
    
    if category == "symptom_recommendation":
        return templates.TemplateResponse("symptom_result.html", {
            "request": request,
            "symptom": q,
            "answer": final_answer,
            "ingredients_data": result.get("ingredients_data", []),
            "dur_details": result.get("dur_data", []),  # 모달 상세 정보용 유지
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })


    elif category == "product_request":
        fda = result.get("drug_data")
        dur = result.get("dur_data", [])
        
        if not fda:
             return templates.TemplateResponse("error.html", {
                "request": request, "message": final_answer or f"'{q}' 관련 정보를 찾을 수 없습니다."
            })
            
        return templates.TemplateResponse("search_result.html", {
            "request": request,
            "drug_name": fda.get("brand_name", q),
            "ingredients": fda.get("active_ingredients"),
            "us_guideline": fda,
            "kr_dur": dur,
            "dur_count": len(dur),
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })
        
    elif category == "general_medical":
        return templates.TemplateResponse("symptom_result.html", {
            "request": request,
            "symptom": q,
            "answer": final_answer,
            "dur_details": [],
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })
    
    else: 
        return templates.TemplateResponse("error.html", {
            "request": request, "message": final_answer or "요청을 처리할 수 없습니다."
        })

@router.get("/global-search/{drug_name}")
async def global_drug_search(drug_name: str):
    drug_result = await DrugService.search_drug(drug_name)
    if not drug_result:
        raise HTTPException(status_code=404, detail="Drug info not found")

    kr_dur_result = await DrugService.get_dur_by_ingr(drug_result['active_ingredients'])

    return {
        "status": "success",
        "origin": "USA",
        "drug_identity": {
            "name": drug_result['brand_name'],
            "ingredients": drug_result['active_ingredients']
        },
        "us_guideline": {
            "purpose": drug_result['indications'],
            "warnings": drug_result['warnings']
        },
        "kr_safety_standard": {
            "dur_count": len(kr_dur_result),
            "dur_details": kr_dur_result
        }
    }

@app.get("/api/pharmacies")
async def get_nearby_pharmacies(lat: float, lng: float):
    try:
        results = await MapService.find_nearby_pharmacies(lat, lng)
        return {"status": "success", "results": results}
    except Exception as e:
        logger.error(f"Error fetching pharmacies: {e}")
        return {"status": "error", "message": str(e)}

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    # 실행 시: python api_fastapi/main2.py
    uvicorn.run(app, host="127.0.0.1", port=8001)
