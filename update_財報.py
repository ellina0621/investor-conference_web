# -*- coding: utf-8 -*-
"""每日更新主程式：法說會每天更新，財報只在申報季更新。

排程建議：每天跑一次。
  • 法說會 + 00981A + 重生 index.html：**每天都做**（產生法說會查詢網站.py 內含法說會即時爬取）。
  • 財報三支爬蟲(決議/通過/客製) + 偵測新公告：**只在四個申報窗口內做**
    （2/15–3/31、4/15–5/15、7/15–8/14、10/15–11/14），淡季自動略過。

用法：
  python update_財報.py            # 依日期自動判斷（法說會必跑；財報視窗口）
  python update_財報.py --force    # 強制連財報爬蟲一起跑(測試/手動補跑)
  python update_財報.py --no-gen   # 只跑財報爬蟲+偵測，不重生網站
"""
import os, sys, json, subprocess
from datetime import date, datetime
import pandas as pd

BASE = r"E:\法說會+主動型"
PY = sys.executable

# 四個申報窗口 (起月, 起日, 迄月, 迄日)；涵蓋 ~98% 財報公告
WINDOWS = [(2, 15, 3, 31), (4, 15, 5, 15), (7, 15, 8, 14), (10, 15, 11, 14)]

ROC_NOW = date.today().year - 1911

# 依序執行的爬蟲：(腳本檔, 參數列)
SCRAPERS = [
    ("mops_董事會決議.py", ["董事會決議", "財務", os.path.join(BASE, "董事會決議.csv"), "110", str(ROC_NOW)]),
    ("mops_董事會決議.py", ["董事會通過", "財務", os.path.join(BASE, "董事會通過.csv"), "110", str(ROC_NOW)]),
    ("mops_客製財報.py",   []),
]
GEN = "產生法說會查詢網站.py"

BOARD_CSVS = ["董事會決議.csv", "董事會通過.csv", "公告財報.csv", "董事會客製財報.csv"]
STORE_JSON = os.path.join(BASE, "財報公告事件庫.json")   # 累積看過的所有財報公告（永不覆寫、只增量）
NEW_JSON   = os.path.join(BASE, "財報新公告.json")         # 本次偵測到的新公告（每次覆寫）


def in_window(d: date) -> bool:
    return any(date(d.year, ms, ds) <= d <= date(d.year, me, de)
               for ms, ds, me, de in WINDOWS)


def run(script: str, args: list) -> bool:
    print(f"\n▶ 執行 {script} {' '.join(args)}", flush=True)
    r = subprocess.run([PY, os.path.join(BASE, script)] + args)
    ok = (r.returncode == 0)
    print(f"  ↳ {'完成' if ok else '失敗(碼 %d)' % r.returncode}", flush=True)
    return ok


def load_board_keys() -> dict:
    """讀四個財報來源 CSV → {key: row}；key = 代號|日期|序號|主旨。"""
    rows = {}
    for f in BOARD_CSVS:
        p = os.path.join(BASE, f)
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
        except Exception as e:
            print(f"  [警告] 讀取 {f} 失敗：{e}")
            continue
        for _, r in df.iterrows():
            code = str(r.get("代號", "")).strip()
            if not code or code in ("nan", "代號"):
                continue
            d    = str(r.get("日期", "")).strip()
            seq  = str(r.get("序號", "")).strip()
            subj = str(r.get("主旨", "")).strip()
            key  = f"{code}|{d}|{seq}|{subj}"
            rows[key] = {"代號": code, "簡稱": str(r.get("簡稱", "")).strip(),
                         "市場別": str(r.get("市場別", "")).strip(),
                         "日期": d, "序號": seq, "主旨": subj, "來源": f}
    return rows


def detect_new() -> list:
    """比對事件庫，找出本次新出現的公告，更新事件庫並寫出 財報新公告.json。"""
    current = load_board_keys()
    first_run = not os.path.exists(STORE_JSON)
    store = {}
    if not first_run:
        try:
            store = json.load(open(STORE_JSON, encoding="utf-8"))
        except Exception:
            store = {}

    if first_run:
        new_rows = []   # 首次只建立基準，不把全部當新公告
        print(f"  [首次建庫] 事件庫不存在，將 {len(current):,} 筆設為基準（新公告=0）")
    else:
        new_rows = [current[k] for k in current if k not in store]

    store.update(current)
    json.dump(store, open(STORE_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    out = {"偵測時間": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "事件庫總筆數": len(store),
           "新公告筆數": len(new_rows),
           "新公告": sorted(new_rows, key=lambda x: (x["日期"], x["代號"]))}
    json.dump(out, open(NEW_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return new_rows


def main():
    force  = "--force" in sys.argv
    no_gen = "--no-gen" in sys.argv
    today  = date.today()
    print(f"=== {datetime.now():%Y-%m-%d %H:%M} 每日更新（民國{ROC_NOW}年）===")

    # ── 財報爬蟲：只在申報窗口內跑（淡季略過，省得每天空爬）──
    if force or in_window(today):
        print("【申報窗口】更新財報資料…")
        for script, args in SCRAPERS:
            try:
                run(script, args)
            except Exception as e:
                print(f"  ✗ {script} 例外：{e}")
        new = detect_new()
        print(f"\n★ 本次新增財報公告 {len(new)} 筆 → 財報新公告.json")
        for r in new[:40]:
            src = r["來源"].replace(".csv", "")
            print(f"    [{src}] {r['代號']} {r['簡稱']} {r['日期']}  {r['主旨'][:34]}")
        if len(new) > 40:
            print(f"    …其餘 {len(new) - 40} 筆見 財報新公告.json")
    else:
        wins = "、".join(f"{ms}/{ds}~{me}/{de}" for ms, ds, me, de in WINDOWS)
        print(f"【淡季】今天 {today} 不在申報窗口（{wins}），略過財報爬蟲（加 --force 可強制）。")

    # ── 法說會 + 00981A + 網站：每天都跑（產生法說會查詢網站.py 內含法說會即時爬取）──
    if no_gen:
        print("\n(--no-gen) 略過重生網站。")
    else:
        print("\n重算各股法說會前後波動率（含 yfinance 補齊缺口）…")
        if not run("compute_vol_stats.py", []):
            print("  [警告] 波動率重算失敗，沿用既有 vol_stats.json。")
        print("\n更新法說會/00981A 並重生 index.html…")
        run(GEN, [])
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
