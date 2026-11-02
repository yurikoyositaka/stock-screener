import os
import time
import requests
import json
import pandas as pd
import yfinance as yf
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

# --- 3. スクリーニング処理 ---
matched_stocks = []
matched_data_for_csv = []
target_tickers = all_tickers

print("スクリーニングを開始します...")
for i, ticker in enumerate(target_tickers):
    stock_info = stock_dict[ticker]
    stock_name = stock_info["name"]
    sector = stock_info["sector"]
    code_num = ticker.replace(".T", "")
    
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        per = info.get("trailingPE", 999)
        pbr = info.get("priceToBook", 999)
        
        dividend_rate = info.get("dividendRate", 0)
        price = info.get("currentPrice", info.get("previousClose", 0))
        
        if price > 0 and dividend_rate > 0:
            dividend_yield = (dividend_rate / price) * 100
        else:
            raw_yield = info.get("dividendYield", 0)
            dividend_yield = raw_yield if raw_yield > 1 else raw_yield * 100
            
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
            
        if (i + 1) % 50 == 0:
            print(f"{i + 1}件完了...")
            
    except Exception as e:
        print(f"{stock_name} の取得エラー: {e}")
        
    time.sleep(1)

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
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
        ]
    }
    response = requests.post(WEBHOOK_URL, json=payload)
    if response.status_code in [200, 204]:
        print("\n該当なしの通知を送信しました。")
