# S2S 系統完整架構與資料流

## 高層架構圖

```
┌──────────────────┐                    ┌─────────────────────┐                    ┌──────────────────────────┐
│   Browser        │                    │   Render            │                    │  Google Gemini           │
│   (index.html)   │  ◀──── WSS ────▶  │   (server.py)       │  ◀──── WSS ────▶  │  Multimodal Live API     │
│                  │                    │   FastAPI proxy     │                    │  (BidiGenerateContent)   │
└──────────────────┘                    └──────────┬──────────┘                    └──────────────────────────┘
                                                   │
                              當 toolCall 觸發時 ──┼──────────────────┐
                                                   │                  │
                                                   ▼                  ▼
                                         ┌──────────────────┐  ┌──────────────────────────┐
                                         │  OpenAI API      │  │  Supabase                │
                                         │  Embeddings      │  │  Postgres + pgvector     │
                                         │  (HTTPS REST)    │  │  (asyncpg, port 6543)    │
                                         └──────────────────┘  └──────────────────────────┘
```

---

## 階段 1：建立連線（按下按鈕瞬間）

### 1-1. 瀏覽器：使用者按住按鈕

| | 內容 |
|---|---|
| **觸發** | `mousedown` / `touchstart` 事件 |
| **動作** | `startAction()` → 初始化 AudioContext (24kHz 播放用) → `connectAndStream()` |
| **API** | Web Audio API: `new AudioContext({ sampleRate: 24000 })` |

### 1-2. 瀏覽器 → Render：開 WebSocket

| | 內容 |
|---|---|
| **Output** | WebSocket 連線到 `wss://s2stest.onrender.com/ws` |
| **Protocol** | WebSocket (RFC 6455) |

### 1-3. server.py：接受連線並開第二條 WS 到 Gemini

```python
gemini_url = "wss://generativelanguage.googleapis.com/ws/" \
             "google.ai.generativelanguage.v1beta.GenerativeService." \
             "BidiGenerateContent?key=GEMINI_API_KEY"
```

| | 內容 |
|---|---|
| **API** | Google Gemini Multimodal Live API (BidiGenerateContent v1beta) |
| **Auth** | URL query param `key=GEMINI_API_KEY`（從 Render 環境變數讀） |
| **後續** | 開兩個 async coroutine 並行：`receive_from_client` + `receive_from_gemini` |

### 1-4. 瀏覽器:送 setup 訊息（透過 server.py 透傳給 Gemini）

```json
{
  "setup": {
    "model": "models/gemini-2.5-flash-native-audio-preview-12-2025",
    "generationConfig": {
      "responseModalities": ["AUDIO"],
      "speechConfig": { "voiceConfig": { "prebuiltVoiceConfig": { "voiceName": "Aoede" } } },
      "thinkingConfig": { "thinkingBudget": 0 }
    },
    "realtimeInputConfig": { "automaticActivityDetection": { "disabled": true } },
    "systemInstruction": { "parts": [{ "text": "你是一個語音助理..." }] },
    "tools": [{ "function_declarations": [{ "name": "search_database", ... }] }]
  }
}
```

**關鍵設定**：
- `thinkingBudget: 0` — 關掉思考省 4-6 秒延遲
- `automaticActivityDetection.disabled: true` — 改用手動 VAD（push-to-talk）
- 註冊 `search_database` 工具給 Gemini 用

---

## 階段 2：麥克風串流（按住期間）

### 2-1. 瀏覽器：擷取麥克風

| | 內容 |
|---|---|
| **API** | `navigator.mediaDevices.getUserMedia({ audio: true })` |
| **AudioContext** | `new AudioContext({ sampleRate: 16000 })` |
| **處理器** | `ScriptProcessor` (deprecated 但通用)，buffer = 4096 samples |
| **Input** | 麥克風 → Float32 PCM @ 16kHz |
| **轉換** | Float32 → Int16 (`× 32767` clamping) → bytes → base64 |

### 2-2. 送出 activityStart + 持續送音訊

```json
// 一次性
{ "realtimeInput": { "activityStart": {} } }

// 每 4096 sample / 256ms 一次
{
  "realtimeInput": {
    "mediaChunks": [{ "mimeType": "audio/pcm;rate=16000", "data": "<base64>" }]
  }
}
```

### 2-3. server.py：透傳給 Gemini

```python
async def receive_from_client():
    while True:
        data = await client_ws.receive_text()
        await gemini_ws.send(data)  # 1:1 透傳，server 不解析
```

### 2-4. 瀏覽器：放開按鈕

```json
{ "realtimeInput": { "activityEnd": {} } }
```

→ Gemini 知道「使用者說完了」，開始處理

---

## 階段 3：Gemini 工具呼叫 (RAG)

### 3-1. Gemini 收到完整音訊後判斷需要查資料

Gemini 內部：語音 → 文字理解 → 決定呼叫 `search_database`，送出：

```json
{
  "toolCall": {
    "functionCalls": [{
      "id": "<call_id>",
      "name": "search_database",
      "args": { "query": "報名費" }
    }]
  }
}
```

### 3-2. server.py 攔截 toolCall（**這條訊息不轉發給瀏覽器**）

```python
if "toolCall" in response_data:
    function_call = response_data["toolCall"]["functionCalls"][0]
    call_id = function_call.get("id", "")
    query = function_call["args"]["query"]
    answer = await search_faq(query)  # 進入 3-3 + 3-4
```

### 3-3. server.py → OpenAI：產生 query embedding

| | 內容 |
|---|---|
| **API** | `POST https://api.openai.com/v1/embeddings` |
| **SDK** | `AsyncOpenAI` (官方 Python SDK) |
| **Model** | `text-embedding-3-small` |
| **Input** | `{ "input": "報名費", "model": "text-embedding-3-small" }` |
| **Output** | 1536 維 float vector |
| **延遲** | warm: 200-400ms / cold: 1-2s |

### 3-4. server.py → Supabase：pgvector 相似度查詢

```sql
SELECT answer, 1 - (embedding <=> $1::vector) AS similarity
FROM faq
ORDER BY embedding <=> $1::vector
LIMIT 1
```

| | 內容 |
|---|---|
| **連線** | asyncpg over Transaction pooler (port **6543**) |
| **算法** | pgvector cosine distance (`<=>`) + HNSW 索引 |
| **Input** | 1536 維 query vector |
| **Output** | 最相似的 `answer` + `similarity` 分數 |
| **判斷** | `similarity < 0.7` → 回 "資料庫中找不到相關資訊" |
| **延遲** | 同區域 warm: 10-50ms |

### 3-5. server.py → Gemini：回送 tool_response

```json
{
  "tool_response": {
    "function_responses": [{
      "id": "<call_id>",
      "name": "search_database",
      "response": { "output": "<找到的 answer 或找不到>" }
    }]
  }
}
```

→ Gemini 拿到答案後繼續產生語音

---

## 階段 4：語音回應播放

### 4-1. Gemini 串流產生 PCM 音訊

```json
{
  "serverContent": {
    "modelTurn": {
      "parts": [
        { "inlineData": { "mimeType": "audio/pcm;rate=24000", "data": "<base64>" } },
        ...
      ]
    }
  }
}
```

→ Gemini 會送多個 chunk，最後送 `turnComplete: true`

### 4-2. server.py：透傳給瀏覽器

```python
await client_ws.send_text(response_text)
```

### 4-3. 瀏覽器：解碼並排程播放

```javascript
base64 → bytes (Uint8Array) → Int16Array → Float32Array (÷ 32768)
        → AudioBuffer (sampleRate 24000)
        → AudioBufferSourceNode → 接到 destination
        → source.start(nextPlayTime)
        → nextPlayTime += duration  // 無縫銜接下一塊
```

| | 內容 |
|---|---|
| **API** | Web Audio API (`createBuffer`, `createBufferSource`) |
| **採樣率** | **24kHz**（注意跟麥克風的 16kHz 不同） |
| **播放策略** | `nextPlayTime` 累加，多個 chunk 排程播放避免破音 |

### 4-4. 收到 turnComplete → 排程關 WS

```javascript
if (response.serverContent.turnComplete) {
    setTimeout(() => ws.close(), 500);  // Gemini 那端先關
}

// 加上每塊聲音播完 3 秒後也檢查關閉
source.onended = () => {
    setTimeout(() => { if (!isRecording && ws) ws.close(); }, 3000);
}
```

---

## 階段 5：連線關閉（省錢模式）

| 觸發 | 動作 |
|---|---|
| Gemini 送 `turnComplete` | 500ms 後關 WS |
| 最後一塊聲音播完 | 3 秒後關 WS（防止使用者立刻接續說話） |
| 使用者再次按住 | 重開新的 WebSocket（不重用） |

→ Render 上沒有持久連線，**只有按住期間計費**

---

## 用到的所有 API/服務一覽

| API/服務 | 端點 / SDK | 用途 | 認證 |
|---|---|---|---|
| **Web Audio API** | 瀏覽器原生 | 麥克風擷取 + 播放 | 使用者授權 |
| **WebSocket** | 瀏覽器原生 | 雙向音訊串流 | — |
| **Gemini Live API** | `wss://generativelanguage.googleapis.com/ws/.../v1beta.GenerativeService.BidiGenerateContent` | 語音轉語音 + tool calling | `GEMINI_API_KEY` (URL query) |
| **Gemini 模型** | `gemini-2.5-flash-native-audio-preview-12-2025` + voice `Aoede` | native audio 模型 | (同上) |
| **OpenAI Embeddings** | `https://api.openai.com/v1/embeddings` | RAG 向量化 | `OPENAI_API_KEY` (Bearer) |
| **OpenAI 模型** | `text-embedding-3-small` (1536 維) | embedding | (同上) |
| **Supabase Postgres** | `aws-0-ap-southeast-1.pooler.supabase.com:6543` | 向量檢索 | `SUPABASE_DB_URL` 含密碼 |
| **pgvector extension** | `vector` 型別 + HNSW 索引 + `<=>` 算子 | cosine 相似度 | (隨 DB 連線) |

---

## 端到端延遲拆解（warm 狀態）

```
按住 → 說話完成 → 放開按鈕      ← 取決於使用者
                ↓
          Gemini 處理音訊         ~500ms
                ↓
          decide toolCall         ~200ms (因為 thinkingBudget=0)
                ↓
   server.py 收到 toolCall
                ↓
          OpenAI embedding        ~300ms  ← 主要瓶頸
                ↓
          Supabase 查詢           ~30ms   ← HNSW 很快
                ↓
   server.py 送 tool_response
                ↓
          Gemini 產生語音          ~500ms
                ↓
          串流首個 chunk 到瀏覽器  ~100ms
                ↓
          瀏覽器播放               立即
─────────────────────────────────────
總延遲                           ~1.5-2s
```

**最大優化空間**：OpenAI embedding (~300ms)。加 LRU cache 重複問題降到 < 1ms，是下一步最高 ROI 的優化。
