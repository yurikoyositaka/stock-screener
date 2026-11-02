import os
import time
import requests
import json
import pandas as pd
import datetime
import sys
import jpholiday
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# --- 0. 営業日判定 ---
JST = datetime.timezone(datetime.timedelta(hours=+9), 'JST')
today = datetime.datetime.now(JST).date()

if today.weekday() >= 5:
    print(f"{today} は休場日（土日）のため、スクリーニングをスキップします。")
    sys.exit()

if jpholiday.is_holiday(today):
    print(f"{today} は休場日（祝日）のため、スクリーニングをスキップします。")
    sys.exit()

if (today.month == 12 and today.day == 31) or (today.month == 1 and today.day in [1, 2, 3]):
    print(f"{today} は休場日（年末年始）のため、スクリーニングをスキップします。")
    sys.exit()

print(f"{today} は営業日です。スクリーニングを開始します。")

# --- 1. 環境変数からWebhook URLを取得 ---
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

if not WEBHOOK_URL:
    raise ValueError("環境変数 WEBHOOK_URL が設定されていません。")

# --- 2. JPXから最新の全銘柄一覧を取得 ---
print("JPXサイトから最新の銘柄データURLを検索中...")
jpx_page_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

response = requests.get(jpx_page_url, headers=headers)
soup = BeautifulSoup(response.content, 'html.parser')
target_link = soup.find('a', href=lambda href: href and (href.endswith('.xls') or href.endswith('.xlsx')))

if target_link:
    latest_excel_url = urljoin(jpx_page_url, target_link['href'])
    print(f"最新データURL取得成功: {latest_excel_url}")
    
    df = pd.read_excel(latest_excel_url)
    df_stocks = df[df['市場・商品区分'].str.contains('プライム', na=False)]
    
    stock_dict = {
        f"{row['コード']}.T": {
            "name": row['銘柄名'],
            "sector": row['33業種区分']
        }
        for _, row in df_stocks.iterrows()
    }
    all_tickers = list(stock_dict.keys())
    print(f"取得対象銘柄数（プライム市場）: {len(all_tickers)} 銘柄")
else:
    raise Exception("JPXのサイトからエクセルファイルが見つかりませんでした。")

# --- 3. 一括（バッチ）通信による爆速スクリーニング ---
matched_stocks = []
matched_data_for_csv = []

# 100銘柄ごとにグループ化（全17回程度のリクエストで完了）
chunk_size = 100
ticker_chunks = [all_tickers[i:i + chunk_size] for i in range(0, len(all_tickers), chunk_size)]

print(f"爆速一括スクリーニングを開始します（リクエスト数: 全{len(ticker_chunks)}回 / 目標時間: 約10秒）...")

for idx, chunk in enumerate(ticker_chunks, start=1):
    symbols_str = ",".join(chunk)
    quote_url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"
    
    try:
        res = requests.get(quote_url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            results = data.get("quoteResponse", {}).get("result", [])
            
            for item in results:
                ticker = item.get("symbol")
                if not ticker or ticker not in stock_dict:
                    continue
                
                stock_info = stock_dict[ticker]
                stock_name = stock_info["name"]
                sector = stock_info["sector"]
                code_num = ticker.replace(".T", "")
                
                per = item.get("trailingPE", 999)
                pbr = item.get("priceToBook", 999)
                
                # 株価と配当金の取得・利回り計算
                price = item.get("regularMarketPrice", item.get("previousClose", 0))
                div_rate = item.get("trailingAnnualDividendRate", 0)
                
                if price > 0 and div_rate > 0:
                    dividend_yield = (div_rate / price) * 100
                else:
                    raw_yield = item.get("trailingAnnualDividendYield", 0)
                    dividend_yield = raw_yield * 100 if raw_yield < 1 else raw_yield
                
                # 条件判定：PER 15以下 ＆ PBR 1.0以下 ＆ 配当利回り4.0%以上
                if per < 15 and pbr < 1.0 and dividend_yield >= 4.0:
                    matched_stocks.append(f"・{stock_name} [{sector}] ({code_num}) (PER: {per:.1f}, PBR: {pbr:.2f}, 配当: {dividend_yield:.2f}%)")
                    matched_data_for_csv.append({
                        "コード": code_num,
                        "銘柄名": stock_name,
                        "業種": sector,
                        "PER": round(per, 2),
                        "PBR": round(pbr, 2),
                        "配当利回り(%)": round(dividend_yield, 2)
                    })
    except Exception as e:
        print(f"バッチ {idx} の取得エラー: {e}")
        
    time.sleep(0.3) # サーバー負荷防止のわずかな待機

# --- 4. CSVファイルの作成とDiscordへ通知 ---
if matched_stocks:
    csv_filename = "screening_result.csv"
    df_result = pd.DataFrame(matched_data_for_csv)
    df_result.to_csv(csv_filename, index=False, encoding="utf-8-sig")

    results_text = "\n".join(matched_stocks)
    if len(results_text) > 4000:
        results_text = results_text[:4000] + "\n\n...（文字数制限のため省略。詳細は添付のCSVを確認してください）"

    payload = {
        "embeds": [
            {
                "title": "📊 プライム市場 高配当スクリーニングレポート",
                "description": results_text,
                "color": int("2ecc71", 16),
                "footer": {"text": f"該当件数: {len(matched_stocks)}件"},
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
        ]
    }
    
    with open(csv_filename, "rb") as f:
        response = requests.post(
            WEBHOOK_URL,
            data={"payload_json": json.dumps(payload)},
            files={"file": (csv_filename, f, "text/csv")}
        )
        
    if response.status_code in [200, 204]:
        print("\nDiscordへ通知とCSVファイルを送信しました。")
    else:
        print(f"\n通知エラー: {response.status_code}")
else:
    payload = {
        "embeds": [
            {
                "title": "📊 プライム市場 高配当スクリーニングレポート",
                "description": "本日はスクリーニング条件に合致する銘柄はありませんでした。",
                "color": int("e74c3c", 16),
                "footer": {"text": "該当件数: 0件"},
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
        ]
    }
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code in [200, 204]:
        print("\n該当なしの通知を送信しました。")
