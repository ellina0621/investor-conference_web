"""
執行此腳本 → 自動讀取 CSV → 處理資料 → 輸出 法說會查詢.html → 啟動本地伺服器並開啟瀏覽器
"""

import json
import glob
import os
import re
import threading
import webbrowser
from datetime import date, datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import time
import urllib3
import pandas as pd
import exchange_calendars as xcals
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE      = r'E:\法說會+主動型'
TWSE_PATH = os.path.join(BASE, '上市法說會')
OTC_PATH  = os.path.join(BASE, '上櫃法說會')
FIRM_CSV  = os.path.join(BASE, 'firm_data.csv')
CAP_CSV   = os.path.join(BASE, '實收資本額.csv')
OUT_HTML  = os.path.join(BASE, 'index.html')

LARGE_CAP_THRESHOLD = 10_000_000_000  # 100億元
PKL_PATH            = os.path.join(BASE, '財報_df.pkl')
UPCOMING_IR_CACHE   = os.path.join(BASE, 'upcoming_ir_cache.json')
MOPS_IR_URL         = 'https://mopsov.twse.com.tw/mops/web/t100sb02_1'

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


# ---------- 法說會爬蟲（參照法說會時間.py）----------

def _ir_norm_date(value: str) -> str:
    if not value:
        return ''
    m = re.search(r'(\d{3,4})[/年](\d{1,2})[/月](\d{1,2})', value)
    if not m:
        return value.strip()
    year = int(m.group(1))
    if year < 1911:
        year += 1911
    return f'{year:04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}'


def _ir_norm_time(value: str) -> str:
    if not value:
        return ''
    m = re.search(r'(\d{1,2})[:：](\d{2})', value) or \
        re.search(r'(\d{1,2})時(\d{2})分?', value)
    if not m:
        return value.strip()
    return f'{int(m.group(1)):02d}:{m.group(2)}'


class MopsConferenceScraper:
    MARKET_TYPES = ['sii', 'otc', 'rotc']
    FIELD_MAP = {
        'co_code':  ['公司代號', '代號', '股票代號'],
        'co_name':  ['公司名稱', '名稱'],
        'date':     ['召開法人說明會日期', '召開法人說明會之日期', '說明會日期', '日期', '召開日期'],
        'time':     ['召開法人說明會時間', '召開法人說明會之時間', '說明會時間', '時間', '召開時間'],
        'location': ['召開法人說明會地點', '召開法人說明會之地點', '說明會地點', '地點', '召開地點'],
        'detail':   ['法人說明會擇要訊息', '擇要訊息', '說明', '備註'],
    }
    VIDEO_LINK_HEADERS  = ['影音連結資訊', '影音連結', '視訊連結', '連結資訊']
    OTHER_INFO_HEADERS  = ['其他應敘明事項', '其他應敘述事項', '其他事項', '應敘明事項', '應敘述事項', '備註說明']
    _URL_RE = re.compile(r'https?://[^\s<>"\'\)]+')

    def __init__(self):
        self.driver = None

    def _init_driver(self):
        opts = Options()
        opts.add_argument('--headless')
        opts.add_argument('--window-size=1920,1080')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=opts)

    def _quit_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def scrape(self) -> list:
        today = datetime.today()
        months = []
        for i in range(3):
            total = today.month - 1 + i
            months.append((today.year + total // 12, total % 12 + 1))
        self._init_driver()
        all_events, seen = [], set()
        try:
            for year, month in months:
                for typek in self.MARKET_TYPES:
                    for ev in self._scrape_month(year, month, typek):
                        k = (ev.get('co_code', ''), ev.get('date', ''), ev.get('time', ''))
                        if k not in seen:
                            seen.add(k)
                            all_events.append(ev)
        finally:
            self._quit_driver()
        return all_events

    def _scrape_month(self, year: int, month: int, typek: str) -> list:
        roc_year = year - 1911
        print(f'  [爬取] 民國{roc_year}年{month}月 {typek}...')
        try:
            self.driver.get(MOPS_IR_URL)
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            time.sleep(1.5)
            self._set_typek(typek)
            self._set_year(roc_year)
            self._set_month(month)
            if not self._submit_form(wait):
                self.driver.execute_script("""
                    var names=['month','MONTH','mon'];
                    for(var i=0;i<names.length;i++){
                        var s=document.querySelector('select[name="'+names[i]+'"]');
                        if(s&&s.form){s.form.submit();break;}
                    }
                """)
                try:
                    WebDriverWait(self.driver, 15).until(
                        lambda d: len(d.find_elements(By.CSS_SELECTOR, 'table tr')) > 3)
                except Exception:
                    pass
                time.sleep(2)
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            events = self._parse_events(soup)
            print(f'  [爬取] {year}/{month:02d}: {len(events)} 筆')
            return events
        except Exception as e:
            print(f'  [爬取] {year}/{month:02d} 失敗: {e}')
            return []

    def _set_typek(self, typek):
        for val in [typek, typek.upper()]:
            rs = self.driver.find_elements(By.XPATH, f"//input[@type='radio' and @name='TYPEK' and @value='{val}']")
            if rs:
                rs[0].click(); return
        for css in ["select[name='TYPEK']", "select[id='TYPEK']"]:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            if els:
                sel = Select(els[0])
                for v in [typek, typek.upper()]:
                    try:
                        sel.select_by_value(v); return
                    except Exception:
                        pass

    def _set_year(self, roc_year):
        for css in ['input[name="year"]', 'input[name="YEAR"]', 'input[name="Year"]', 'input[id="year"]']:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            if els:
                els[0].clear(); els[0].send_keys(str(roc_year)); return

    def _set_month(self, month):
        for css in ['select[name="month"]', 'select[name="MONTH"]', 'select[name="mon"]', 'select[id="month"]']:
            els = self.driver.find_elements(By.CSS_SELECTOR, css)
            if not els:
                continue
            sel = Select(els[0])
            for v in [f'{month:02d}', str(month)]:
                try:
                    sel.select_by_value(v); return
                except Exception:
                    pass
            try:
                sel.select_by_index(month); return
            except Exception:
                pass
        for val in [f'{month:02d}', str(month)]:
            rs = self.driver.find_elements(By.XPATH, f"//input[@type='radio' and @value='{val}']")
            if rs:
                rs[0].click(); return

    def _submit_form(self, wait):
        for xpath in ["//input[@value='查詢']", "//input[@type='submit']",
                      "//button[contains(text(),'查詢')]", "//input[@value='送出']"]:
            els = self.driver.find_elements(By.XPATH, xpath)
            if els:
                els[0].click()
                try:
                    wait.until(EC.presence_of_element_located((By.TAG_NAME, 'table')))
                except Exception:
                    time.sleep(2)
                return True
        return False

    def _parse_events(self, soup) -> list:
        target_table = None
        for table in soup.find_all('table'):
            text = table.get_text()
            if ('代號' in text or '公司代號' in text) and ('日期' in text or '時間' in text or '地點' in text):
                target_table = table
                break
        if not target_table:
            return []
        rows = target_table.find_all('tr')
        headers, events = [], []
        for row in rows:
            cells = row.find_all(['th', 'td'])
            texts = [c.get_text(separator=' ', strip=True) for c in cells]
            if not headers and any('代號' in t for t in texts):
                headers = texts; continue
            if not headers or len(texts) < 3:
                continue
            ev = self._map_row(headers, texts, cells)
            if ev:
                events.append(ev)
        return events

    def _map_row(self, headers, texts, cells=None):
        row_dict = {headers[i]: texts[i] for i in range(min(len(headers), len(texts)))}
        ev = {}
        for field, candidates in self.FIELD_MAP.items():
            for key in candidates:
                if row_dict.get(key):
                    ev[field] = row_dict[key]; break
        if not ev.get('co_code') and texts:
            ev['co_code'] = texts[0]
        if not ev.get('co_name') and len(texts) >= 2:
            ev['co_name'] = texts[1]
        if not ev.get('co_code') or ev['co_code'] in ('公司代號', '代號'):
            return None
        ev['date'] = _ir_norm_date(ev.get('date', ''))
        ev['time'] = _ir_norm_time(ev.get('time', ''))
        if cells:
            n_extra = max(0, len(cells) - len(headers))
            for i, header in enumerate(headers):
                if i >= len(cells):
                    break
                ci_list = [i + n_extra, i] if n_extra > 0 and (i + n_extra) < len(cells) else [i]
                if any(kw in header for kw in self.VIDEO_LINK_HEADERS):
                    for ci in ci_list:
                        cell = cells[ci]
                        urls = [a['href'].strip() for a in cell.find_all('a', href=True) if a['href'].strip().startswith('http')]
                        if not urls:
                            urls = self._URL_RE.findall(cell.get_text(' ', strip=True))
                        if urls:
                            ev['video_links'] = urls; break
                elif any(kw in header for kw in self.OTHER_INFO_HEADERS):
                    for ci in ci_list:
                        text = cells[ci].get_text(separator='\n', strip=True)
                        if 'http' in text.lower():
                            continue
                        clean = text.strip().rstrip('。.')
                        if clean and clean not in ('', '無', '-', '無資料', 'N/A'):
                            ev['other_info'] = text.strip(); break
        return ev


def scrape_upcoming_ir():
    """爬取近期法說會，更新快取，回傳 (全部未來事件, 新event keys集合)"""
    today_str = date.today().strftime('%Y/%m/%d')

    # 載入上次快取
    cached_keys: set = set()
    if os.path.exists(UPCOMING_IR_CACHE):
        try:
            with open(UPCOMING_IR_CACHE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            cached_keys = {
                f"{e.get('co_code','')}|{e.get('date','')}|{e.get('time','')}"
                for e in cached
            }
        except Exception:
            pass

    scraper = MopsConferenceScraper()
    raw = scraper.scrape()

    # 只保留今天以後
    upcoming = [ev for ev in raw if ev.get('date', '') >= today_str]
    upcoming.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))

    # 記錄哪些是新的
    new_keys: set = set()
    for ev in upcoming:
        k = f"{ev.get('co_code','')}|{ev.get('date','')}|{ev.get('time','')}"
        if k not in cached_keys:
            new_keys.add(k)

    print(f'  近期法說：{len(upcoming)} 筆，新增：{len(new_keys)} 筆')

    # 存快取（下次比較用）
    try:
        with open(UPCOMING_IR_CACHE, 'w', encoding='utf-8') as f:
            json.dump(upcoming, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'  [警告] 快取儲存失敗：{e}')

    return upcoming, new_keys


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

    # 已通過但尚未舉行法說會的公司（只顯示近期相關財報期）
    curr_year = date.today().year
    prev_year = curr_year - 1
    _valid_periods = {
        f'{prev_year} 年報',
        f'{prev_year} Q4',
        f'{curr_year} Q1',
        f'{curr_year} Q2',
        f'{curr_year} Q3',
    }
    _no_ir = ex['法說日'].isna() if '法說日' in ex.columns else pd.Series(True, index=ex.index)
    if '說明財報期' in ex.columns:
        _no_ir = _no_ir & ex['說明財報期'].isin(_valid_periods)
    pending_base = ex[_no_ir].sort_values('日期', ascending=False).copy()
    print(f'  待法說（未過濾）：{len(pending_base):,} 筆（財報期限：{sorted(_valid_periods)}）')

    # 爬取近期法說會
    print('爬取近期法說會（MOPS）…')
    upcoming_all = []
    _new_ev_keys: set = set()
    try:
        upcoming_all, _new_ev_keys = scrape_upcoming_ir()
    except Exception as _e:
        print(f'  [警告] 法說爬取失敗：{_e}')

    # 比對 pending vs upcoming → 分兩個 board
    upcoming_by_code: dict = {}
    for _ev in upcoming_all:
        _code = str(_ev.get('co_code', '')).strip()
        if _code:
            upcoming_by_code.setdefault(_code, []).append(_ev)

    _codes_s = pending_base['公司代號'].astype(str).str.strip()
    _mask_up = _codes_s.isin(upcoming_by_code)

    pending_no_ir_df  = pending_base[~_mask_up].copy()
    pending_has_ir_df = pending_base[_mask_up].copy()

    # 新增即將法說詳情 + 是否新爬到
    if not pending_has_ir_df.empty:
        _ph_codes = pending_has_ir_df['公司代號'].astype(str).str.strip()
        pending_has_ir_df['即將法說日']   = _ph_codes.apply(
            lambda c: upcoming_by_code[c][0].get('date', '') if c in upcoming_by_code else '')
        pending_has_ir_df['即將法說時間'] = _ph_codes.apply(
            lambda c: upcoming_by_code[c][0].get('time', '') if c in upcoming_by_code else '')
        pending_has_ir_df['即將法說地點'] = _ph_codes.apply(
            lambda c: upcoming_by_code[c][0].get('location', '') if c in upcoming_by_code else '')
        # True = 這次爬蟲才首次出現的法說
        pending_has_ir_df['即將法說新']   = _ph_codes.apply(
            lambda c: any(
                f"{c}|{ev.get('date','')}|{ev.get('time','')}" in _new_ev_keys
                for ev in upcoming_by_code.get(c, [])
            )
        )

    print(f'  待法說：{len(pending_no_ir_df):,} 筆　即將法說：{len(pending_has_ir_df):,} 筆')

    def _df_to_json(df):
        return json.dumps(df.where(df.notna(), other=None).to_dict(orient='records'), ensure_ascii=False)

    pending_ir_json  = _df_to_json(pending_no_ir_df)
    upcoming_ir_json = _df_to_json(pending_has_ir_df)

    html = (HTML_TEMPLATE
            .replace('__DATA__', data_json)
            .replace('__FIRM_INFO__', firm_info_json)
            .replace('__INDUSTRIES__', industries_json)
            .replace('__LARGE_CAP__', large_cap_json)
            .replace('__PENDING_IR__', pending_ir_json)
            .replace('__UPCOMING_IR__', upcoming_ir_json))
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

/* ── PENDING IR PANEL ── */
#pending-btn{
  position:fixed;top:14px;right:20px;z-index:999;
  padding:8px 16px;border-radius:12px;
  background:#1e3a5f;border:none;color:#60a5fa;
  font-size:13px;font-weight:800;cursor:pointer;
  display:flex;align-items:center;gap:8px;
  box-shadow:0 2px 10px rgba(0,0,0,.3);
  font-family:'Nunito',sans-serif;transition:.2s;
}
#pending-btn:hover{background:#2d5a8a;color:#bfdbfe;}
#pending-cnt{background:#ef4444;color:#fff;border-radius:8px;padding:1px 8px;font-size:11px;font-weight:900;}
#pending-overlay{display:none;position:fixed;inset:0;z-index:990;background:rgba(0,20,50,.45);}
#pending-overlay.open{display:block;}
#pending-panel{
  display:none;position:fixed;top:0;right:0;bottom:0;
  width:calc(100% - 280px);background:#f0f6ff;
  z-index:991;flex-direction:column;overflow:hidden;
  box-shadow:-6px 0 30px rgba(0,0,0,.2);
}
body.sb-off #pending-panel{width:100%;}
#pending-panel.open{display:flex;}
#pending-head{
  padding:14px 28px;background:#0d2137;flex-shrink:0;
  display:flex;align-items:center;justify-content:space-between;
}
#pending-head h2{color:#60a5fa;font-size:17px;font-weight:900;margin:0;}
#pending-close{
  background:none;border:2px solid #1e3a5f;color:#93c5fd;
  border-radius:10px;padding:6px 16px;cursor:pointer;
  font-size:13px;font-weight:800;font-family:'Nunito',sans-serif;transition:.2s;
}
#pending-close:hover{background:#1e3a5f;color:#fff;}
#pending-body{flex:1;overflow-y:auto;padding:20px 28px;}
#pending-body::-webkit-scrollbar{width:5px;}
#pending-body::-webkit-scrollbar-thumb{background:#bfdbfe;border-radius:5px;}
#p-meta{font-size:13px;color:#6b8fc7;font-weight:700;margin-bottom:14px;}
#ptable{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
  border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(59,130,246,.1);}
#ptable th{
  padding:10px 12px;background:#0d2137;color:#93c5fd;
  font-size:12px;font-weight:800;text-align:left;
  cursor:pointer;user-select:none;white-space:nowrap;
}
#ptable th:hover{background:#1e3a5f;color:#bfdbfe;}
#ptable th.asc::after{content:' ▲';}
#ptable th.desc::after{content:' ▼';}
#ptable td{padding:8px 12px;color:#1e3a5f;border-bottom:1px solid #f0f6ff;}
#ptable tr:last-child td{border-bottom:none;}
#ptable tr:hover td{background:#f8fbff;}

/* Panel search bar */
.panel-search-bar{padding:10px 28px 0;flex-shrink:0;}
.panel-search-bar input{
  width:100%;padding:8px 14px;border:2px solid #1e3a5f;border-radius:12px;
  background:#112233;color:#bfdbfe;font-size:14px;font-family:'Nunito',sans-serif;outline:none;
  transition:.2s;
}
.panel-search-bar input:focus{border-color:#3b82f6;}
.panel-search-bar input::placeholder{color:#2d5a8a;}
#upcoming-panel .panel-search-bar input{
  background:#fff;color:#1e3a5f;border-color:#93c5fd;
}
#upcoming-panel .panel-search-bar input:focus{border-color:#2A5470;}
#upcoming-panel .panel-search-bar input::placeholder{color:#93c5fd;}

/* Upcoming IR panel */
#upcoming-btn{
  position:fixed;top:14px;right:200px;z-index:999;
  padding:8px 16px;border-radius:12px;
  background:#14532d;border:none;color:#4ade80;
  font-size:13px;font-weight:800;cursor:pointer;
  display:flex;align-items:center;gap:8px;
  box-shadow:0 2px 10px rgba(0,0,0,.3);
  font-family:'Nunito',sans-serif;transition:.2s;
}
#upcoming-btn:hover{background:#166534;color:#bbf7d0;}
#upcoming-cnt{background:#16a34a;color:#fff;border-radius:8px;padding:1px 8px;font-size:11px;font-weight:900;}
#upcoming-overlay{display:none;position:fixed;inset:0;z-index:990;background:rgba(0,20,50,.45);}
#upcoming-overlay.open{display:block;}
#upcoming-panel{
  display:none;position:fixed;top:0;right:0;bottom:0;
  width:calc(100% - 280px);background:#eef4ff;
  z-index:991;flex-direction:column;overflow:hidden;
  box-shadow:-6px 0 30px rgba(0,0,0,.2);
}
body.sb-off #upcoming-panel{width:100%;}
#upcoming-panel.open{display:flex;}
#upcoming-head{
  padding:14px 28px;background:#2A5470;flex-shrink:0;
  display:flex;align-items:center;justify-content:space-between;
}
#upcoming-head h2{color:#B0E2FF;font-size:17px;font-weight:900;margin:0;flex:1;text-align:center;}
#upcoming-close{
  display:inline-flex;align-items:center;gap:6px;
  padding:7px 18px;border-radius:12px;
  border:2px solid #B0E2FF;background:rgba(255,255,255,.08);
  color:#B0E2FF;font-size:13px;font-weight:800;
  cursor:pointer;font-family:'Nunito',sans-serif;transition:.2s;
}
#upcoming-close:hover{background:rgba(176,226,255,.2);color:#fff;border-color:#fff;}
#upcoming-body{flex:1;overflow-y:auto;padding:20px 28px;}
#upcoming-body::-webkit-scrollbar{width:5px;}
#upcoming-body::-webkit-scrollbar-thumb{background:#93c5fd;border-radius:5px;}
#u-meta{font-size:13px;color:#2A5470;font-weight:700;margin-bottom:14px;}
#utable{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
  border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(42,84,112,.12);}
#utable th{
  padding:10px 12px;background:#2A5470;color:#B0E2FF;
  font-size:12px;font-weight:800;text-align:left;
  cursor:pointer;user-select:none;white-space:nowrap;
}
#utable th:hover{background:#1e3a5f;color:#bfdbfe;}
#utable th.asc::after{content:' ▲';}
#utable th.desc::after{content:' ▼';}
#utable td{padding:8px 12px;color:#1e3a5f;border-bottom:1px solid #eef4ff;}
#utable tr:last-child td{border-bottom:none;}
#utable tr:hover td{background:#f0f6ff;}
.new-badge{
  display:inline-block;background:#ef4444;color:#fff;
  font-size:10px;font-weight:900;letter-spacing:.3px;
  padding:1px 6px;border-radius:6px;margin-left:5px;
  vertical-align:middle;line-height:1.5;
}

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
<button id="upcoming-btn" onclick="openUpcoming()">📅 即將法說 <span id="upcoming-cnt">0</span></button>
<button id="pending-btn" onclick="openPending()">📋 待法說 <span id="pending-cnt">0</span></button>
<div id="upcoming-overlay" onclick="closeUpcoming()"></div>
<div id="upcoming-panel">
  <div id="upcoming-head">
    <button id="upcoming-close" onclick="closeUpcoming()">← 返回</button>
    <h2>📅 董事會已通過、即將舉行法說會</h2>
    <div style="width:90px"></div>
  </div>
  <div class="panel-search-bar">
    <input id="upcoming-search" type="text" placeholder="🔍 搜尋代號或名稱…" oninput="renderUpcoming()">
  </div>
  <div id="upcoming-body">
    <div id="u-meta"></div>
    <table id="utable">
      <thead><tr>
        <th data-col="公司代號" onclick="sortU('公司代號')">代號</th>
        <th data-col="公司名稱" onclick="sortU('公司名稱')">名稱</th>
        <th data-col="市場" onclick="sortU('市場')">市場</th>
        <th data-col="說明財報期" onclick="sortU('說明財報期')">財報期</th>
        <th data-col="日期" onclick="sortU('日期')">通過日</th>
        <th data-col="財報錨點" onclick="sortU('財報錨點')">截止日</th>
        <th data-col="即將法說日" onclick="sortU('即將法說日')">即將法說日</th>
        <th data-col="即將法說時間" onclick="sortU('即將法說時間')">時間</th>
        <th>地點</th>
        <th>公告主旨</th>
      </tr></thead>
      <tbody id="utbody"></tbody>
    </table>
  </div>
</div>
<div id="pending-overlay" onclick="closePending()"></div>
<div id="pending-panel">
  <div id="pending-head">
    <h2>📋 董事會已通過、尚未舉行法說會</h2>
    <button id="pending-close" onclick="closePending()">✕ 關閉</button>
  </div>
  <div class="panel-search-bar">
    <input id="pending-search" type="text" placeholder="🔍 搜尋代號或名稱…" oninput="renderPending()">
  </div>
  <div id="pending-body">
    <div id="p-meta"></div>
    <table id="ptable">
      <thead><tr>
        <th data-col="公司代號" onclick="sortP('公司代號')">代號</th>
        <th data-col="公司名稱" onclick="sortP('公司名稱')">名稱</th>
        <th data-col="市場" onclick="sortP('市場')">市場</th>
        <th data-col="說明財報期" onclick="sortP('說明財報期')">財報期</th>
        <th data-col="日期" onclick="sortP('日期')">通過日</th>
        <th data-col="財報錨點" onclick="sortP('財報錨點')">截止日</th>
        <th data-col="距截止日交易日" onclick="sortP('距截止日交易日')">距截止(交易日)</th>
        <th>公告主旨</th>
      </tr></thead>
      <tbody id="ptbody"></tbody>
    </table>
  </div>
</div>
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
const PENDING_IR  = __PENDING_IR__;
const UPCOMING_IR = __UPCOMING_IR__;

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

// ── Pending IR panel ──────────────────────────────────────────
let pSortCol = '日期';
let pSortAsc = false;

function openPending() {
  document.getElementById('pending-panel').classList.add('open');
  document.getElementById('pending-overlay').classList.add('open');
  renderPending();
}
function closePending() {
  document.getElementById('pending-panel').classList.remove('open');
  document.getElementById('pending-overlay').classList.remove('open');
}
function sortP(col) {
  if (pSortCol === col) pSortAsc = !pSortAsc;
  else { pSortCol = col; pSortAsc = true; }
  renderPending();
}
function renderPending() {
  const q = (document.getElementById('pending-search')?.value || '').toLowerCase().trim();
  const data = [...PENDING_IR].filter(r => {
    if (!q) return true;
    return String(r['公司代號']||'').includes(q) ||
           String(r['公司名稱']||'').toLowerCase().includes(q);
  }).sort((a, b) => {
    let va = a[pSortCol], vb = b[pSortCol];
    if (va == null) va = ''; if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number')
      return pSortAsc ? va - vb : vb - va;
    return pSortAsc
      ? String(va).localeCompare(String(vb), 'zh-TW')
      : String(vb).localeCompare(String(va), 'zh-TW');
  });
  document.querySelectorAll('#ptable th[data-col]').forEach(th => {
    th.classList.remove('asc','desc');
    if (th.dataset.col === pSortCol) th.classList.add(pSortAsc ? 'asc' : 'desc');
  });
  document.getElementById('p-meta').textContent = `共 ${data.length} 筆（總計 ${PENDING_IR.length} 筆）`;
  document.getElementById('ptbody').innerHTML = data.map(r => {
    const td = r['距截止日交易日'];
    const isLarge = LARGE_CAP.has(String(r['公司代號'] || '').trim());
    const mkt = r['市場'] || '';
    const mktHtml = mkt ? `<span class="${mkt.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${mkt}</span>` : '';
    const subj = (r['主旨'] || '').replace(/"/g,'&quot;');
    return `<tr>
      <td><strong style="${isLarge ? 'color:#1d4ed8' : ''}">${r['公司代號']||''}</strong></td>
      <td>${r['公司名稱']||''}</td>
      <td>${mktHtml}</td>
      <td>${r['說明財報期'] ? `<span class="period-tag">${r['說明財報期']}</span>` : ''}</td>
      <td>${r['日期']||''}</td>
      <td>${r['財報錨點']||''}</td>
      <td class="${td!=null?(td<0?'dp':'dn'):''}" style="font-weight:800;text-align:center">${td!=null?td:'—'}</td>
      <td class="memo-cell" title="${subj}">${r['主旨']||''}</td>
    </tr>`;
  }).join('');
}

// ── Upcoming IR panel ─────────────────────────────────────────
let uSortCol = '即將法說日';
let uSortAsc = true;

function openUpcoming() {
  document.getElementById('upcoming-panel').classList.add('open');
  document.getElementById('upcoming-overlay').classList.add('open');
  renderUpcoming();
}
function closeUpcoming() {
  document.getElementById('upcoming-panel').classList.remove('open');
  document.getElementById('upcoming-overlay').classList.remove('open');
}
function sortU(col) {
  if (uSortCol === col) uSortAsc = !uSortAsc;
  else { uSortCol = col; uSortAsc = true; }
  renderUpcoming();
}
function renderUpcoming() {
  const q = (document.getElementById('upcoming-search')?.value || '').toLowerCase().trim();
  const data = [...UPCOMING_IR].filter(r => {
    if (!q) return true;
    return String(r['公司代號']||'').includes(q) ||
           String(r['公司名稱']||'').toLowerCase().includes(q);
  }).sort((a, b) => {
    let va = a[uSortCol], vb = b[uSortCol];
    if (va == null) va = ''; if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number')
      return uSortAsc ? va - vb : vb - va;
    return uSortAsc
      ? String(va).localeCompare(String(vb), 'zh-TW')
      : String(vb).localeCompare(String(va), 'zh-TW');
  });
  document.querySelectorAll('#utable th[data-col]').forEach(th => {
    th.classList.remove('asc','desc');
    if (th.dataset.col === uSortCol) th.classList.add(uSortAsc ? 'asc' : 'desc');
  });
  document.getElementById('u-meta').textContent = `共 ${data.length} 筆（總計 ${UPCOMING_IR.length} 筆）`;
  document.getElementById('utbody').innerHTML = data.map(r => {
    const isLarge = LARGE_CAP.has(String(r['公司代號']||'').trim());
    const isNew   = r['即將法說新'] === true;
    const mkt = r['市場'] || '';
    const mktHtml = mkt ? `<span class="${mkt.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${mkt}</span>` : '';
    const subj = (r['主旨'] || '').replace(/"/g,'&quot;');
    return `<tr>
      <td><strong style="${isLarge ? 'color:#1d4ed8' : ''}">${r['公司代號']||''}</strong>${isNew ? '<span class="new-badge">NEW</span>' : ''}</td>
      <td>${r['公司名稱']||''}</td>
      <td>${mktHtml}</td>
      <td>${r['說明財報期'] ? `<span class="period-tag">${r['說明財報期']}</span>` : ''}</td>
      <td>${r['日期']||''}</td>
      <td>${r['財報錨點']||''}</td>
      <td style="color:#1d4ed8;font-weight:800">${r['即將法說日']||'—'}</td>
      <td>${r['即將法說時間']||'—'}</td>
      <td style="font-size:11px;max-width:130px;word-break:break-word">${r['即將法說地點']||'—'}</td>
      <td class="memo-cell" title="${subj}">${r['主旨']||''}</td>
    </tr>`;
  }).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('pending-cnt').textContent  = PENDING_IR.length;
  document.getElementById('upcoming-cnt').textContent = UPCOMING_IR.length;
});
</script>
</body>
</html>"""


if __name__ == '__main__':
    main()
