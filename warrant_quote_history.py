"""權證每日收盤行情下載 + 近 N 交易日成交量 rolling（只抓認購，資料來源：TWSE）

- 資料來源：https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX（type=0999＝認購權證）
- 每個交易日存一個 CSV 到「權證行情/YYYYMMDD.csv」
- rolling()：讀近 N 天 CSV，算每檔近 N 交易日平均成交股數（換算張）、標記哪天完全沒交易、列出每日量

獨立執行：python warrant_quote_history.py       # 下載近5交易日並印摘要
供 warrant_screener 匯入：download_recent(); rolling_by_code(codes)
"""

import os
import csv
import time
import requests
import urllib3
import exchange_calendars as xcals

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_DIR = os.path.dirname(os.path.abspath(__file__))
QUOTE_DIR = os.path.join(_DIR, "權證行情")
MI_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"           # 上市（type=0999＝認購）
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/warrant/wntQuts"            # 上櫃（POST type=Daily）
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
LOTS = 1000  # 1 張 = 1000 股

# 存進 CSV 的欄位（中文表頭，utf-8-sig 供 Excel）
CSV_FIELDS = ["市場", "代號", "名稱", "收盤價", "成交股數", "成交張數", "成交筆數", "標的代號", "標的名稱"]

_session = requests.Session()
_session.headers.update(HEADERS)


def _to_int(s):
    try:
        return int(str(s).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def last_n_trading_days(n=5, end=None):
    """回傳截至 end（含）的最近 n 個台股交易日，格式 'YYYYMMDD'，由舊到新。"""
    cal = xcals.get_calendar("XTAI")
    import pandas as pd
    end = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    sess = cal.sessions_in_range(end - pd.Timedelta(days=n * 3 + 15), end)
    days = [d.strftime("%Y%m%d") for d in sess][-n:]
    return days


def _fetch_twse_day(yyyymmdd):
    """上市認購權證（TWSE type=0999）。非交易日回 None。"""
    r = _session.get(MI_URL, params={"date": yyyymmdd, "type": "0999", "response": "json"},
                     timeout=30, verify=False)
    r.raise_for_status()
    d = r.json()
    if d.get("stat") != "OK":
        return None
    table = None
    for t in d.get("tables", []):
        fields = t.get("fields") or []
        if t.get("data") and ("標的代號" in fields or len(fields) >= 18):
            table = t
            break
    if not table:
        return None
    out = []
    for row in table["data"]:
        # ['',代號,名稱,成交股數,成交筆數,成交金額,開,高,低,收,...,標的代號(17),標的名稱(18),標的收(19)]
        code = str(row[1]).strip()
        if not code:
            continue
        shares = _to_int(row[3])
        out.append({"市場": "上市", "代號": code, "名稱": str(row[2]).strip(),
                    "收盤價": str(row[9]).strip(), "成交股數": shares,
                    "成交張數": round(shares / LOTS, 2), "成交筆數": _to_int(row[4]),
                    "標的代號": str(row[17]).strip() if len(row) > 17 else "",
                    "標的名稱": str(row[18]).strip() if len(row) > 18 else ""})
    return out


def _fetch_tpex_day(yyyymmdd):
    """上櫃認購權證（TPEx wntQuts，只留名稱含「購」者）。查無回 []。"""
    date_slash = f"{yyyymmdd[:4]}/{yyyymmdd[4:6]}/{yyyymmdd[6:8]}"
    r = _session.post(TPEX_URL, data={"type": "Daily", "date": date_slash, "id": "", "response": "json"},
                      timeout=30, verify=False)
    r.raise_for_status()
    d = r.json()
    if str(d.get("stat", "")).lower() != "ok" or d.get("date") != yyyymmdd:
        return []   # 非交易日或日期不符（避免拿到別天資料）
    table = None
    for t in d.get("tables", []):
        if t.get("data"):
            table = t
            break
    if not table:
        return []
    out = []
    for row in table["data"]:
        # [代號,名稱,開,高,低,收(5),漲跌,成交量股(7),成交筆數(8),成交金額,標的代號(10),標的名稱(11),...]
        code = str(row[0]).strip()
        name = str(row[1]).strip()
        if not code or "購" not in name:   # 只認購
            continue
        shares = _to_int(row[7])
        close = str(row[5]).strip()
        out.append({"市場": "上櫃", "代號": code, "名稱": name,
                    "收盤價": "" if close in ("---", "--") else close, "成交股數": shares,
                    "成交張數": round(shares / LOTS, 2), "成交筆數": _to_int(row[8]),
                    "標的代號": str(row[10]).strip() if len(row) > 10 else "",
                    "標的名稱": str(row[11]).strip() if len(row) > 11 else ""})
    return out


def _fetch_day(yyyymmdd):
    """某日認購權證收盤行情（上市+上櫃合併）。非交易日回 None。"""
    twse = _fetch_twse_day(yyyymmdd)
    if twse is None:
        return None
    try:
        tpex = _fetch_tpex_day(yyyymmdd)
    except Exception:
        tpex = []
    return twse + tpex


def _csv_path(yyyymmdd):
    return os.path.join(QUOTE_DIR, f"{yyyymmdd}.csv")


def download_recent(n=5, end=None, pause=1.0, verbose=True):
    """下載近 n 交易日認購權證收盤行情到 權證行情/YYYYMMDD.csv。
    已存在的過去交易日直接沿用（收盤定案不會變），只有最後一天一定重抓。回傳日期清單。"""
    os.makedirs(QUOTE_DIR, exist_ok=True)
    days = last_n_trading_days(n, end)
    for i, day in enumerate(days):
        path = _csv_path(day)
        is_last = (i == len(days) - 1)
        if os.path.exists(path) and not is_last:
            if verbose:
                print(f"  {day}：已存在，沿用")
            continue
        try:
            rows = _fetch_day(day)
        except Exception as e:
            if verbose:
                print(f"  {day}：抓取失敗 {e}")
            continue
        if rows is None:
            if verbose:
                print(f"  {day}：非交易日/無資料")
            continue
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        if verbose:
            print(f"  {day}：{len(rows)} 檔 → {os.path.basename(path)}")
        time.sleep(pause)
    return days


def _read_csv(yyyymmdd):
    path = _csv_path(yyyymmdd)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {row["代號"]: row for row in csv.DictReader(f)}


def rolling_by_code(codes=None, n=5, end=None):
    """讀近 n 交易日 CSV，回傳 {代號: {...}}。
    codes 給定時只算那些代號；否則算全部。
    每檔欄位：
      dates      近 n 交易日（YYYYMMDD，由舊到新）
      lots       每日成交張數（沒交易/未掛牌＝0）
      avg_lots   近 n 日平均成交張數
      zero_dates 完全沒交易的日期（成交股數=0 或當天沒這檔）
      traded     有交易的天數
    """
    days = last_n_trading_days(n, end)
    daily = {day: _read_csv(day) for day in days}
    if codes is None:
        codes = sorted({c for m in daily.values() for c in m})
    codes = [str(c).strip() for c in codes]
    out = {}
    for code in codes:
        lots, zero_dates, present = [], [], False
        for day in days:
            row = daily.get(day, {}).get(code)
            if row is not None:
                present = True   # 該代號有在 TWSE 上市資料出現（區分「真沒交易」vs「非上市/上櫃無資料」）
            lot = float(row["成交張數"]) if row else 0.0
            lots.append(lot)
            if lot == 0:
                zero_dates.append(day)
        out[code] = {
            "dates": days,
            "lots": [round(x, 2) for x in lots],
            "avg_lots": round(sum(lots) / len(lots), 2) if lots else 0.0,
            "zero_dates": zero_dates,
            "traded": sum(1 for x in lots if x > 0),
            "present": present,   # False＝TWSE 上市資料查無此權證（多為上櫃 7 開頭），不應顯示量能
        }
    return out


if __name__ == "__main__":
    print(f"下載近 5 交易日認購權證收盤行情 → {QUOTE_DIR}")
    days = download_recent(5)
    print(f"交易日：{days}")
    roll = rolling_by_code(n=5)
    # 抽樣印幾檔
    print("\n抽樣（近5日平均張數最高的前5檔）：")
    top = sorted(roll.items(), key=lambda kv: kv[1]["avg_lots"], reverse=True)[:5]
    for code, r in top:
        print(f"  {code}  每日張數 {r['lots']}  平均 {r['avg_lots']} 張  沒交易日 {r['zero_dates']}")
