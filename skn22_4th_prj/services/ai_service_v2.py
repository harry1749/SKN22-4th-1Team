import os
import re
import json
import logging
from openai import AsyncOpenAI
from prompts.system_prompts import INTENT_CLASS_PROMPT
from prompts.answer_prompts_v2 import SYMPTOM_RESPONSE_PROMPT_V2
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class AIService:
    _client = None

    @staticmethod
    def _to_plain_text(text: str) -> str:
        """Normalize LLM output into plain text for UI rendering."""
        value = str(text or "")
        if not value.strip():
            return ""

        value = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
        value = re.sub(r"^\s*#{1,6}\s*", "", value, flags=re.MULTILINE)
        value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.MULTILINE)
        value = re.sub(r"^\s*\d+\.\s+", "", value, flags=re.MULTILINE)
        value = re.sub(r"^\s*>\s?", "", value, flags=re.MULTILINE)
        value = re.sub(r"(```|`)", "", value)
        value = re.sub(r"(\*\*|__|\*|_|~~)", "", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @classmethod
    def get_client(cls):
        if cls._client:
            return cls._client
        try:
            api_key = os.getenv("OPENAI_API_KEY")
            if api_key:
                cls._client = AsyncOpenAI(api_key=api_key)
            return cls._client
        except Exception as e:
            logger.error(f"Error initializing OpenAI client: {e}")
            return None

    @classmethod
    async def classify_intent_v2(cls, query: str):
        """질문 분류 및 영어 키워드, 캐시 키 동시 추출 (통합 라우터)"""
        client = cls.get_client()
        if not client:
            return {"category": "general_medical", "keyword": query, "cache_key": query}

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": INTENT_CLASS_PROMPT.format(user_query=query)},
                ],
                temperature=0,
                response_format={"type": "json_object"}
            )
            data = json.loads(res.choices[0].message.content)
            if data.get("category") == "symptom_recommendation":
                canonical = await cls.canonicalize_symptom_term(
                    query=query,
                    hint_keyword=data.get("keyword"),
                )
                if canonical:
                    data["keyword"] = canonical
            if "cache_key" not in data:
                data["cache_key"] = data.get("keyword") or query
            return data
        except Exception as e:
            logger.error(f"Error in classify_intent_v2: {e}")
            return {"category": "general_medical", "keyword": query, "cache_key": query}


    @classmethod
    async def classify_intent(cls, query: str):
        """Backward-compatible classifier entrypoint used by graph nodes."""
        data = await cls.classify_intent_v2(query)
        if "category" not in data:
            data["category"] = "general_medical"
        if "keyword" not in data:
            data["keyword"] = query
        return data

    @classmethod
    async def normalize_product_keyword(cls, query: str, hint_keyword: str = "") -> str:
        """
        Normalize product-focused query into a single FDA-searchable term.
        Returns English brand/generic when possible (e.g., Tylenol, acetaminophen).
        """
        fallback = str(hint_keyword or query or "").strip()
        client = cls.get_client()
        if not client:
            return fallback

        prompt = (
            "You are an FDA OTC search term normalizer.\n"
            "Given a user query and optional hint keyword, return ONE best searchable term.\n"
            "Rules:\n"
            "- Prefer a specific English brand name or generic ingredient used by FDA labels.\n"
            "- If the user query already contains a valid product term, keep it.\n"
            "- If no product can be identified, return 'none'.\n"
            "Return JSON only."
        )

        payload = {
            "query": str(query or "").strip(),
            "hint_keyword": str(hint_keyword or "").strip(),
        }

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "normalized_product_keyword",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "product_term": {"type": "string"},
                            },
                            "required": ["product_term"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content or "{}")
            term = str((data or {}).get("product_term") or "").strip()
            if not term or term.lower() == "none":
                return fallback
            return term
        except Exception as e:
            logger.warning(f"Error in normalize_product_keyword: {e}")
            return fallback

    @classmethod
    async def canonicalize_symptom_term(cls, query: str, hint_keyword: str = "") -> str:
        """
        Convert user symptom text into one Korean canonical medical term.
        Example: '머리 아파' -> '두통'
        """
        client = cls.get_client()
        if not client:
            return hint_keyword or query

        prompt = f"""
        사용자 증상 표현을 한국어 표준 의학용어 1개로 정규화하세요.
        - 최대 1~2 단어
        - 가장 대표적인 증상명으로 선택
        - 약품 효능(efficacy) 컬럼 검색에 쓰일 수 있어야 함

        사용자 입력: "{query}"
        분류기 힌트 키워드: "{hint_keyword}"
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "당신은 한국어 의학용어 표준화 전문가입니다."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "canonical_symptom_term",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "symptom_term": {"type": "string"},
                            },
                            "required": ["symptom_term"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content)
            term = str(data.get("symptom_term") or "").strip()
            return term or (hint_keyword or query)
        except Exception as e:
            logger.warning(f"Error in canonicalize_symptom_term: {e}")
            return hint_keyword or query

    @staticmethod
    def _normalize_string_list(values, limit: int = 8):
        """Normalize model output into a de-duplicated list of non-empty strings."""
        if not isinstance(values, list):
            return []

        cleaned = []
        seen = set()
        for item in values:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if len(value) < 2:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
            if len(cleaned) >= limit:
                break
        return cleaned

    @classmethod
    async def select_direct_symptom_ingredients(
        cls,
        symptom: str,
        candidates,
        top_n: int = 5,
    ):
        """
        Select ingredients that directly affect the given symptom from candidate list.
        Input candidates may be:
        - list[str]
        - list[{"ingredient": str, "score": int}]
        """
        normalized = []
        seen = set()

        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict):
                    name = canonicalize_ingredient_name(item.get("ingredient"))
                    score = int(item.get("score", 0) or 0)
                else:
                    name = canonicalize_ingredient_name(item)
                    score = 0
                if not name or name in seen:
                    continue
                seen.add(name)
                normalized.append({"ingredient": name, "score": score})

        if not normalized:
            return []

        normalized.sort(key=lambda x: (-x["score"], x["ingredient"]))
        fallback = [x["ingredient"] for x in normalized[: max(top_n, 1)]]

        client = cls.get_client()
        if not client:
            return fallback[:top_n]

        # Limit payload size for latency/stability while keeping broad candidate coverage.
        shortlist = normalized[:80]
        prompt = (
            "You are a clinical pharmacology assistant.\n"
            "From the provided candidate active ingredients, select ingredients that directly help relieve the symptom.\n"
            "Exclude ingredients that are mostly supportive (coating agents, probiotics, vitamins/minerals) unless they directly treat the symptom.\n"
            "Use ONLY ingredients from the candidate list exactly as written.\n"
            f"Select up to {top_n} ingredients.\n"
        )

        payload = {
            "symptom": str(symptom or "").strip(),
            "candidates": shortlist,
        }

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "direct_ingredient_selection",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "direct_ingredients": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 0,
                                    "maxItems": 10,
                                }
                            },
                            "required": ["direct_ingredients"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content)
            raw = data.get("direct_ingredients", []) if isinstance(data, dict) else []
            selected = []
            allowed = {x["ingredient"] for x in shortlist}
            for name in raw:
                token = canonicalize_ingredient_name(name)
                if not token or token not in allowed or token in selected:
                    continue
                selected.append(token)
                if len(selected) >= top_n:
                    break

            if len(selected) < top_n:
                for item in shortlist:
                    token = item["ingredient"]
                    if token in selected:
                        continue
                    selected.append(token)
                    if len(selected) >= top_n:
                        break
            return selected[:top_n]
        except Exception as e:
            logger.warning(f"Error in select_direct_symptom_ingredients: {e}")
            return fallback[:top_n]

    @staticmethod
    def _truncate_text(value, max_len: int):
        if value is None:
            return None
        text = str(value).strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    @classmethod
    def _compact_symptom_data(
        cls,
        data,
        max_ingredients: int = 8,
        max_kr_durs: int = 3,
        max_warning_chars: int = 700,
    ):
        """
        Compact DUR/FDA payload to improve model stability and avoid token bloat.
        """
        if not isinstance(data, list):
            return data

        compact = []
        for item in data[:max_ingredients]:
            if not isinstance(item, dict):
                continue

            ingredient = str(item.get("ingredient") or "").strip().upper()
            kr_durs = item.get("kr_durs") if isinstance(item.get("kr_durs"), list) else []
            compact_kr = []

            for dur in kr_durs[:max_kr_durs]:
                if not isinstance(dur, dict):
                    continue
                compact_kr.append(
                    {
                        "type": str(dur.get("type") or "").strip(),
                        "kor_name": str(dur.get("kor_name") or "").strip(),
                        "warning": cls._truncate_text(dur.get("warning"), 220),
                    }
                )

            compact.append(
                {
                    "ingredient": ingredient,
                    "kr_durs": compact_kr,
                    "fda_warning": cls._truncate_text(
                        item.get("fda_warning"), max_warning_chars
                    ),
                }
            )

        return compact

    @classmethod
    async def generate_symptom_answer(cls, symptom, data, user_profile=None):
        """성분 및 DUR 데이터를 기반으로 최종 AI 답변 생성 (RAG)"""
        client = cls.get_client()
        if not client:
            return "OpenAI API 키가 설정되지 않아 답변을 생성할 수 없습니다."

        meds = "None"
        allergies = "None"
        diseases = "None"

        if user_profile:
            meds = user_profile.get("current_medications") or "None"
            allergies = user_profile.get("allergies") or "None"
            diseases = user_profile.get("chronic_diseases") or "None"
            logger.debug(f"User Profile — Meds: {meds}, Allergies: {allergies}, Diseases: {diseases}")

        try:
            analysis_data = {
                "symptom": symptom,
                "current_medications": meds,
                "allergies": allergies,
                "chronic_diseases": diseases
            }
            
            compact_data = cls._compact_symptom_data(data)
            ingredient_count = len(compact_data) if isinstance(compact_data, list) else 1
            
            # .format() 대신 .replace() 사용하여 중괄호 {} 충돌 방지
            system_prompt = SYMPTOM_RESPONSE_PROMPT_V2.replace(
                "{analysis}", "Refer to the USER message JSON field: analysis"
            )
            system_prompt = system_prompt.replace(
                "{data}", "Refer to the USER message JSON field: data"
            )
            system_prompt = system_prompt.replace("{ingredient_count}", str(ingredient_count))

            user_payload = {"analysis": analysis_data, "data": compact_data}
            user_payload_json = json.dumps(
                user_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": f"[INPUT_JSON]{user_payload_json}",
                    }
                ],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(res.choices[0].message.content)
        except Exception as e:
            return {"summary": f"답변 생성 중 오류가 발생했습니다: {str(e)}", "ingredients": []}

    @classmethod
    async def generate_general_answer(cls, query: str):
        """일반 의학 지식 질문 처리"""
        client = cls.get_client()
        if not client:
            return "OpenAI API 키가 설정되지 않았습니다."

        res = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "친절한 의료 지식 가이드."},
                {"role": "user", "content": query}
            ]
        )
        return cls._to_plain_text(res.choices[0].message.content)

    @staticmethod
    def _extract_responses_text(response) -> str:
        """Best-effort extractor for Responses API text output."""
        text = getattr(response, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        chunks = []
        output = getattr(response, "output", None)
        if isinstance(output, list):
            for item in output:
                content_list = getattr(item, "content", None)
                if isinstance(content_list, list):
                    for content in content_list:
                        content_text = getattr(content, "text", None)
                        if isinstance(content_text, str) and content_text.strip():
                            chunks.append(content_text.strip())
                        elif isinstance(content, dict):
                            dict_text = content.get("text")
                            if isinstance(dict_text, str) and dict_text.strip():
                                chunks.append(dict_text.strip())
                elif isinstance(item, dict):
                    for content in item.get("content", []) or []:
                        if not isinstance(content, dict):
                            continue
                        dict_text = content.get("text")
                        if isinstance(dict_text, str) and dict_text.strip():
                            chunks.append(dict_text.strip())
        return "\n".join(chunks).strip()

    @classmethod
    async def generate_web_search_answer(cls, query: str):
        """
        Generate answer using OpenAI Responses API + web search tool.
        Falls back to `generate_general_answer` when web tool/model is unavailable.
        """
        client = cls.get_client()
        if not client:
            return "OpenAI API 키가 설정되지 않았습니다."

        web_system_prompt = (
            "당신은 의약품 정보 안내 도우미입니다. "
            "가능하면 최신 공개 웹 정보를 검색해 한국어로 간결하고 정확하게 답하세요. "
            "의학적 진단/처방은 하지 말고, 안전수칙(복용량·상호작용·금기 확인 필요)을 포함하세요."
        )
        composed_input = (
            f"{web_system_prompt}\n\n"
            f"[사용자 질문]\n{str(query or '').strip()}"
        )

        model_candidates = [
            os.getenv("OPENAI_WEB_SEARCH_MODEL", "gpt-4.1-mini"),
            "gpt-4o-mini",
        ]
        # Some SDK/model combinations use `web_search_preview`, others `web_search`.
        tool_candidates = [{"type": "web_search_preview"}, {"type": "web_search"}]

        seen = set()
        for model_name in model_candidates:
            model_name = str(model_name or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)

            for tool in tool_candidates:
                try:
                    response = await client.responses.create(
                        model=model_name,
                        tools=[tool],
                        input=composed_input,
                    )
                    answer = cls._extract_responses_text(response)
                    if answer:
                        return cls._to_plain_text(answer)
                except Exception as e:
                    logger.warning(
                        "web search answer failed (model=%s, tool=%s): %s",
                        model_name,
                        tool.get("type"),
                        e,
                    )

        logger.warning("Falling back to non-web general answer for query: %s", query)
        return await cls.generate_general_answer(query)

    @classmethod
    async def recommend_ingredients_for_symptom(cls, symptom: str):
        """
        FDA 검색 실패 시, AI에게 해당 증상에 효과적인 성분(영문 성분명) 리스트를 추천받음 (Agentic Search)
        """
        client = cls.get_client()
        if not client:
            return []

        prompt = f"""
        Users asked for medicine recommendations for: "{symptom}"
        But no direct match was found in the FDA indication database.
        
        Please listed 3-5 standard, over-the-counter active ingredients (generic names in English) 
        that are commonly used for this symptom.
        
        Return ONLY a JSON object with this exact shape:
        {{"ingredients": ["acetaminophen", "ibuprofen"]}}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical assistant."},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ingredient_recommendation",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "ingredients": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 0,
                                    "maxItems": 8,
                                }
                            },
                            "required": ["ingredients"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content)
            if not isinstance(data, dict):
                return []
            return cls._normalize_string_list(data.get("ingredients", []), limit=8)
        except Exception as e:
            logger.error(f"Error in recommend_ingredients_for_symptom: {e}")
            return []

    @classmethod
    async def normalize_symptom_query(cls, query: str) -> str:
        """
        사용자의 증상 입력을 분석하여 표준화된 영어 해시 키(Cache Key)로 변환합니다.
        예: "머리가 깨질듯 아파" -> "headache_severe_splitting"
        """
        client = cls.get_client()
        if not client:
            return query.strip().lower()

        prompt = f"""
        Analyze the following symptom described by a user: "{query}"
        
        Extract the core symptom, its severity, and its quality (if any).
        Normalize these into standard English medical terms.
        
        Rules:
        1. "symptom": The core issue (e.g., "headache", "stomachache", "cough")
        2. "severity": "mild", "moderate", or "severe". (Default is "moderate" if not specified)
        3. "quality": How it feels (e.g., "splitting", "dull", "sharp", "burning"). If not specified or obvious, use "none".
        
        Return a JSON object with these exactly 3 keys:
        {{"symptom": "...", "severity": "...", "quality": "..."}}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical semantics analyzer."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            data = json.loads(res.choices[0].message.content)

            symptom = data.get("symptom", "").strip().lower().replace(" ", "_").replace("-", "_")
            severity = data.get("severity", "moderate").strip().lower()
            quality = data.get("quality", "none").strip().lower()

            if not symptom:
                return query.strip().lower()

            return f"{symptom}_{severity}_{quality}"
        except Exception as e:
            logger.error(f"Error in normalize_symptom_query: {e}")
            return re.sub(r'\s+', '_', re.sub(r'[^\w\s가-힣]', '', query.strip())).lower()

    @classmethod
    async def get_symptom_synonyms(cls, symptom: str):
        """
        FDA 검색 실패 시, 해당 증상과 유사한 영문 의학 용어(Synonyms)를 AI에게 조회하여
        FDA API 재검색에 사용할 키워드를 확보함
        """
        client = cls.get_client()
        if not client:
            return []

        prompt = f"""
        The user searched for the medical symptom: "{symptom}", but no direct match was found in the FDA indication database.
        Please provide 3-5 alternative standard English medical terms or related keywords (e.g., "headache" -> "migraine", "pain relief").
        
        Return ONLY a JSON object with this exact shape:
        {{"synonyms": ["migraine", "pain relief", "head pain"]}}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical terminologist."},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "symptom_synonyms",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "synonyms": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 0,
                                    "maxItems": 10,
                                }
                            },
                            "required": ["synonyms"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content)
            if not isinstance(data, dict):
                return []
            return cls._normalize_string_list(data.get("synonyms", []), limit=10)
        except Exception as e:
            logger.error(f"Error in get_symptom_synonyms: {e}")
            return []

    @classmethod
    async def get_synonyms(cls, ingredient: str):
        """
        DUR 검색 실패 시, 해당 성분의 이명(Synonyms)이나 한국어 통용 명칭을 AI에게 조회
        """
        client = cls.get_client()
        if not client:
            return []

        prompt = f"""
        Provide 3-5 common synonyms or alternate names for the drug ingredient: "{ingredient}".
        Include:
        1. Official synonyms (e.g., Acetaminophen <-> Paracetamol)
        2. Common brand names treated as generics in some contexts (if applicable)
        3. Korean standard name if known (written in English or Korean)
        
        Return ONLY a JSON object with this exact shape:
        {{"synonyms": ["Paracetamol", "APAP", "N-acetyl-p-aminophenol"]}}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a pharmaceutical terminologist."},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ingredient_synonyms",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "synonyms": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 0,
                                    "maxItems": 10,
                                }
                            },
                            "required": ["synonyms"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            data = json.loads(res.choices[0].message.content)
            if not isinstance(data, dict):
                return []
            return cls._normalize_string_list(data.get("synonyms", []), limit=10)
        except Exception as e:
            logger.error(f"Error in get_synonyms: {e}")
            return []

    @classmethod
    async def bulk_summarize_fda_warnings(cls, warnings_dict: dict) -> dict:
        """여러 성분의 FDA 경고문을 한 번에 요약 (벌크 처리)"""
        client = cls.get_client()
        if not client or not warnings_dict:
            return {}

        targets = {k: v for k, v in warnings_dict.items() if v and len(v) > 20}
        if not targets:
            return {k: "특이사항 없음" for k in warnings_dict.keys()}

        prompt = f"""
        Translate and summarize the following FDA drug warnings into Korean (1-2 sentences each).
        Return ONLY a JSON object where keys are the ingredient names and values are the summarized Korean text.
        
        [Warnings to Summarize]:
        {json.dumps(targets, ensure_ascii=False)}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a medical translator specialized in drug safety."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                response_format={"type": "json_object"}
            )
            summaries = json.loads(res.choices[0].message.content)
            result = {k: "특이사항 없음" for k in warnings_dict.keys()}
            result.update(summaries)
            return result
        except Exception as e:
            logger.error(f"Error in bulk_summarize_fda_warnings: {e}")
            return {k: "요약 오류" for k in warnings_dict.keys()}


    @classmethod
    async def summarize_fda_warning(cls, text: str):
        """Single-warning helper kept for compatibility with existing services."""
        if not text:
            return None
        result = await cls.bulk_summarize_fda_warnings({"_single": text})
        summary = result.get("_single")
        if not summary:
            return None
        return summary

    @classmethod
    async def translate_purposes(cls, purposes: list) -> list:
        """
        FDA 약물 purpose(효능/설명)를 한국어로 일괄 번역
        """
        client = cls.get_client()
        if not client or not purposes:
            return purposes

        prompt = f"""
        Translate the following list of medical drug purposes (indications/descriptions) into Korean concisely (1-2 sentences each).
        Return ONLY a JSON object with a key 'translated_purposes' containing the list of translated strings in the exact same order and length.
        
        Input list:
        {json.dumps(purposes, ensure_ascii=False)}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional medical translator."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            content = json.loads(res.choices[0].message.content)
            return content.get("translated_purposes", purposes)
        except Exception as e:
            logger.error(f"Error in translate_purposes: {e}")
            return purposes
