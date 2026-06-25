#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""計算每檔股票法說會前後（t-2, t-1, t0, t+1）的平均 Parkinson 日內波動率。

事件來源（兩者合併去重）：
  - `上市法說會/`、`上櫃法說會/` 的 CSV（歷史大宗，民國日期，含市場別→ticker 後綴）
  - `法說會事件庫.json`（網站每日爬蟲累積的法說場次，西元日期）——讓「剛爬到、
    還沒手動匯進 CSV」的新法說會也能自動納入。

行情來源：
  - `market_data.parquet`（資料商手動匯出，目前到 2026-05）
  - 缺口補齊：法說日 > parquet 最後一天的個股，自動用 yfinance（上市 .TW / 上櫃 .TWO，
    抓不到就互換後綴重試）補抓高低價合併。Parkinson 只用 ln(高/低)，還原權值不影響。

納入規則：**完整窗口**——t-2/t-1/t0/t+1 四天行情都齊才納入該場（剛開完 t+1 還沒到的
先不算，隔日資料到位下次自動補上）。

輸出：vol_stats.json ── { 代號: {"t_2":.., "t_1":.., "t0":.., "t1":.., "n":事件數} }
Parkinson 日內波動率(%) = 100 * (1/sqrt(4 ln2)) * ln(High/Low)
"""
import os, glob, re, json
from datetime import date
import numpy as np
import pandas as pd

BASE = r'E:\法說會+主動型'
FACTOR = 1 / np.sqrt(4 * np.log(2))
OUT = os.path.join(BASE, 'vol_stats.json')
GAP_CACHE = os.path.join(BASE, 'market_gap_yf.parquet')
IR_STORE = os.path.join(BASE, '法說會事件庫.json')

OFFSETS = {'t_2': -2, 't_1': -1, 't0': 0, 't1': 1}
MKT_SUFFIX = {'上市法說會': '.TW', '上櫃法說會': '.TWO'}


def _roc_ymd(s):   # 民國 115/06/12
    m = re.search(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', str(s))
    return (int(f'{int(m.group(1)) + 1911}{int(m.group(2)):02d}{int(m.group(3)):02d}')
            if m else None)


def _ce_ymd(s):    # 西元 2026/06/12
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', str(s))
    return int(f'{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}') if m else None


def _park(H, L):
    H = pd.to_numeric(H, errors='coerce')
    L = pd.to_numeric(L, errors='coerce')
    return np.where((H > 0) & (L > 0) & (H >= L), 100 * FACTOR * np.log(H / L), np.nan)


# ── 1) 事件清單：CSV（歷史，帶市場別）＋ 事件庫 json（近期爬蟲）合併去重 ──
_evs = []
for _folder, _suf in MKT_SUFFIX.items():
    for _f in glob.glob(os.path.join(BASE, _folder, '*.csv')):
        _d = pd.read_csv(_f, encoding='cp950', encoding_errors='ignore', on_bad_lines='skip')
        _evs.append(pd.DataFrame({
            '代號': _d['公司代號'].astype(str).str.strip(),
            'ymd': _d['召開法人說明會日期'].apply(_roc_ymd),
            'suf': _suf,
        }))
csv_ir = pd.concat(_evs, ignore_index=True).dropna(subset=['ymd'])
csv_ir['ymd'] = csv_ir['ymd'].astype(int)
# 市場別（ticker 後綴）以 CSV 來源為準
suffix_of = csv_ir.drop_duplicates('代號').set_index('代號')['suf'].to_dict()

store_rows = []
if os.path.exists(IR_STORE):
    try:
        for _e in json.load(open(IR_STORE, encoding='utf-8')):
            c = str(_e.get('co_code', '')).strip()
            y = _ce_ymd(_e.get('date', ''))
            if c and y:
                store_rows.append((c, y))
        print(f'法說會事件庫.json：{len(store_rows):,} 筆')
    except Exception as _e:
        print(f'  [警告] 事件庫讀取失敗：{_e}')
store_ir = pd.DataFrame(store_rows, columns=['代號', 'ymd'])

all_ir = pd.concat([csv_ir[['代號', 'ymd']], store_ir], ignore_index=True)
all_ir = all_ir.drop_duplicates(['代號', 'ymd'])
print(f'法說事件（CSV＋事件庫，去重）：{len(all_ir):,}')

# ── 2) 行情（資料商 parquet）→ 每檔每日 Parkinson 波動率 ──
mk = pd.read_parquet(os.path.join(BASE, 'market_data.parquet'))
mkd = pd.DataFrame({
    '代號': mk['代號'].astype(str).str.split().str[0],
    'ymd': pd.to_numeric(mk['年月日'], errors='coerce'),
    'park': _park(mk['最高價(元)'], mk['最低價(元)']),
}).dropna(subset=['ymd'])
PMAX = int(mkd['ymd'].max())
print(f'parquet 行情最新日：{PMAX}')

# ── 3) yfinance 補齊缺口：法說日 > PMAX 的個股，抓 PMAX 之後高低價 ──
today = int(date.today().strftime('%Y%m%d'))
gap_codes = sorted(all_ir.loc[(all_ir['ymd'] > PMAX) & (all_ir['ymd'] <= today), '代號'].unique())
gap_rows = []
if gap_codes:
    print(f'需 yfinance 補齊缺口的個股：{len(gap_codes)} 檔（法說日 > {PMAX}）')
    try:
        import yfinance as yf
        start = pd.to_datetime(str(PMAX), format='%Y%m%d').strftime('%Y-%m-%d')
        end = (date.today() + pd.Timedelta(days=1)).strftime('%Y-%m-%d')

        def _fetch(codes, suf):
            """批次抓一組 ticker，回傳 {code: 補抓 (ymd, park) list}；抓不到的回傳空。"""
            got = {}
            if not codes:
                return got
            tickers = [f'{c}{suf}' for c in codes]
            data = yf.download(tickers, start=start, end=end, auto_adjust=True,
                               progress=False, group_by='ticker', threads=True)
            for c in codes:
                try:
                    sub = data[f'{c}{suf}'] if len(tickers) > 1 else data
                    sub = sub.dropna(subset=['High', 'Low'])
                    if sub.empty:
                        continue
                    ymd = sub.index.strftime('%Y%m%d').astype(int)
                    pk = _park(sub['High'].values, sub['Low'].values)
                    rows = [(int(y), float(p)) for y, p in zip(ymd, pk) if y > PMAX]
                    if rows:
                        got[c] = rows
                except Exception:
                    continue
            return got

        known_tw = [c for c in gap_codes if suffix_of.get(c) == '.TW']
        known_two = [c for c in gap_codes if suffix_of.get(c) == '.TWO']
        unknown = [c for c in gap_codes if c not in suffix_of]
        res = {}
        res.update(_fetch(known_tw, '.TW'))
        res.update(_fetch(known_two, '.TWO'))
        # 未知市場：先試 .TW，沒抓到的再試 .TWO
        miss1 = _fetch(unknown, '.TW'); res.update(miss1)
        retry = [c for c in unknown if c not in miss1]
        res.update(_fetch(retry, '.TWO'))
        # 已知後綴但整批沒抓到的，互換後綴再試一次（防後綴標錯）
        failed = [c for c in (known_tw + known_two) if c not in res]
        res.update(_fetch([c for c in failed if suffix_of.get(c) == '.TW'], '.TWO'))
        res.update(_fetch([c for c in failed if suffix_of.get(c) == '.TWO'], '.TW'))

        for c, rows in res.items():
            for y, p in rows:
                gap_rows.append((c, y, p))
        print(f'  yfinance 補齊 {len(gap_rows):,} 筆（{len(res)} 檔；'
              f'未補到 {len(gap_codes) - len(res)} 檔）')
    except Exception as _e:
        print(f'  [警告] yfinance 補齊失敗，剛開過的法說會暫不納入：{_e}')

if gap_rows:
    gap_df = pd.DataFrame(gap_rows, columns=['代號', 'ymd', 'park'])
    gap_df.to_parquet(GAP_CACHE, index=False)
    mkd = pd.concat([mkd, gap_df], ignore_index=True)

# ── 4) 每檔排序成 (日期陣列, 波動率陣列)，去重日期 ──
mkd = mkd.dropna(subset=['park']).drop_duplicates(['代號', 'ymd']).sort_values(['代號', 'ymd'])
_cd, _cp = {}, {}
for _c, _g in mkd.groupby('代號', sort=False):
    _cd[_c] = _g['ymd'].to_numpy()
    _cp[_c] = _g['park'].to_numpy()

# ── 5) 逐事件取 t-2..t+1（完整窗口才納入），依股票聚合平均 ──
acc, n_ev, miss = {}, {}, 0
for _code, _ymd in zip(all_ir['代號'], all_ir['ymd']):
    arr = _cd.get(_code)
    if arr is None:
        miss += 1
        continue
    pos = int(np.searchsorted(arr, _ymd, side='left'))
    if pos >= len(arr) or arr[pos] != _ymd:
        miss += 1
        continue
    p = _cp[_code]
    vals, ok = {}, True
    for key, off in OFFSETS.items():
        j = pos + off
        if 0 <= j < len(arr) and np.isfinite(p[j]):
            vals[key] = p[j]
        else:
            ok = False
            break
    if not ok:
        continue
    bucket = acc.setdefault(_code, {k: [] for k in OFFSETS})
    n_ev[_code] = n_ev.get(_code, 0) + 1
    for key in OFFSETS:
        bucket[key].append(vals[key])

print(f'對不到行情/窗口不完整而略過的事件：{miss:,}；有統計的股票：{len(acc):,}')

# ── 6) 平均並輸出 ──
result = {}
for _code, bucket in acc.items():
    rec = {key: round(float(np.mean(bucket[key])), 2) for key in OFFSETS}
    rec['n'] = int(n_ev.get(_code, 0))
    result[_code] = rec

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, sort_keys=True)
print(f'已輸出 {OUT}（{len(result):,} 檔）')

for c in ['1440', '2330', '2317', '1312']:
    if c in result:
        print(c, result[c])
