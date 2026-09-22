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

# ---- 你的持股設定（跟網頁 holdings 保持一致，之後有交易記得同步改這裡）----
HOLDINGS_TW = [
    {"id": "0050",  "name": "元大台灣50",     "shares": 5990,  "buy_price": 102.54},
    {"id": "00646", "name": "元大S&P500",     "shares": 4000,  "buy_price": 60.28},
    {"id": "00662", "name": "富邦NASDAQ",     "shares": 4000,  "buy_price": 101.26},
    {"id": "00713", "name": "元大台灣高息低波", "shares": 8000,  "buy_price": 52.71},
    {"id": "00919", "name": "群益台灣精選高息", "shares": 18000, "buy_price": 23.67},
]
HOLDINGS_US = [
    {"id": "KLAC", "name": "KLA Corporation", "shares": 40, "buy_price": 181.33},
    {"id": "SPCX", "name": "SpaceX",          "shares": 1,  "buy_price": 138.94},
]
CASH_TWD = 85116


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
            # 每次成功呼叫後也稍微停一下，降低下一檔被判定為濫用的機率
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
    """依你的投資者設定檔組出分析 prompt"""
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
4. 對每檔持股（0050/00646/00662/00713/00919/KLAC/SPCX）的簡短建議
5. 今日市場判斷（🟢偏多/🟡中性/🟠謹慎/🔴偏空）+ 今日操作建議

請只做事實陳述與紀律提醒，不要鼓勵追高或頻繁交易。輸出繁體中文，控制在600字內。
"""


def call_gemini(prompt):
    # Gemini 模型改版很快（1.5→2.0→2.5→3.x），依序嘗試，
    # 第一個成功回應的就採用，避免單一模型被下架就整支腳本掛掉。
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
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 404:
                last_status = 404
                continue  # 這個模型不存在/已下架，換下一個試試
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.exceptions.HTTPError as e:
            last_status = getattr(e.response, "status_code", "unknown")
            if last_status == 404:
                continue
            # 非 404 的錯誤（例如 401/403 金鑰無效、429 額度用完）直接中止，換模型也沒用
            raise RuntimeError(
                f"Gemini API 呼叫失敗（HTTP {last_status}），請檢查 GEMINI_API_KEY 是否有效，"
                f"或前往 Google Cloud Console 確認該專案已啟用 Generative Language API"
            )
    raise RuntimeError(
        f"Gemini API 呼叫失敗：候選模型 {candidate_models} 全部回傳 404，"
        f"請至 Google AI Studio 重新建立 API key（建議選『Create API key in new project』），"
        f"並確認 Google Cloud Console 的 Generative Language API 已啟用"
    )


def main():
    market_data = {}

    # 台股：抓價格 + 計算0050日K
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
                }
                if h["id"] == "0050":
                    entry["k_value"] = calc_kd(rows)
                market_data[h["id"]] = entry
        except Exception as e:
            market_data[h["id"]] = {"error": str(e)}

    # 美股
    for h in HOLDINGS_US:
        try:
            price = fetch_yahoo_quote(h["id"])
            market_data[h["id"]] = {
                "name": h["name"], "close": price,
                "buy_price": h["buy_price"], "shares": h["shares"],
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
