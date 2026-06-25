#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""計算每檔股票法說會前後（t-2, t-1, t0, t+1）的平均 Parkinson 日內波動率。

來源：
  - 事件：上市法說會/、上櫃法說會/ 的 CSV（公司代號 + 首次法人說明會日期）
  - 行情：market_data.parquet（證券代碼/年月日/最高價/最低價）

輸出：vol_stats.json  ── { 代號: {"t_2":.., "t_1":.., "t0":.., "t1":.., "n":事件數} }
Parkinson 日內波動率(%) = 100 * (1/sqrt(4 ln2)) * ln(High/Low)
"""
import os, glob, re, json
import numpy as np
import pandas as pd

BASE = r'E:\法說會+主動型'
FACTOR = 1 / np.sqrt(4 * np.log(2))
OUT = os.path.join(BASE, 'vol_stats.json')

OFFSETS = {'t_2': -2, 't_1': -1, 't0': 0, 't1': 1}


def _roc_ymd(s):
    m = re.search(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', str(s))
    return (int(f'{int(m.group(1)) + 1911}{int(m.group(2)):02d}{int(m.group(3)):02d}')
            if m else None)


# ── 1) 事件清單：每檔股票的歷年法說日（去重）──────────────────
_evs = []
for _folder in ('上市法說會', '上櫃法說會'):
    for _f in glob.glob(os.path.join(BASE, _folder, '*.csv')):
        _d = pd.read_csv(_f, encoding='cp950', encoding_errors='ignore', on_bad_lines='skip')
        _evs.append(pd.DataFrame({
            '代號': _d['公司代號'].astype(str).str.strip(),
            'ymd': _d['召開法人說明會日期'].apply(_roc_ymd),
        }))
all_ir = pd.concat(_evs, ignore_index=True).dropna()
all_ir['ymd'] = all_ir['ymd'].astype(int)
all_ir = all_ir.drop_duplicates(['代號', 'ymd'])
print(f'法說事件（去重）：{len(all_ir):,}')

# ── 2) 行情 → 每檔每日 Parkinson 波動率 ───────────────────────
mk = pd.read_parquet(os.path.join(BASE, 'market_data.parquet'))
code = mk['代號'].astype(str).str.split().str[0]
ymd = pd.to_numeric(mk['年月日'], errors='coerce')
H = pd.to_numeric(mk['最高價(元)'], errors='coerce')
L = pd.to_numeric(mk['最低價(元)'], errors='coerce')
park = np.where((H > 0) & (L > 0) & (H >= L), 100 * FACTOR * np.log(H / L), np.nan)

mkd = pd.DataFrame({'代號': code, 'ymd': ymd, 'park': park}).dropna(subset=['ymd'])
mkd = mkd.sort_values(['代號', 'ymd'])
_cd, _cp = {}, {}
for _c, _g in mkd.groupby('代號', sort=False):
    _cd[_c] = _g['ymd'].to_numpy()
    _cp[_c] = _g['park'].to_numpy()

# ── 3) 逐事件蒐集 t-2..t+1，依股票聚合平均 ────────────────────
acc = {}   # 代號 -> {key: [values]}
n_ev = {}  # 代號 -> 納入計算的事件數（有對到 t=0 行情）
miss = 0
for _code, _ymd in zip(all_ir['代號'], all_ir['ymd']):
    arr = _cd.get(_code)
    if arr is None:
        miss += 1
        continue
    pos = int(np.searchsorted(arr, _ymd, side='left'))
    # 要求 t=0 當天確實存在於行情（精準對齊法說當日交易日）
    if pos >= len(arr) or arr[pos] != _ymd:
        miss += 1
        continue
    p = _cp[_code]
    bucket = acc.setdefault(_code, {k: [] for k in OFFSETS})
    n_ev[_code] = n_ev.get(_code, 0) + 1
    for key, off in OFFSETS.items():
        j = pos + off
        if 0 <= j < len(arr) and np.isfinite(p[j]):
            bucket[key].append(p[j])

print(f'對不到行情的事件：{miss:,}；有統計的股票：{len(acc):,}')

# ── 4) 平均並輸出 ─────────────────────────────────────────────
result = {}
for _code, bucket in acc.items():
    rec = {}
    for key in OFFSETS:
        vals = bucket[key]
        rec[key] = round(float(np.mean(vals)), 2) if vals else None
    rec['n'] = int(n_ev.get(_code, 0))
    result[_code] = rec

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, sort_keys=True)
print(f'已輸出 {OUT}（{len(result):,} 檔）')

# 抽樣檢視
for c in ['1440', '2330', '2317']:
    if c in result:
        print(c, result[c])
