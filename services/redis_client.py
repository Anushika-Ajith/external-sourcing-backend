# backend/services/redis_client.py
import json
import redis.asyncio as redis
import os

# Read from .env
REDIS_URL = os.getenv("REDIS_URL")

# Create redis client
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# async def publish_pr_created(order_id: str,user_email: str,user_Id,amount):
#     """
#     Publish PR_created event to redis
#     """
#     event = {
#         "event": "PR_CREATED",
#         "order_id": order_id,
#         "document_type":"PR",
#         "email": user_email,
#         "userId":user_Id,
#         "total_amount":amount

#     }
#     await redis_client.publish("pr_channel", json.dumps(event))
#     print(f"✅ Published PR_CREATED for ID={order_id}")


async def publish_pr_created(order_id: int, user_email: str, user_id: int, amount: float):
    event = {
        "event": "PR_CREATED",
        "order_id": order_id,
        "document_type": "PR",
        "email": user_email,
        "userId": user_id,
        "total_amount": amount,
    }

    await redis_client.publish("pr_channel", json.dumps(event))
    print(f"🔔 SSE Event Published: PR_CREATED (order_id={order_id})")
