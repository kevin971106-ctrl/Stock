"""
每日投資晨報產生器
資料來源：FinMind（台股）+ Yahoo Finance（美股/匯率）
分析引擎：Gemini API
輸出：report.json（給網站前端讀取顯示）

使用方式：
1. pip install requests
2. 設定環境變數：FINMIND_TOKEN、GEMINI_API_KEY
3. python daily_report.py
（建議透過 GitHub Actions 排程每天自動執行，見 daily-report.yml）
"""

import os
import json
import time
import random
import requests
from datetime import datetime, timedelta

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Disclosed Capitol：追蹤美國國會議員／行政部門（含川普）依STOCK Act公開申報的股票交易，
# 免費金鑰到 https://www.disclosedcapitol.com/signup 申請。沒設定就自動略過這部分，不影響其他內容。
DISCLOSED_CAPITOL_API_KEY = os.environ.get("DISCLOSED_CAPITOL_API_KEY", "")

# Yahoo Finance 對沒有瀏覽器標頭、或來自雲端主機（如 GitHub Actions）的請求
# 容易回傳 429 Too Many Requests，因此統一用一個帶標頭的 Session，
# 並在每次呼叫之間加入隨機延遲、失敗時自動重試。
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_session = requests.Session()
_session.headers.update(YAHOO_HEADERS)

# ---- 持股設定（保底預設值：只有在 holdings.json 不存在或讀取失敗時才會用到）----
# 網頁每次新增/編輯/刪除持股時，會自動透過GitHub API把最新持股寫進 holdings.json，
# 正常情況下下面這份不需要手動改；只有在還沒設定同步、或同步失敗時才會用到這份保底資料。
_DEFAULT_HOLDINGS_TW = [
    {"id": "0050",  "name": "元大台灣50",     "shares": 5990,  "buy_price": 102.54},
    {"id": "00646", "name": "元大S&P500",     "shares": 4000,  "buy_price": 60.28},
    {"id": "00662", "name": "富邦NASDAQ",     "shares": 4000,  "buy_price": 101.26},
    {"id": "00713", "name": "元大台灣高息低波", "shares": 8000,  "buy_price": 52.71},
    {"id": "00919", "name": "群益台灣精選高息", "shares": 18000, "buy_price": 23.67},
]
_DEFAULT_HOLDINGS_US = [
    {"id": "KLAC", "name": "KLA Corporation", "shares": 40, "buy_price": 181.33},
    {"id": "SPCX", "name": "SpaceX",          "shares": 1,  "buy_price": 138.94},
]
_DEFAULT_CASH_TWD = 85116

HOLDINGS_FILE = "holdings.json"
HOLDINGS_JSON_ENV = "HOLDINGS_JSON"  # 對應 daily-report.yml 裡的 ${{ vars.HOLDINGS_JSON }}


def _normalize_holdings(raw, cash, source_label):
    def norm(h):
        return {
            "id": h["id"],
            "name": h.get("name", h["id"]),
            "shares": h["shares"],
            "buy_price": h.get("buyPrice", h.get("buy_price")),
        }
    tw = [norm(h) for h in raw if h.get("currency") == "TWD"]
    us = [norm(h) for h in raw if h.get("currency") == "USD"]
    if tw or us:
        print(f"✅ 從{source_label}讀到 {len(tw)} 檔台股 + {len(us)} 檔美股，現金 NT${cash:,}")
        return tw, us, cash
    return None


def load_holdings():
    """持股來源優先順序：
    1. HOLDINGS_JSON 環境變數（來自 GitHub repo 的 Repository Variables，
       你在網頁上按「複製持股設定」貼過去的——這是唯一會反映你「目前真正持有」的來源，
       不會分析你已經賣掉的舊持股，也不會漏掉剛買的新持股）
    2. holdings.json 檔案（保留舊機制相容，目前網頁沒有自動寫入這個檔案）
    3. 內建保底值（上面兩個都沒有時才會用到，多半代表你還沒同步過）
    回傳 (holdings_tw, holdings_us, cash_twd)。"""
    env_json = os.environ.get(HOLDINGS_JSON_ENV, "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            result = _normalize_holdings(
                data.get("holdings", []), data.get("cash", _DEFAULT_CASH_TWD), "HOLDINGS_JSON變數"
            )
            if result:
                return result
            print("⚠️ HOLDINGS_JSON 變數存在但內容是空的，改試其他來源")
        except Exception as e:
            print(f"⚠️ 解析 HOLDINGS_JSON 變數失敗（{e}），改試其他來源")

    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = _normalize_holdings(
                data.get("holdings", []), data.get("cash", _DEFAULT_CASH_TWD), "holdings.json"
            )
            if result:
                return result
            print("⚠️ holdings.json 存在但內容是空的，改用內建保底持股")
        except Exception as e:
            print(f"⚠️ 讀取 holdings.json 失敗（{e}），改用內建保底持股")
    else:
        print("ℹ️ 尚未設定 HOLDINGS_JSON 變數、也沒有 holdings.json，使用內建保底持股"
              "（記得去網頁按「複製持股設定」同步一次）")
    return _DEFAULT_HOLDINGS_TW, _DEFAULT_HOLDINGS_US, _DEFAULT_CASH_TWD


HOLDINGS_TW, HOLDINGS_US, CASH_TWD = load_holdings()


def fetch_finmind_price(stock_id, days=90):
    """抓取台股日線資料（含日K計算所需的高低收）"""
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date,
        "token": FINMIND_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def calc_kd(rows, n=9):
    """標準 KD(9,3,3)，rows 需含 max/min/close 欄位（依日期升序）"""
    if len(rows) < n:
        return None
    k = d = 50.0
    for i in range(n - 1, len(rows)):
        window = rows[i - n + 1: i + 1]
        h9 = max(r["max"] for r in window)
        l9 = min(r["min"] for r in window)
        close = rows[i]["close"]
        rsv = 50.0 if h9 == l9 else (close - l9) / (h9 - l9) * 100
        k = (2 / 3) * k + (1 / 3) * rsv
        d = (2 / 3) * d + (1 / 3) * k
    return round(k, 1)


def fetch_yahoo_quote(symbol, max_retries=3):
    """抓取即時報價，失敗（含429）時自動重試並延長等待時間"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = _session.get(url, params={"interval": "1d", "range": "5d"}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            time.sleep(random.uniform(1.0, 2.0))
            return data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        except requests.exceptions.HTTPError as e:
            last_error = e
            if resp is not None and resp.status_code == 429:
                wait = (attempt + 1) * 5 + random.uniform(0, 2)
                print(f"  429 rate limited on {symbol}，等待 {wait:.1f} 秒後重試...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_error = e
            time.sleep(2)
    raise last_error


def fetch_yahoo_history(symbol, max_retries=3):
    """抓取美股近3個月完整OHLC歷史資料，供網頁K線圖使用（格式對齊 report.json 的 history 欄位）"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = _session.get(url, params={"interval": "1d", "range": "3mo"}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            time.sleep(random.uniform(1.0, 2.0))
            result = data["chart"]["result"][0]
            ts = result.get("timestamp", [])
            q = result["indicators"]["quote"][0]
            rows = []
            for i in range(len(ts)):
                if q["close"][i] is None:
                    continue
                rows.append({
                    "date": datetime.utcfromtimestamp(ts[i]).strftime("%Y-%m-%d"),
                    "open": q["open"][i],
                    "high": q["high"][i],
                    "low": q["low"][i],
                    "close": q["close"][i],
                })
            return rows
        except requests.exceptions.HTTPError as e:
            last_error = e
            if resp is not None and resp.status_code == 429:
                wait = (attempt + 1) * 5 + random.uniform(0, 2)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_error = e
            time.sleep(2)
    raise last_error


def fetch_congress_trades(ticker, limit=5):
    """查詢美股上，國會議員/行政部門（含川普等，依 STOCK Act 公開揭露）對這檔股票的近期交易。
    資料來源：Disclosed Capitol API。沒設定金鑰、或查詢失敗，都直接回傳空清單，
    不會讓整個晨報產生失敗——這一塊是加值資訊，不是關鍵路徑。"""
    if not DISCLOSED_CAPITOL_API_KEY:
        return []
    try:
        resp = requests.get(
            f"https://api.disclosedcapitol.com/tickers/{ticker}/trades",
            headers={"DC-API-Key": DISCLOSED_CAPITOL_API_KEY},
            params={"limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "politician": t.get("politician_name"),
                "party": t.get("party"),
                "chamber": t.get("chamber"),
                "type": t.get("trade_type"),
                "amount_range": t.get("amount_range"),
                "transaction_date": t.get("transaction_date"),
                "disclosure_date": t.get("disclosure_date"),
            }
            for t in data.get("trades", [])
        ]
    except Exception as e:
        print(f"  查詢 {ticker} 的國會交易資料失敗（{e}），略過這部分")
        return []


def pyramid_suggestion(k_value):
    if k_value is None:
        return "K值資料不足，暫無法判斷"
    if k_value < 20:
        return f"K={k_value}（<20）→ 建議加碼 NT$50,000（極端恐慌）"
    if k_value < 30:
        return f"K={k_value}（<30）→ 建議加碼 NT$30,000"
    if k_value < 40:
        return f"K={k_value}（<40）→ 建議加碼 NT$20,000"
    if k_value < 50:
        return f"K={k_value}（<50）→ 建議加碼 NT$10,000"
    return f"K={k_value}（≥50）→ 高掛免戰牌，一張不追，維持定期定額"


def build_prompt(market_data):
    """依你的投資者設定檔組出 prompt。
    持股代號從 HOLDINGS_TW/HOLDINGS_US 動態組出——這兩份清單是由 load_holdings() 決定的
    （優先讀你在網頁上設定、同步過來的持股；沒有同步過就用內建保底清單），
    所以只要你有做「複製持股設定」同步，這裡分析的就一定是你目前真正持有的股票，
    不會出現已經賣掉的舊持股，也不會漏掉剛買的新持股。
    另外開啟了Google搜尋grounding，模型回答前會先查當下真實新聞，不是只憑舊知識瞎猜。"""
    ticker_list = "/".join([h["id"] for h in HOLDINGS_TW] + [h["id"] for h in HOLDINGS_US])
    return f"""你是一位保守防禦型的投資分析助手，服務對象是林義凱。你有Google搜尋工具可以查詢當下最新的新聞，請務必先搜尋今天/近期的真實新聞再回答，不要只憑舊有知識瞎猜，也絕對不要分析或提到不在下面「目前持股」清單裡的股票。

他的投資原則：
- 重視下檔風險，不追高，不因短期情緒交易
- ETF為核心，長期投資，台美跨市場分散
- 每月5日/15日定期定額買 0050、00646 各NT$5,000
- 0050金字塔加碼法：日K<50才開始分階梯加碼，現金備用金 NT${CASH_TWD:,}

【目前持股，只分析這些，不要提到清單以外的股票】
{ticker_list}

今日市場數據：
{json.dumps(market_data, ensure_ascii=False, indent=2)}

請用「保守、防禦、數據導向」的風格，搜尋後輸出今日投資晨報，需包含以下區塊（請保留這些標題）：

【市場摘要】全球市場摘要（美股三大指數、費半、台股、VIX），依上面的數據陳述現況。

【持股相關新聞】搜尋並列出今天跟「目前持股」清單裡每一檔直接相關的新聞（若某檔今天沒有重大新聞可省略不寫，不要硬湊，也不要提清單外的股票）。

【本週/本月焦點新聞】搜尋近一週、近一月與股市或半導體/AI/美股大盤/台股大盤相關產業最熱門的重點新聞（例如：Fed利率決策、地緣政治、半導體法案、AI晶片需求、大型科技公司財報等），挑2-4則最重要的簡述。

【總經與意見領袖動向】搜尋以下兩個來源近期（優先近1-3天，其次近1週）的公開發言，分別條列：
1. 川普（Donald Trump）— 近期跟總體經濟/股市/貿易關稅/產業政策相關的公開發言或政策動向，摘要重點並說明可能影響的產業或市場方向。
2. Serenity（X帳號 @aleabitoreddit，AI/半導體供應鏈分析型KOL）— 近期發文或在會議/訪談上提到的個股觀點，明確列出「看多（做多/加碼）」的股票代號有哪些、「看空（做空/看衰）」的股票代號有哪些，並用一句話摘要他的理由。
規則：只寫你搜尋到、有實際依據的內容，用你自己的話摘要不要整段照抄；如果某個來源這幾天查不到新的相關發言，就寫「近期查無新發言」，絕對不要編造引言或編造他們沒說過的股票代號。這只是市場意見/輿情參考，不代表本報告或林義凱的立場，也不是投資建議。

【國會議員交易動向】檢查上方market_data裡，各檔持股是否有附帶「congress_trades」欄位（美國國會議員/行政部門依STOCK Act公開申報的交易紀錄，只有美股才可能有）。如果有，列出裡面的交易（政治人物姓名、政黨、買/賣、金額區間、交易日期），特別點出「Donald Trump」或知名人物的交易；如果完全沒有這個欄位或是空的，就寫「近期無國會議員申報交易資料」，不要編造。這是公開申報資料的參考，不代表投資建議。

【趨勢變化與可能影響】根據以上新聞與意見領袖動向，探討可能出現的趨勢變化，並說明對他「目前持股」組合可能的影響方向——只做情境分析與風險提醒，不做加碼/停利等具體操作建議。

【金字塔加碼判斷】0050金字塔加碼法目前是否觸發？

【持股建議】對「目前持股」清單裡每一檔的簡短建議（一行一檔），只根據上方market_data裡實際有的資料分析；若某檔資料是error或缺漏，請明確說明「該檔今日資料抓取失敗，暫無法分析」，不要憑空編造價格或建議。

【今日判斷】今日市場判斷（🟢偏多/🟡中性/🟠謹慎/🔴偏空）+ 一句話操作提醒。

請只做事實陳述與紀律提醒，不要鼓勵追高或頻繁交易，不要編造搜尋不到的新聞或發言。輸出繁體中文，控制在1200字內（不含來源清單）。
"""


def call_gemini(prompt):
    # gemini-2.5-flash 已經確認被下架（每次都404），拿掉不再嘗試，省時間。
    # gemini-3.5-flash 是目前主力穩定版，gemini-flash-latest 當備援。
    candidate_models = [
        "gemini-3.5-flash",
        "gemini-flash-latest",
    ]
    # v1beta 目前有個 Google 那端已知的不穩定問題：models.list 顯示模型存在、也支援
    # generateContent，但實際呼叫卻回傳404（2026年8-9月Google官方論壇上多次被回報）。
    # 每個模型都順便多試一次穩定版 v1 端點當備援，繞開這個問題。
    api_versions = ["v1beta", "v1"]
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 開啟 Google 搜尋 grounding，讓模型回答前能先查詢當下真實的新聞。
        # 這個功能需要Google帳號開通付款方式才能穩定使用（即使在免費額度內），
        # 純無卡的免費金鑰常常會直接收到429，這是Google那邊的已知限制。
        "tools": [{"google_search": {}}],
    }

    failure_log = []  # 記錄每個組合各自失敗的原因，方便診斷（不要只留最後一筆，容易誤導）
    for model in candidate_models:
        for version in api_versions:
            url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent"
            for attempt in range(3):  # 同一組 模型+版本 最多重試3次
                try:
                    # 有加搜尋工具會比純文字生成慢，timeout拉長一點
                    resp = requests.post(url, headers=headers, json=body, timeout=60)
                    if resp.status_code == 404:
                        failure_log.append(f"{model}({version})=404")
                        print(f"  {model}（{version}）回傳404，改試下一個組合...")
                        break  # 這個組合不存在，不用重試，直接換下一個
                    if resp.status_code in (503, 429):
                        wait = (attempt + 1) * 8 + random.uniform(0, 3)
                        print(f"  {model}（{version}）回傳 {resp.status_code}（伺服器過載/限流/需開通付款），"
                              f"等待 {wait:.1f} 秒後重試 (第{attempt+1}次)...")
                        if attempt == 2:
                            failure_log.append(f"{model}({version})={resp.status_code}")
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    candidate = data["candidates"][0]
                    parts = candidate.get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts if "text" in p)

                    # 有用到搜尋的話，把引用來源整理附在報告最後，方便回頭查證
                    grounding = candidate.get("groundingMetadata", {}) or {}
                    sources = []
                    for chunk in grounding.get("groundingChunks") or []:
                        web = chunk.get("web") or {}
                        if web.get("uri") and web.get("title"):
                            sources.append(f"- {web['title']}：{web['uri']}")
                    if sources:
                        text += "\n\n📎 資料來源：\n" + "\n".join(sources[:8])

                    if model != candidate_models[0] or version != api_versions[0]:
                        print(f"  注意：主要組合失敗，這次報告是用備援組合 {model}（{version}）產生的")
                    return text
                except requests.exceptions.HTTPError as e:
                    status = getattr(e.response, "status_code", "unknown")
                    failure_log.append(f"{model}({version})={status}")
                    print(f"  {model}（{version}）呼叫失敗（HTTP {status}），改試下一個組合...")
                    break  # 換組合，不整個中斷（例如某模型不支援搜尋工具語法而回傳400）
                except Exception as e:
                    failure_log.append(f"{model}({version})={type(e).__name__}")
                    print(f"  {model}（{version}）呼叫發生例外（{type(e).__name__}），改試下一個組合...")
                    break
            # 對這組 模型+版本 重試3次仍失敗（503/429）或直接404/其他錯誤 → 換下一個版本/模型
    raise RuntimeError(
        f"Gemini API 呼叫失敗：所有候選模型與版本組合都無法使用。"
        f"各組合失敗原因：{', '.join(failure_log)}。"
        f"若都是429，很可能是Google帳號還沒開通付款方式（即使沒超過免費額度，grounding搜尋功能也常需要）；"
        f"若是503，通常是暫時過載，明天重跑大多會自動恢復"
    )



ASSET_HISTORY_FILE = "asset_history.json"


def update_asset_history(total_assets, market_value, cash):
    """把今天的總資產快照加進歷史紀錄檔，供網頁畫累積趨勢折線圖用。
    同一天重跑會覆蓋當天那一筆，不會一天內累積出多筆重複資料；
    只保留最近365筆，避免檔案無限長大。"""
    history = []
    if os.path.exists(ASSET_HISTORY_FILE):
        try:
            with open(ASSET_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            print(f"  讀取舊的 asset_history.json 失敗，將視為空歷史重新開始: {e}")
            history = []

    today = datetime.now().strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "total_assets": round(total_assets),
        "market_value": round(market_value),
        "cash": cash,
    })
    history.sort(key=lambda h: h["date"])
    history = history[-365:]

    with open(ASSET_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return history


def main():
    market_data = {}

    # 台股：抓價格 + 完整歷史K棒 + 計算0050日K
    for h in HOLDINGS_TW:
        try:
            rows = fetch_finmind_price(h["id"])
            if rows:
                latest = rows[-1]
                entry = {
                    "name": h["name"],
                    "close": latest["close"],
                    "buy_price": h["buy_price"],
                    "shares": h["shares"],
                    # 網頁K線圖用：轉成跟Yahoo美股一致的 {date,open,high,low,close} 格式
                    "history": [
                        {"date": r["date"], "open": r["open"], "high": r["max"], "low": r["min"], "close": r["close"]}
                        for r in rows[-90:]
                    ],
                }
                if h["id"] == "0050":
                    entry["k_value"] = calc_kd(rows)
                market_data[h["id"]] = entry
        except Exception as e:
            market_data[h["id"]] = {"error": str(e)}

    # 美股：抓即時價 + 完整歷史K棒
    for h in HOLDINGS_US:
        try:
            price = fetch_yahoo_quote(h["id"])
            history = []
            try:
                history = fetch_yahoo_history(h["id"])
            except Exception as hist_err:
                print(f"  {h['id']} 歷史K棒抓取失敗（不影響現價）: {hist_err}")
            market_data[h["id"]] = {
                "name": h["name"], "close": price,
                "buy_price": h["buy_price"], "shares": h["shares"],
                "history": history,
            }
            congress_trades = fetch_congress_trades(h["id"])
            if congress_trades:
                market_data[h["id"]]["congress_trades"] = congress_trades
        except Exception as e:
            market_data[h["id"]] = {"error": str(e)}

    # 大盤/指數參考
    for sym in ["^TWII", "^GSPC", "^IXIC", "^SOX", "^VIX", "TWD=X"]:
        try:
            market_data[sym] = {"close": fetch_yahoo_quote(sym)}
        except Exception as e:
            market_data[sym] = {"error": str(e)}

    k_value = market_data.get("0050", {}).get("k_value")
    pyramid_note = pyramid_suggestion(k_value)

    # ---- 計算今天的總資產，寫進歷史紀錄檔（供網頁畫累積趨勢折線圖）----
    fx_rate = market_data.get("TWD=X", {}).get("close") or 32.5
    market_value_twd = 0.0
    for h in HOLDINGS_TW:
        entry = market_data.get(h["id"], {})
        if isinstance(entry.get("close"), (int, float)):
            market_value_twd += entry["close"] * h["shares"]
    for h in HOLDINGS_US:
        entry = market_data.get(h["id"], {})
        if isinstance(entry.get("close"), (int, float)):
            market_value_twd += entry["close"] * h["shares"] * fx_rate
    total_assets_twd = market_value_twd + CASH_TWD
    try:
        update_asset_history(total_assets_twd, market_value_twd, CASH_TWD)
        print(f"✅ asset_history.json 已更新（今日總資產約 NT${total_assets_twd:,.0f}）")
    except Exception as e:
        print(f"⚠️ 寫入 asset_history.json 失敗: {e}")

    prompt = build_prompt(market_data)
    try:
        ai_report = call_gemini(prompt) if GEMINI_API_KEY else "（未設定 GEMINI_API_KEY，僅顯示原始數據）"
    except Exception as e:
        ai_report = f"Gemini 呼叫失敗：{e}"

    # Disclosed Capitol API 條款要求：有引用/轉載他們整理過的資料時要附上出處標註
    if any("congress_trades" in v for v in market_data.values() if isinstance(v, dict)):
        ai_report += "\n\n（國會議員交易資料由 Disclosed Capitol 提供：disclosedcapitol.com）"

    output = {
        "generated_at": datetime.now().isoformat(),
        "pyramid_status": pyramid_note,
        "k_value_0050": k_value,
        "market_data": market_data,
        "ai_report": ai_report,
    }

    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("✅ report.json 已產生")
    print(ai_report)


if __name__ == "__main__":
    main()
