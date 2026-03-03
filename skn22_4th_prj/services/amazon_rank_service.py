import asyncio
import datetime as dt
import difflib
import hashlib
import hmac
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class AmazonRankService:
    """Lookup Amazon BSR-like rank via PA-API SearchItems and sort products."""

    _ENDPOINT = os.getenv("AMAZON_PAAPI_ENDPOINT", "https://webservices.amazon.com")
    _HOST = os.getenv("AMAZON_PAAPI_HOST", "webservices.amazon.com")
    _REGION = os.getenv("AMAZON_PAAPI_REGION", "us-east-1")
    _SERVICE = "ProductAdvertisingAPI"
    _TARGET = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
    _MARKETPLACE = os.getenv("AMAZON_PAAPI_MARKETPLACE", "www.amazon.com")
    _SEARCH_INDEX = os.getenv("AMAZON_PAAPI_SEARCH_INDEX", "HealthPersonalCare")
    _CACHE_TTL_SEC = int(os.getenv("AMAZON_RANK_CACHE_TTL_SEC", "21600"))
    _MAX_CONCURRENCY = max(int(os.getenv("AMAZON_RANK_MAX_CONCURRENCY", "2")), 1)
    _semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    _cache: Dict[str, Tuple[float, dict]] = {}

    @staticmethod
    def _as_bool(value: str, default: bool = False) -> bool:
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    @classmethod
    def _is_enabled(cls) -> bool:
        flag = cls._as_bool(os.getenv("AMAZON_RANK_ENABLED"), default=False)
        if not flag:
            return False
        required = [
            os.getenv("AMAZON_PAAPI_ACCESS_KEY"),
            os.getenv("AMAZON_PAAPI_SECRET_KEY"),
            os.getenv("AMAZON_PAAPI_PARTNER_TAG"),
        ]
        return all(required)

    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return " ".join(str(value or "").strip().upper().split())

    @classmethod
    def _cache_key(cls, brand_name: str, manufacturer_name: str) -> str:
        return f"{cls._normalize_text(brand_name)}|{cls._normalize_text(manufacturer_name)}"

    @classmethod
    def _from_cache(cls, key: str) -> Optional[dict]:
        row = cls._cache.get(key)
        if not row:
            return None
        ts, value = row
        if (dt.datetime.now(dt.timezone.utc).timestamp() - ts) > cls._CACHE_TTL_SEC:
            cls._cache.pop(key, None)
            return None
        return value

    @classmethod
    def _put_cache(cls, key: str, value: dict) -> None:
        cls._cache[key] = (dt.datetime.now(dt.timezone.utc).timestamp(), value)

    @classmethod
    def _sign(cls, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    @classmethod
    def _signature_key(cls, secret_key: str, date_stamp: str) -> bytes:
        k_date = cls._sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
        k_region = cls._sign(k_date, cls._REGION)
        k_service = cls._sign(k_region, cls._SERVICE)
        return cls._sign(k_service, "aws4_request")

    @classmethod
    async def _search_items(cls, keywords: str) -> Optional[dict]:
        access_key = os.getenv("AMAZON_PAAPI_ACCESS_KEY", "")
        secret_key = os.getenv("AMAZON_PAAPI_SECRET_KEY", "")
        partner_tag = os.getenv("AMAZON_PAAPI_PARTNER_TAG", "")

        amz_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]

        payload = {
            "Keywords": keywords,
            "SearchIndex": cls._SEARCH_INDEX,
            "ItemCount": 6,
            "Marketplace": cls._MARKETPLACE,
            "PartnerTag": partner_tag,
            "PartnerType": "Associates",
            "Resources": [
                "ItemInfo.Title",
                "BrowseNodeInfo.WebsiteSalesRank",
                "BrowseNodeInfo.BrowseNodes.SalesRank",
                "Images.Primary.Medium",
            ],
        }
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

        canonical_headers = (
            f"content-encoding:amz-1.0\n"
            f"content-type:application/json; charset=utf-8\n"
            f"host:{cls._HOST}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{cls._TARGET}\n"
        )
        signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"
        canonical_request = (
            "POST\n"
            "/paapi5/searchitems\n"
            "\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        credential_scope = f"{date_stamp}/{cls._REGION}/{cls._SERVICE}/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signing_key = cls._signature_key(secret_key, date_stamp)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        headers = {
            "Content-Encoding": "amz-1.0",
            "Content-Type": "application/json; charset=utf-8",
            "Host": cls._HOST,
            "X-Amz-Date": amz_date,
            "X-Amz-Target": cls._TARGET,
            "Authorization": authorization,
        }

        url = f"{cls._ENDPOINT.rstrip('/')}/paapi5/searchitems"
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(url, headers=headers, content=payload_str.encode("utf-8"))
            if res.status_code != 200:
                logger.warning(
                    "Amazon PA-API search failed (%s): %s", res.status_code, res.text[:240]
                )
                return None
            return res.json()

    @classmethod
    def _to_rank(cls, value) -> Optional[int]:
        try:
            n = int(value)
            return n if n > 0 else None
        except Exception:
            return None

    @classmethod
    def _extract_rank(cls, item: dict) -> Tuple[Optional[int], str, str]:
        browse = item.get("BrowseNodeInfo") or {}
        website = browse.get("WebsiteSalesRank") or {}
        website_rank = cls._to_rank(website.get("SalesRank"))
        if website_rank is not None:
            category = (
                website.get("ContextFreeName")
                or website.get("DisplayName")
                or "Amazon"
            )
            return website_rank, f"#{website_rank} in {category}", "website_sales_rank"

        best_rank = None
        best_label = ""
        for node in browse.get("BrowseNodes") or []:
            if not isinstance(node, dict):
                continue
            rank = cls._to_rank(node.get("SalesRank"))
            name = node.get("ContextFreeName") or node.get("DisplayName") or "Category"
            if rank is None and isinstance(node.get("SalesRank"), dict):
                rank = cls._to_rank((node.get("SalesRank") or {}).get("SalesRank"))
            if rank is None:
                continue
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_label = f"#{rank} in {name}"

        if best_rank is not None:
            return best_rank, best_label, "browse_node_rank"
        return None, "", ""

    @classmethod
    def _select_best_item(cls, items: List[dict], target_brand: str) -> Optional[dict]:
        target = cls._normalize_text(target_brand)
        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rank_value, rank_label, rank_source = cls._extract_rank(item)
            if rank_value is None:
                continue
            title = (
                ((item.get("ItemInfo") or {}).get("Title") or {}).get("DisplayValue")
                or ""
            )
            ratio = difflib.SequenceMatcher(
                None, target, cls._normalize_text(title)
            ).ratio()
            candidates.append(
                (
                    -ratio,
                    rank_value,
                    {
                        "amazon_asin": item.get("ASIN"),
                        "amazon_title": title,
                        "amazon_rank_value": rank_value,
                        "amazon_rank_label": rank_label,
                        "amazon_rank_source": rank_source,
                        "amazon_url": item.get("DetailPageURL"),
                        "amazon_image_url": (
                            ((item.get("Images") or {}).get("Primary") or {})
                            .get("Medium", {})
                            .get("URL")
                        ),
                        "amazon_rank_observed_at": dt.datetime.now(
                            dt.timezone.utc
                        ).isoformat(),
                    },
                )
            )

        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2]

    @classmethod
    async def get_rank_for_product(
        cls, brand_name: str, manufacturer_name: str = "", active_ingredient: str = ""
    ) -> Optional[dict]:
        if not cls._is_enabled():
            return None

        key = cls._cache_key(brand_name, manufacturer_name)
        cached = cls._from_cache(key)
        if cached is not None:
            return cached

        keyword_parts = [brand_name, manufacturer_name, active_ingredient]
        keywords = " ".join([str(x or "").strip() for x in keyword_parts if str(x or "").strip()])
        if not keywords:
            cls._put_cache(key, None)
            return None

        try:
            async with cls._semaphore:
                payload = await cls._search_items(keywords)
        except Exception as e:
            logger.warning("Amazon rank lookup failed for '%s': %s", brand_name, e)
            cls._put_cache(key, None)
            return None

        if not payload:
            cls._put_cache(key, None)
            return None

        items = ((payload.get("SearchResult") or {}).get("Items") or [])
        selected = cls._select_best_item(items, brand_name)
        cls._put_cache(key, selected)
        return selected

    @classmethod
    def _sort_key(cls, product: dict):
        rank = product.get("amazon_rank_value")
        has_rank = isinstance(rank, int) and rank > 0
        return (
            0 if has_rank else 1,
            rank if has_rank else 10**12,
            str(product.get("brand_name") or "").upper(),
        )

    @classmethod
    async def enrich_and_sort_products(cls, products: list) -> list:
        if not isinstance(products, list) or not products:
            return products

        if not cls._is_enabled():
            return products

        async def enrich_one(product: dict):
            if not isinstance(product, dict):
                return
            rank_info = await cls.get_rank_for_product(
                brand_name=product.get("brand_name", ""),
                manufacturer_name=product.get("manufacturer_name", ""),
                active_ingredient=product.get("active_ingredient", ""),
            )
            if not rank_info:
                return
            product.update(rank_info)

        await asyncio.gather(*[enrich_one(p) for p in products])
        products.sort(key=cls._sort_key)
        return products
