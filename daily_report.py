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


def load_holdings():
    """優先讀取網頁自動同步過來的 holdings.json；讀不到/格式不對就退回內建保底值。
    回傳 (holdings_tw, holdings_us, cash_twd)。"""
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("holdings", [])
            cash = data.get("cash", _DEFAULT_CASH_TWD)

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
                print(f"✅ 從 holdings.json 讀到 {len(tw)} 檔台股 + {len(us)} 檔美股，現金 NT${cash:,}")
                return tw, us, cash
            print("⚠️ holdings.json 存在但內容是空的，改用內建保底持股")
        except Exception as e:
            print(f"⚠️ 讀取 holdings.json 失敗（{e}），改用內建保底持股")
    else:
        print("ℹ️ 尚未找到 holdings.json（可能還沒設定網頁自動同步），使用內建保底持股")
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
    """依你的投資者設定檔組出分析 prompt。
    持股代號改成從 HOLDINGS_TW/HOLDINGS_US 動態組出，
    之後你在這兩個清單加減股票，這裡的文字說明會自動跟著變，不用再手動改這段文字。"""
    ticker_list = "/".join([h["id"] for h in HOLDINGS_TW] + [h["id"] for h in HOLDINGS_US])
    return f"""你是一位保守防禦型的投資分析助手，服務對象是林義凱。

他的投資原則：
- 重視下檔風險，不追高，不因短期情緒交易
- ETF為核心，長期投資，台美跨市場分散
- 每月5日/15日定期定額買 0050、00646 各NT$5,000
- 0050金字塔加碼法：日K<50才開始分階梯加碼，現金備用金 NT${CASH_TWD:,}

今日市場數據：
{json.dumps(market_data, ensure_ascii=False, indent=2)}

請用「保守、防禦、數據導向」的風格，輸出今日投資晨報，需包含：
1. 全球市場摘要（美股三大指數、費半、台股、VIX）
2. 半導體/AI產業重點（若數據中有KLAC相關資訊）
3. 0050金字塔加碼法：目前是否觸發？
4. 對每檔持股（{ticker_list}）的簡短建議，只根據上方market_data裡實際有的資料分析，
   若某檔股票的資料是error或缺漏，請明確說明「該檔今日資料抓取失敗，暫無法分析」，不要憑空編造價格或建議
5. 今日市場判斷（🟢偏多/🟡中性/🟠謹慎/🔴偏空）+ 今日操作建議

請只做事實陳述與紀律提醒，不要鼓勵追高或頻繁交易。輸出繁體中文，控制在600字內。
"""




def call_gemini(prompt):
    # Gemini 模型改版很快（1.5→2.0→2.5→3.x），依序嘗試多個候選模型；
    # 503（伺服器過載）、429（額度限流）是暫時性問題，同一個模型先重試幾次再放棄。
    candidate_models = [
        "gemini-2.5-flash",
        "gemini-flash-latest",
        "gemini-2.0-flash",
        "gemini-1.5-flash-8b",
    ]
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    last_status = None
    for model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):  # 同一個模型最多重試3次
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=30)
                if resp.status_code == 404:
                    last_status = 404
                    break  # 模型不存在，不用重試，直接換下一個模型
                if resp.status_code in (503, 429):
                    last_status = resp.status_code
                    wait = (attempt + 1) * 8 + random.uniform(0, 3)
                    print(f"  {model} 回傳 {resp.status_code}（伺服器過載/限流），"
                          f"等待 {wait:.1f} 秒後重試 (第{attempt+1}次)...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except requests.exceptions.HTTPError as e:
                last_status = getattr(e.response, "status_code", "unknown")
                raise RuntimeError(
                    f"Gemini API 呼叫失敗（HTTP {last_status}），請檢查 GEMINI_API_KEY 是否有效"
                )
        # 對這個模型重試3次仍失敗（503/429）或直接404 → 換下一個候選模型
    raise RuntimeError(
        f"Gemini API 呼叫失敗：所有候選模型都無法使用（最後狀態碼 HTTP {last_status}）。"
        f"若是 503/429，通常是 Google 免費層暫時過載，明天排程重跑大多會自動恢復；"
        f"若持續發生，可能要考慮改用付費層或其他免費模型（如 Groq、OpenRouter）"
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
