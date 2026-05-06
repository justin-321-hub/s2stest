import os
import json
import time
import asyncio
import logging
import asyncpg
import websockets
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
EMBEDDING_MODEL = "text-embedding-3-large"
SIMILARITY_THRESHOLD = 0.5

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
db_pool: asyncpg.Pool | None = None


async def init_connection(conn):
    await register_vector(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(
        SUPABASE_DB_URL,
        min_size=1,
        max_size=5,
        init=init_connection,
        statement_cache_size=0,  # transaction pooler 不支援 prepared statements
    )
    logger.info("[db] connection pool ready")
    yield
    await db_pool.close()


app = FastAPI(lifespan=lifespan)


async def search_faq(query: str) -> str:
    t0 = time.perf_counter()
    response = await openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=query,
    )
    query_vec = response.data[0].embedding
    t1 = time.perf_counter()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select content, 1 - (embedding <=> $1::vector) as similarity
            from jaundice_rag
            order by embedding <=> $1::vector
            limit 1
            """,
            query_vec,
        )
    t2 = time.perf_counter()

    similarity = float(row["similarity"])
    logger.info(
        f"[search] query={query} sim={similarity:.4f} "
        f"embed={(t1-t0)*1000:.0f}ms db={(t2-t1)*1000:.0f}ms"
    )

    if similarity < SIMILARITY_THRESHOLD:
        return "資料庫中找不到相關資訊。"
    return row["content"]


@app.websocket("/ws")
async def websocket_endpoint(client_ws: WebSocket):
    await client_ws.accept()

    gemini_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

    # ping_interval：每 20s 送 WebSocket ping 給 Gemini，防止 Gemini 那側閒置 timeout
    async with websockets.connect(gemini_url, ping_interval=20, ping_timeout=10) as gemini_ws:
        async def receive_from_client():
            try:
                while True:
                    data = await client_ws.receive_text()
                    await gemini_ws.send(data)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.info(f"[receive_from_client] ended: {e}")

        async def receive_from_gemini():
            try:
                while True:
                    raw = await gemini_ws.recv()
                    if isinstance(raw, bytes):
                        response_text = raw.decode("utf-8")
                    else:
                        response_text = raw
                    response_data = json.loads(response_text)

                    if "toolCall" in response_data:
                        function_call = response_data["toolCall"]["functionCalls"][0]
                        call_id = function_call.get("id", "")
                        query = function_call["args"]["query"]
                        logger.info(f"[toolCall] query: {query}")

                        answer = await search_faq(query)

                        tool_response = {
                            "tool_response": {
                                "function_responses": [{
                                    "id": call_id,
                                    "name": "search_database",
                                    "response": {"output": answer}
                                }]
                            }
                        }
                        await gemini_ws.send(json.dumps(tool_response))

                    await client_ws.send_text(response_text)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.info(f"[receive_from_gemini] ended: {e}")

        async def keepalive_client():
            """每 15s 送一個 keepalive 訊息給 browser，防止 Render load balancer 切斷閒置連線"""
            try:
                while True:
                    await asyncio.sleep(15)
                    await client_ws.send_text(json.dumps({"type": "keepalive"}))
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        t1 = asyncio.create_task(receive_from_client())
        t2 = asyncio.create_task(receive_from_gemini())
        t3 = asyncio.create_task(keepalive_client())
        try:
            await asyncio.wait({t1, t2, t3}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2, t3):
                if not t.done():
                    t.cancel()
            await asyncio.gather(t1, t2, t3, return_exceptions=True)
            logger.info("[session] closed")
