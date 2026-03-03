import os
import re
import logging
import asyncio
import json
from supabase import create_client, Client

from services.ai_service_v2 import AIService
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class SupabaseService:
    _client = None
    _roadmap_cache_disabled = False
    _KCD_TABLE = "kcd_info"
    _KCD_CODE_COL = "kcd_code"
    _KCD_NAME_KOR_COL = "kcd_name_kor"
    _KCD_NAME_ENG_COL = "kcd_name_eng"

    @staticmethod
    async def _run_io(func):
        """Run blocking Supabase SDK calls in a worker thread."""
        return await asyncio.to_thread(func)

    @classmethod
    def get_client(cls) -> Client:
        if cls._client:
            return cls._client

        url = os.environ.get("SUPABASE_URL")
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        anon_key = os.environ.get("SUPABASE_KEY")
        key = service_role_key or anon_key

        if not url or not key:
            logger.error(
                "SUPABASE_URL and one of SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY must be set in .env"
            )
            return None

        if not service_role_key:
            logger.warning(
                "[Supabase] SUPABASE_SERVICE_ROLE_KEY is not set; RLS may block profile writes."
            )

        cls._client = create_client(url, key)
        return cls._client

    @staticmethod
    def _dedupe_ordered(items):
        unique_items = []
        seen = set()
        for raw in items or []:
            value = str(raw or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(value)
        return unique_items

    @staticmethod
    def _parse_ingredient_tokens(text: str):
        raw = str(text or "").upper()
        if not raw:
            return []
        parts = re.split(r",|/|;|\bAND\b|\bWITH\b|\+|\n", raw, flags=re.IGNORECASE)
        tokens = []
        seen = set()
        for part in parts:
            token = str(part or "").strip()
            if not token:
                continue
            token = re.sub(r"\([^)]*\)", " ", token)
            token = re.sub(
                r"\b\d+(?:\.\d+)?\s*(MG|MCG|G|ML|%)\b",
                " ",
                token,
                flags=re.IGNORECASE,
            )
            token = re.sub(r"[^A-Z0-9\s\-]", " ", token)
            token = re.sub(r"\s+", " ", token).strip()
            token = canonicalize_ingredient_name(token)
            token = str(token or "").strip().upper()
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @staticmethod
    def _kcd_compact(text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    @classmethod
    def _is_kcd_source_missing_error(cls, error: Exception) -> bool:
        message = str(error or "").lower()
        return (
            ("relation" in message and "does not exist" in message)
            or ("table" in message and "not found" in message)
            or ("could not find the table" in message)
        )

    @classmethod
    async def _ensure_kcd_source(cls):
        """Standardized to use kcd_info table for KCD 9th revision."""
        source = {
            "table": cls._KCD_TABLE,
            "code_col": cls._KCD_CODE_COL,
            "name_kor_col": cls._KCD_NAME_KOR_COL,
            "name_eng_col": cls._KCD_NAME_ENG_COL,
            "ready": False,
            "reason": None,
            "error": None,
        }

        client = cls.get_client()
        if not client:
            source["reason"] = "supabase_unavailable"
            source["error"] = "Supabase client is unavailable."
            return source

        try:
            await cls._run_io(
                lambda: (
                    client.table(source["table"])
                    .select(f"{source['code_col']}, {source['name_kor_col']}")
                    .limit(1)
                    .execute()
                )
            )
            source["ready"] = True
            return source
        except Exception as e:
            source["reason"] = (
                "kcd_source_missing"
                if cls._is_kcd_source_missing_error(e)
                else "kcd_source_query_error"
            )
            source["error"] = str(e)
            logger.error(
                f"[Supabase] KCD source check failed ({source['table']}): {source['error']}"
            )
            return source

    @classmethod
    async def auth_sign_up(cls, email, password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.sign_up({"email": email, "password": password})
            )
            return response.user, None
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Supabase Auth] Sign up error: {error_msg}")
            if "already registered" in error_msg.lower():
                return None, "exists"
            return None, error_msg

    @classmethod
    async def auth_sign_in(cls, email, password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.sign_in_with_password(
                    {"email": email, "password": password}
                )
            )
            return response.user, response.session
        except Exception as e:
            logger.error(f"[Supabase Auth] Sign in error: {e}")
            return None, None

    @classmethod
    async def auth_update_password(cls, new_password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.update_user({"password": new_password})
            )
            return response.user, None
        except Exception as e:
            logger.error(f"[Supabase Auth] Password update error: {e}")
            return None, str(e)

    @classmethod
    async def auth_delete_user(cls, user_id: str):
        url = os.environ.get("SUPABASE_URL")
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        key = service_role_key or os.environ.get("SUPABASE_KEY")
        if not url or not key:
            return False, "Supabase credentials are not configured."
        if not service_role_key:
            logger.warning(
                "[Supabase] SUPABASE_SERVICE_ROLE_KEY is missing; admin.delete_user may fail."
            )
        try:
            # Use a fresh client to avoid stale auth state after admin operations.
            fresh_client = await cls._run_io(lambda: create_client(url, key))
            await cls._run_io(lambda: fresh_client.auth.admin.delete_user(str(user_id)))
            cls._client = None
            return True, None
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Supabase Auth] Account delete error for {user_id}: {error_msg}")
            return False, error_msg

    @classmethod
    async def get_dur_by_ingr(cls, ingr_text: str):
        if not ingr_text:
            return []

        ingr_list = [
            i.strip() for i in ingr_text.replace(",", "/").split("/") if len(i.strip()) > 1
        ]
        dur_data = await cls._get_dur_data_from_supabase(ingr_list)

        seen = set()
        results = []
        for d in dur_data:
            dur_type = d.get("dur_type")
            type_name = d.get("type_name") or dur_type
            mixture_ingr = (d.get("mixture_ingr_eng_name") or "").strip()
            warning_msg = d.get("prohbt_content") or d.get("remark")
            if dur_type == "COMBINED" and mixture_ingr:
                warning_msg = f"병용금기 성분: {mixture_ingr}"

            # Deduplication key
            key = (type_name, warning_msg)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "type": type_name,
                    "ingr_name": d.get("ingr_kor_name"),
                    "warning_msg": warning_msg,
                    "severity": d.get("critical_value"),
                }
            )
        return results

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))
        enriched_data = []

        from services.drug_service import DrugService as OriginalDrugService

        for ingr in unique_ingrs:
            durs = await cls._get_kr_durs_supabase(ingr)
            fda_warn = await OriginalDrugService.get_fda_warnings_by_ingr(ingr)
            if fda_warn:
                summary = await AIService.summarize_fda_warning(fda_warn)
                if summary:
                    fda_warn = summary

            enriched_data.append(
                {
                    "ingredient": ingr,
                    "kr_durs": durs,
                    "fda_warning": fda_warn,
                }
            )
        return enriched_data

    @classmethod
    async def _get_kr_durs_supabase(cls, ingr_name):
        if not ingr_name:
            return []

        target_name = canonicalize_ingredient_name(ingr_name.strip()) or ingr_name.strip()
        if not target_name:
            return []

        client = cls.get_client()
        if not client:
            return []

        dur_list = []
        try:
            # 1) prefix match first (faster/saner), 2) broad contains fallback
            response = await cls._run_io(
                lambda: (
                    client.table("dur_master")
                    .select(
                        "dur_type,type_name,ingr_kor_name,prohbt_content,remark,mixture_ingr_eng_name"
                    )
                    .ilike("ingr_eng_name", f"{target_name.lower()}%")
                    .execute()
                )
            )
            dur_list = response.data or []

            if not dur_list:
                response = await cls._run_io(
                    lambda: (
                        client.table("dur_master")
                        .select(
                            "dur_type,type_name,ingr_kor_name,prohbt_content,remark,mixture_ingr_eng_name"
                        )
                        .ilike("ingr_eng_name", f"%{target_name.lower()}%")
                        .execute()
                    )
                )
                dur_list = response.data or []
        except Exception as e:
            logger.error(f"[Supabase] DUR query error for '{target_name}': {e}")
            return []

        grouped_results = {}
        for d in dur_list:
            dur_type = d.get("dur_type")
            type_name = str(d.get("type_name") or dur_type or "").strip()
            if not type_name:
                continue

            mixture_ingr = (d.get("mixture_ingr_eng_name") or "").strip()
            if dur_type == "COMBINED" and mixture_ingr:
                content = f"병용금기 성분: {mixture_ingr}"
            else:
                content = f"{type_name} 금기 사항이 있을 수 있습니다. 의사/약사와 상담 후 복용하세요."

            if type_name not in grouped_results:
                grouped_results[type_name] = {
                    "type": type_name,
                    "kor_name": d.get("ingr_kor_name"),
                    "warnings": set(),
                }
            grouped_results[type_name]["warnings"].add(content)

        results = []
        for val in grouped_results.values():
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append(
                {
                    "type": val["type"],
                    "kor_name": val["kor_name"],
                    "warning": combined_warning,
                }
            )
        return results

    @classmethod
    async def _get_dur_data_from_supabase(cls, ingr_list: list):
        client = cls.get_client()
        if not client:
            return []

        all_results = []
        for ingr in ingr_list:
            if not ingr:
                continue
            target = canonicalize_ingredient_name(ingr.strip()) or ingr.strip()
            try:
                # 1) prefix match first
                response = await cls._run_io(
                    lambda: (
                        client.table("dur_master")
                        .select(
                            "dur_type,type_name,ingr_kor_name,critical_value,prohbt_content,remark,mixture_ingr_eng_name"
                        )
                        .ilike("ingr_eng_name", f"{target.lower()}%")
                        .execute()
                    )
                )
                rows = response.data or []

                # 2) contains fallback
                if not rows:
                    response = await cls._run_io(
                        lambda: (
                            client.table("dur_master")
                            .select(
                                "dur_type,type_name,ingr_kor_name,critical_value,prohbt_content,remark,mixture_ingr_eng_name"
                            )
                            .ilike("ingr_eng_name", f"%{target.lower()}%")
                            .execute()
                        )
                    )
                    rows = response.data or []

                if rows:
                    all_results.extend(rows)
            except Exception as e:
                logger.error(f"[Supabase] Batch DUR query error for '{target}': {e}")

        return all_results

    @classmethod
    async def get_symptom_cache(cls, query_text: str):
        client = cls.get_client()
        if not client:
            return None

        try:
            response = await cls._run_io(
                lambda: (
                client.table("search_cache")
                .select("*")
                .eq("query_text", query_text)
                .limit(1)
                .execute()
                )
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            logger.error(f"[Cache] Error reading cache for '{query_text}': {e}")
        return None

    @classmethod
    async def set_symptom_cache(
        cls,
        query_text: str,
        category: str,
        fda_data: list,
        dur_data: list,
        final_answer: str,
        recommended_ingredients: list,
    ):
        client = cls.get_client()
        if not client:
            return False

        try:
            payload = {
                "query_text": query_text,
                "category": category,
                "fda_data": fda_data if fda_data else [],
                "dur_data": dur_data if dur_data else [],
                "final_answer": final_answer,
                "recommended_ingredients": (
                    recommended_ingredients if recommended_ingredients else []
                ),
            }
            await cls._run_io(
                lambda: client.table("search_cache")
                .upsert(payload, on_conflict="query_text")
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"[Cache] Error saving cache for '{query_text}': {e}")
            return False

    @classmethod
    async def get_roadmap_cache(cls, query_text: str):
        """
        Load roadmap cache payload from search_cache.
        Uses category='roadmap' and maps existing columns into roadmap fields.
        """
        if cls._roadmap_cache_disabled:
            return None

        client = cls.get_client()
        if not client:
            return None

        try:
            response = await cls._run_io(
                lambda: (
                    client.table("search_cache")
                    .select("*")
                    .eq("query_text", query_text)
                    .eq("category", "roadmap")
                    .limit(1)
                    .execute()
                )
            )
            row = (response.data or [None])[0]
            if not row:
                return None

            raw_card = row.get("final_answer")
            pharmacist_card = {}
            if isinstance(raw_card, dict):
                pharmacist_card = raw_card
            elif isinstance(raw_card, str) and raw_card.strip():
                try:
                    parsed = json.loads(raw_card)
                    if isinstance(parsed, dict):
                        pharmacist_card = parsed
                except Exception:
                    pharmacist_card = {}

            return {
                "mapping_result": row.get("fda_data") or {},
                "dosage_warnings": row.get("dur_data") or [],
                "pharmacist_card": pharmacist_card,
            }
        except Exception as e:
            if cls._is_kcd_source_missing_error(e):
                if not cls._roadmap_cache_disabled:
                    logger.warning(
                        "[Cache] search_cache table is unavailable. "
                        "Roadmap cache will be disabled."
                    )
                cls._roadmap_cache_disabled = True
                return None
            logger.warning(f"[Cache] Roadmap cache read failed for '{query_text}': {e}")
            return None

    @classmethod
    async def set_roadmap_cache(
        cls,
        query_text: str,
        mapping_result: dict,
        pharmacist_card: dict,
        dosage_warnings: list,
    ):
        """
        Save roadmap cache payload into search_cache.
        Never raises; returns bool.
        """
        if cls._roadmap_cache_disabled:
            return False

        client = cls.get_client()
        if not client:
            return False

        try:
            payload = {
                "query_text": query_text,
                "category": "roadmap",
                "fda_data": mapping_result if isinstance(mapping_result, dict) else {},
                "dur_data": dosage_warnings if isinstance(dosage_warnings, list) else [],
                "final_answer": json.dumps(
                    pharmacist_card if isinstance(pharmacist_card, dict) else {},
                    ensure_ascii=False,
                ),
                "recommended_ingredients": [],
            }
            await cls._run_io(
                lambda: (
                    client.table("search_cache")
                    .upsert(payload, on_conflict="query_text")
                    .execute()
                )
            )
            return True
        except Exception as e:
            if cls._is_kcd_source_missing_error(e):
                if not cls._roadmap_cache_disabled:
                    logger.warning(
                        "[Cache] search_cache table is unavailable. "
                        "Roadmap cache will be disabled."
                    )
                cls._roadmap_cache_disabled = True
                return False
            logger.warning(f"[Cache] Roadmap cache save failed for '{query_text}': {e}")
            return False

    @classmethod
    async def search_ingredient_scores_by_symptom(
        cls,
        keyword: str,
        raw_query: str = "",
        max_rows: int = 5000,
        batch_size: int = 1000,
    ):
        """
        Return ranked ingredient scores from unified_drug_info rows matched by efficacy.
        Each score represents how frequently the ingredient appears in matched rows,
        which is used as a direct relevance proxy for the symptom.
        """
        client = cls.get_client()
        if not client:
            return []

        symptom_term = (keyword or "").strip()
        if not symptom_term and raw_query:
            symptom_term = raw_query.strip()
        if not symptom_term:
            return []

        rows = []
        start = 0
        while start < max_rows:
            end = min(start + batch_size - 1, max_rows - 1)
            try:
                response = await cls._run_io(
                    lambda: (
                    client.table("unified_drug_info")
                    .select("main_ingr_eng, efficacy")
                    .ilike("efficacy", f"%{symptom_term}%")
                    .range(start, end)
                    .execute()
                    )
                )
                batch = response.data or []
            except Exception as e:
                logger.warning(
                    f"[Supabase] Symptom efficacy query failed for term '{symptom_term}': {e}"
                )
                return []

            if not batch:
                break

            rows.extend(batch)
            if len(batch) < (end - start + 1):
                break
            start += batch_size

        def extract_ingredient_tokens(text: str):
            if not text:
                return []

            cleaned = re.sub(r"\([^)]*\)", " ", str(text))
            cleaned = re.sub(
                r"\b\d+(\.\d+)?\s*(mg|mcg|g|ml|%)\b", " ", cleaned, flags=re.I
            )
            parts = re.split(r"[,;/+\n]| and | AND ", cleaned)

            tokens = []
            for part in parts:
                token = re.sub(r"\s{2,}", " ", part).strip(" .:-_")
                if len(token) < 3:
                    continue
                tokens.append(token)
            return tokens

        scores = {}
        efficacy_by_ingredient = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            efficacy_text = re.sub(r"\s+", " ", str(row.get("efficacy") or "")).strip()
            for token in extract_ingredient_tokens(row.get("main_ingr_eng")):
                if not re.search(r"[A-Za-z]", token):
                    continue
                normalized = canonicalize_ingredient_name(token)
                if not normalized:
                    continue
                scores[normalized] = scores.get(normalized, 0) + 1
                if efficacy_text and normalized not in efficacy_by_ingredient:
                    efficacy_by_ingredient[normalized] = efficacy_text

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return [
            {
                "ingredient": name,
                "score": score,
                "sample_efficacy": efficacy_by_ingredient.get(name, ""),
            }
            for name, score in ranked
        ]

    @classmethod
    async def search_ingredients_by_symptom(
        cls,
        keyword: str,
        raw_query: str = "",
        top_n: int = 8,
        max_rows: int = 5000,
        limit: int = None,
    ):
        if isinstance(limit, int) and limit > 0:
            max_rows = limit

        ranked = await cls.search_ingredient_scores_by_symptom(
            keyword=keyword,
            raw_query=raw_query,
            max_rows=max_rows,
        )
        if not ranked:
            return []

        ingredients = [item["ingredient"] for item in ranked if item.get("ingredient")]
        if top_n is None or top_n <= 0:
            return ingredients
        return ingredients[:top_n]

    @classmethod
    async def search_drugs(cls, query_text: str, limit: int = 20):
        client = cls.get_client()
        if not client:
            return []

        keyword = str(query_text or "").strip()
        if not keyword:
            return []

        try:
            response = await cls._run_io(
                lambda: (
                client.table("drug_permit_info")
                .select("item_name, entp_name")
                .ilike("item_name", f"%{keyword}%")
                .limit(limit)
                .execute()
                )
            )
            return response.data
        except Exception as e:
            logger.error(f"[Supabase] Drug search error: {e}")
            return []

    @classmethod
    async def get_product_profile(cls, query_terms):
        """
        Build product profile from Supabase tables.
        Primary source: drug_permit_info
        Supplemental efficacy/warnings/dosage: unified_drug_info
        """
        client = cls.get_client()
        if not client:
            return None

        if isinstance(query_terms, str):
            terms = [query_terms]
        elif isinstance(query_terms, list):
            terms = query_terms
        else:
            terms = []

        candidates = cls._dedupe_ordered([str(x or "").strip() for x in terms])
        if not candidates:
            return None

        async def _find_permit_row(term: str):
            token = str(term or "").strip()
            if not token:
                return None
            try:
                # 1) exact name first
                response = await cls._run_io(
                    lambda: (
                        client.table("drug_permit_info")
                        .select("item_name, entp_name, main_ingr_eng, main_ingr_kor")
                        .eq("item_name", token)
                        .limit(1)
                        .execute()
                    )
                )
                rows = response.data or []
                if rows:
                    return rows[0]

                # 2) fuzzy contains fallback
                response = await cls._run_io(
                    lambda: (
                        client.table("drug_permit_info")
                        .select("item_name, entp_name, main_ingr_eng, main_ingr_kor")
                        .ilike("item_name", f"%{token}%")
                        .limit(10)
                        .execute()
                    )
                )
                rows = response.data or []
                if not rows:
                    return None

                token_l = token.lower()
                rows.sort(
                    key=lambda r: (
                        0
                        if str((r or {}).get("item_name") or "").strip().lower() == token_l
                        else 1,
                        len(str((r or {}).get("item_name") or "")),
                    )
                )
                return rows[0]
            except Exception as e:
                logger.warning(f"[Supabase] product profile lookup failed for '{token}': {e}")
                return None

        permit_row = None
        for term in candidates:
            permit_row = await _find_permit_row(term)
            if permit_row:
                break
        if not permit_row:
            return None

        item_name = str((permit_row or {}).get("item_name") or "").strip()
        entp_name = str((permit_row or {}).get("entp_name") or "").strip()
        permit_main_eng = str((permit_row or {}).get("main_ingr_eng") or "").strip()
        permit_main_kor = str((permit_row or {}).get("main_ingr_kor") or "").strip()

        unified_row = {}
        if item_name:
            try:
                response = await cls._run_io(
                    lambda: (
                        client.table("unified_drug_info")
                        .select(
                            "efficacy, use_method, precautions, interaction, side_effects, main_ingr_eng, main_ingr_kor"
                        )
                        .eq("item_name", item_name)
                        .limit(1)
                        .execute()
                    )
                )
                unified_row = (response.data or [{}])[0] or {}
            except Exception as e:
                logger.warning(
                    f"[Supabase] unified_drug_info lookup failed for '{item_name}': {e}"
                )

        main_ingr_eng = (
            permit_main_eng
            or str(unified_row.get("main_ingr_eng") or "").strip()
            or permit_main_kor
            or str(unified_row.get("main_ingr_kor") or "").strip()
        )
        ingredient_list = cls._parse_ingredient_tokens(main_ingr_eng)

        indications = str(unified_row.get("efficacy") or "").strip()
        dosage = str(unified_row.get("use_method") or "").strip()

        warning_candidates = [
            str(unified_row.get("precautions") or "").strip(),
            str(unified_row.get("interaction") or "").strip(),
            str(unified_row.get("side_effects") or "").strip(),
        ]
        warnings = next((x for x in warning_candidates if x), "")

        return {
            "brand_name": item_name or (candidates[0] if candidates else ""),
            "manufacturer_name": entp_name,
            "active_ingredients": main_ingr_eng or "Ingredient Not Found",
            "ingredient_list": ingredient_list,
            "indications": indications or "정보 없음",
            "warnings": warnings or "정보 없음",
            "dosage": dosage or "정보 없음",
            "source": "supabase",
        }

    @classmethod
    async def get_product_dur_by_ingredients(cls, ingredients):
        """
        Return DUR items only for the given product ingredient list.
        """
        if isinstance(ingredients, str):
            ingredient_list = cls._parse_ingredient_tokens(ingredients)
        elif isinstance(ingredients, list):
            ingredient_list = cls._dedupe_ordered(
                [canonicalize_ingredient_name(x) for x in ingredients]
            )
            ingredient_list = [str(x or "").strip().upper() for x in ingredient_list if x]
        else:
            ingredient_list = []

        if not ingredient_list:
            return []

        results = []
        seen = set()
        for ingredient in ingredient_list:
            durs = await cls._get_kr_durs_supabase(ingredient)
            for row in durs or []:
                if not isinstance(row, dict):
                    continue
                type_name = str(row.get("type") or "").strip()
                kor_name = str(row.get("kor_name") or ingredient).strip()
                warning = str(row.get("warning") or "").strip()
                if not type_name:
                    continue
                key = (ingredient.upper(), type_name, warning[:120])
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "type": type_name,
                        "ingr_name": kor_name,
                        "warning_msg": warning,
                        "severity": None,
                    }
                )
        return results

    @classmethod
    async def resolve_valid_drug_names(cls, drug_names: list):
        """Return (valid_names, invalid_names) by exact name lookup in drug_permit_info."""
        client = cls.get_client()
        if not client:
            return [], cls._dedupe_ordered(drug_names)

        names = cls._dedupe_ordered(drug_names)
        if not names:
            return [], []

        valid_names = []
        invalid_names = []
        for name in names:
            rows = []
            try:
                response = await cls._run_io(
                    lambda: (
                        client.table("drug_permit_info")
                        .select("item_name")
                        .eq("item_name", name)
                        .limit(1)
                        .execute()
                    )
                )
                rows = response.data or []

                if not rows:
                    response = await cls._run_io(
                        lambda: (
                            client.table("drug_permit_info")
                            .select("item_name")
                            .ilike("item_name", name)
                            .limit(1)
                            .execute()
                        )
                    )
                    rows = response.data or []
            except Exception as e:
                logger.warning(f"[Supabase] Drug name validation failed for '{name}': {e}")
                invalid_names.append(name)
                continue

            if rows:
                canonical_name = str((rows[0] or {}).get("item_name") or "").strip()
                valid_names.append(canonical_name or name)
            else:
                invalid_names.append(name)

        return cls._dedupe_ordered(valid_names), cls._dedupe_ordered(invalid_names)

    @classmethod
    async def search_kcd(cls, query_text: str, limit: int = 20):
        source = await cls._ensure_kcd_source()
        if not source.get("ready"):
            return []

        keyword = str(query_text or "").strip()
        if not keyword:
            return []
        keyword_lower = keyword.lower()
        keyword_code = re.sub(r"\s+", "", keyword).upper()
 
        client = cls.get_client()
        if not client:
            return []
 
        table_name = source["table"]
        code_col = source["code_col"]
        name_kor_col = source["name_kor_col"]
        name_eng_col = source["name_eng_col"]
 
        try:
            # Search in both Korean and English names
            response = await cls._run_io(
                lambda: (
                    client.table(table_name)
                    .select(f"{code_col}, {name_kor_col}, {name_eng_col}")
                    .or_(
                        f"{name_kor_col}.ilike.%{keyword}%,"
                        f"{name_eng_col}.ilike.%{keyword}%,"
                        f"{code_col}.ilike.%{keyword}%"
                    )
                    .limit(limit)
                    .execute()
                )
            )
            rows = response.data or []
        except Exception as e:
            logger.error(f"[Supabase] KCD search error for '{keyword}': {e}")
            return []

        def _score(row):
            code = str(row.get(code_col) or "").strip().upper()
            kor_name = str(row.get(name_kor_col) or "").strip().lower()
            eng_name = str(row.get(name_eng_col) or "").strip().lower()

            if code == keyword_code:
                return (0, len(code), code)
            if code.startswith(keyword_code):
                return (1, len(code), code)
            if keyword_code and keyword_code in code:
                return (2, len(code), code)
            if kor_name.startswith(keyword_lower):
                return (3, len(kor_name), code)
            if keyword_lower in kor_name:
                return (4, len(kor_name), code)
            if eng_name.startswith(keyword_lower):
                return (5, len(eng_name), code)
            if keyword_lower in eng_name:
                return (6, len(eng_name), code)
            return (7, len(code), code)

        rows = sorted(rows, key=_score)
 
        results = []
        seen_codes = set()
        for row in rows:
            code = str(row.get(code_col) or "").strip().upper()
            kor_name = str(row.get(name_kor_col) or "").strip()
            eng_name = str(row.get(name_eng_col) or "").strip()
            
            if not code or not kor_name or code in seen_codes:
                continue
            seen_codes.add(code)
            
            # Display format: "Korean Name (English Name) [Code]" or "Korean Name [Code]"
            display_name = kor_name
            if eng_name:
                display_name = f"{kor_name} ({eng_name})"
                
            results.append({
                "kcd_code": code,
                "kcd_name": kor_name,
                "kcd_name_eng": eng_name,
                "display": display_name
            })
        return results

    @classmethod
    async def resolve_kcd_terms(cls, raw_terms: list):
        """
        Resolve user-entered disease/allergy terms into canonical KCD records.
        Returns (resolved_items, unresolved_terms, kcd_ready, details).
        """
        terms = cls._dedupe_ordered(raw_terms)
        if not terms:
            return [], [], True, {
                "source_error": None,
                "unmatched": [],
                "ambiguous": [],
                "lookup_errors": [],
            }

        source = await cls._ensure_kcd_source()
        if not source.get("ready"):
            return [], terms, False, {
                "source_error": source.get("reason"),
                "unmatched": [],
                "ambiguous": [],
                "lookup_errors": [],
            }
 
        client = cls.get_client()
        if not client:
            return [], terms, False, {
                "source_error": "supabase_unavailable",
                "unmatched": [],
                "ambiguous": [],
                "lookup_errors": [],
            }
 
        table_name = source["table"]
        code_col = source["code_col"]
        name_kor_col = source["name_kor_col"]
        name_eng_col = source["name_eng_col"]
 
        resolved = []
        unresolved = []
        unmatched = []
        ambiguous = []
        lookup_errors = []
        code_pattern = re.compile(r"\b([A-Za-z]\d{2}(?:\.\d+)?)\b")
 
        for term in terms:
            token = str(term or "").strip()
            if not token:
                continue
 
            rows = []
            code_match = code_pattern.search(token)
            if code_match:
                code = code_match.group(1).upper()
                try:
                    response = await cls._run_io(
                        lambda: (
                            client.table(table_name)
                            .select(f"{code_col}, {name_kor_col}, {name_eng_col}")
                            .eq(code_col, code)
                            .limit(1)
                            .execute()
                        )
                    )
                    rows = response.data or []
                except Exception as e:
                    logger.warning(f"[Supabase] KCD code lookup failed for '{code}': {e}")
                    if cls._is_kcd_source_missing_error(e):
                        return [], terms, False, {
                            "source_error": "kcd_source_missing",
                            "unmatched": [],
                            "ambiguous": [],
                            "lookup_errors": [token],
                        }
                    lookup_errors.append(token)
                    unresolved.append(term)
                    continue
            else:
                try:
                    # Match in either kor or eng names
                    response = await cls._run_io(
                        lambda: (
                            client.table(table_name)
                            .select(f"{code_col}, {name_kor_col}, {name_eng_col}")
                            .or_(
                                f"{name_kor_col}.ilike.%{token}%,"
                                f"{name_eng_col}.ilike.%{token}%,"
                                f"{code_col}.ilike.%{token}%"
                            )
                            .limit(10)
                            .execute()
                        )
                    )
                    candidates = response.data or []
                except Exception as e:
                    logger.warning(f"[Supabase] KCD name lookup failed for '{token}': {e}")
                    if cls._is_kcd_source_missing_error(e):
                        return [], terms, False, {
                            "source_error": "kcd_source_missing",
                            "unmatched": [],
                            "ambiguous": [],
                            "lookup_errors": [token],
                        }
                    lookup_errors.append(token)
                    unresolved.append(term)
                    continue
 
                exact_matches = []
                compact_token = cls._kcd_compact(token)
                for row in candidates:
                    cand_kor = str(row.get(name_kor_col) or "").strip()
                    cand_eng = str(row.get(name_eng_col) or "").strip()
                    if cls._kcd_compact(cand_kor) == compact_token or cls._kcd_compact(cand_eng) == compact_token:
                        exact_matches.append(row)
 
                if len(exact_matches) == 1:
                    rows = exact_matches
                elif len(candidates) == 1:
                    rows = candidates
                else:
                    if candidates:
                        ambiguous.append(token)
                    else:
                        unmatched.append(token)
                    unresolved.append(term)
                    continue
 
            if not rows:
                unmatched.append(token)
                unresolved.append(term)
                continue
 
            row = rows[0] or {}
            code = str(row.get(code_col) or "").strip().upper()
            kor_name = str(row.get(name_kor_col) or "").strip()
            eng_name = str(row.get(name_eng_col) or "").strip()
             
            if not code or not kor_name:
                unmatched.append(token)
                unresolved.append(term)
                continue
 
            display = kor_name
            if eng_name:
                display = f"{kor_name} ({eng_name}) [{code}]"
            else:
                display = f"{kor_name} [{code}]"
 
            resolved.append(
                {
                    "kcd_code": code,
                    "kcd_name": kor_name,
                    "kcd_name_eng": eng_name,
                    "display": display,
                }
            )
 
        deduped_resolved = []
        seen_codes = set()
        for item in resolved:
            code = str(item.get("kcd_code") or "").upper()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            deduped_resolved.append(item)
 
        details = {
            "source_error": None,
            "unmatched": cls._dedupe_ordered(unmatched),
            "ambiguous": cls._dedupe_ordered(ambiguous),
            "lookup_errors": cls._dedupe_ordered(lookup_errors),
        }
        return deduped_resolved, cls._dedupe_ordered(unresolved), True, details

    @classmethod
    async def get_main_ingr_eng_for_drugs(cls, drug_names: list):
        """Resolve selected drug names to unified main_ingr_eng list."""
        client = cls.get_client()
        if not client or not isinstance(drug_names, list):
            return ""

        unique_names = []
        seen = set()
        for raw in drug_names:
            name = str(raw or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            unique_names.append(name)

        if not unique_names:
            return ""

        ingredients = []
        ingredient_seen = set()

        for name in unique_names:
            try:
                response = await cls._run_io(
                    lambda: (
                        client.table("unified_drug_info")
                        .select("main_ingr_eng")
                        .eq("item_name", name)
                        .limit(1)
                        .execute()
                    )
                )
            except Exception as e:
                logger.warning(f"[Supabase] main_ingr lookup failed for '{name}': {e}")
                continue

            rows = response.data or []
            if not rows:
                continue
            token = str((rows[0] or {}).get("main_ingr_eng") or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in ingredient_seen:
                continue
            ingredient_seen.add(key)
            ingredients.append(token)

        return ", ".join(ingredients)

    @classmethod
    async def get_user_profile(cls, user_id: str):
        client = cls.get_client()
        if not client:
            return None

        try:
            response = await cls._run_io(
                lambda: (
                client.table("user_profile")
                .select("*")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
                )
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            logger.error(f"[Supabase] Profile fetch error for user {user_id}: {e}")
        return None

    @classmethod
    async def update_user_profile(
        cls,
        user_id: str,
        current_medications: str,
        allergies: str,
        chronic_diseases: str,
        is_pregnant: bool = False,
        main_ingr_eng: str = "",
        applied_allergies: str = "",
        applied_chronic_diseases: str = "",
        food_allergy_detail: str = "",
    ):
        client = cls.get_client()
        if not client:
            return None

        def _is_missing_col_error(err: Exception, column_name: str) -> bool:
            message = str(err or "").lower()
            missing_column_hint = (
                "column" in message and ("could not find" in message or "not found" in message)
            )
            return (
                column_name.lower() in message
                and (
                    "does not exist" in message
                    or "not found" in message
                    or missing_column_hint
                )
            )

        def _is_rls_error(err: Exception) -> bool:
            message = str(err or "").lower()
            return (
                "row-level security" in message
                or "'code': '42501'" in message
                or '"code": "42501"' in message
            )

        async def _upsert(payload: dict):
            return await cls._run_io(
                lambda: client.table("user_profile")
                .upsert(payload, on_conflict="user_id")
                .execute()
            )

        try:
            payload = {
                "user_id": str(user_id),
                "current_medications": current_medications,
                "allergies": allergies,
                "chronic_diseases": chronic_diseases,
                "is_pregnant": is_pregnant,
                "main_ingr_eng": main_ingr_eng,
                "applied_allergies": applied_allergies or allergies,
                "applied_chronic_diseases": (
                    applied_chronic_diseases or chronic_diseases
                ),
                "food_allergy_detail": food_allergy_detail,
            }
            optional_columns = [
                ("main_ingr_eng", "main_ingr_eng"),
                ("applied_allergies", "applied_allergies"),
                ("applied_chronic_diseases", "applied_chronic_diseases"),
                ("food_allergy_detail", "food_allergy_detail"),
            ]

            while True:
                try:
                    response = await _upsert(payload)
                    if response.data:
                        return response.data[0]

                    # Some Supabase setups return empty data on upsert; fetch row explicitly.
                    return await cls.get_user_profile(str(user_id))
                except Exception as upsert_error:
                    missing_col = None
                    for payload_key, column_name in optional_columns:
                        if payload_key in payload and _is_missing_col_error(
                            upsert_error, column_name
                        ):
                            missing_col = payload_key
                            break

                    if not missing_col:
                        raise upsert_error

                    payload.pop(missing_col, None)
                    logger.warning(
                        f"[Supabase] {missing_col} column missing, retrying profile upsert without it for user {user_id}"
                    )
        except Exception as e:
            if _is_rls_error(e):
                logger.error(
                    "[Supabase] Profile update blocked by RLS. "
                    "Use SUPABASE_SERVICE_ROLE_KEY on server-side DB writes "
                    "or adjust user_profile RLS policies."
                )
            logger.error(f"[Supabase] Profile update error for user {user_id}: {e}")
            return None

    @classmethod
    async def delete_user_profile(cls, user_id: str):
        client = cls.get_client()
        if not client:
            return False

        try:
            await cls._run_io(
                lambda: client.table("user_profile")
                .delete()
                .eq("user_id", str(user_id))
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"[Supabase] Profile delete error for user {user_id}: {e}")
            return False