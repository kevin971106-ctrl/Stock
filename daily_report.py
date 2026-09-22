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


def fetch_yahoo_chart(symbol, range_="3mo", max_retries=3):
    """
    抓取最新價 + 歷史日K（近3個月），給網頁的K線圖、個股評分機制用。
    因為網頁前端直接連 Yahoo Finance 常被瀏覽器 CORS 政策擋下，
    改由這裡（伺服器端，不受CORS限制）先抓好存進 report.json，前端只要讀現成資料。
    回傳: {"latest": 最新收盤價, "history": [{"date","open","high","low","close"}, ...]}
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = _session.get(url, params={"interval": "1d", "range": range_}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            time.sleep(random.uniform(1.0, 2.0))

            result = data["chart"]["result"][0]
            latest = result["meta"]["regularMarketPrice"]
            timestamps = result.get("timestamp") or []
            quote = result["indicators"]["quote"][0]

            history = []
            for i, ts in enumerate(timestamps):
                close = quote["close"][i]
                if close is None:
                    continue
                history.append({
                    "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                    "open": quote["open"][i],
                    "high": quote["high"][i],
                    "low": quote["low"][i],
                    "close": close,
                })
            return {"latest": latest, "history": history}
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
    """依你的投資者設定檔組出分析 prompt（現在會要求模型先上網搜尋最新新聞再回答）"""
    holdings_list = "0050、00646、00662、00713、00919、KLAC（KLA Corporation）、SPCX（SpaceX）"
    return f"""你是一位保守防禦型的投資分析助手，服務對象是林義凱。你有Google搜尋工具可以查詢當下最新的新聞，請務必先搜尋今天/近期的真實新聞再回答，不要只憑舊有知識瞎猜。

他的投資原則：
- 重視下檔風險，不追高，不因短期情緒交易
- ETF為核心，長期投資，台美跨市場分散
- 每月5日/15日定期定額買 0050、00646 各NT$5,000
- 0050金字塔加碼法：日K<50才開始分階梯加碼，現金備用金 NT${CASH_TWD:,}
- 持股：{holdings_list}

今日市場數據：
{json.dumps(market_data, ensure_ascii=False, indent=2)}

請用「保守、防禦、數據導向」的風格，搜尋後輸出今日投資晨報，需包含以下區塊（請保留這些標題）：

【市場摘要】全球市場摘要（美股三大指數、費半、台股、VIX），依上面的數據陳述現況。

【持股相關新聞】搜尋並列出今天跟林義凱持股直接相關的新聞（{holdings_list} 各自對應的公司/追蹤指數/成分股，若某檔今天沒有重大新聞可省略不寫，不要硬湊）。

【本週/本月焦點新聞】搜尋近一週、近一月與股市或半導體/AI/美股大盤/台股大盤相關產業最熱門的重點新聞（例如：Fed利率決策、地緣政治、半導體法案、AI晶片需求、大型科技公司財報等），挑2-4則最重要的簡述。

【趨勢變化與可能影響】根據以上新聞，探討可能出現的趨勢變化（例如：升息/降息預期改變、AI產業景氣循環、地緣政治風險升溫或降溫等），並說明對他持股組合可能的影響方向——只做情境分析與風險提醒，不做加碼/停利等具體操作建議。

【金字塔加碼判斷】0050金字塔加碼法目前是否觸發？

【持股建議】對每檔持股的簡短建議（一行一檔）。

【今日判斷】今日市場判斷（🟢偏多/🟡中性/🟠謹慎/🔴偏空）+ 一句話操作提醒。

請只做事實陳述與紀律提醒，不要鼓勵追高或頻繁交易，不要編造搜尋不到的新聞。輸出繁體中文，控制在900字內（不含來源清單）。
"""


def call_gemini(prompt, max_retries=3):
    # 改用釘住的穩定版本 gemini-2.5-flash，而不是「永遠指向最新版」的 gemini-flash-latest。
    # 最新版模型（目前是 Gemini 3.5 系列）剛上線時容量通常比較緊繃，503頻率較高；
    # 2.5-flash 已經上線一段時間、比較穩定。代價是：以後 Google 真的把 2.5-flash 淘汰時
    # （通常會提前很久公告），需要手動把這裡的版本號改成新的穩定版。
    model = "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 開啟 Google 搜尋 grounding，讓模型回答前能先查詢當下真實的新聞，
        # 而不是只憑訓練資料瞎猜（訓練資料本來就不會有「今天」的新聞）。
        # 免費額度：每天500次搜尋，這裡一天只用1次，完全用不到額外費用。
        "tools": [{"google_search": {}}],
    }
    last_status = "unknown"
    for attempt in range(max_retries):
        try:
            # 有加搜尋工具時，模型要先搜尋網頁才能作答，會比純文字生成慢，timeout拉長一點
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            candidate = data["candidates"][0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if "text" in p)

            # 有用到搜尋的話，把引用來源整理附在報告最後，方便回頭點進去查證原文
            grounding = candidate.get("groundingMetadata", {}) or {}
            sources = []
            for chunk in grounding.get("groundingChunks") or []:
                web = chunk.get("web") or {}
                if web.get("uri") and web.get("title"):
                    sources.append(f"- {web['title']}：{web['uri']}")
            if sources:
                text += "\n\n📎 資料來源：\n" + "\n".join(sources[:8])

            return text
        except Exception as e:
            # 重要：絕對不要把原始例外訊息直接寫進 report.json！
            # requests 的例外物件可能包含完整的請求 URL，若金鑰是用 query string 帶入
            # （例如 ?key=xxx）就會連同金鑰一起被記錄下來、被 commit 進 git 歷史。
            # 這裡改用 x-goog-api-key header 傳金鑰（不會出現在 URL 裡），
            # 並且錯誤訊息只保留 HTTP 狀態碼，不輸出任何原始例外內容。
            status = getattr(getattr(e, "response", None), "status_code", "unknown")
            last_status = status
            # 503 = Gemini 那端暫時過載（跟金鑰是否有效無關），值得重試；其他錯誤（如401/403金鑰確實有問題）直接放棄重試
            if status == 503 and attempt < max_retries - 1:
                wait = (attempt + 1) * 8 + random.uniform(0, 3)
                print(f"  Gemini 503（服務暫時過載），等待 {wait:.1f} 秒後重試（第 {attempt+1}/{max_retries} 次）...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini API 呼叫失敗（HTTP {last_status}），請檢查 GEMINI_API_KEY 是否有效")


def main():
    market_data = {}

    # 台股：抓價格 + 計算0050日K + 存近60日歷史K線（給網頁K線圖/評分用）
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
                    "history": [
                        {
                            "date": r["date"],
                            "open": r["open"],
                            "high": r["max"],
                            "low": r["min"],
                            "close": r["close"],
                        }
                        for r in rows[-60:]
                    ],
                }
                if h["id"] == "0050":
                    entry["k_value"] = calc_kd(rows)
                market_data[h["id"]] = entry
        except Exception as e:
            market_data[h["id"]] = {"error": str(e)}

    # 美股：一次抓最新價 + 近60日歷史K線
    for h in HOLDINGS_US:
        try:
            chart = fetch_yahoo_chart(h["id"], range_="3mo")
            market_data[h["id"]] = {
                "name": h["name"], "close": chart["latest"],
                "buy_price": h["buy_price"], "shares": h["shares"],
                "history": chart["history"][-60:],
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
