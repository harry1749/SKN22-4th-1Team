import os
import asyncio
import logging
from pinecone import Pinecone

logger = logging.getLogger(__name__)

class PineconeService:
    _pc = None
    _index = None
    _embedding_cache: dict = {}
    _st_model = None
    INDEX_NAME = "fda-drug-labels"

    @classmethod
    def _get_st_model(cls):
        """sentence-transformers 모델 싱글톤 (최초 호출 시 로드)"""
        if cls._st_model is None:
            from sentence_transformers import SentenceTransformer
            cls._st_model = SentenceTransformer('intfloat/multilingual-e5-large')
            logger.info("[SentenceTransformer] multilingual-e5-large 로드 완료")
        return cls._st_model

    @classmethod
    def get_index(cls):
        if cls._index is not None:
            return cls._index

        try:
            api_key = os.getenv("PINECONE_API_KEY")
            if not api_key:
                logger.error("PINECONE_API_KEY is not set.")
                return None

            cls._pc = Pinecone(api_key=api_key)
            cls._index = cls._pc.Index(cls.INDEX_NAME)
            return cls._index
        except Exception as e:
            logger.error(f"Error initializing Pinecone client: {e}")
            return None

    @classmethod
    async def get_embedding(cls, text: str) -> list[float]:
        """
        로컬 sentence-transformers로 임베딩 생성 (intfloat/multilingual-e5-large).
        CPU-bound 작업은 asyncio.to_thread로 이벤트 루프 블로킹 방지.
        """
        cache_key = text.strip().lower()
        if cache_key in cls._embedding_cache:
            logger.debug(f"[Embedding Cache Hit] key='{cache_key}'")
            return cls._embedding_cache[cache_key]

        try:
            model = cls._get_st_model()
            # CPU-bound → asyncio.to_thread로 이벤트 루프 블로킹 방지
            vector = await asyncio.to_thread(
                lambda: model.encode(f"query: {text}", normalize_embeddings=True).tolist()
            )
            if vector:
                cls._embedding_cache[cache_key] = vector
            return vector
        except Exception as e:
            logger.error(f"[LocalEmbed] Error: {repr(e)}")
            return []

    @classmethod
    async def prefetch_embeddings(cls, texts: list[str]):
        """여러 텍스트를 1회 배치 encode로 캐시 사전 저장 (CPU 경합 방지)"""
        uncached = [t for t in texts if t.strip().lower() not in cls._embedding_cache]
        if not uncached:
            return
        model = cls._get_st_model()
        prefixed = [f"query: {t}" for t in uncached]
        vectors = await asyncio.to_thread(
            lambda: model.encode(prefixed, normalize_embeddings=True).tolist()
        )
        for text, vector in zip(uncached, vectors):
            cls._embedding_cache[text.strip().lower()] = vector
        logger.debug(f"[Prefetch] {len(uncached)}개 임베딩 사전 캐시 완료")

    @classmethod
    async def search(cls, query_text: str = None, filter_dict: dict = None, top_k: int = 10):
        """
        Search Pinecone index using either vector similarity (if query_text is provided) 
        and/or metadata filtering.
        """
        index = cls.get_index()
        if not index:
            return []
            
        query_args = {
            "top_k": top_k,
            "include_metadata": True
        }
        
        if filter_dict:
            query_args["filter"] = filter_dict
            
        if query_text:
            vector = await cls.get_embedding(query_text)
            if not vector:
                # Fallback if embedding fails but we have a filter
                if filter_dict:
                    # Create a dummy zero vector if we only want to filter (though Pinecone might require a non-zero vector depending on metric)
                    # For cosine, zero vector is invalid. We shouldn't reach here ideally if embedding API works.
                    logger.error("Failed to generate embedding for vector search.")
                    return []
                return []
            query_args["vector"] = vector
        else:
            # If no query_text, we MUST have a filter, and we need a dummy vector or use query without vector if Pinecone supports it
            # To purely filter without vector, we still need a vector in standard query. 
            # Alternatively, we can use a dummy vector if metric allows, or we just rely on the structure.
            # Assuming we always have an embedding or we can't query effectively.
            logger.error("search requires at least query_text for vector search.")
            return []

        try:
            response = await asyncio.to_thread(index.query, **query_args)
            return response.get('matches', [])
        except Exception as e:
            logger.error(f"Error searching Pinecone: {e}")
            return []
