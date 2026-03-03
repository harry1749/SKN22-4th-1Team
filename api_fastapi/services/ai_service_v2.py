import os
import re
import json
import logging
from openai import AsyncOpenAI
from prompts.system_prompts import INTENT_CLASS_PROMPT
from prompts.answer_prompts_v2 import SYMPTOM_RESPONSE_PROMPT_V2

logger = logging.getLogger(__name__)


class AIService:
    _client = None

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
    async def classify_intent(cls, query: str):
        """질문 분류 및 영어 키워드 동시 추출 (Router)"""
        client = cls.get_client()
        if not client:
            logger.warning("OpenAI Client is None. Returning default.")
            return {"category": "product_request", "category_reason": "No Client", "keyword": query}

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": INTENT_CLASS_PROMPT.format(user_query=query)},
                ],
                temperature=0,
                max_tokens=150,
                response_format={"type": "json_object"}
            )
            return json.loads(res.choices[0].message.content)
        except Exception as e:
            logger.error(f"Error in classify_intent: {e}")
            return {"category": "product_request", "keyword": query}

    @staticmethod
    def _compress_dur(data: list) -> str:
        """DUR 데이터를 핵심 필드만 추출하여 압축 (입력 토큰 절감)"""
        lines = []
        for item in data:
            ingr = item.get("ingredient", "")
            kr_durs = item.get("kr_durs", [])[:2]
            warnings = "; ".join(
                f"{d.get('type', '')}: {d.get('warning', '')[:60]}"
                for d in kr_durs
            )
            fda = (item.get("fda_warning") or "")[:100]
            lines.append(f"- {ingr} | KR: {warnings} | FDA: {fda}")
        return "\n".join(lines)

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

        compressed_data = cls._compress_dur(data) if isinstance(data, list) else str(data)

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {
                        "role": "system",
                        "content": SYMPTOM_RESPONSE_PROMPT_V2.format(
                            symptom=symptom,
                            data=compressed_data,
                            medications=meds,
                            allergies=allergies,
                            chronic_diseases=diseases
                        )
                    }
                ],
                max_tokens=600,
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
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "친절한 의료 지식 가이드."},
                {"role": "user", "content": query}
            ]
        )
        return res.choices[0].message.content

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
        
        Return ONLY a JSON list of strings. Example: ["acetaminophen", "ibuprofen"]
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": "You are a medical assistant."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            data = json.loads(res.choices[0].message.content)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return list(data.values())[0] if data else []
            return []
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
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": "You are a medical semantics analyzer."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=60,
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
        
        Return ONLY a JSON list of strings. Example: ["migraine", "pain relief", "head pain"]
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": "You are a medical terminologist."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            data = json.loads(res.choices[0].message.content)

            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                synonyms = []
                for v in data.values():
                    if isinstance(v, list):
                        synonyms.extend(v)
                    elif isinstance(v, str):
                        synonyms.append(v)
                return synonyms
            return []
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
        
        Return ONLY a JSON list of strings. Example: ["Paracetamol", "APAP", "N-acetyl-p-aminophenol"]
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": "You are a pharmaceutical terminologist."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            data = json.loads(res.choices[0].message.content)

            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                synonyms = []
                for v in data.values():
                    if isinstance(v, list):
                        synonyms.extend(v)
                    elif isinstance(v, str):
                        synonyms.append(v)
                return synonyms
            return []
        except Exception as e:
            logger.error(f"Error in get_synonyms: {e}")
            return []

    @classmethod
    async def summarize_fda_warning(cls, text: str):
        """
        FDA 경고문(영문)을 한국어로 핵심만 요약
        """
        client = cls.get_client()
        if not client or not text:
            return None

        prompt = f"""
        Summarize the following FDA warning text into **Korean** in 1-2 concise sentences.
        Focus on the most critical safety information (contraindications, serious side effects).
        If the text is generic (e.g., "See full prescribing information"), return "특이사항 없음".
        
        [FDA Warning Text]:
        {text[:1000]}
        """

        try:
            res = await client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": "You are a medical translator."},
                    {"role": "user", "content": prompt}
                ]
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error in summarize_fda_warning: {e}")
            return None

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
                model="gpt-4.1-nano",
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
