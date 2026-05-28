"""
執行此腳本 → 自動讀取 CSV → 處理資料 → 輸出 法說會查詢.html → 啟動本地伺服器並開啟瀏覽器
"""

import json
import glob
import os
import re
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
import pandas as pd
import exchange_calendars as xcals

BASE      = r'E:\法說會+主動型'
TWSE_PATH = os.path.join(BASE, '上市法說會')
OTC_PATH  = os.path.join(BASE, '上櫃法說會')
FIRM_CSV  = os.path.join(BASE, 'firm_data.csv')
CAP_CSV   = os.path.join(BASE, '實收資本額.csv')
OUT_HTML  = os.path.join(BASE, 'index.html')

LARGE_CAP_THRESHOLD = 10_000_000_000  # 100億元
PKL_PATH = os.path.join(BASE, '財報_df.pkl')

START_YEAR, END_YEAR = 2021, 2026


# ---------- 擇要訊息期別解析 ----------

_Q_MAP = {'一': 'Q1', '二': 'Q2', '三': 'Q3', '四': 'Q4'}
_D_MAP = {'1': 'Q1', '2': 'Q2', '3': 'Q3', '4': 'Q4'}

def _q(ch):
    return _Q_MAP.get(ch) or _D_MAP.get(ch, ch)

def extract_period_mention(text, conf_date=None):
    """從擇要訊息中抽出財報期別，例如 '114年度第4季' → '2025 Q4'
    conf_date: 說明會日期，用於無年份時推算財報年度（Q4 → 前一年）
    """
    if pd.isna(text) or not str(text).strip():
        return None
    t = str(text).strip()

    # 西元年 + (年度?) + 第X季 (國字或數字): "2024年第一季" / "2025年度第4季"
    m = re.search(r'(20\d{2})年度?第([一二三四1-4])季', t)
    if m:
        return f'{m.group(1)} {_q(m.group(2))}'

    # 民國年 + (年度?) + 第X季 (國字或數字): "114年第四季" / "114年度第4季"
    m = re.search(r'(1[01]\d)年度?第([一二三四1-4])季', t)
    if m:
        return f'{int(m.group(1))+1911} {_q(m.group(2))}'

    # 西元年 + Q數字: "2026Q1" "2025q4"
    m = re.search(r'(20\d{2})[Qq]([1-4])', t)
    if m:
        return f'{m.group(1)} Q{m.group(2)}'

    # 民國年 + Q數字: "114Q4"
    m = re.search(r'(1[01]\d)[Qq]([1-4])', t)
    if m:
        return f'{int(m.group(1))+1911} Q{m.group(2)}'

    # 西元年 + 全年/年度/年報: "2024年度" "2024全年"
    m = re.search(r'(20\d{2})年?(?:度|全年|年報)', t)
    if m:
        return f'{m.group(1)} 年報'

    # 民國年 + 年度: "114年度"
    m = re.search(r'(1[01]\d)年度', t)
    if m:
        return f'{int(m.group(1))+1911} 年報'

    # 無年份，只有第X季 (國字或數字)
    m = re.search(r'第([一二三四1-4])季', t)
    if m:
        q_str = _q(m.group(1))  # e.g. "Q4"
        if conf_date is not None and not pd.isna(conf_date):
            yr = conf_date.year
            # Q4 財報是前一年的，所以說明會年份 -1
            infer_yr = yr - 1 if q_str == 'Q4' else yr
            return f'{infer_yr} {q_str}'
        return q_str

    return None


# ---------- 輔助函數 ----------

def roc_to_date(roc_str):
    try:
        parts = str(roc_str).strip().split('/')
        if len(parts) == 3:
            return pd.Timestamp(f'{int(parts[0]) + 1911}/{parts[1]}/{parts[2]}')
    except Exception:
        pass
    return pd.NaT


def build_trading_arr(start_year, end_year):
    cal = xcals.get_calendar('XTAI')
    buf_start = pd.Timestamp(f'{start_year - 1}-11-01')
    buf_end   = min(pd.Timestamp(f'{end_year + 1}-06-30'),
                    cal.last_session.tz_localize(None))
    sessions = cal.sessions_in_range(buf_start, buf_end)
    return pd.DatetimeIndex([s.tz_localize(None) for s in sessions])


# 大型股年報實際截止日（依年份，key=申報年，即財報年度+1）
_LC_ANNUAL = {2023: (3, 16), 2024: (3, 15), 2025: (3, 17), 2026: (3, 16)}


def get_report_anchors(start_year, end_year):
    monthly = [(m, 10) for m in range(1, 13)]
    # 移除固定3/15與4/1；大型股年報改為逐年實際日期；年報其餘併入Q4 3/31
    special_fixed = [(3, 31), (5, 15), (8, 14), (11, 14)]
    anchors = []
    for year in range(start_year, end_year + 1):
        for m, d in monthly + special_fixed:
            try:
                anchors.append(pd.Timestamp(f'{year}-{m:02d}-{d:02d}'))
            except Exception:
                pass
        lc_m, lc_d = _LC_ANNUAL.get(year, (3, 15))
        try:
            anchors.append(pd.Timestamp(f'{year}-{lc_m:02d}-{lc_d:02d}'))
        except Exception:
            pass
    return sorted(set(anchors))


def filter_closest_to_anchors(df, anchors, trading_arr, window=20):
    kept = []
    for anchor in anchors:
        idx = trading_arr.searchsorted(anchor, side='right') - 1
        if not (0 <= idx < len(trading_arr)):
            continue
        lo = trading_arr[max(0, idx - window)]
        hi = trading_arr[min(len(trading_arr) - 1, idx + window)]
        win = df[(df['日期'] >= lo) & (df['日期'] <= hi)]
        if win.empty:
            continue
        before = (win[win['日期'] <= anchor]
                  .sort_values('日期')
                  .groupby('公司代號', sort=False).last()
                  .reset_index())
        after = (win[win['日期'] > anchor]
                 .sort_values('日期')
                 .groupby('公司代號', sort=False).first()
                 .reset_index())
        kept.extend([before, after])
    if not kept:
        return df.iloc[0:0].copy()
    return (pd.concat(kept, ignore_index=True)
            .drop_duplicates()
            .sort_values(['公司代號', '日期'])
            .reset_index(drop=True))


_ANCHOR_LABEL = {
    (3, 31): 'Q4財報',
    (5, 15): 'Q1財報',
    (8, 14): 'Q2財報',
    (11,14): 'Q3財報',
}

def get_anchor_label(anchor):
    if anchor.day == 10:
        return '月營收'
    # 大型股年報：3月中旬（15/16/17日皆可能）
    if anchor.month == 3 and anchor.day in (15, 16, 17):
        return '年報(大型股/金融)'
    return _ANCHOR_LABEL.get((anchor.month, anchor.day), '其他')


def attach_anchor_info(df, anchors, trading_arr):
    anchor_series = pd.Series(anchors)

    def _nearest(date):
        if pd.isna(date):
            return (pd.NaT, None)
        diffs = (anchor_series - date).dt.days.abs()
        min_diff = int(diffs.min())

        # 優先選季報/年報錨點（day != 10），若其距離 ≤ 最近錨點 + 10 日曆天
        non_monthly = anchor_series[anchor_series.apply(lambda a: a.day != 10)]
        if not non_monthly.empty:
            nm_diffs = diffs[non_monthly.index]
            nm_min   = int(nm_diffs.min())
            if nm_min <= min_diff + 10:
                i = nm_diffs.idxmin()
                a = anchors[i]
                idx_date   = int(trading_arr.searchsorted(date, side='left'))
                idx_anchor = int(trading_arr.searchsorted(a,    side='left'))
                return (a, idx_date - idx_anchor)

        # 退而求其次：最近的月營收錨點
        i = diffs.idxmin()
        a = anchors[i]
        idx_date   = int(trading_arr.searchsorted(date, side='left'))
        idx_anchor = int(trading_arr.searchsorted(a,    side='left'))
        return (a, idx_date - idx_anchor)

    info = df['日期'].apply(_nearest)
    df = df.copy()
    df['財報錨點']      = info.apply(lambda x: x[0])
    df['距截止日交易日'] = info.apply(lambda x: x[1])
    df['財報期別']      = df['財報錨點'].apply(lambda a: get_anchor_label(a) if pd.notna(a) else None)
    df['財報年度']      = df['財報錨點'].apply(lambda a: a.year if pd.notna(a) else None)
    df['前後']         = df['距截止日交易日'].apply(
        lambda x: '截止日後' if x is not None and x > 0
                  else ('截止日前' if x is not None and x < 0 else '截止日當天'))
    return df


# ---------- 讀取 & 處理 ----------

def load_market(folder, market_label):
    files = sorted(glob.glob(os.path.join(folder, '*.csv')))
    print(f'  讀取 {market_label}：{len(files)} 個檔案')
    dfs = []
    for f in files:
        df = pd.read_csv(f, encoding='cp950', encoding_errors='ignore', on_bad_lines='skip')
        dfs.append(df)
    combined = pd.concat(dfs, ignore_index=True)
    combined['日期'] = combined['召開法人說明會日期'].apply(roc_to_date)
    combined = combined[combined['召開法人說明會時間'].astype(str).str.strip() >= '13:30']
    combined['市場'] = market_label
    return combined


def main():
    print('讀取財報_df.pkl…')
    df = pd.read_pickle(PKL_PATH)

    # ── 欄位對應 ────────────────────────────────────────────────
    out = pd.DataFrame()
    out['公司代號'] = df['代號'].astype(str).str.strip()
    out['公司名稱'] = df['簡稱'].astype(str)
    out['日期']    = pd.to_datetime(df['通過日'])

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

    # 財報年度 = 截止日所在年份（JS 端對 Q4/年報 顯示時會自動 -1）
    out['財報年度'] = pd.to_datetime(df['截止日']).dt.year
    out['財報錨點'] = pd.to_datetime(df['截止日'])

    # 網站慣例：正=截止後，負=截止前
    # 財報_df 慣例：正=截止前（剩餘），負=截止後（逾期）→ 取反
    out['距截止日交易日'] = (-df['距截止交易日']).astype(int)
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

    # 法說會日期（董事會通過後第一場）與主旨
    out['法說日'] = (
        pd.to_datetime(df['首次法說日'], errors='coerce')
        if '首次法說日' in df.columns else pd.NaT
    )
    out['主旨'] = df['主旨'].astype(str)
    # 董事會通過 → 首次法說會 相差日曆天數
    diff = (out['法說日'] - out['日期']).dt.days
    out['通過到法說天數'] = diff.where(diff.notna(), other=None)
    # 首次法說 → 截止日 交易日數（正=截止前，負=截止後；已在 notebook 算好）
    out['法說到截止交易日'] = (
        df['法說到截止交易日'] if '法說到截止交易日' in df.columns else None
    )

    print(f'  載入 {len(out):,} 筆')

    # ── 讀取實收資本額 ──────────────────────────────────────────
    print('讀取實收資本額.csv…')
    with open(CAP_CSV, 'rb') as f:
        cap_text = f.read().decode('utf-16')
    cap_lines = cap_text.splitlines()
    large_cap_codes = set()
    for line in cap_lines[1:]:
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        code = parts[0].strip().split()[0]
        try:
            cap = float(parts[1].strip().replace(',', ''))
            if cap >= LARGE_CAP_THRESHOLD:
                large_cap_codes.add(code)
        except Exception:
            pass
    print(f'  大型股(>=100億)：{len(large_cap_codes)} 家')

    # ── 讀取 firm_data ──────────────────────────────────────────
    print('讀取 firm_data.csv…')
    firm_df = pd.read_csv(FIRM_CSV, encoding='utf-16', sep='\t')
    firm_df['_code']  = firm_df['證券代碼'].astype(str).str.split().str[0]
    firm_df['_short'] = firm_df['證券代碼'].astype(str).str.split(n=1).str[1].fillna('')
    firm_df['_ind']   = firm_df['TSE產業名'].fillna('未分類')
    firm_info = {
        row['_code']: {'name': row['_short'], 'industry': row['_ind']}
        for _, row in firm_df.iterrows()
    }
    from collections import defaultdict
    ind_groups = defaultdict(list)
    for code, info in sorted(firm_info.items()):
        ind_groups[info['industry']].append(code)
    industries = dict(sorted(ind_groups.items()))

    # ── 產生 HTML ───────────────────────────────────────────────
    print('產生 HTML…')
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

    data_json       = json.dumps(ex.to_dict(orient='records'), ensure_ascii=False)
    firm_info_json  = json.dumps(firm_info, ensure_ascii=False)
    industries_json = json.dumps(industries, ensure_ascii=False)
    large_cap_json  = json.dumps(sorted(large_cap_codes), ensure_ascii=False)

    html = (HTML_TEMPLATE
            .replace('__DATA__', data_json)
            .replace('__FIRM_INFO__', firm_info_json)
            .replace('__INDUSTRIES__', industries_json)
            .replace('__LARGE_CAP__', large_cap_json))
    with open(OUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'\n完成！共 {len(ex):,} 筆')

    # ── 本地伺服器 ──────────────────────────────────────────────
    PORT = 8888
    os.chdir(BASE)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    server = HTTPServer(('', PORT), QuietHandler)
    url = f'http://localhost:{PORT}/index.html'
    print(f'伺服器啟動：{url}')
    print('按 Ctrl+C 停止伺服器')

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


# ---------- HTML 模板 ----------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>📊 法說會查詢</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Nunito',sans-serif;display:flex;height:100vh;overflow:hidden;background:#eef4ff;}

/* ── SIDEBAR ── */
#sb{
  width:280px;min-width:280px;
  background:#0d2137;display:flex;flex-direction:column;height:100vh;
  box-shadow:3px 0 20px rgba(0,40,100,.25);
  transition:width .25s ease, min-width .25s ease;
  overflow:hidden;
}
body.sb-off #sb{width:0;min-width:0;box-shadow:none;}
#sb-top{display:flex;align-items:center;padding:16px 14px 4px;gap:8px;flex-shrink:0;}
#sb-logo{font-size:19px;font-weight:900;color:#60a5fa;letter-spacing:-.3px;white-space:nowrap;overflow:hidden;}
#sb-logo span{font-size:11px;font-weight:700;color:#4b7aa8;display:block;margin-top:2px;}
#srch{
  margin:8px 14px 4px;padding:10px 14px;
  border:2px solid #1e3a5f;border-radius:14px;
  background:#112233;color:#bfdbfe;font-size:15px;font-family:'Nunito',sans-serif;
  outline:none;width:calc(100% - 28px);transition:.2s;flex-shrink:0;
}
#srch:focus{border-color:#3b82f6;background:#0d2137;}
#srch::placeholder{color:#2d5a8a;}
#sb-count{padding:4px 18px 8px;font-size:12px;color:#4b7aa8;font-weight:700;flex-shrink:0;white-space:nowrap;}
#clist{flex:1;overflow-y:auto;padding:4px 8px 16px;}
#clist::-webkit-scrollbar{width:4px;}
#clist::-webkit-scrollbar-thumb{background:#1e3a5f;border-radius:4px;}

/* toggle button */
#sb-toggle{
  position:fixed;top:14px;left:14px;z-index:999;
  width:36px;height:36px;border-radius:10px;
  background:#1e3a5f;border:none;color:#60a5fa;
  font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 10px rgba(0,0,0,.3);transition:.2s;
}
#sb-toggle:hover{background:#2d5a8a;}
body.sb-off #sb-toggle{left:14px;}
body:not(.sb-off) #sb-toggle{left:246px;}

/* industry group */
.ind-header{
  padding:9px 10px;margin:4px 0 1px;
  color:#7db8f7;font-size:12px;font-weight:800;
  border-radius:10px;cursor:pointer;
  display:flex;align-items:center;justify-content:space-between;
  letter-spacing:.2px;transition:.15s;
}
.ind-header:hover{background:#112233;}
.ind-header.open{color:#93c5fd;}
.ind-arrow{font-size:10px;transition:.2s;display:inline-block;}
.ind-header.open .ind-arrow{transform:rotate(90deg);}
.ind-cnt{background:#2A5470;color:#fff;border-radius:8px;padding:1px 7px;font-size:11px;font-weight:800;}
.ind-body{display:none;padding-left:4px;}
.ind-header.open + .ind-body{display:block;}

/* company item */
.ci{
  padding:8px 10px 8px 14px;margin:2px 0;
  color:#93c5fd;font-size:14px;cursor:pointer;
  border-radius:10px;display:flex;align-items:center;gap:6px;
  transition:.15s;border-left:3px solid transparent;
}
.ci:hover{background:#112233;color:#bfdbfe;}
.ci.active{background:#1e3a5f;border-left-color:#3b82f6;color:#fff;}
.ci-code{font-weight:800;font-size:14px;min-width:38px;}
.ci-name{font-size:12px;color:#4b7aa8;font-weight:600;}
.ci.active .ci-name{color:#93c5fd;}

/* ── MAIN ── */
#main{flex:1;overflow-y:auto;padding:32px 38px;transition:padding .25s;}
body.sb-off #main{padding-left:58px;}
#main::-webkit-scrollbar{width:5px;}
#main::-webkit-scrollbar-thumb{background:#bfdbfe;border-radius:5px;}

/* home grid */
#home{display:block;}
#home-title{font-size:26px;font-weight:900;color:#1e3a5f;margin-bottom:6px;}
#home-sub{font-size:14px;color:#6b8fc7;font-weight:700;margin-bottom:24px;}

/* deadline timeline */
#dl-section{margin-bottom:32px;}
#dl-title{font-size:14px;font-weight:800;color:#1e3a5f;margin-bottom:12px;display:flex;align-items:center;gap:6px;}
#dl-row{display:flex;gap:10px;flex-wrap:wrap;}
.dl-chip{
  display:flex;flex-direction:column;align-items:center;gap:3px;
  padding:10px 14px;border-radius:14px;border:2px solid transparent;
  background:#64748b;min-width:86px;font-family:'Nunito',sans-serif;
  transition:.2s;cursor:default;
}
.dl-chip.past{opacity:.4;filter:grayscale(.6);}
.dl-chip.today{border-color:#ef4444;background:#dc2626;box-shadow:0 0 0 3px rgba(239,68,68,.25);}
.dl-chip.soon{border-color:#f59e0b;background:#b45309;box-shadow:0 2px 10px rgba(245,158,11,.3);}
.dl-chip.upcoming{border-color:transparent;background:#475569;}
.dl-icon{font-size:18px;}
.dl-label{font-size:11px;font-weight:800;color:#fff;text-align:center;line-height:1.2;}
.dl-date{font-size:12px;font-weight:700;color:rgba(255,255,255,.85);}
.dl-badge{font-size:10px;font-weight:800;padding:1px 7px;border-radius:8px;background:rgba(255,255,255,.25);color:#fff;}
.dl-td{font-size:11px;font-weight:700;color:rgba(255,255,255,.75);white-space:nowrap;}
.dl-chip.past .dl-td,.dl-chip.today .dl-td{display:none;}

#ind-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:12px;
}
.ind-card{
  background:#fff;border-radius:16px;
  border:2px solid #dbeafe;
  box-shadow:0 2px 10px rgba(59,130,246,.07);
  overflow:hidden;transition:box-shadow .2s,border-color .2s;
}
.ind-card:hover{box-shadow:0 4px 18px rgba(59,130,246,.15);}
.ind-card.open{border-color:#3b82f6;}
.ind-card-head{
  display:flex;align-items:center;gap:12px;
  padding:16px 18px;cursor:pointer;
  transition:background .15s;
}
.ind-card-head:hover{background:#f0f6ff;}
.ind-card.open .ind-card-head{background:#eff6ff;border-bottom:2px solid #dbeafe;}
.ic-emoji{font-size:26px;flex-shrink:0;}
.ic-name{font-size:15px;font-weight:800;color:#1e3a5f;flex:1;line-height:1.3;}
.ic-cnt{font-size:12px;font-weight:700;color:#fff;background:#002366;padding:2px 10px;border-radius:10px;flex-shrink:0;}
.ic-arrow{font-size:12px;color:#93c5fd;flex-shrink:0;transition:transform .2s;}
.ind-card.open .ic-arrow{transform:rotate(180deg);}
.ind-card-body{display:none;padding:12px 14px;flex-wrap:wrap;gap:6px;}
.ind-card.open .ind-card-body{display:flex;}
.co-chip{
  padding:5px 12px;border-radius:10px;
  background:#f0f6ff;color:#1e3a5f;
  font-size:13px;font-weight:700;cursor:pointer;
  border:1.5px solid #dbeafe;transition:.15s;
}
.co-chip:hover{background:#dbeafe;color:#1d4ed8;border-color:#93c5fd;}
.co-chip.large{
  background:#223A5E;color:#fff;
  border-color:#223A5E;
}
.co-chip.large:hover{background:#182d4a;border-color:#142540;}

/* back button */
#back-btn{
  display:inline-flex;align-items:center;gap:6px;
  margin-bottom:20px;padding:8px 18px;
  border-radius:14px;border:2px solid #bfdbfe;
  background:#fff;color:#2563eb;font-size:14px;font-weight:800;
  cursor:pointer;font-family:'Nunito',sans-serif;transition:.2s;
}
#back-btn:hover{background:#eff6ff;border-color:#3b82f6;}

#detail{display:none;}
#ch{font-size:26px;font-weight:900;color:#1e3a5f;margin-bottom:4px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
#ch-code{color:#1d4ed8;}
#ch-name{font-size:17px;font-weight:700;color:#3b82f6;background:#eff6ff;padding:3px 14px;border-radius:10px;}
#ch-ind{font-size:13px;color:#6b8fc7;font-weight:700;margin-bottom:6px;}
#ch-mkt{font-size:13px;margin-bottom:22px;display:flex;gap:6px;flex-wrap:wrap;}
.tag-twse{background:#dbeafe;color:#1d4ed8;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:800;}
.tag-otc {background:#e0f2fe;color:#0369a1;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:800;}
.period-tag{background:#223A5E;color:#fff;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:800;white-space:nowrap;}
.memo-cell{font-size:11px;color:#64748b;max-width:260px;word-break:break-word;}

/* Year buttons */
#ybtns{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:28px;}
.yb{
  padding:8px 22px;border-radius:20px;
  border:2px solid #bfdbfe;background:#fff;
  color:#2563eb;font-size:15px;font-weight:800;
  cursor:pointer;font-family:'Nunito',sans-serif;
  transition:.2s;box-shadow:0 2px 6px rgba(59,130,246,.08);
}
.yb:hover{border-color:#3b82f6;background:#eff6ff;transform:translateY(-1px);}
.yb.active{
  background:linear-gradient(135deg,#1d4ed8,#3b82f6);
  border-color:transparent;color:#fff;
  box-shadow:0 4px 14px rgba(59,130,246,.4);transform:translateY(-1px);
}

/* Q Cards */
#qs{display:flex;flex-direction:column;gap:18px;}
.qc{background:#fff;border-radius:18px;box-shadow:0 4px 18px rgba(59,130,246,.1);overflow:hidden;border:2px solid #dbeafe;transition:.2s;}
.qc:hover{box-shadow:0 6px 24px rgba(59,130,246,.18);}
.qh{padding:14px 20px;font-weight:900;font-size:17px;color:#1e3a5f;display:flex;align-items:center;gap:8px;background:linear-gradient(90deg,#eff6ff,#fff);border-bottom:2px solid #dbeafe;}
.qh-icon{font-size:20px;}
.qbody{padding:14px 20px 18px;display:flex;flex-direction:column;gap:10px;}
.qcol{flex:1;min-width:200px;}
.qst{font-size:12px;font-weight:800;letter-spacing:.3px;margin-bottom:10px;padding:5px 14px;border-radius:20px;display:inline-flex;align-items:center;gap:5px;}
.st-b{background:#fef3c7;color:#b45309;border:1.5px solid #fcd34d;}
.st-a{background:#d1fae5;color:#065f46;border:1.5px solid #6ee7b7;}
.st-s{background:#dbeafe;color:#1d4ed8;border:1.5px solid #93c5fd;}

table{width:100%;border-collapse:collapse;font-size:14px;}
th{text-align:left;padding:6px 8px;color:#93c5fd;font-size:12px;font-weight:800;border-bottom:2px solid #eff6ff;}
td{padding:7px 8px;color:#1e3a5f;border-bottom:1px solid #f0f6ff;font-size:14px;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#f8fbff;}
.dn{color:#b45309;font-weight:800;font-size:15px;}
.dp{color:#059669;font-weight:800;font-size:15px;}
.dz{color:#1d4ed8;font-weight:800;font-size:15px;}
.empty{color:#93c5fd;font-size:13px;font-style:italic;padding:8px 0;font-weight:700;}
</style>
</head>
<body>
<button id="sb-toggle" onclick="toggleSb()" title="展開/收合側欄">☰</button>
<div id="sb">
  <div id="sb-top">
    <div id="sb-logo">📊 法說會查詢<span>財報期對應一覽</span></div>
  </div>
  <input id="srch" placeholder="🔍  搜尋代號或名稱…" oninput="onSearch(this.value)">
  <div id="sb-count"></div>
  <div id="clist"></div>
</div>
<div id="main">
  <div id="home">
    <div id="home-title">📊 法說會財報對應查詢</div>
    <div id="home-sub">選擇產業，再點選公司，查看各財報期前後的法說會紀錄</div>
    <div style="display:inline-flex;align-items:center;gap:8px;margin-bottom:18px;padding:7px 14px;background:#eff6ff;border-radius:10px;border:1.5px solid #bfdbfe;">
      <span style="display:inline-block;width:14px;height:14px;border-radius:4px;background:#223A5E;"></span>
      <span style="font-size:12px;color:#1e3a5f;font-weight:700;">深藍色 = 實收資本額 ≥ 100億（大型股）</span>
    </div>
    <div id="dl-section">
      <div id="dl-title">📅 台股財報截止日</div>
      <div id="dl-row"></div>
    </div>
    <div id="ind-grid"></div>
  </div>
  <div id="detail">
    <button id="back-btn" onclick="goHome()">← 返回主頁</button>
    <div id="ch">
      <span id="ch-code"></span>
      <span id="ch-name"></span>
    </div>
    <div id="ch-ind"></div>
    <div id="ch-mkt"></div>
    <div id="ybtns"></div>
    <div id="qs"></div>
  </div>
</div>
<script>
function toggleSb() {
  document.body.classList.toggle('sb-off');
  document.getElementById('sb-toggle').textContent =
    document.body.classList.contains('sb-off') ? '☰' : '✕';
}

const RECORDS    = __DATA__;
const FIRM_INFO  = __FIRM_INFO__;
const INDUSTRIES = __INDUSTRIES__;
const LARGE_CAP  = new Set(__LARGE_CAP__);

const PERIOD_ICON = {
  'Q1財報':'🌱','Q2財報':'☀️','Q3財報':'🍂','Q4財報':'❄️',
  '年報(大型股/金融)':'🏦','年報(其餘)':'📋','月營收':'📊','其他':'📌'
};
const PERIOD_ORDER = ['Q1財報','Q2財報','Q3財報','Q4財報','年報(大型股/金融)','年報(其餘)','月營收','其他'];

// Q4財報/年報 → 財報年度為申報年-1；Q1~Q3月營收 → 申報年本身
function fiscalLabel(period, yr) {
  if (period === 'Q4財報' || period.startsWith('年報')) {
    return `${parseInt(yr)-1} ${period}`;
  }
  return `${yr} ${period}`;
}

// Build company data index from records
const companies = {};
RECORDS.forEach(r => {
  const code = String(r['公司代號'] || '').trim();
  if (!code) return;
  if (!companies[code]) {
    const fi = FIRM_INFO[code] || {};
    companies[code] = { name: fi.name || r['公司名稱'] || '', industry: fi.industry || '', mkts: new Set(), data: {} };
  }
  companies[code].mkts.add(r['市場'] || '');
  const yr = String(r['財報年度'] || '');
  if (!yr) return;
  if (!companies[code].data[yr]) companies[code].data[yr] = {};
  const period = r['財報期別'] || '其他';
  if (!companies[code].data[yr][period])
    companies[code].data[yr][period] = { before: [], after: [], same: [] };
  const slot = r['前後'] === '截止日前' ? 'before' : r['前後'] === '截止日後' ? 'after' : 'same';
  companies[code].data[yr][period][slot].push(r);
});

const totalCo = Object.keys(companies).length;
document.getElementById('sb-count').textContent = '共 ' + totalCo + ' 家公司';

let activeCode = null;
let openInds = new Set();
let openCards = new Set();  // 首頁可同時展開多個產業卡片

// ── Industry emoji map ──
const IND_EMOJI = {
  '水泥':'🏗️','食品':'🍱','塑膠':'🧪','紡織':'🧵','電機機械':'⚙️',
  '電器電纜':'🔌','化學':'⚗️','生技醫療':'💊','玻璃陶瓷':'🫙','造紙':'📄',
  '鋼鐵':'🏭','橡膠':'🔧','汽車':'🚗','電子':'💻','半導體':'🔬',
  '光電':'💡','通信網路':'📡','電子零組件':'🔩','電腦周邊':'🖥️',
  '建材營造':'🏗️','航運':'🚢','觀光餐旅':'🏨','金融':'🏦','貿易百貨':'🛍️',
  '油電燃氣':'⛽','其他':'📌'
};
function indEmoji(name) {
  for (const [k, v] of Object.entries(IND_EMOJI))
    if (name.includes(k)) return v;
  return '🏢';
}
function indShort(name) {
  // strip leading code like "M1100 " → "水泥工業"
  return name.replace(/^[A-Z]\d+\s+/, '');
}

// ── Deadline timeline ──
function renderDeadlines() {
  const today = new Date(); today.setHours(0,0,0,0);
  const yr = today.getFullYear();

  // Count weekdays between today and a future date (approximate trading days)
  function bizDays(to) {
    let count = 0, cur = new Date(today);
    while (cur < to) {
      cur.setDate(cur.getDate() + 1);
      const d = cur.getDay();
      if (d !== 0 && d !== 6) count++;
    }
    return count;
  }

  // 大型股年報實際截止日（申報年 → [月,日]）
  const LC_ANNUAL = {2023:[3,16], 2024:[3,15], 2025:[3,17], 2026:[3,16]};
  function lcDate(y) {
    const [m, d] = LC_ANNUAL[y] || [3, 15];
    return new Date(y, m-1, d);
  }

  const FIXED = [
    {m:3, d:31, label:'Q4\n財報',   icon:'❄️'},
    {m:5, d:15, label:'Q1\n財報',   icon:'🌱'},
    {m:8, d:14, label:'Q2\n財報',   icon:'☀️'},
    {m:11,d:14, label:'Q3\n財報',   icon:'🍂'},
  ];

  let all = [];

  // 季報：今年＋明年
  for (const f of FIXED) {
    for (const y of [yr, yr + 1]) {
      all.push({date: new Date(y, f.m-1, f.d), label: f.label, icon: f.icon});
    }
  }
  // 大型股年報：逐年實際日期
  for (const y of [yr, yr + 1]) {
    all.push({date: lcDate(y), label: '年報\n大型股', icon: '🏦'});
  }

  // 月營收：只顯示前1個月 ～ 後5個月
  for (let i = -1; i <= 5; i++) {
    const d = new Date(yr, today.getMonth() + i, 10);
    all.push({date: d, label: '月營收\n' + (d.getMonth()+1) + '月', icon: '📊'});
  }

  // 篩選：前2週 ～ 後12個月
  const lo = new Date(today); lo.setDate(lo.getDate() - 14);
  const hi = new Date(today); hi.setMonth(hi.getMonth() + 13);
  all = all.filter(x => x.date >= lo && x.date <= hi)
           .sort((a, b) => a.date - b.date);

  // 去重
  const seen = new Set();
  all = all.filter(x => {
    const k = x.date.toISOString().slice(0,10) + x.label;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });

  const fmt = d => `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()}`;
  const diffDays = d => Math.round((d - today) / 86400000);

  document.getElementById('dl-row').innerHTML = all.map(item => {
    const diff = diffDays(item.date);
    let cls = 'upcoming', badge = '';
    if (diff < 0)        { cls = 'past'; }
    else if (diff === 0) { cls = 'today'; badge = '<span class="dl-badge">今天</span>'; }
    else if (diff <= 7)  { cls = 'soon';  badge = `<span class="dl-badge soon-badge">${diff}天後</span>`; }

    const biz = diff > 0 ? bizDays(item.date) : 0;
    const tdStr = diff > 0 ? `<span class="dl-td">≈${biz} 交易日</span>` : '';
    const labelHtml = item.label.replace('\n', '<br>');

    return `<div class="dl-chip ${cls}">
      <span class="dl-icon">${item.icon}</span>
      <span class="dl-label">${labelHtml}</span>
      <span class="dl-date">${fmt(item.date)}</span>
      ${tdStr}
      ${badge}
    </div>`;
  }).join('');
}
renderDeadlines();

// ── Home grid ──
function escId(s) { return s.replace(/[^a-z0-9]/gi, '_'); }

function renderHomeGrid() {
  const grid = document.getElementById('ind-grid');
  let html = '';
  for (const [ind, codes] of Object.entries(INDUSTRIES)) {
    const validCodes = codes.filter(c => companies[c]);
    if (!validCodes.length) continue;
    const short = indShort(ind);
    const emoji = indEmoji(short);
    const isOpen = openCards.has(ind);
    const chips = validCodes.map(c => {
      const isLarge = LARGE_CAP.has(c);
      return `<span class="co-chip${isLarge ? ' large' : ''}" onclick="selectCompany('${c}')" title="${isLarge ? '大型股（實收資本額≥100億）' : ''}">${c} ${companies[c].name || ''}</span>`;
    }).join('');
    html += `<div class="ind-card${isOpen ? ' open' : ''}" id="icard-${escId(ind)}">
      <div class="ind-card-head" onclick="toggleIndCard('${escAttr(ind)}')">
        <span class="ic-emoji">${emoji}</span>
        <span class="ic-name">${short}</span>
        <span class="ic-cnt">${validCodes.length} 家</span>
        <span class="ic-arrow">▼</span>
      </div>
      <div class="ind-card-body">${chips}</div>
    </div>`;
  }
  grid.innerHTML = html;
}
renderHomeGrid();

function toggleIndCard(ind) {
  if (openCards.has(ind)) openCards.delete(ind);
  else openCards.add(ind);
  renderHomeGrid();
  if (openCards.has(ind)) {
    setTimeout(() => {
      const el = document.getElementById('icard-' + escId(ind));
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 60);
  }
}

function openIndustry(ind) { toggleIndCard(ind); }

// ── Sidebar rendering ──
function renderSidebar(filter) {
  const f = (filter || '').trim().toLowerCase();
  const el = document.getElementById('clist');

  if (f) {
    // flat search mode
    const hits = Object.keys(companies)
      .filter(c => c.includes(f) || (companies[c].name || '').toLowerCase().includes(f))
      .sort();
    el.innerHTML = hits.map(c => ciHTML(c)).join('');
    return;
  }

  // grouped by industry
  let html = '';
  for (const [ind, codes] of Object.entries(INDUSTRIES)) {
    const validCodes = codes.filter(c => companies[c]);
    if (!validCodes.length) continue;
    const isOpen = openInds.has(ind);
    html += `<div class="ind-header${isOpen ? ' open' : ''}" onclick="toggleInd('${escAttr(ind)}')">
      <span>🏭 ${ind}</span>
      <span style="display:flex;align-items:center;gap:6px;">
        <span class="ind-cnt">${validCodes.length}</span>
        <span class="ind-arrow">▶</span>
      </span>
    </div>
    <div class="ind-body">
      ${validCodes.map(c => ciHTML(c)).join('')}
    </div>`;
  }
  // companies not in any industry
  const inInd = new Set(Object.values(INDUSTRIES).flat());
  const orphans = Object.keys(companies).filter(c => !inInd.has(c)).sort();
  if (orphans.length) {
    const isOpen = openInds.has('__other__');
    html += `<div class="ind-header${isOpen ? ' open' : ''}" onclick="toggleInd('__other__')">
      <span>📌 未分類</span>
      <span style="display:flex;align-items:center;gap:6px;">
        <span class="ind-cnt">${orphans.length}</span>
        <span class="ind-arrow">▶</span>
      </span>
    </div>
    <div class="ind-body">${orphans.map(c => ciHTML(c)).join('')}</div>`;
  }
  el.innerHTML = html;
}

function ciHTML(code) {
  const co = companies[code];
  return `<div class="ci${code === activeCode ? ' active' : ''}" onclick="selectCompany('${code}')">
    <span class="ci-code">${code}</span>
    <span class="ci-name">${co.name || ''}</span>
  </div>`;
}

function escAttr(s) { return s.replace(/'/g, "\\'"); }

function toggleInd(ind) {
  if (openInds.has(ind)) openInds.delete(ind);
  else openInds.add(ind);
  renderSidebar(document.getElementById('srch').value);
}

function onSearch(v) { renderSidebar(v); }

renderSidebar('');

function goHome() {
  activeCode = null;
  renderSidebar(document.getElementById('srch').value);
  document.getElementById('detail').style.display = 'none';
  document.getElementById('home').style.display = 'block';
}

// ── Company detail ──
function selectCompany(code) {
  activeCode = code;
  renderSidebar(document.getElementById('srch').value);
  document.getElementById('home').style.display = 'none';
  document.getElementById('detail').style.display = 'block';

  const co = companies[code];
  document.getElementById('ch-code').textContent = code;
  document.getElementById('ch-name').textContent = co.name || '';
  document.getElementById('ch-ind').textContent  = co.industry ? '🏭 ' + co.industry : '';
  const mkts = [...co.mkts].filter(Boolean);
  document.getElementById('ch-mkt').innerHTML =
    mkts.map(m => `<span class="${m.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${m}</span>`).join('');

  const years = Object.keys(co.data).sort((a, b) => b - a);
  document.getElementById('ybtns').innerHTML =
    years.map(y => `<button class="yb" onclick="selectYear('${y}')">${y}</button>`).join('');
  document.getElementById('qs').innerHTML = '';

  // 預設選最新年度
  if (years.length) selectYear(years[0]);
}

// ── Year detail ──
function selectYear(yr) {
  document.querySelectorAll('.yb').forEach(b =>
    b.classList.toggle('active', b.textContent === yr));
  const data = companies[activeCode].data[yr] || {};
  const qs = document.getElementById('qs');
  qs.innerHTML = '';
  PERIOD_ORDER.forEach(period => {
    if (!data[period]) return;
    const { before, after, same } = data[period];
    if (!before.length && !after.length && !same.length) return;
    const sB = [...before].sort((a, b) => a['距截止日交易日'] - b['距截止日交易日']);
    const sA = [...after].sort((a, b) => a['距截止日交易日'] - b['距截止日交易日']);
    function mktTag(m) {
      if (!m) return '';
      return `<span class="${m.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${m}</span>`;
    }
    function tbl(arr, cls) {
      if (!arr.length) return '<div class="empty">💤 無資料</div>';
      return `<table>
        <tr><th>董事會通過日</th><th>法說會日期</th><th>通過→法說</th><th>法說↔截止(交易日)</th><th>財報截止日</th><th>市場</th><th>說明期別</th><th>公告主旨</th><th>擇要訊息</th></tr>
        ${arr.map(r => {
          const memo   = r['法人說明會擇要訊息'] || '';
          const period = r['說明財報期'] || '';
          const calDiff = r['通過到法說天數'];
          const calStr = (calDiff != null && calDiff !== '' && !isNaN(calDiff))
            ? `<span style="color:#059669;font-weight:800">+${calDiff}天</span>`
            : '<span style="color:#cbd5e1">—</span>';
          const tdVal = r['法說到截止交易日'];
          const tdStr = (tdVal != null && tdVal !== '' && !isNaN(tdVal))
            ? `<span class="${tdVal < 0 ? 'dp' : 'dn'}">${tdVal}</span>`
            : '<span style="color:#cbd5e1">—</span>';
          const 主旨 = r['主旨'] || '';
          return `<tr>
            <td>${r['日期'] || ''}</td>
            <td style="color:#0369a1;font-weight:700">${r['法說日'] || '<span style="color:#cbd5e1">—</span>'}</td>
            <td style="text-align:center">${calStr}</td>
            <td style="text-align:center">${tdStr}</td>
            <td>${r['財報錨點'] || ''}</td>
            <td>${mktTag(r['市場'])}</td>
            <td>${period ? `<span class="period-tag">${period}</span>` : ''}</td>
            <td class="memo-cell" title="${主旨.replace(/"/g,'&quot;')}">${主旨}</td>
            <td class="memo-cell">${memo}</td>
          </tr>`;
        }).join('')}
      </table>`;
    }
    const icon = PERIOD_ICON[period] || '📌';
    const div = document.createElement('div');
    div.className = 'qc';
    div.innerHTML = `
      <div class="qh"><span class="qh-icon">${icon}</span>${fiscalLabel(period, yr)}</div>
      <div class="qbody">
        <div class="qst st-b">🕐 截止日前（${before.length} 筆）</div>
        ${before.length ? tbl(sB, 'dn') : ''}
        <div class="qst st-a">✅ 截止日後（${after.length} 筆）</div>
        ${after.length ? tbl(sA, 'dp') : ''}
        <div class="qst st-s">🎯 截止日當天（${same.length} 筆）</div>
        ${same.length ? tbl(same, 'dz') : ''}
      </div>`;
    qs.appendChild(div);
  });
}
</script>
</body>
</html>"""


if __name__ == '__main__':
    main()
