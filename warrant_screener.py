"""權證篩選工具（全市場快照）

對指定標的呼叫元大權證網 GetWarData.ashx（不帶發行商條件＝全市場所有發行商），
套用流動性 / 買賣價差比 / 價內外 / 剩餘天數 / 槓桿門檻，挑出「好進出」的權證。

供 產生法說會查詢網站.py 匯入使用：
    from warrant_screener import screen_codes
    picks = screen_codes(codes, strictness='strict')   # {code: [warrant, ...]}

也可獨立執行做快照：
    python warrant_screener.py 2330 2308 3008      # 指定代號
    python warrant_screener.py                     # 讀 upcoming_codes.txt（每行一代號）
"""

import json
import sys
import time
import requests

BASE = "https://www.warrantwin.com.tw/eyuanta"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Referer": f"{BASE}/Warrant/Search.aspx",
}

# GetWarData 要回傳的欄位（缺 columns 會回 code:0001）
COLUMNS = [
    "FLD_WAR_ID", "FLD_WAR_NM", "FLD_WAR_TYPE", "FLD_ISSUE_AGT_ID",
    "FLD_UND_ID", "FLD_UND_NM", "FLD_WAR_TXN_PRICE", "FLD_WAR_TXN_VOLUME",
    "FLD_WAR_BUY_PRICE", "FLD_WAR_BUY_VOLUME", "FLD_WAR_SELL_PRICE", "FLD_WAR_SELL_VOLUME",
    "FLD_BUY_SELL_RATE", "FLD_YUANTA_IV", "FLD_IV_CLOSE_PRICE",
    "FLD_IV_BUY_PRICE", "FLD_IV_SELL_PRICE",
    "FLD_DUR_END", "FLD_PERIOD", "FLD_N_STRIKE_PRC", "FLD_IN_OUT",
    "FLD_OBJ_TXN_PRICE", "FLD_DELTA", "FLD_THETA", "FLD_LEVERAGE", "FLD_N_UND_CONVER",
]

# 門檻預設（可在此調整）
#   spread_max  : 買賣價差比上限（%），越小流動性越好
#   inout_max   : |價內外|上限（%），越小越貼近價平、反應越靈敏
#   period_min/max : 剩餘天數區間
#   lev_min/max : |實質槓桿| 區間
#   require_volume : 只留「當天有成交量」的權證（成交量>0）
PRESETS = {
    "strict": dict(spread_max=3.0,  inout_max=20.0, period_min=90, period_max=180,
                   lev_min=None, lev_max=None, require_volume=True),  # 不設槓桿硬門檻，改由差槓比排序主導
    "medium": dict(spread_max=5.0,  inout_max=15.0, period_min=30, period_max=180,
                   lev_min=2.5, lev_max=8.0, require_volume=True),
    "loose":  dict(spread_max=None, inout_max=None, period_min=20, period_max=None,
                   lev_min=None, lev_max=None, require_volume=True),
}

# 每檔標的最多保留幾檔（依成交量由大到小），避免 index.html 過度膨脹
MAX_PER_STOCK = 30

# 該站憑證缺 Subject Key Identifier 擴充，新版 OpenSSL 會拒絕（curl 可、Python 不行）；
# 這是公開資料站，關閉憑證驗證以正常取得資料。
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_session = requests.Session()
_session.headers.update(HEADERS)
_session.verify = False


def _num(s):
    """把 '2.91' / '13.25%價外' / '1,504' / '' 轉成 float；無法解析回 None。"""
    if s is None:
        return None
    t = str(s).strip()
    if t == "":
        return None
    buf = []
    for ch in t:
        if ch.isdigit() or ch in ".-":
            buf.append(ch)
        elif buf:                     # 數字後遇到中文（如「價外」）即停
            break
    try:
        return float("".join(buf)) if buf else None
    except ValueError:
        return None


def get_issuer_map() -> dict:
    """發行商 ID → 名稱，如 {'980':'元大證券', ...}"""
    try:
        r = _session.post(f"{BASE}/ws/GetWarCommodity.ashx",
                          data={"category": "issuers", "type": ""}, timeout=15)
        r.raise_for_status()
        return {x["ID"]: x["NAME"] for x in r.json().get("list", [])}
    except Exception as e:
        print(f"  [警告] 發行商清單抓取失敗：{e}")
        return {}


def fetch_warrants(code: str) -> list:
    """抓某標的的全市場權證（所有發行商）；回傳 raw dict list。"""
    payload = {
        "format": "JSON",
        "factor": {
            "columns": COLUMNS,
            "condition": [{"field": "FLD_UND_ID", "values": [str(code)]}],
            "orderby": {"field": "FLD_WAR_TXN_VOLUME", "sort": "DESC", "agtfirst": "980"},
        },
        "pagination": {"row": "3000", "page": "1"},
        "callback": 0,
    }
    r = _session.post(f"{BASE}/ws/GetWarData.ashx",
                     data={"data": json.dumps(payload)}, timeout=20)
    r.raise_for_status()
    j = r.json()
    return j.get("result", []) or []


def _passes(w: dict, th: dict) -> bool:
    if th.get("require_volume"):
        # 只留「當天有成交量」的權證（今天確實成交得掉；盤後跑也穩，成交量整天保留）。
        vol = _num(w.get("FLD_WAR_TXN_VOLUME")) or 0
        if vol <= 0:
            return False
    if th["spread_max"] is not None:
        sp = _num(w.get("FLD_BUY_SELL_RATE"))
        if sp is None or sp > th["spread_max"]:
            return False
    if th["inout_max"] is not None:
        io = _num(w.get("FLD_IN_OUT"))
        if io is None or abs(io) > th["inout_max"]:
            return False
    per = _num(w.get("FLD_PERIOD"))
    if per is None:
        return False
    if th["period_min"] is not None and per < th["period_min"]:
        return False
    if th["period_max"] is not None and per > th["period_max"]:
        return False
    if th["lev_min"] is not None or th["lev_max"] is not None:
        lev = _num(w.get("FLD_LEVERAGE"))
        if lev is None:
            return False
        a = abs(lev)
        if th["lev_min"] is not None and a < th["lev_min"]:
            return False
        if th["lev_max"] is not None and a > th["lev_max"]:
            return False
    return True


def _round(x, n=4):
    return None if x is None else round(x, n)


def _tw_tick(price):
    """台股股票最小升降單位（tick）。"""
    if price is None or price <= 0:
        return None
    if price < 10:   return 0.01
    if price < 50:   return 0.05
    if price < 100:  return 0.1
    if price < 500:  return 0.5
    if price < 1000: return 1.0
    return 5.0


def _derive(w: dict) -> dict:
    """計算衍生指標：差槓比、每日時間價值換算標的、抵掉 theta 需幾檔。"""
    spr = _num(w.get("FLD_BUY_SELL_RATE"))
    lev = _num(w.get("FLD_LEVERAGE"))
    dlt = _num(w.get("FLD_DELTA"))
    tht = _num(w.get("FLD_THETA"))
    obj = _num(w.get("FLD_OBJ_TXN_PRICE"))
    # 差槓比 = 買賣價差比 / |實質槓桿|，越小越划算（低價差、高槓桿）
    sl = (spr / abs(lev)) if (spr is not None and lev not in (None, 0)) else None
    # 每日時間價值損耗換算成標的：|theta| / |delta|（元/天）；delta 已含行使比例，與 theta 同基準。
    #   即「標的要漲多少元才能打平當天 theta」。
    td = (abs(tht) / abs(dlt)) if (tht is not None and dlt not in (None, 0)) else None
    tdp = (td / obj * 100) if (td is not None and obj not in (None, 0)) else None  # %/天
    # 換成標的的「檔數」：td / 一個 tick。tkn<=1 代表標的往有利方向動一檔就抵得過當天 theta。
    tick = _tw_tick(obj)
    tkn = (td / tick) if (td is not None and tick) else None
    # 直接對照：每動 1 tick 權證漲多少(|delta|×tick) vs 當天 theta 損耗(|theta|)，淨剩 = 前-後
    gpt = (abs(dlt) * tick) if (dlt is not None and tick) else None   # 每 1 tick 權證漲多少(元)
    thabs = abs(tht) if tht is not None else None                     # 當天 theta 損耗(元)
    net = (gpt - thabs) if (gpt is not None and thabs is not None) else None  # 淨剩(>0=包得過)
    return {"sl": _round(sl, 3), "td": _round(td, 2), "tdp": _round(tdp, 3),
            "tk": tick, "tkn": _round(tkn, 2),
            "gpt": _round(gpt, 4), "thabs": _round(thabs, 4), "net": _round(net, 4)}


def _slim(w: dict, issuer_map: dict) -> dict:
    """挑要顯示的欄位，短鍵縮小體積。"""
    d = _derive(w)
    return {
        "id":   w.get("FLD_WAR_ID"),
        "nm":   w.get("FLD_WAR_NM"),
        "u":    w.get("FLD_UND_NM"),                           # 標的股名
        "typ":  w.get("FLD_WAR_TYPE"),                         # 認購 / 認售
        "iss":  issuer_map.get(w.get("FLD_ISSUE_AGT_ID"), w.get("FLD_ISSUE_AGT_ID")),
        "px":   w.get("FLD_WAR_TXN_PRICE"),                    # 成交價
        "vol":  w.get("FLD_WAR_TXN_VOLUME"),                   # 成交量(張)
        "spr":  w.get("FLD_BUY_SELL_RATE"),                    # 買賣價差比(%)
        "io":   w.get("FLD_IN_OUT"),                           # 價內外
        "lev":  w.get("FLD_LEVERAGE"),                         # 實質槓桿
        "sl":   d["sl"],                                       # 差槓比(越小越划算)
        "td":   d["td"],                                       # 每日時間價值換算標的(元/天)
        "tdp":  d["tdp"],                                      # 同上(%/天)
        "tk":   d["tk"],                                       # 標的 tick 大小
        "tkn":  d["tkn"],                                      # 抵掉當天 theta 需幾檔(≤1=動一檔即可)
        "gpt":  d["gpt"],                                      # 每動1tick權證漲多少(元)
        "thabs":d["thabs"],                                    # 當天theta損耗(元)
        "net":  d["net"],                                      # 淨剩=每tick漲-theta(>0=包過)
        "per":  w.get("FLD_PERIOD"),                           # 剩餘天數
        "end":  w.get("FLD_DUR_END"),                          # 到期日
        "iv":   w.get("FLD_YUANTA_IV"),                        # 元大隱波
        "ivb":  w.get("FLD_IV_BUY_PRICE"),                     # 買進隱波(發行商掛買)
        "ivs":  w.get("FLD_IV_SELL_PRICE"),                    # 賣出隱波(發行商掛賣)
        # "ivh" 由 build_payload 從隱波歷史補上（近期 [日期,元大IV] 序列）
    }


def _iv_tuple(w):
    """本次觀測的隱波組：元大IV / 收盤IV / 買IV / 賣IV（無值為 None）。"""
    return [_num(w.get("FLD_YUANTA_IV")), _num(w.get("FLD_IV_CLOSE_PRICE")),
            _num(w.get("FLD_IV_BUY_PRICE")), _num(w.get("FLD_IV_SELL_PRICE"))]


def record_iv_history(raw_list, code, issuer_map, hist, ts):
    """把本次抓到的每檔權證隱波記入 hist（僅在隱波組較上次有變動時追加，抓「偷調」時點）。"""
    for w in raw_list:
        wid = w.get("FLD_WAR_ID")
        if not wid:
            continue
        ivs = _iv_tuple(w)
        if all(v is None for v in ivs):
            continue
        rec = hist.get(wid)
        if rec is None:
            rec = hist[wid] = {"nm": w.get("FLD_WAR_NM"), "u": w.get("FLD_UND_NM"),
                               "und": str(code), "typ": w.get("FLD_WAR_TYPE"),
                               "iss": issuer_map.get(w.get("FLD_ISSUE_AGT_ID"), w.get("FLD_ISSUE_AGT_ID")),
                               "obs": []}
        if not rec["obs"] or rec["obs"][-1][1:] != ivs:   # 僅在隱波有變動時追加
            rec["obs"].append([ts] + ivs)


def screen_codes(codes, strictness: str = "strict", issuer_map: dict = None,
                 pause: float = 0.3, verbose: bool = True,
                 iv_hist: dict = None, ts: str = None) -> dict:
    """對多檔標的篩選，回傳 {code: [slim warrant, ...]}（無合格者則不列入）。
    若給 iv_hist（dict）與 ts，會把全市場每檔權證的隱波記入歷史。"""
    th = PRESETS[strictness]
    if issuer_map is None:
        issuer_map = get_issuer_map()
    out = {}
    codes = [str(c).strip() for c in codes if str(c).strip()]
    for i, code in enumerate(dict.fromkeys(codes), 1):   # 去重、保序
        try:
            raw = fetch_warrants(code)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(codes)}] {code} 抓取失敗：{e}")
            continue
        if iv_hist is not None and ts:
            record_iv_history(raw, code, issuer_map, iv_hist, ts)
        picked = [w for w in raw if _passes(w, th)]
        # 依差槓比由小到大（越划算越前面）；無差槓比者排最後
        def _slkey(w):
            d = _derive(w)["sl"]
            return (d is None, d if d is not None else 0.0)
        picked.sort(key=_slkey)
        picked = picked[:MAX_PER_STOCK]
        if picked:
            out[code] = [_slim(w, issuer_map) for w in picked]
        if verbose:
            print(f"  [{i}/{len(codes)}] {code}：全市場 {len(raw)} 檔 → 合格 {len(picked)} 檔")
        time.sleep(pause)
    return out


import os

_DIR = os.path.dirname(os.path.abspath(__file__))
CODES_FILE = os.path.join(_DIR, "upcoming_codes.txt")
OUT_FILE = os.path.join(_DIR, "warrant_picks.json")   # 與 index.html 同目錄，供其 fetch
HIST_FILE = os.path.join(_DIR, "warrant_iv_history.json")  # 累積隱波歷史（永不覆寫、僅追加變動點）
IVH_INLINE = 12   # 內嵌到每檔 pick 的近期隱波點數（供權證頁顯示趨勢）


def _load_hist() -> dict:
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_hist(hist: dict):
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def _attach_quote_history(picks, verbose=True):
    """把近 5 交易日成交量（張）掛到每檔『認購』pick（TWSE 0999 僅認購）。"""
    try:
        import warrant_quote_history as wq
    except Exception as e:
        if verbose:
            print(f"  [警告] 量能歷史模組載入失敗，略過：{e}")
        return
    call_ids = [w["id"] for ws in picks.values() for w in ws if "購" in str(w.get("typ", ""))]
    if not call_ids:
        return
    try:
        wq.download_recent(5, verbose=verbose)
        roll = wq.rolling_by_code(call_ids, n=5)
    except Exception as e:
        if verbose:
            print(f"  [警告] 量能歷史計算失敗，略過：{e}")
        return
    for ws in picks.values():
        for w in ws:
            r = roll.get(w["id"])
            if r and r["present"]:   # 只掛「TWSE 上市」查得到的（上櫃 7 開頭查無→不掛，避免誤顯示全 0）
                # 短鍵：d=日期(MMDD) l=每日張數 avg=平均張 z=沒交易日 td=有交易天數
                w["q"] = {"d": [x[4:] for x in r["dates"]], "l": r["lots"],
                          "avg": r["avg_lots"], "z": [x[4:] for x in r["zero_dates"]],
                          "td": r["traded"]}


def build_payload(codes, strictness: str = "strict") -> dict:
    """組成要寫檔/內嵌的完整結構（含中繼資料）；同時累積隱波歷史、掛近5日量能。"""
    ts = time.strftime("%Y-%m-%d %H:%M")
    hist = _load_hist()
    picks = screen_codes(codes, strictness, iv_hist=hist, ts=ts)
    _save_hist(hist)
    # 把近期買/賣隱波序列內嵌進每檔 pick（[日期時間, 買IV, 賣IV]），供權證頁看發行商調整
    for code, ws in picks.items():
        for w in ws:
            rec = hist.get(w["id"])
            if rec and rec["obs"]:
                w["ivh"] = [[o[0], o[3], o[4]] for o in rec["obs"][-IVH_INLINE:]]
    # 近 5 交易日成交量（張）→ 掛到認購 pick
    print("下載/計算近5交易日成交量（TWSE 認購）…")
    _attach_quote_history(picks)
    return {
        "as_of": ts,
        "strictness": strictness,
        "thresholds": PRESETS[strictness],
        "picks": picks,
    }


def _read_codes_from_file(path=CODES_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


if __name__ == "__main__":
    args = sys.argv[1:]
    codes = args if args else _read_codes_from_file()
    if not codes:
        print("用法：python warrant_screener.py 2330 2308 ...  或先跑 產生法說會查詢網站.py 產出 upcoming_codes.txt")
        sys.exit(1)
    data = build_payload(codes, strictness="strict")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    n = sum(len(v) for v in data["picks"].values())
    print(f"\n完成：{len(data['picks'])} 檔標的、共 {n} 檔權證 → {OUT_FILE}（{data['as_of']}）")
