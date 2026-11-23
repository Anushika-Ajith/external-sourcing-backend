import redis
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from config import settings
import ssl

logger = logging.getLogger(__name__)

class CacheManager:
    def __init__(self):
        self.redis_client = None
        self.redis_enabled = False
        self._connect_to_redis()
    
    def _connect_to_redis(self):
        """Initialize Redis connection optimized for AP South Redis Cloud"""
        try:
            # Check if Redis URL is provided
            if not hasattr(settings, 'redis_url') or not settings.redis_url:
                logger.warning("REDIS_URL not configured in .env file")
                logger.warning("Add: REDIS_URL=redis://default:password@host:port to your .env")
                logger.warning("Running without cache (database only mode)")
                return
            
            logger.info(f"Connecting to Redis Cloud: {settings.redis_url[:30]}...")
            
            # Optimized connection params for Redis Cloud AP South
            connection_params = {
                'decode_responses': True,
                'health_check_interval': 30,
                'socket_connect_timeout': 3,  # Reduced from 15
                'socket_timeout': 3,          # Reduced from 15
                'retry_on_timeout': True,
                'retry_on_error': [redis.ConnectionError, redis.TimeoutError],
                'socket_keepalive': True,
                'socket_keepalive_options': {},
            }            
            # Initialize Redis connection
            self.redis_client = redis.from_url(
                settings.redis_url,
                **connection_params
            )
            
            # Test connection
            self.redis_client.ping()
            self.redis_enabled = True
            
            # Log success
            logger.info(f"✅ Redis Cloud (AP South) connection established")
            logger.info(f"🌏 Connected to: redis-15302.c264.ap-south-1-1.ec2.redns.redis-cloud.com:15302")
            
        except redis.ConnectionError as e:
            logger.error(f"❌ Redis Cloud connection failed: {e}")
            logger.error("🔧 Please check your Redis Cloud instance status")
            self._disable_redis()
        except redis.AuthenticationError as e:
            logger.error(f"🔐 Redis Cloud authentication failed: {e}")
            logger.error("🔧 Please verify your password in REDIS_URL")
            self._disable_redis()
        except redis.TimeoutError as e:
            logger.error(f"⏰ Redis Cloud timeout (AP South latency): {e}")
            logger.error("🔧 This might be due to regional latency")
            self._disable_redis()
        except Exception as e:
            logger.error(f"❌ Unexpected Redis Cloud error: {e}")
            self._disable_redis()    
    def _disable_redis(self):
        """Disable Redis and set fallback mode"""
        self.redis_client = None
        self.redis_enabled = False
        logger.warning("🔄 Redis disabled - using database-only mode")
    
    def _mask_redis_url(self, url: str) -> str:
        """Mask sensitive parts of Redis URL for logging"""
        if '@' in url:
            protocol, rest = url.split('://', 1)
            if '@' in rest:
                auth, host = rest.split('@', 1)
                # Mask password but keep username
                if ':' in auth:
                    username, _ = auth.split(':', 1)
                    return f"{protocol}://{username}:***@{host}"
                else:
                    return f"{protocol}://***@{host}"
        return url
    
    def _is_connected(self) -> bool:
        """Check if Redis is connected and available"""
        if not self.redis_enabled or not self.redis_client:
            return False
        try:
            # Quick ping with timeout
            self.redis_client.ping()
            return True
        except Exception as e:
            logger.warning(f"Redis connection lost: {e}")
            self.redis_enabled = False
            return False
    
    async def get_cart(self, user_id: int) -> Optional[Dict[Any, Any]]:
        """Get cart from Redis cache"""
        if not self._is_connected():
            return None
            
        try:
            cache_key = f"cart:user:{user_id}"
            cached_cart = self.redis_client.get(cache_key)
            
            if cached_cart:
                cart_data = json.loads(cached_cart)
                logger.debug(f"🎯 Cache HIT for user {user_id}")
                return cart_data
                
            logger.debug(f"💨 Cache MISS for user {user_id}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting cart from cache for user {user_id}: {e}")
            return None
    
    async def set_cart(self, user_id: int, cart_data: Dict[Any, Any], ttl: int = 3600):
        """Cache cart data with TTL (1 hour default)"""
        if not self._is_connected():
            return False
            
        try:
            cache_key = f"cart:user:{user_id}"
            
            # Add cache metadata
            cache_payload = {
                "data": cart_data,
                "cached_at": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "source": "redis_cloud"
            }
            
            success = self.redis_client.setex(
                cache_key, 
                ttl, 
                json.dumps(cache_payload, default=str, separators=(',', ':'))  # Compact JSON
            )
            
            if success:
                logger.debug(f"💾 Cart cached for user {user_id} with TTL {ttl}s")
                return True
                
        except Exception as e:
            logger.error(f"Error caching cart for user {user_id}: {e}")
            
        return False
    
    async def invalidate_cart(self, user_id: int):
        """Remove cart from cache"""
        if not self._is_connected():
            return False
            
        try:
            cache_key = f"cart:user:{user_id}"
            deleted = self.redis_client.delete(cache_key)
            
            if deleted:
                logger.debug(f"🗑️ Cart cache invalidated for user {user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error invalidating cart cache for user {user_id}: {e}")
            
        return False
    
    async def extend_cart_ttl(self, user_id: int, ttl: int = 3600):
        """Extend cart cache TTL on user activity"""
        if not self._is_connected():
            return False
            
        try:
            cache_key = f"cart:user:{user_id}"
            extended = self.redis_client.expire(cache_key, ttl)
            
            if extended:
                logger.debug(f"⏰ Cart cache TTL extended for user {user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error extending cart cache TTL for user {user_id}: {e}")
            
        return False
    
    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        if not self.redis_enabled:
            return {
                "status": "disconnected",
                "message": "Redis Cloud is not configured or unavailable",
                "redis_url": self._mask_redis_url(getattr(settings, 'redis_url', 'Not configured'))
            }
            
        if not self._is_connected():
            return {
                "status": "disconnected", 
                "message": "Redis Cloud connection failed",
                "redis_url": self._mask_redis_url(settings.redis_url)
            }
            
        try:
            info = self.redis_client.info()
            cart_keys = self.redis_client.keys("cart:user:*")
            
            total_hits = info.get("keyspace_hits", 0)
            total_misses = info.get("keyspace_misses", 0)
            total_requests = total_hits + total_misses
            
            return {
                "status": "connected",
                "provider": "Redis Cloud",
                "redis_url": self._mask_redis_url(settings.redis_url),
                "total_cart_keys": len(cart_keys),
                "memory_used": info.get("used_memory_human", "N/A"),
                "connected_clients": info.get("connected_clients", 0),
                "keyspace_hits": total_hits,
                "keyspace_misses": total_misses,
                "total_requests": total_requests,
                "hit_rate": round(total_hits / max(total_requests, 1) * 100, 2),
                "uptime_seconds": info.get("uptime_in_seconds", 0),
                "redis_version": info.get("redis_version", "unknown"),
                "ssl_enabled": settings.redis_url.startswith('rediss://'),
                "max_memory": info.get("maxmemory_human", "N/A"),
                "eviction_policy": info.get("maxmemory_policy", "unknown")
            }
            
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {"status": "error", "error": str(e)}

# Global cache manager instance
cache_manager = CacheManager()
