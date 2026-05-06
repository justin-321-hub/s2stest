import os
import json
import time
import asyncio
import logging
import websockets
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from openai import AsyncOpenAI
from supabase import acreate_client, AsyncClient
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://urzcqkkcrmitcskbgvci.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVyemNxa2tjcm1pdGNza2JndmNpIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0MzEwNjk1NywiZXhwIjoyMDU4NjgyOTU3fQ.7k03VTOKS4iuqVnOxlNEu3elfZMz4GbTcqLFUqukrbM")
EMBEDDING_MODEL = "text-embedding-3-large"
SIMILARITY_THRESHOLD = 0.5

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
supabase_client: AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supabase_client
    supabase_client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("[db] Supabase client ready")
    yield


app = FastAPI(lifespan=lifespan)


async def search_faq(query: str) -> str:
    t0 = time.perf_counter()
    response = await openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=query,
    )
    query_vec = response.data[0].embedding
    t1 = time.perf_counter()

    result = await supabase_client.rpc(
        "match_jaundice_rag",
        {
            "query_embedding": query_vec,
            "match_threshold": SIMILARITY_THRESHOLD,
            "match_count": 1,
        },
    ).execute()
    t2 = time.perf_counter()

    rows = result.data
    if not rows:
        logger.info(
            f"[search] query={query} no match "
            f"embed={(t1-t0)*1000:.0f}ms db={(t2-t1)*1000:.0f}ms"
        )
        return "資料庫中找不到相關資訊。"

    row = rows[0]
    similarity = float(row.get("similarity", 0))
    logger.info(
        f"[search] query={query} sim={similarity:.4f} "
        f"embed={(t1-t0)*1000:.0f}ms db={(t2-t1)*1000:.0f}ms"
    )

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
