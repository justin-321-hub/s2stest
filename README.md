# S2S Test（Speech-to-Speech 即時語音問答系統）

以 Google Gemini Multimodal Live API 為核心的語音問答系統，透過 FastAPI proxy 保護 API Key，並結合 Supabase pgvector 實現 RAG（檢索增強生成）。支援 Gemini Auto VAD：說話停頓後自動回應，回答中途可直接開口打斷。

## 架構說明

```
Browser (index.html) ◀── WSS ──▶ server.py (FastAPI / Render) ◀── WSS ──▶ Gemini Live API
                                          │
                               on toolCall│
                                   ┌──────┴──────┐
                                   ▼             ▼
                             OpenAI API      Supabase
                             embeddings      pgvector
```

- **Frontend (`index.html`)**: 單一 HTML 檔，按鈕切換整個 session（開始/結束對話）。連線後持續串流麥克風音訊，由 Gemini Auto VAD 偵測語音活動；Gemini 說話時可直接開口打斷。透過 AudioWorkletNode 擷取 16kHz PCM 音訊。
- **Backend (`server.py`)**: FastAPI WebSocket proxy，攔截 Gemini 的 `toolCall` 並查詢 FAQ 資料庫，其餘訊息雙向透傳。
- **資料庫**: Supabase Postgres + pgvector，儲存 FAQ 向量。

## 環境變數

在根目錄建立 `.env`（本地）或在 Render 環境設定（部署）：

```
GEMINI_API_KEY=...
OPENAI_API_KEY=...
SUPABASE_DB_URL=postgresql://postgres.<project>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
```

> **注意**：`SUPABASE_DB_URL` 必須使用 **port 6543**（Transaction Pooler）。server.py 設定了 `statement_cache_size=0`，Session Pooler（port 5432）不相容。

## 本地端開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動 FastAPI server（Terminal 1）
uvicorn server:app --reload

# 開啟前端（Terminal 2，或直接用 http://localhost:8080）
python -m http.server 8080
```

在 `index.html` 第 35 行將 `BACKEND_WS_URL` 改為本地位址：
```javascript
const BACKEND_WS_URL = "ws://localhost:8000/ws";
```

## 部署到 Render

1. 將 `GEMINI_API_KEY`、`OPENAI_API_KEY`、`SUPABASE_DB_URL` 設定為 Render 環境變數。
2. Start command：`uvicorn server:app --host 0.0.0.0 --port 10000`
3. 部署完成後，將 `index.html` 的 `BACKEND_WS_URL` 改回 Render 網址：
   ```javascript
   const BACKEND_WS_URL = "wss://<your-service>.onrender.com/ws";
   ```
4. `index.html` 透過任意 HTTP server serve(如：python -m http.server 8000)，避免麥克風權限限制。

> **Render 免費版冷啟動**：超過 15 分鐘無流量會 sleep，第一次連線需等 20–30 秒。前端已內建 **20 秒連線逾時**：若 WebSocket 在時限內未建立成功，會自動關閉並顯示提示，使用者可再次點擊重試。

## Supabase 資料表設定

`search_faq()` 查詢的資料表結構：

```sql
create table faq (
  id bigserial primary key,
  question text,
  content text,         -- server.py 中 select 的欄位，需與實際欄位名稱一致，目前修改為 content 以符合 Supabase 的 RAG 資料欄位
  embedding vector(1536)
);
```

若欄位名稱不同，修改 `server.py` 中的 SQL 查詢（`select content` 及 `row["content"]`）以符合實際欄位名稱。

## 主要修改紀錄

### Auto VAD 模式（取代 Push-to-Talk）

- **舊行為**：手動按住說話、放開送出（Push-to-Talk）。前端自行送 `activityStart` / `activityEnd`，Gemini 設定 `automaticActivityDetection: { disabled: true }`。
- **新行為**：連線後持續串流音訊，由 Gemini 內建 Auto VAD 偵測語音活動。說話停頓約 0.8 秒後自動觸發回應，無需任何按鈕操作。
- 移除 `realtimeInputConfig` 覆寫（使用 Gemini 預設值即可啟用 Auto VAD）。
- 移除手動 `activityStart` / `activityEnd` 訊息。

### 打斷（Barge-in）支援

- Gemini 說話中途，使用者開口說話時，Gemini 會送出 `serverContent.interrupted`。
- 前端收到後立即關閉並重建 `AudioContext`，清除所有已排程的音訊緩衝，幾乎無延遲地停止播放。

### 按鈕改為 Session 切換

- **舊行為**：五個狀態（`idle` / `connecting` / `recording` / `waiting` / `ready`），每輪問答後保持連線等待使用者按鈕繼續；20 秒無操作才斷線。
- **新行為**：三個狀態（`idle` / `connecting` / `active`）。第一下點擊建立連線並開始持續聆聽，再次點擊斷線。閒置計時器調整為 **30 秒**（Gemini `turnComplete` 後起算，收到新的 `modelTurn` 即重設）。

### 多輪對話支援（Multi-turn conversation）

- **舊行為**：`turnComplete` 後 500ms 自動關閉 WebSocket，每次按鈕建立新的 Gemini session，對話記憶消失。
- **新行為**：`turnComplete` 後保持連線，同一 Gemini session 中上下文持續累積。

### AudioWorklet 取代 ScriptProcessorNode

- `ScriptProcessorNode` 已棄用且在主執行緒執行，改為 `AudioWorkletNode`（獨立 audio thread）。
- Worklet 程式碼以 inline Blob 方式載入，不需要額外的 `.js` 檔案。

### 冷啟動連線逾時保護

- 點擊「開始對話」後，前端立即顯示提示：「連線中，伺服器可能需要 20–30 秒喚醒...」
- 啟動 **20 秒計時器**；若 WebSocket `onopen` 在時限內未觸發，自動呼叫 `ws.close()`，按鈕恢復可點擊，並在日誌顯示逾時訊息。
- 解決 Render 免費版冷啟動時按鈕被鎖住、使用者無法操作的問題。

### WebSocket Keepalive（防止 Render 切斷閒置連線）

- Server → Gemini：`websockets.connect(..., ping_interval=20, ping_timeout=10)`
- Server → Browser：每 15 秒送一個 `{"type": "keepalive"}` JSON 訊息；Browser 端直接忽略。

### Server 任務生命週期修正

- 舊的 `asyncio.gather` 在一個 task 結束後無法主動取消另一個。
- 改為 `asyncio.wait(FIRST_COMPLETED)` + 明確 `task.cancel()`，任一側（Browser 或 Gemini）斷線時兩個 task 都乾淨結束。
