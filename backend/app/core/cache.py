"""
Redis caching layer for performance optimization.

Features:
- Synchronous Redis support for FastAPI sync endpoints
- Cache keys with prefixes for different entity types
- TTL-based expiration
- Pattern-based cache invalidation
- Cached decorator for easy caching of functions
"""
from typing import Optional, Any, Callable, TypeVar, List
import json
from datetime import datetime
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    logger.warning("Redis not installed. Caching will be disabled.")
    REDIS_AVAILABLE = False
    redis = None

T = TypeVar('T')

# Cache key prefixes
class CacheKeys:
    """Cache key prefixes for different entity types."""
    PARTS = "parts"
    PARTS_LIST = "parts:list"
    PART = "parts:id"
    
    WORK_CENTERS = "work_centers"
    WORK_CENTERS_LIST = "work_centers:list"
    WORK_CENTER = "work_centers:id"
    
    CUSTOMERS = "customers"
    CUSTOMERS_LIST = "customers:list"
    CUSTOMER = "customers:id"
    
    WORK_ORDERS = "work_orders"
    WORK_ORDER = "work_orders:id"
    
    ROUTINGS = "routings"
    ROUTING = "routings:id"
    
    BOMS = "boms"
    BOM = "boms:id"
    
    DASHBOARD = "dashboard"
    ANALYTICS = "analytics"
    SEARCH = "search"
    
    @staticmethod
    def part(part_id: int) -> str:
        return f"parts:id:{part_id}"
    
    @staticmethod
    def work_center(wc_id: int) -> str:
        return f"work_centers:id:{wc_id}"
    
    @staticmethod
    def customer(customer_id: int) -> str:
        return f"customers:id:{customer_id}"
    
    @staticmethod
    def work_order(wo_id: int) -> str:
        return f"work_orders:id:{wo_id}"
    
    @staticmethod
    def routing(routing_id: int) -> str:
        return f"routings:id:{routing_id}"
    
    @staticmethod
    def bom(bom_id: int) -> str:
        return f"boms:id:{bom_id}"


# Default TTLs in seconds
class CacheTTL:
    """Default cache TTLs for different data types."""
    SHORT = 60  # 1 minute - for frequently changing data
    MEDIUM = 300  # 5 minutes - for moderately stable data
    LONG = 900  # 15 minutes - for stable data
    VERY_LONG = 3600  # 1 hour - for rarely changing data
    
    # Specific TTLs
    PARTS_LIST = 300  # 5 minutes
    WORK_CENTERS_LIST = 900  # 15 minutes (rarely changes)
    CUSTOMERS_LIST = 300  # 5 minutes
    DASHBOARD = 60  # 1 minute
    ANALYTICS = 300  # 5 minutes
    SEARCH = 60  # 1 minute


def json_serializer(obj: Any) -> Any:
    """Custom JSON serializer for objects not serializable by default."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, '__dict__'):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class CacheBackend:
    """Redis cache backend with synchronous support."""

    def __init__(self, redis_url: Optional[str] = None):
        self._redis: Optional[redis.Redis] = None
        self._redis_url = redis_url
        self._enabled = False
        self._stats = {"hits": 0, "misses": 0, "sets": 0, "deletes": 0}

    def init(self, redis_url: Optional[str] = None):
        """Initialize Redis connection."""
        if redis_url:
            self._redis_url = redis_url
            
        if not REDIS_AVAILABLE or not self._redis_url:
            logger.info("Caching is disabled (Redis not available or not configured)")
            return

        try:
            self._redis = redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            # Test connection
            self._redis.ping()
            self._enabled = True
            logger.info("Redis caching initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize Redis caching: {e}")
            self._enabled = False

    def close(self):
        """Close Redis connection."""
        if self._redis:
            self._redis.close()
            logger.info("Redis connection closed")

    @property
    def enabled(self) -> bool:
        """Check if caching is enabled."""
        return self._enabled

    @property
    def stats(self) -> dict:
        """Get cache statistics."""
        return self._stats.copy()

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if not self._enabled:
            return None

        try:
            value = self._redis.get(key)
            if value is not None:
                self._stats["hits"] += 1
                return json.loads(value)
            self._stats["misses"] += 1
        except Exception as e:
            logger.error(f"Cache get error for key {key}: {e}")
            self._stats["misses"] += 1
        return None

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = CacheTTL.MEDIUM
    ) -> bool:
        """Set value in cache with optional TTL."""
        if not self._enabled:
            return False

        try:
            json_value = json.dumps(value, default=json_serializer)
            if ttl:
                self._redis.setex(key, ttl, json_value)
            else:
                self._redis.set(key, json_value)
            self._stats["sets"] += 1
            return True
        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if not self._enabled:
            return False

        try:
            self._redis.delete(key)
            self._stats["deletes"] += 1
            return True
        except Exception as e:
            logger.error(f"Cache delete error for key {key}: {e}")
            return False

    def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching pattern."""
        if not self._enabled:
            return 0

        try:
            keys = list(self._redis.scan_iter(match=pattern))
            if keys:
                count = self._redis.delete(*keys)
                self._stats["deletes"] += count
                logger.debug(f"Deleted {count} cache keys matching pattern: {pattern}")
                return count
            return 0
        except Exception as e:
            logger.error(f"Cache delete pattern error for {pattern}: {e}")
            return 0

    def invalidate_entity(self, entity_type: str, entity_id: Optional[int] = None):
        """Invalidate cache for an entity type."""
        if entity_id:
            # Invalidate specific entity
            self.delete(f"{entity_type}:id:{entity_id}")
        # Invalidate list cache
        self.delete_pattern(f"{entity_type}:list*")
        # Invalidate search cache that might include this entity
        self.delete_pattern(f"search:*")

    def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if not self._enabled:
            return False

        try:
            return bool(self._redis.exists(key))
        except Exception as e:
            logger.error(f"Cache exists error for key {key}: {e}")
            return False

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        ttl: Optional[int] = CacheTTL.MEDIUM
    ) -> T:
        """Get value from cache or compute and set it."""
        cached = self.get(key)
        if cached is not None:
            return cached
        
        value = factory()
        self.set(key, value, ttl)
        return value


# Global cache instance
cache = CacheBackend()


def init_cache(redis_url: Optional[str] = None):
    """Initialize global cache instance."""
    cache.init(redis_url)


def cache_work_centers_list(wc_data: List[dict]):
    """Cache work centers list."""
    cache.set(CacheKeys.WORK_CENTERS_LIST, wc_data, CacheTTL.WORK_CENTERS_LIST)


def get_cached_work_centers_list() -> Optional[List[dict]]:
    """Get cached work centers list."""
    return cache.get(CacheKeys.WORK_CENTERS_LIST)


def invalidate_work_centers_cache(wc_id: Optional[int] = None):
    """Invalidate work centers cache."""
    cache.invalidate_entity(CacheKeys.WORK_CENTERS, wc_id)


