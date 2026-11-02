"""
プライム市場 高配当・割安スクリーニング → Discord通知

必要ライブラリ:
    pip install -U yfinance pandas requests beautifulsoup4 jpholiday xlrd openpyxl

環境変数:
    WEBHOOK_URL : DiscordのWebhook URL（必須）
    TEST_WEBHOOK_URL : テスト用Webhook（任意。テスト時はこちらを優先）
    FORCE_RUN=1 : 土日祝でも強制的に実行（任意）

テスト実行の例:
    python high_dividend_screening.py --mock --dry-run   # ネット不要・送信なしでレイアウト確認
    python high_dividend_screening.py --mock             # ダミーデータをテスト用Discordへ送信
    python high_dividend_screening.py --test --dry-run   # 全銘柄・条件緩和・送信なし
    python high_dividend_screening.py --test             # 全銘柄・条件緩和・Discordへ送信
    （環境変数 TEST_MODE=1 / DRY_RUN=1 / MOCK=1 でも指定可）
"""
import argparse
import concurrent.futures
import datetime
import io
import json
import os
import random
import sys
import time
import traceback
from urllib.parse import urljoin

import jpholiday
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

# ===== 設定 =====
MAX_PER = 15.0        # PER がこれ未満
MAX_PBR = 1.0         # PBR がこれ未満
MIN_YIELD = 4.0       # 配当利回り(%)がこれ以上

MAX_WORKERS = 3       # 並列数（多いとYahooに制限されます）
SLEEP_SEC = 0.3       # 1銘柄ごとの待ち時間
RETRIES = 3           # リトライ回数
TOP_N = 20            # Discord本文に表示する最大件数（全件はCSVに添付）
FAIL_WARN_RATIO = 0.2 # 取得失敗がこの割合を超えたら警告を表示

JPX_PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
JST = datetime.timezone(datetime.timedelta(hours=9), "JST")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# 土日祝・年末年始でも実行するか（True=常に実行 / False=休場日はスキップ）
# ※本番運用に戻すときは False にしてください
RUN_ON_HOLIDAYS = True

COLOR_OK = 0x2ECC71    # 緑
COLOR_NONE = 0x95A5A6  # グレー
COLOR_WARN = 0xF39C12  # オレンジ
COLOR_ERR = 0xE74C3C   # 赤


# ===== 営業日判定 =====
def is_business_day(d: datetime.date, force: bool = False) -> bool:
    if force or os.environ.get("FORCE_RUN") == "1":
        print("営業日チェックをスキップします（テスト/強制実行）。")
        return True
    if d.weekday() >= 5:
        print(f"{d} は休場日（土日）のためスキップします。")
        return False
    if jpholiday.is_holiday(d):
        print(f"{d} は休場日（祝日: {jpholiday.is_holiday_name(d)}）のためスキップします。")
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day in (1, 2, 3)):
        print(f"{d} は休場日（年末年始）のためスキップします。")
        return False
    return True


# ===== JPXから銘柄一覧取得 =====
def fetch_prime_stocks() -> dict:
    print("JPXサイトから最新の銘柄一覧を検索中...")
    res = requests.get(JPX_PAGE_URL, headers=HEADERS, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.content, "html.parser")

    links = [
        a["href"]
        for a in soup.find_all("a", href=True)
        if a["href"].lower().endswith((".xls", ".xlsx"))
    ]
    if not links:
        raise RuntimeError("JPXのサイトからExcelファイルが見つかりませんでした。")

    # 東証上場銘柄一覧（data_j）を優先
    target = next((h for h in links if "data_j" in h), links[0])
    excel_url = urljoin(JPX_PAGE_URL, target)

    xl = requests.get(excel_url, headers=HEADERS, timeout=60)
    xl.raise_for_status()
    df = pd.read_excel(io.BytesIO(xl.content))
    df = df[df["市場・商品区分"].str.contains("プライム", na=False)]

    stocks = {}
    for _, row in df.iterrows():
        code = str(row["コード"]).strip()
        stocks[f"{code}.T"] = {"name": row["銘柄名"], "sector": row["33業種区分"]}
    print(f"対象銘柄数（プライム市場）: {len(stocks)} 銘柄")
    return stocks


# ===== 1銘柄の処理 =====
def process_ticker(ticker: str, meta: dict):
    """戻り値: (status, data, error)  status = 'match' | 'nomatch' | 'failed'"""
    code = ticker.replace(".T", "")
    last_error = ""

    for attempt in range(RETRIES):
        try:
            info = yf.Ticker(ticker).info
            if not info or len(info) < 5:
                raise ValueError("データ未取得（infoが空）")

            price = (
                info.get("regularMarketPrice")
                or info.get("currentPrice")
                or info.get("previousClose")
                or 0
            )

            per = info.get("trailingPE")
            if not per or per <= 0:
                per = info.get("forwardPE")
            pbr = info.get("priceToBook")
            div_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate") or 0

            time.sleep(SLEEP_SEC)

            # 必要な値が揃わない銘柄は対象外
            if not per or per <= 0 or not pbr or pbr <= 0 or price <= 0 or div_rate <= 0:
                return "nomatch", None, ""

            dividend_yield = div_rate / price * 100

            if per < MAX_PER and pbr < MAX_PBR and dividend_yield >= MIN_YIELD:
                return "match", {
                    "code": code,
                    "name": meta["name"],
                    "sector": meta["sector"],
                    "price": price,
                    "per": per,
                    "pbr": pbr,
                    "yield": dividend_yield,
                }, ""
            return "nomatch", None, ""

        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"[:120]
            time.sleep(2 ** attempt)  # 1秒 → 2秒 → 4秒

    return "failed", None, last_error


# ===== Discord送信 =====
DRY_RUN = False


def send_discord(payload: dict, csv_path: str | None = None) -> bool:
    if DRY_RUN:
        print("--- [DRY RUN] Discordには送信しません。送信内容は以下 ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if csv_path:
            print(f"（CSV添付予定: {csv_path}）")
        return True
    try:
        if csv_path:
            with open(csv_path, "rb") as f:
                res = requests.post(
                    WEBHOOK_URL,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (os.path.basename(csv_path), f, "text/csv")},
                    timeout=30,
                )
        else:
            res = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"Discord送信中に例外: {e}")
        return False

    if res.status_code in (200, 204):
        print("Discordへ送信しました。")
        return True
    print(f"Discord送信エラー: {res.status_code} {res.text[:300]}")
    return False


def build_embed(matches, total, failed, errors, elapsed, today, test_label="") -> dict:
    fail_ratio = failed / total if total else 0
    warn = fail_ratio > FAIL_WARN_RATIO

    if matches:
        lines = []
        for i, m in enumerate(matches[:TOP_N], 1):
            url = f"https://finance.yahoo.co.jp/quote/{m['code']}.T"
            lines.append(
                f"**{i}. [{m['name']}]({url})** `{m['code']}`\n"
                f"　{m['sector']}\n"
                f"　💰 **{m['yield']:.2f}%**　📈 PER {m['per']:.1f}　📘 PBR {m['pbr']:.2f}"
            )
        description = "\n\n".join(lines)
        if len(matches) > TOP_N:
            description += f"\n\n…他 {len(matches) - TOP_N} 件は添付CSVをご確認ください"
        color = COLOR_WARN if warn else COLOR_OK
    else:
        description = "本日は条件に合致する銘柄はありませんでした。"
        color = COLOR_WARN if warn else COLOR_NONE

    fields = [
        {
            "name": "🔎 スクリーニング条件",
            "value": f"PER < {MAX_PER:g} ／ PBR < {MAX_PBR:g} ／ 配当利回り ≥ {MIN_YIELD:g}%",
            "inline": False,
        },
        {"name": "✅ 該当", "value": f"{len(matches)} 件", "inline": True},
        {"name": "📋 対象", "value": f"{total} 銘柄", "inline": True},
        {"name": "⚠️ 取得失敗", "value": f"{failed} 銘柄", "inline": True},
    ]
    if warn:
        sample = "\n".join(errors[:3]) or "(詳細なし)"
        fields.append(
            {
                "name": "⚠️ データ取得に失敗した銘柄が多く、結果が不完全な可能性があります",
                "value": f"```\n{sample}\n```",
                "inline": False,
            }
        )

    return {
        "title": f"{test_label}📊 プライム市場 高配当・割安スクリーニング（{today:%Y/%m/%d}）",
        "description": description[:4000],
        "color": color,
        "fields": fields,
        "footer": {"text": f"実行時間 {elapsed:.0f}秒 ｜ 配当利回り順に表示"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ===== メイン =====
def parse_args():
    env = lambda k: os.environ.get(k) == "1"  # noqa: E731
    p = argparse.ArgumentParser(description="プライム市場 高配当スクリーニング")
    p.add_argument("--test", action="store_true", default=env("TEST_MODE"),
                   help="テストモード（営業日チェック無視・条件緩和・件名に【テスト】）")
    p.add_argument("--dry-run", action="store_true", default=env("DRY_RUN"),
                   help="Discordに送信せず、内容を画面に表示する")
    p.add_argument("--mock", action="store_true", default=env("MOCK"),
                   help="ダミーデータを使う（JPX・Yahooにアクセスしない）")
    p.add_argument("--limit", type=int, default=None,
                   help="処理する銘柄数の上限（指定しなければ全銘柄）")
    return p.parse_args()


def mock_stocks(n: int = 100) -> dict:
    sectors = ["銀行業", "卸売業", "電気機器", "輸送用機器", "化学", "保険業", "鉄鋼"]
    rnd = random.Random(1)
    return {
        f"{1300 + i}.T": {"name": f"テスト銘柄{i + 1}", "sector": rnd.choice(sectors)}
        for i in range(n)
    }


def mock_process(ticker: str, meta: dict):
    """ネットワークを使わないダミー判定（レイアウト確認用）"""
    code = ticker.replace(".T", "")
    rnd = random.Random(ticker)
    if rnd.random() < 0.03:
        return "failed", None, "MockError: ダミーの取得失敗"
    price = rnd.uniform(300, 5000)
    per, pbr, y = rnd.uniform(4, 25), rnd.uniform(0.3, 1.8), rnd.uniform(1, 7)
    if per < MAX_PER and pbr < MAX_PBR and y >= MIN_YIELD:
        return "match", {"code": code, "name": meta["name"], "sector": meta["sector"],
                         "price": price, "per": per, "pbr": pbr, "yield": y}, ""
    return "nomatch", None, ""


def main(args):
    global MAX_PER, MAX_PBR, MIN_YIELD, DRY_RUN, WEBHOOK_URL
    DRY_RUN = args.dry_run
    test = args.test or args.mock
    test_label = "【テスト】" if test else ""

    today = datetime.datetime.now(JST).date()
    if not is_business_day(today, force=test or RUN_ON_HOLIDAYS):
        return

    if test and os.environ.get("TEST_WEBHOOK_URL"):
        WEBHOOK_URL = os.environ["TEST_WEBHOOK_URL"]
        print("TEST_WEBHOOK_URL を使用します。")
    if not WEBHOOK_URL and not DRY_RUN:
        raise ValueError("環境変数 WEBHOOK_URL が設定されていません（送信しない場合は --dry-run）。")

    # テスト時は条件を緩めて、該当銘柄が出やすくする（実データのみ）
    if args.test and not args.mock:
        MAX_PER, MAX_PBR, MIN_YIELD = 30.0, 3.0, 2.0
        print(f"[テスト] 条件を緩和: PER<{MAX_PER:g} PBR<{MAX_PBR:g} 利回り≥{MIN_YIELD:g}%")

    print(f"{today} スクリーニングを開始します。{'（テストモード）' if test else ''}")

    stocks = mock_stocks() if args.mock else fetch_prime_stocks()
    worker = mock_process if args.mock else process_ticker
    tickers = list(stocks.keys())
    limit = args.limit  # 指定がなければ全銘柄（プライム市場すべて）
    if limit:
        tickers = tickers[:limit]
        print(f"[テスト] 先頭 {len(tickers)} 銘柄のみ処理します。")
    total = len(tickers)

    matches, errors, failed = [], [], 0
    start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker, t, stocks[t]): t for t in tickers}
        for done, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            status, data, err = fut.result()
            if status == "match":
                matches.append(data)
            elif status == "failed":
                failed += 1
                if err and err not in errors:
                    errors.append(err)
            if done % 200 == 0:
                print(f"  進捗 {done}/{total}（該当 {len(matches)} / 失敗 {failed}）")

    elapsed = time.time() - start
    matches.sort(key=lambda m: m["yield"], reverse=True)
    print(f"完了: 該当 {len(matches)} 件 / 失敗 {failed} 件 / {elapsed:.0f}秒")

    embed = build_embed(matches, total, failed, errors, elapsed, today, test_label)

    if matches:
        csv_path = "screening_result_test.csv" if test else "screening_result.csv"
        pd.DataFrame(
            [
                {
                    "コード": m["code"],
                    "銘柄名": m["name"],
                    "業種": m["sector"],
                    "株価": round(m["price"], 1),
                    "配当利回り(%)": round(m["yield"], 2),
                    "PER": round(m["per"], 2),
                    "PBR": round(m["pbr"], 2),
                }
                for m in matches
            ]
        ).to_csv(csv_path, index=False, encoding="utf-8-sig")
        send_discord({"embeds": [embed]}, csv_path)
    else:
        send_discord({"embeds": [embed]})


if __name__ == "__main__":
    try:
        main(parse_args())
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb)
        # 失敗時もDiscordに知らせる（通知が来ない原因を見逃さないため）
        if WEBHOOK_URL:
            send_discord(
                {
                    "embeds": [
                        {
                            "title": "❌ スクリーニング実行エラー",
                            "description": f"```\n{tb[-1500:]}\n```",
                            "color": COLOR_ERR,
                            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        }
                    ]
                }
            )
        sys.exit(1)
