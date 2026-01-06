"""
Redis caching layer for performance optimization.
"""
from typing import Optional, Any
import json
import logging

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    from redis.asyncio import Redis
    REDIS_AVAILABLE = True
except ImportError:
    logger.warning("Redis not installed. Caching will be disabled.")
    REDIS_AVAILABLE = False
    Redis = None


class CacheBackend:
    """Redis cache backend with async support."""

    def __init__(self, redis_url: Optional[str] = None):
        self._redis: Optional[Redis] = None
        self._redis_url = redis_url
        self._enabled = False

    async def init(self):
        """Initialize Redis connection."""
        if not REDIS_AVAILABLE or not self._redis_url:
            logger.info("Caching is disabled (Redis not available)")
            return

        try:
            self._redis = await redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                port=6379,
                db=0
            )
            # Test connection
            await self._redis.ping()
            self._enabled = True
            logger.info("Redis caching initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Redis: {e}")
            self._enabled = False

    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            logger.info("Redis connection closed")

    @property
    def enabled(self) -> bool:
        """Check if caching is enabled."""
        return self._enabled

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if not self._enabled:
            return None

        try:
            value = await self._redis.get(key)
            if value is not None:
                return json.loads(value)
        except Exception as e:
            logger.error(f"Cache get error: {e}")
        return None

    async def set(
        self,
        key: str,
        value: Any,
        expire: Optional[int] = None
    ) -> bool:
        """Set value in cache."""
        if not self._enabled:
            return False

        try:
            json_value = json.dumps(value)
            if expire:
                await self._redis.setex(key, expire, json_value)
            else:
                await self._redis.set(key, json_value)
            return True
        except Exception as e:
            logger.error(f"Cache set error: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if not self._enabled:
            return False

        try:
            await self._redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Cache delete error: {e}")
            return False

    async def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern."""
        if not self._enabled:
            return 0

        try:
            keys = []
            async for key in self._redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await self._redis.delete(*keys)
            return len(keys)
        except Exception as e:
            logger.error(f"Cache clear pattern error: {e}")
            return 0

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if not self._enabled:
            return False

        try:
            return bool(await self._redis.exists(key))
        except Exception as e:
            logger.error(f"Cache exists error: {e}")
            return False


# Global cache instance
cache = CacheBackend()


async def init_cache(redis_url: Optional[str] = None):
    """Initialize global cache instance."""
    cache._redis_url = redis_url
    await cache.init()


async def close_cache():
    """Close global cache instance."""
    await cache.close()
