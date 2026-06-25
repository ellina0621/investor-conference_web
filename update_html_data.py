#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新現有 HTML 中的 JSON 數據（使用新的前置法說會邏輯）"""

import json
import os
import re
import pandas as pd
from datetime import date
from _warrant_utils import get_warrant_eligible_codes

BASE = r'E:\法說會+主動型'
PKL_PATH = os.path.join(BASE, '財報_df.pkl')
OUT_HTML = os.path.join(BASE, 'index.html')

print('讀取財報_df.pkl…')
df = pd.read_pickle(PKL_PATH)

print('抓取可發行權證標的（MOPS）…')
warrant_codes: set = set()
try:
    warrant_codes, _w_info = get_warrant_eligible_codes()
    print(f'  權證標的：{_w_info}')
except Exception as _e:
    print(f'  [警告] 權證標的抓取失敗：{_e}')

# ── 準備數據 ────────────────────────────────────────────────
out = pd.DataFrame()
out['公司代號'] = df['代號'].astype(str).str.strip()
out['公司名稱'] = df['簡稱'].astype(str)

# 前置法說會優先用法說日期；其餘用通過日期
is_before_ir = df['法說類型'].eq('前置法說')
out['日期'] = (
    pd.to_datetime(df['首次法說日']).where(is_before_ir,
                                          pd.to_datetime(df['通過日']))
)

out['召開法人說明會時間'] = (
    df['法說時間'].where(df['法說時間'].notna(), None)
    if '法說時間' in df.columns else None
)

def map_period(row):
    p = row['期別']
    if p == 'Q1': return 'Q1財報'
    if p == 'Q2': return 'Q2財報'
    if p == 'Q3': return 'Q3財報'
    if p == 'Q4': return 'Q4財報'
    if p == '年報': return '年報(大型股/金融)' if row.get('大型股', False) else '年報(其餘)'
    return '其他'
out['財報期別'] = df.apply(map_period, axis=1)

out['財報年度'] = pd.to_datetime(df['截止日']).dt.year
out['財報錨點'] = pd.to_datetime(df['截止日'])

# 距截止日交易日
is_before_ir = df['法說類型'].eq('前置法說')
out['距截止日交易日'] = (
    (-df['法說到截止交易日']).where(is_before_ir, (-df['距截止交易日']))
).astype(int)

out['前後'] = out['距截止日交易日'].apply(
    lambda x: '截止日後' if x > 0 else ('截止日前' if x < 0 else '截止日當天')
)

out['市場'] = df['市場別'].map({'上市': '上市(TWSE)', '上櫃': '上櫃(OTC)'})

out['說明財報期'] = df.apply(
    lambda r: f'{int(r["財報民國年"]) + 1911} {r["期別"]}', axis=1
)

out['法人說明會擇要訊息'] = (
    df['法說擇要'].where(df['法說擇要'].notna(), None)
    if '法說擇要' in df.columns else None
)

out['法說日'] = (
    pd.to_datetime(df['首次法說日'], errors='coerce')
    if '首次法說日' in df.columns else pd.NaT
)
out['主旨'] = df['主旨'].astype(str)
diff = (out['法說日'] - out['日期']).dt.days
out['通過到法說天數'] = diff.where(diff.notna(), other=None)
out['法說到截止交易日'] = (
    df['法說到截止交易日'] if '法說到截止交易日' in df.columns else None
)

# ── 準備 JSON ────────────────────────────────────────────────
_cols = ['公司代號', '公司名稱', '日期', '法說日', '通過到法說天數', '主旨',
         '召開法人說明會時間', '財報期別', '財報年度', '財報錨點', '前後',
         '距截止日交易日', '法說到截止交易日', '市場', '說明財報期', '法人說明會擇要訊息']
ex = out[[c for c in _cols if c in out.columns]].copy()
ex['公司代號'] = ex['公司代號'].astype(str).str.strip()
ex['日期']    = ex['日期'].dt.strftime('%Y-%m-%d')
ex['法說日']  = ex['法說日'].apply(
    lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None
)
ex['財報錨點'] = ex['財報錨點'].apply(
    lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None
)

data_json = json.dumps(ex.to_dict(orient='records'), ensure_ascii=False)

# ── 讀取現有 HTML ───────────────────────────────────────────
print(f'讀取現有 HTML ({OUT_HTML})…')
with open(OUT_HTML, 'r', encoding='utf-8') as f:
    html = f.read()

# ── 替換 JSON 數據 ──────────────────────────────────────────
print('替換數據…')
html = re.sub(
    r"const RECORDS = \[.*?\];",
    f"const RECORDS = {data_json};",
    html,
    flags=re.DOTALL,
    count=1
)

# ── 替換權證標的清單 ─────────────────────────────────────────
warrant_json = json.dumps(sorted(warrant_codes), ensure_ascii=False)
html = re.sub(
    r"const WARRANT\s*=\s*new Set\(.*?\);",
    f"const WARRANT           = new Set({warrant_json});",
    html,
    flags=re.DOTALL,
    count=1
)

# ── 替換各股法說會前後波動率統計（VOL_STATS）─────────────────
VOL_STATS_JSON = os.path.join(BASE, 'vol_stats.json')
if os.path.exists(VOL_STATS_JSON):
    with open(VOL_STATS_JSON, 'r', encoding='utf-8') as f:
        vol_stats = json.load(f)
    vol_json = json.dumps(vol_stats, ensure_ascii=False, separators=(',', ':'))
    if 'const VOL_STATS' in html:
        html = re.sub(
            r"const VOL_STATS\s*=\s*\{.*?\};",
            f"const VOL_STATS  = {vol_json};",
            html, flags=re.DOTALL, count=1,
        )
    else:  # 首次注入：放在 LARGE_CAP 之後
        html = re.sub(
            r"(const LARGE_CAP\s*=\s*new Set\(\[.*?\]\);)",
            lambda m: m.group(1) + f"\nconst VOL_STATS  = {vol_json};",
            html, flags=re.DOTALL, count=1,
        )
    print(f'  VOL_STATS：{len(vol_stats):,} 檔')
else:
    print('  [警告] 找不到 vol_stats.json，略過波動率注入')

# ── 寫回 HTML ───────────────────────────────────────────────
print(f'寫入更新 HTML…')
with open(OUT_HTML, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'Done! Updated {len(ex):,} records')
print(f'  - Pre-board IR: {(df["法說類型"]=="前置法說").sum():,} items')
print(f'  - Post-board IR: {(df["法說類型"]=="後續法說").sum():,} items')
