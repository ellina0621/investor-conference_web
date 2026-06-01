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
IR_EVENTS_JSON      = os.path.join(BASE, '法說會事件庫.json')   # 累積所有爬到的法說事件（增量、永不覆寫；供新爬偵測+歷史分段更新）
MOPS_IR_URL         = 'https://mopsov.twse.com.tw/mops/web/t100sb02_1'
ETF981A_URL         = 'https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode=49YTW'
ETF981A_CSV         = os.path.join(BASE, '00981A_投組.csv')
ETF981A_DAILY_JSON  = os.path.join(BASE, 'etf981a_daily.json')
ETF981A_NAV_JSON    = os.path.join(BASE, 'etf981a_nav_daily.json')
FUTURES_ODS         = os.path.join(BASE, '個股期貨代號.ods')

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
    combined = combined[combined['召開法人說明會時間'].astype(str).str.strip() >= '07:00']
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
    MARKET_TYPES = ['sii', 'otc']
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
        for i in range(5):                       # 前2月 ~ 後2月，涵蓋整季法說(含當月已過去場次)
            total = today.month - 3 + i
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


def _mmdd_to_full(mmdd: str) -> str:
    """'MM/DD' → 'YYYY/MM/DD'，跨年邊界（1月抓到12月）自動修正"""
    if not mmdd or '/' not in mmdd:
        return ''
    mm, dd = mmdd.split('/')
    today = datetime.today()
    year = today.year
    if today.month == 1 and mm == '12':
        year -= 1
    return f'{year}/{mm}/{dd}'


def save_etf981a_daily(holdings: dict, data_date: str, json_path: str = ETF981A_DAILY_JSON,
                       is_full_date: bool = False):
    """將本次爬取的成分股寫入 etf981a_daily.json。
    key 以頁面「上傳時間」的日期為準（YYYY/MM/DD）。
    同一上傳日期重跑 → 覆蓋；新日期 → 新增。
    is_full_date=True 表示 data_date 已是 YYYY/MM/DD，不需再轉換。
    """
    if not holdings or not data_date:
        return
    full_date = data_date if is_full_date else _mmdd_to_full(data_date)
    if not full_date:
        return
    try:
        daily = {}
        if os.path.isfile(json_path):
            with open(json_path, encoding='utf-8') as f:
                daily = json.load(f)
        daily[full_date] = holdings          # 覆蓋或新增
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(daily, f, ensure_ascii=False, indent=2)
        print(f'  [00981A] 已存 {full_date}（{len(holdings)} 檔）→ etf981a_daily.json')
    except Exception as e:
        print(f'  [警告] 儲存 etf981a_daily.json 失敗：{e}')


_NAV_SEED = {
    '2026/05/27': {'total_nav_raw': 290_447_804_921.0, 'nav_per_unit_raw': 31.89}
}

def save_etf981a_nav(metrics: dict, date_full: str,
                     json_path: str = ETF981A_NAV_JSON):
    """每日爬取後，將 NAV 原始數值存入 etf981a_nav_daily.json。
    同一上傳日期重跑 → 覆蓋；新日期 → 新增。
    """
    raw_nav   = metrics.get('total_nav_raw')
    raw_unit  = metrics.get('nav_per_unit_raw')
    if not date_full or (raw_nav is None and raw_unit is None):
        return
    try:
        if os.path.isfile(json_path):
            with open(json_path, encoding='utf-8') as f:
                daily = json.load(f)
        else:
            daily = dict(_NAV_SEED)   # 用種子初始化
        entry = {}
        if raw_nav  is not None: entry['total_nav_raw']   = raw_nav
        if raw_unit is not None: entry['nav_per_unit_raw'] = raw_unit
        daily[date_full] = entry
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(daily, f, ensure_ascii=False, indent=2)
        print(f'  [NAV] 已存 {date_full}：淨資產={raw_nav} 淨值={raw_unit}')
    except Exception as e:
        print(f'  [警告] 儲存 etf981a_nav_daily.json 失敗：{e}')


def load_etf981a_nav_delta(date_full: str,
                           json_path: str = ETF981A_NAV_JSON) -> dict:
    """讀取前一日 NAV，計算百分比差異。
    回傳 {'total_nav_pct': '+1.2%', 'nav_unit_pct': '-0.5%'}（可能為空值）
    """
    if not os.path.isfile(json_path):
        return {}
    try:
        with open(json_path, encoding='utf-8') as f:
            daily = json.load(f)
        if not daily:
            return {}
        dates = sorted(daily.keys(), reverse=True)
        prev_date = next((d for d in dates if d != date_full), None)
        if not prev_date or date_full not in daily:
            return {}
        curr = daily[date_full]
        prev = daily[prev_date]
        result = {}
        for key, label in [('total_nav_raw','total_nav_pct'), ('nav_per_unit_raw','nav_unit_pct')]:
            c, p = curr.get(key), prev.get(key)
            if c is not None and p and p != 0:
                pct = (c - p) / abs(p) * 100
                sign = '+' if pct >= 0 else ''
                result[label] = f'{sign}{pct:.2f}%'
        return result
    except Exception as e:
        print(f'  [警告] 讀取 etf981a_nav_daily.json 失敗：{e}')
        return {}


def load_futures_codes(ods_path: str = FUTURES_ODS) -> list:
    """讀取個股期貨代號 ODS，回傳股票代號 list（4位數字串）"""
    if not os.path.isfile(ods_path):
        print(f'  [警告] 找不到 {ods_path}')
        return []
    try:
        df = pd.read_excel(ods_path, engine='odf', header=1)
        codes = (df.iloc[:, 2].dropna().astype(str).str.strip().tolist())
        codes = [c for c in codes if c.isdigit() and 4 <= len(c) <= 5]
        print(f'  個股期貨：{len(codes)} 檔')
        return codes
    except Exception as e:
        print(f'  [警告] 讀取個股期貨代號.ods失敗：{e}')
        return []


def load_etf981a_prev(csv_path: str, current_mmdd: str = '', json_path: str = ETF981A_DAILY_JSON):
    """讀取前一個交易日的00981A成分股。優先查 etf981a_daily.json，其次 CSV。
    回傳 (prev_holdings, prev_date_str, names_dict)
    """
    # ── 先從 CSV 建名稱字典 ──────────────────────────────────
    names_dict: dict = {}
    if os.path.isfile(csv_path):
        try:
            _df = pd.read_csv(csv_path, encoding='utf-8-sig')
            names_dict = {
                str(r['股票代號']).strip(): str(r['股票名稱']).strip()
                for _, r in _df.iterrows()
                if str(r.get('股票代號', '')).strip()
            }
        except Exception:
            pass

    current_full = _mmdd_to_full(current_mmdd) if current_mmdd else ''

    # ── 優先：etf981a_daily.json ────────────────────────────
    if os.path.isfile(json_path):
        try:
            with open(json_path, encoding='utf-8') as f:
                daily = json.load(f)
            if daily:
                dates = sorted(daily.keys(), reverse=True)
                for d in dates:
                    if d != current_full:
                        prev_mmdd = '/'.join(d.split('/')[1:])  # YYYY/MM/DD → MM/DD
                        return daily[d], prev_mmdd, names_dict
        except Exception as e:
            print(f'  [警告] 讀取 etf981a_daily.json 失敗：{e}')

    # ── 備用：CSV ────────────────────────────────────────────
    if not os.path.isfile(csv_path):
        return {}, '', names_dict
    try:
        df = pd.read_csv(csv_path, encoding='utf-8-sig')
        if df.empty:
            return {}, '', names_dict
        df['日期'] = pd.to_datetime(df['日期'], format='%Y/%m/%d')
        dates = sorted(df['日期'].unique(), reverse=True)
        prev_date = None
        for d in dates:
            if d.strftime('%m/%d') != current_mmdd:
                prev_date = d
                break
        if prev_date is None and len(dates) >= 2:
            prev_date = dates[1]
        if prev_date is None:
            return {}, '', names_dict
        prev_df = df[df['日期'] == prev_date]
        prev_holdings = {
            str(r['股票代號']).strip(): str(r['持股權重']).strip()
            for _, r in prev_df.iterrows()
        }
        return prev_holdings, prev_date.strftime('%m/%d'), names_dict
    except Exception as e:
        print(f'  [警告] 讀取00981A_投組.csv失敗：{e}')
        return {}, '', names_dict


def scrape_00981a_holdings():
    """爬取00981A最新成分股與申贖摘要。
    回傳 (holdings, data_date, metrics)
      holdings  : {股票代號: '持股權重字串'}，例如 {'7757': '9.80%'}
      data_date : 頁面上傳時間 'MM/DD'，取不到時用查詢日期
      metrics   : {diff_groups, nav_per_unit, total_nav} 格式化後的顯示字串
    """
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    wait   = WebDriverWait(driver, 15)
    holdings: dict = {}
    data_date = ''
    metrics: dict = {}
    try:
        driver.get(ETF981A_URL)
        time.sleep(4)
        today = pd.Timestamp(datetime.today().date())
        _cal = xcals.get_calendar('XTAI')
        _sess = [s.tz_localize(None) for s in
                 _cal.sessions_in_range(today - pd.Timedelta(days=12), today + pd.Timedelta(days=12))]
        _nxt = next((s for s in _sess if s > today), None)            # 次一交易日
        _upto = _nxt if _nxt is not None else today
        # 從「次一交易日」往回查；第一張有效 PCF 即最新（前一營業日上傳、含當日淨值）
        for target in sorted([s for s in _sess if s <= _upto], reverse=True)[:7]:
            roc_str = f'{target.year - 1911}/{target.month:02d}/{target.day:02d}'
            # 輸入民國日期
            try:
                inp = wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='text']")))
                driver.execute_script("arguments[0].value = '';", inp)
                inp.clear()
                inp.send_keys(roc_str)
                time.sleep(0.3)
            except Exception as e:
                print(f'  [00981A] 輸入日期失敗：{e}')
                continue
            # 點查詢
            queried = False
            for by, sel in [
                (By.XPATH, "//button[normalize-space()='查詢']"),
                (By.XPATH, "//input[@value='查詢']"),
                (By.CSS_SELECTOR, 'button.btn-primary'),
            ]:
                try:
                    btn = wait.until(EC.element_to_be_clickable((by, sel)))
                    btn.click()
                    queried = True
                    break
                except Exception:
                    continue
            if not queried:
                continue
            time.sleep(3)
            # 無資料 alert
            try:
                driver.switch_to.alert.accept()
                continue
            except Exception:
                pass
            # 讀取「上傳時間」（格式：115/05/28 → 民國年/月/日）
            try:
                body_text = driver.find_element(By.TAG_NAME, 'body').text
                m = re.search(r'上傳時間[：:]\s*(\d{2,3})/(\d{2})/(\d{2})', body_text)
                if m:
                    ce_year = int(m.group(1)) + 1911
                    data_date = f'{m.group(2)}/{m.group(3)}'        # MM/DD，用於顯示
                    metrics['_date_full'] = f'{ce_year}/{m.group(2)}/{m.group(3)}'  # YYYY/MM/DD，用於 JSON key
            except Exception:
                pass
            # 找股票表格
            tbl = None
            for t in driver.find_elements(By.TAG_NAME, 'table'):
                hdrs = [th.text.strip() for th in t.find_elements(By.TAG_NAME, 'th')]
                if '股票代號' in hdrs and '股票名稱' in hdrs:
                    tbl = t
                    break
            if tbl is None:
                continue
            for row in tbl.find_elements(By.TAG_NAME, 'tr')[1:]:
                cells = [td.text.strip() for td in row.find_elements(By.TAG_NAME, 'td')]
                if len(cells) >= 4 and cells[0]:
                    holdings[cells[0]] = cells[3]
            if holdings:
                if not data_date:
                    data_date = f'{target.month:02d}/{target.day:02d}'
                # 抓摘要指標（跳過股票明細表）
                raw_m: dict = {}
                try:
                    for t2 in driver.find_elements(By.TAG_NAME, 'table'):
                        hdrs2 = [th.text.strip() for th in t2.find_elements(By.TAG_NAME, 'th')]
                        if any(h in ('股票代號', '股票名稱') for h in hdrs2):
                            continue
                        for row2 in t2.find_elements(By.TAG_NAME, 'tr'):
                            cells2 = [c.text.strip() for c in row2.find_elements(By.TAG_NAME, 'td')]
                            if len(cells2) >= 2 and cells2[0]:
                                # 去掉日期前綴（如 "115/05/28 每受益..."）
                                key2 = re.sub(r'^\d{2,3}/\d{2}/\d{2}\s*', '', cells2[0]).strip()
                                raw_m[key2] = cells2[1].strip()
                except Exception:
                    pass
                # 解析 basket 單位數
                basket = 500_000
                for k, v in raw_m.items():
                    if '每現金申購' in k and '基數之受益權單位數' in k:
                        try: basket = int(v.replace(',', ''))
                        except: pass
                # 差異組數
                for k, v in raw_m.items():
                    if '與前日已發行單位差異數' in k:
                        try:
                            diff = int(v.replace(',', ''))
                            g = diff // basket
                            metrics['diff_groups'] = f'{"+" if g >= 0 else ""}{g:,}'
                            metrics['diff_sign']   = 'pos' if g >= 0 else 'neg'
                        except: pass
                        break
                # 淨值
                for k, v in raw_m.items():
                    if '每受益權單位淨資產價值' in k:
                        cleaned = v.replace('NTD', '').replace('NT$', '').strip()
                        metrics['nav_per_unit'] = cleaned
                        try: metrics['nav_per_unit_raw'] = float(cleaned.replace(',', ''))
                        except: pass
                        break
                # 基金總淨資產（億）
                for k, v in raw_m.items():
                    if '基金淨資產' in k or '基金資產淨值' in k or '基金總淨值' in k:
                        try:
                            nav_raw = float(v.replace('NTD', '').replace('NT$', '').replace(',', '').strip())
                            yi = nav_raw / 1e8
                            metrics['total_nav'] = f'{yi:,.0f}億'
                            metrics['total_nav_raw'] = nav_raw
                        except: pass
                        break
                print(f'  [00981A] 取得 {len(holdings)} 筆成分股，截至 {data_date}，metrics={list(metrics.keys())}')
                break
    except Exception as e:
        print(f'  [00981A] 爬取失敗：{e}')
    finally:
        driver.quit()
    return holdings, data_date, metrics


def scrape_upcoming_ir():
    """爬當前視窗法說會，併入累積事件庫(法說會事件庫.json)；庫永不覆寫、只增量合併。
    回傳 (事件庫全部事件, 本次新爬到的 keys)。供分析(含當月已過去場次)與新爬偵測使用。"""
    today_str = date.today().strftime('%Y/%m/%d')

    # 載入既有事件庫
    store_map: dict = {}
    if os.path.exists(IR_EVENTS_JSON):
        try:
            with open(IR_EVENTS_JSON, 'r', encoding='utf-8') as f:
                for e in json.load(f):
                    store_map[(str(e.get('co_code', '')), e.get('date', ''), e.get('time', ''))] = e
        except Exception:
            pass
    _before = len(store_map)

    scraper = MopsConferenceScraper()
    raw = scraper.scrape()

    # 合併：庫裡沒有的 = 本次新爬到；其餘更新內容
    new_keys: set = set()
    for ev in raw:
        key = (str(ev.get('co_code', '')), ev.get('date', ''), ev.get('time', ''))
        if key not in store_map:
            new_keys.add(f"{key[0]}|{key[1]}|{key[2]}")
        store_map[key] = ev

    merged = sorted(store_map.values(), key=lambda e: (e.get('date', ''), e.get('time', '')))

    # 存回事件庫（增量累積）
    try:
        with open(IR_EVENTS_JSON, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f'  [警告] 事件庫儲存失敗：{e}')

    _n_future = sum(1 for e in merged if e.get('date', '') >= today_str)
    print(f'  法說事件庫：{len(merged):,} 筆（庫存 {_before:,} → 本次爬 {len(raw)}、新增 {len(new_keys)}、未來 {_n_future}）')
    return merged, new_keys


# ════════════════════════════════════════════════════════════════════
#  董事會財報分析（由 董事會財務分析.ipynb 移植；本檔為單一可執行來源）
# ════════════════════════════════════════════════════════════════════
import numpy as np

BOARD_PASS_CSV    = os.path.join(BASE, '董事會通過.csv')
BOARD_RESOLVE_CSV = os.path.join(BASE, '董事會決議.csv')
ANNOUNCE_FIN_CSV  = os.path.join(BASE, '公告財報.csv')
AMPM_TRACK_JSON   = os.path.join(BASE, '法說_早上待下午.json')
_an_xtai = xcals.get_calendar('XTAI')
PM_CUTOFF_MIN = 13 * 60 + 30      # 13:30 → 之後算下午

_ACN = {'一':'1','ㄧ':'1','二':'2','三':'3','四':'4','五':'5','六':'6',
        '七':'7','八':'8','九':'9','〇':'0','○':'0','零':'0','０':'0'}
def _an_cn_year(s): return int(''.join(_ACN[c] for c in s if c in _ACN))
_AQ_CN = {'一':'Q1','二':'Q2','三':'Q3','四':'Q4','1':'Q1','2':'Q2','3':'Q3','4':'Q4'}

def _an_extract_roc_year(title, fallback):
    fallback = int(fallback)
    m = re.search(r'\((\d{3})年\)', title)
    if m: return int(m.group(1))
    m = re.search(r'(?<!\d)(\d{3})(?!\d)年', title)
    if m: return int(m.group(1))
    m = re.search(r'(20[012]\d)年', title)
    if m: return int(m.group(1)) - 1911
    _cn3 = r'[一ㄧ二三四五六七八九〇○零０]{3}'
    m = re.search(rf'民國({_cn3})年', title)
    if m: return _an_cn_year(m.group(1))
    _any = r'[一二三四五六七八九〇○零０ㄧ]'
    m = re.search(rf'([一ㄧ]{_any}{_any})年', title)
    if m:
        y = _an_cn_year(m.group(1))
        if 100 <= y <= 130: return y
    return fallback

def _an_extract_period(title):
    m = re.search(r'第([一二三四1234])季', title)
    if m: return _AQ_CN.get(m.group(1))
    m = re.search(r'Q([1234])', title, re.IGNORECASE)
    if m: return f'Q{m.group(1)}'
    if re.search(r'年度|全年|(\d{3})度', title): return '年報'
    if re.search(r'(?<!\d)\d{3}(?!\d)年|民國|[一ㄧ][〇○一]', title): return '年報'
    return '其他'

_AN_EXCLUDE = re.compile(r'主管異動|背書保證|財務預測|財務危機|財測|財務長|財務副|帳務|財物|重編|資金貸與|現金貸')
_AN_INCLUDE = re.compile(r'財務報告|財務報表|財報')

def parse_report_period(title, announce_roc_year):
    t = str(title).replace('\r\n', ' ').replace('\n', ' ')
    if _AN_EXCLUDE.search(t): return (None, None)
    if not _AN_INCLUDE.search(t): return (None, None)
    # 預告開會日期（如「董事會召開日期」「財務報告預計通過董事會日期」「預計召開日期」）非實際通過 → 排除
    # 真正通過的主旨一定有「業經…董事會通過/決議通過」；預告型沒有「業經」
    if '業經' not in t and (re.search(r'召開日期', t) or ('預計' in t and re.search(r'通過|召開|日期', t))):
        return (None, None)
    announce_roc_year = int(announce_roc_year)
    fy = _an_extract_roc_year(t, announce_roc_year)
    period = _an_extract_period(t)
    if abs(fy - announce_roc_year) > 2: fy = announce_roc_year
    if period in ('Q4', '年報') and fy >= announce_roc_year: fy = announce_roc_year - 1
    return (fy, period)

_LC_ANNUAL_FY = {111: (3, 16), 112: (3, 15), 113: (3, 17), 114: (3, 16)}   # 財報民國年 → (月,日)，於隔年
def _an_lc_annual(fy):
    m, d = _LC_ANNUAL_FY.get(int(fy), (3, 16))
    return date(int(fy) + 1912, m, d)

def get_deadline(period, fy, large):
    fy = int(fy); ce = fy + 1911
    if period == 'Q1': return date(ce, 5, 15)
    if period == 'Q2': return date(ce, 8, 14)
    if period == 'Q3': return date(ce, 11, 14)
    if period == 'Q4': return date(ce + 1, 3, 31)
    if period == '年報': return _an_lc_annual(fy) if large else date(ce + 1, 4, 1)
    return None

def _an_roc_to_date(s):
    # 取第一個日期；含 roadshow 區間「115/03/02 至 115/03/05」也只取起始日
    mt = re.search(r'(\d{2,3})/(\d{1,2})/(\d{1,2})', str(s))
    if not mt: return None
    try:
        return date(int(mt.group(1)) + 1911, int(mt.group(2)), int(mt.group(3)))
    except Exception:
        return None

def parse_board_date(subject, filing):
    t = str(subject).replace('\r\n', ' ').replace('\n', ' ')
    m = re.search(r'於(\d{3})年(\d{1,2})月(\d{1,2})日', t)
    if m:
        try:
            dt = date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
            f = _an_roc_to_date(filing)
            if f and abs((dt - f).days) <= 60: return dt
        except Exception:
            pass
    return _an_roc_to_date(filing)

_AQ_T = {'一':'Q1','二':'Q2','三':'Q3','四':'Q4','1':'Q1','2':'Q2','3':'Q3','4':'Q4'}
_OUTLOOK_RE = re.compile(r'(20\d{2}|1[01]\d)?年?第([一二三四1-4])季(?:業績|營運|獲利)?(?:展望|預期|預估|預測)')

def extract_reported_periods(text, conf_date):
    """本場法說『報告』的財報期別 set[(財報民國年, 期別)]，排除展望季。"""
    if text is None or (isinstance(text, float) and pd.isna(text)): return set()
    t = str(text)
    if not t.strip(): return set()
    allp = set()
    for y, q in re.findall(r'(20\d{2})年度?第([一二三四1-4])季', t): allp.add((int(y) - 1911, _AQ_T[q]))
    for y, q in re.findall(r'(1[01]\d)年度?第([一二三四1-4])季', t): allp.add((int(y), _AQ_T[q]))
    for y, q in re.findall(r'(20\d{2})[Qq]([1-4])', t): allp.add((int(y) - 1911, f'Q{q}'))
    for y, q in re.findall(r'(1[01]\d)[Qq]([1-4])', t): allp.add((int(y), f'Q{q}'))
    for y in re.findall(r'(20\d{2})年?(?:度|全年|年報)', t): allp.add((int(y) - 1911, '年報'))
    for y in re.findall(r'(1[01]\d)年度', t): allp.add((int(y), '年報'))
    if conf_date is not None:
        yr = conf_date.year - 1911
        for q in re.findall(r'第([一二三四1-4])季', t):
            qs = _AQ_T[q]; allp.add((yr - 1 if qs == 'Q4' else yr, qs))
    outlook = set()
    for m in _OUTLOOK_RE.finditer(t):
        y = m.group(1); qs = _AQ_T[m.group(2)]
        if y: fy = int(y) - 1911 if int(y) >= 1911 else int(y)
        elif conf_date is not None: fy = conf_date.year - 1911
        else: fy = None
        if fy is not None: outlook.add((fy, qs))
    rep = allp - outlook
    return rep if rep else allp

def _an_ce_date(s):
    try:
        y, m, d = map(int, str(s).split('/')[:3]); return date(y, m, d)
    except Exception:
        return None

def _an_time_min(t):
    m = re.match(r'(\d{1,2}):(\d{2})', str(t).strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None

def _an_td_signed(a, b):
    try:
        a = pd.Timestamp(a); b = pd.Timestamp(b)
        if a > b: return -len(_an_xtai.sessions_in_range(b, a))
        return len(_an_xtai.sessions_in_range(a, b))
    except Exception:
        return np.nan

def load_board_reports(large_cap_int):
    """合併 董事會通過 + 決議 + 公告財報 → 各季各家財報列（每季取最早通過日）。"""
    frames = []
    for p in (BOARD_PASS_CSV, BOARD_RESOLVE_CSV, ANNOUNCE_FIN_CSV):
        if os.path.exists(p):
            frames.append(pd.read_csv(p, encoding='utf-8-sig'))
    b = pd.concat(frames, ignore_index=True)
    b['代號'] = pd.to_numeric(b['代號'], errors='coerce')
    b = b.dropna(subset=['代號']); b['代號'] = b['代號'].astype(int)
    b = b.drop_duplicates(subset=['代號', '日期', '序號', '主旨'])
    per = b.apply(lambda r: parse_report_period(r['主旨'], r['民國年']), axis=1)
    b['財報民國年'] = [p[0] for p in per]; b['期別'] = [p[1] for p in per]
    fr = b.dropna(subset=['期別']).copy()
    fr = fr[fr['期別'] != '其他']
    fr['財報民國年'] = fr['財報民國年'].astype(int)
    fr['大型股'] = fr['代號'].isin(large_cap_int)
    fr['截止日'] = fr.apply(lambda r: get_deadline(r['期別'], r['財報民國年'], r['大型股']), axis=1)
    fr['通過日'] = fr.apply(lambda r: parse_board_date(r['主旨'], r['日期']), axis=1)
    fr = fr.dropna(subset=['通過日', '截止日']).sort_values('通過日')
    season = (fr.groupby(['代號', '財報民國年', '期別'], as_index=False)
                .agg(簡稱=('簡稱', 'first'), 市場別=('市場別', 'first'),
                     大型股=('大型股', 'first'), 截止日=('截止日', 'first'),
                     通過日=('通過日', 'first'), 主旨=('主旨', 'first')))
    return season

def load_ir_pool(upcoming_all):
    """法說池 = 歷史法說會 CSV + 即將法說(upcoming，含時間)，去重。"""
    parts = []
    for folder in (TWSE_PATH, OTC_PATH):
        for f in sorted(glob.glob(os.path.join(folder, '*.csv'))):
            parts.append(pd.read_csv(f, encoding='cp950', encoding_errors='ignore', on_bad_lines='skip'))
    inv = pd.concat(parts, ignore_index=True)
    inv['代號'] = inv['公司代號'].astype(str).str.strip()
    inv['法說日'] = inv['召開法人說明會日期'].apply(_an_roc_to_date)
    inv['時間'] = inv['召開法人說明會時間'].astype(str).apply(_ir_norm_time)
    inv['擇要'] = inv['法人說明會擇要訊息'] if '法人說明會擇要訊息' in inv.columns else ''
    inv['地點'] = inv['召開法人說明會地點'] if '召開法人說明會地點' in inv.columns else ''
    hist = inv[['代號', '法說日', '時間', '擇要', '地點']].dropna(subset=['法說日'])
    up_rows = []
    for ev in (upcoming_all or []):
        code = str(ev.get('co_code', '')).strip()
        d = _an_ce_date(ev.get('date', ''))
        if code and d:
            up_rows.append({'代號': code, '法說日': d,
                            '時間': _ir_norm_time(str(ev.get('time', ''))),
                            '擇要': ev.get('detail', ''), '地點': ev.get('location', '')})
    up = pd.DataFrame(up_rows, columns=['代號', '法說日', '時間', '擇要', '地點'])
    pool = pd.concat([hist, up], ignore_index=True)
    pool = pool.drop_duplicates(subset=['代號', '法說日', '時間'])
    pool['擇要報告期'] = pool.apply(lambda r: extract_reported_periods(r['擇要'], r['法說日']), axis=1)
    return pool

# 主辦券商分類：外資（受邀英文/國外券商）、內資（國內券商）、自辦、其他
_FOREIGN_KW = ('高盛', 'Goldman', '匯豐', '滙豐', 'HSBC', '花旗', 'Citi', '美林', 'Merrill', 'BofA',
               '美銀', 'Bank of America', '摩根士丹利', 'Morgan Stanley', '大摩', '摩根大通',
               'J.P. Morgan', 'JPMorgan', 'JP Morgan', '小摩', '瑞銀', 'UBS', '瑞信', '瑞士信貸',
               'Credit Suisse', '麥格理', 'Macquarie', '野村', 'Nomura', '瑞穗', 'Mizuho',
               'Jefferies', '巴克萊', 'Barclays', '德意志', 'Deutsche', '里昂', 'CLSA', '大和',
               'Daiwa', '法國巴黎', 'BNP', '星展', 'DBS')
_DOMESTIC_KW = ('凱基', 'KGI', '元大', '永豐', '群益', '富邦', '國泰', '統一綜合', '華南永昌', '華南',
                '台新', '兆豐', '中國信託', '中信', '第一金', '日盛', '宏遠', '康和', '大昌', '致和',
                '福邦', '犇亞', '元富', '玉山', '新光', '德信', '大慶', '美好', '亞東', '陽信', '國票', '永昌')

_OVERSEAS_KW = ('香港', '新加坡', '海外', '美國', '紐約', '倫敦', '東京', '日本', '韓國', '首爾',
                '上海', '深圳', '北京', '波士頓', '舊金山', '法蘭克福', '瑞士', '英國',
                'Hong Kong', 'HongKong', 'Singapore', 'New York', 'London', 'Tokyo', 'Seoul',
                'Shanghai', 'Boston', 'San Francisco')

def _broker_type(text, location=''):
    if any(kw in str(location or '') for kw in _OVERSEAS_KW): return '外資'   # 海外舉辦(roadshow)視為外資
    t = str(text or '')
    if any(kw in t for kw in _FOREIGN_KW): return '外資'
    if any(kw in t for kw in _DOMESTIC_KW): return '內資'
    if '受邀' in t or '邀請' in t: return '其他'        # 受邀但券商名未辨識
    if '本公司' in t and re.search(r'法人說明會|法說|財務報告|業績|營運|展望', t): return '自辦'
    return '其他'

def _seg_of(tmin):
    if tmin is None: return None
    return '早上' if tmin < PM_CUTOFF_MIN else '下午'

def _qmatch(qkey, ment):
    """季別比對：同 key 命中；且 Q4 與 年報 視為同一件事（年度結算=第四季）。"""
    if qkey in ment: return True
    y, p = qkey
    if p == '年報' and (y, 'Q4') in ment: return True
    if p == 'Q4'  and (y, '年報') in ment: return True
    return False

def build_report_df(upcoming_all, large_cap_int):
    """各季各家：以『法說當下已通過的最近一季』互斥歸季（含時間上下限）；
    每季依（時段 早上/下午 × 主辦 外資/內資/自辦/其他）各取第一場 → 法說清單。"""
    season = load_board_reports(large_cap_int)
    pool = load_ir_pool(upcoming_all)
    pool_by_code = {c: g.sort_values('法說日') for c, g in pool.groupby('代號')}
    recs = {}
    for code, comp in season.groupby(season['代號'].astype(str)):
        q = comp.sort_values('通過日').reset_index()
        q_keys = list(zip(q['財報民國年'], q['期別']))
        q_pass = [pd.Timestamp(d) for d in q['通過日']]
        bucket = {i: [] for i in range(len(q))}
        g = pool_by_code.get(code)
        if g is not None:
            for _, ir in g.iterrows():
                T = pd.Timestamp(ir['法說日']); ment = ir['擇要報告期']
                passed = [j for j in range(len(q)) if q_pass[j] <= T]
                future = [j for j in range(len(q)) if q_pass[j] > T]
                jp = passed[-1] if passed else None
                jn = future[0] if future else None
                # 時間上下限：避免缺董事會資料時，舊法說亂掛到最早一季
                if jp is not None and _qmatch(q_keys[jp], ment): tgt, typ = jp, '後續法說'
                elif jn is not None and _qmatch(q_keys[jn], ment): tgt, typ = jn, '前置法說'
                elif jp is not None and (T - q_pass[jp]).days <= 120: tgt, typ = jp, '後續法說'
                elif jn is not None and (q_pass[jn] - T).days <= 90: tgt, typ = jn, '前置法說'
                else: tgt = None
                if tgt is not None:
                    bucket[tgt].append((T, ir['時間'], ir['擇要'], ir['地點'], typ))
        def _rank(c):
            tm = _an_time_min(c[1])
            return (c[0], tm if tm is not None else 9999)
        def _mk(c):
            T, tm, detail, location, typ = c
            return {'日期': T.date().isoformat(), '時間': tm, '時段': _seg_of(_an_time_min(tm)),
                    '主辦': _broker_type(detail, location), '類型': typ,
                    '擇要': (str(detail) if detail is not None else '')}
        for pos in range(len(q)):
            idx = q.loc[pos, 'index']
            cands = sorted(bucket[pos], key=_rank)
            # 主場 = 第一場「非外資」；整季只有外資時退用第一場
            non_foreign = [c for c in cands if _broker_type(c[2], c[3]) != '外資']
            main_c = non_foreign[0] if non_foreign else (cands[0] if cands else None)
            sessions = []
            if main_c is not None:
                _mr = _rank(main_c)
                # 領先外資：主場之前才有的第一場外資（早上外資先開、之後才有非外資）
                lead_c = next((c for c in cands if _broker_type(c[2], c[3]) == '外資' and _rank(c) < _mr), None)
                if lead_c is not None: sessions.append(_mk(lead_c))
                sessions.append(_mk(main_c))
            morn = [s for s in sessions if s['時段'] == '早上']
            aft  = [s for s in sessions if s['時段'] == '下午']
            first_m = morn[0] if morn else None
            first_a = aft[0] if aft else None
            primary = _mk(main_c) if main_c is not None else None
            recs[idx] = dict(
                早上法說日=first_m['日期'] if first_m else None,
                早上法說時間=first_m['時間'] if first_m else None,
                下午法說日=first_a['日期'] if first_a else None,
                下午法說時間=first_a['時間'] if first_a else None,
                首次法說日=(pd.Timestamp(primary['日期']).date() if primary else None),
                法說時間=primary['時間'] if primary else None,
                法說擇要=primary['擇要'] if primary else None,
                法說類型=primary['類型'] if primary else None,
                法說時段=primary['時段'] if primary else None,
                法說清單=sessions,
            )
    for col in ['早上法說日', '早上法說時間', '下午法說日', '下午法說時間',
                '首次法說日', '法說時間', '法說擇要', '法說類型', '法說時段', '法說清單']:
        season[col] = season.index.map(lambda i: recs.get(i, {}).get(col))
    season['距截止交易日'] = season.apply(lambda r: _an_td_signed(r['通過日'], r['截止日']), axis=1)
    season['通過到法說交易日'] = season.apply(
        lambda r: _an_td_signed(r['通過日'], r['首次法說日']) if pd.notna(r['首次法說日']) else None, axis=1)
    season['法說到截止交易日'] = season.apply(
        lambda r: _an_td_signed(r['首次法說日'], r['截止日']) if pd.notna(r['首次法說日']) else None, axis=1)
    season = season[season['距截止交易日'].between(-100, 100)].copy()
    return season

def write_ampm_tracking(df, etf_codes, futures_codes):
    """當前申報季：早上已開、第一場下午還沒開的公司 → JSON（全部公司）。"""
    cy = date.today().year
    valid = {(cy - 1, '年報'), (cy - 1, 'Q4'), (cy, 'Q1'), (cy, 'Q2'), (cy, 'Q3')}
    etf_set = {str(c) for c in (etf_codes or [])}
    fut_set = {str(c) for c in (futures_codes or [])}
    out = []
    for _, r in df.iterrows():
        if (int(r['財報民國年']) + 1911, r['期別']) not in valid:
            continue
        if pd.notna(r['早上法說日']) and pd.isna(r['下午法說日']):
            code = str(r['代號'])
            out.append({
                '代號': code, '名稱': r['簡稱'],
                '財報期': f"{int(r['財報民國年']) + 1911} {r['期別']}",
                '早上法說日': str(r['早上法說日']), '早上法說時間': r['早上法說時間'],
                '是否成分股': code in etf_set, '是否有個股期': code in fut_set,
            })
    out.sort(key=lambda x: x['早上法說日'], reverse=True)
    with open(AMPM_TRACK_JSON, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f'  早上待下午追蹤：{len(out)} 家 → {os.path.basename(AMPM_TRACK_JSON)}')
    return out


def main():
    # ── 1. 讀實收資本額（大型股名單）──
    print('讀取實收資本額.csv…')
    large_cap_codes = set()
    with open(CAP_CSV, 'rb') as f:
        cap_text = f.read().decode('utf-16')
    for line in cap_text.splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        code = parts[0].strip().split()[0]
        try:
            if float(parts[1].strip().replace(',', '')) >= LARGE_CAP_THRESHOLD:
                large_cap_codes.add(code)
        except Exception:
            pass
    large_cap_int = {int(c) for c in large_cap_codes if str(c).isdigit()}
    print(f'  大型股(>=100億)：{len(large_cap_codes)} 家')

    # ── 2. 啟動即時爬蟲：00981A 成分股 ──
    print('爬取 00981A 成分股…')
    etf981a_holdings, etf981a_date, etf981a_metrics = {}, '', {}
    try:
        etf981a_holdings, etf981a_date, etf981a_metrics = scrape_00981a_holdings()
        if etf981a_holdings:
            _full = etf981a_metrics.pop('_date_full', '') or _mmdd_to_full(etf981a_date)
            save_etf981a_daily(etf981a_holdings, _full, is_full_date=True)
            save_etf981a_nav(etf981a_metrics, _full)
    except Exception as _e:
        print(f'  [警告] 00981A 爬取失敗：{_e}')
    etf981a_nav_delta = {}
    try:
        etf981a_nav_delta = load_etf981a_nav_delta(_mmdd_to_full(etf981a_date) if etf981a_date else '')
    except Exception as _e:
        print(f'  [警告] 計算NAV差異失敗：{_e}')
    etf981a_prev, etf981a_prev_date, etf981a_names = {}, '', {}
    try:
        etf981a_prev, etf981a_prev_date, etf981a_names = load_etf981a_prev(ETF981A_CSV, etf981a_date)
        print(f'  00981A 前日基準：{etf981a_prev_date}（{len(etf981a_prev)} 檔）')
    except Exception as _e:
        print(f'  [警告] 讀取00981A前日資料失敗：{_e}')

    # ── 3. 啟動即時爬蟲：即將法說（每次啟動都上去爬，看是否有新資料）──
    print('爬取近期法說會（MOPS）…')
    upcoming_all, _new_ev_keys = [], set()
    try:
        upcoming_all, _new_ev_keys = scrape_upcoming_ir()
    except Exception as _e:
        print(f'  [警告] 法說爬取失敗：{_e}')

    # ── 4. 讀個股期貨清單 ──
    print('讀取個股期貨代號…')
    futures_codes = load_futures_codes()

    # ── 5. 分析：合併董事會通過/決議/公告財報 + 法說(含即將法說) → 財報_df ──
    print('分析 財報_df（含即將法說 + 早上/下午）…')
    df = build_report_df(upcoming_all, large_cap_int)
    df.to_pickle(PKL_PATH)
    print(f'  財報_df：{len(df):,} 列')
    write_ampm_tracking(df, etf981a_holdings.keys(), futures_codes)

    # ── 欄位對應 ────────────────────────────────────────────────
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

    # 財報年度 = 截止日所在年份（JS 端對 Q4/年報 顯示時會自動 -1）
    out['財報年度'] = pd.to_datetime(df['截止日']).dt.year
    out['財報錨點'] = pd.to_datetime(df['截止日'])

    # 網站慣例：正=截止後，負=截止前
    # 財報_df 慣例：正=截止前（剩餘），負=截止後（逾期）→ 取反
    # 前置法說會用法說到截止的交易日；其餘用通過到截止的交易日
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

    # 法說會日期（董事會通過後第一場）與主旨
    out['法說日'] = (
        pd.to_datetime(df['首次法說日'], errors='coerce')
        if '首次法說日' in df.columns else pd.NaT
    )
    out['主旨'] = df['主旨'].astype(str)
    # 董事會通過 → 首次法說會 相差日曆天數
    diff = (out['法說日'] - out['日期']).dt.days
    out['通過到法說天數'] = diff.where(diff.notna(), other=None)
    # 首次法說 → 截止日 交易日數（正=截止前，負=截止後）
    out['法說到截止交易日'] = (
        df['法說到截止交易日'] if '法說到截止交易日' in df.columns else None
    )

    # 早上/下午場（每季 first-morning + first-afternoon；primary 已優先下午）
    out['法說時段']   = df['法說時段']   if '法說時段'   in df.columns else None
    out['早上法說日'] = pd.to_datetime(df['早上法說日'], errors='coerce') if '早上法說日' in df.columns else pd.NaT
    out['早上法說時間'] = df['早上法說時間'] if '早上法說時間' in df.columns else None
    out['下午法說日'] = pd.to_datetime(df['下午法說日'], errors='coerce') if '下午法說日' in df.columns else pd.NaT
    out['下午法說時間'] = df['下午法說時間'] if '下午法說時間' in df.columns else None
    # 法說清單（每季 時段×主辦 各第一場）；網站用版去掉擇要、用短鍵以控制體積（JS 端還原）
    def _slim_sessions(lst):
        if not isinstance(lst, list): return []
        return [{'d': s.get('日期'), 't': s.get('時間'), 's': s.get('時段'),
                 'o': s.get('主辦'), 'y': s.get('類型')} for s in lst]
    out['法說清單'] = df['法說清單'].apply(_slim_sessions) if '法說清單' in df.columns else [[] for _ in range(len(df))]

    print(f'  載入 {len(out):,} 筆')

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
             '距截止日交易日', '法說到截止交易日', '市場', '說明財報期', '法人說明會擇要訊息',
             '法說時段', '早上法說日', '早上法說時間', '下午法說日', '下午法說時間', '法說清單']
    ex = out[[c for c in _cols if c in out.columns]].copy()
    ex['公司代號'] = ex['公司代號'].astype(str).str.strip()
    ex['日期']    = ex['日期'].dt.strftime('%Y-%m-%d')
    ex['法說日']  = ex['法說日'].apply(
        lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None
    )
    ex['財報錨點'] = ex['財報錨點'].apply(
        lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None
    )
    for _dc in ['早上法說日', '下午法說日']:
        if _dc in ex.columns:
            ex[_dc] = ex[_dc].apply(lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None)

    # 只保留有產業分類的公司（移除未分類興櫃/冷門股），縮小資料量
    ex = ex[ex['公司代號'].isin(firm_info)].copy()
    print(f'  RECORDS（已分類）：{len(ex):,} 筆')
    # 短鍵序列化以縮小 index.html（JS 端 _rh() 還原成中文鍵）
    _KEYMAP = {'公司代號':'A','公司名稱':'B','日期':'C','法說日':'D','通過到法說天數':'E','主旨':'F',
               '召開法人說明會時間':'G','財報期別':'H','財報年度':'I','財報錨點':'J','前後':'K',
               '距截止日交易日':'L','法說到截止交易日':'M','市場':'N','說明財報期':'O','法人說明會擇要訊息':'P',
               '法說時段':'Q','早上法說日':'R','早上法說時間':'S','下午法說日':'T','下午法說時間':'U','法說清單':'V',
               '即將法說日':'W','即將法說時間':'X','即將法說地點':'Y','即將法說新':'Z'}

    data_json       = json.dumps(ex.rename(columns=_KEYMAP).to_dict(orient='records'), ensure_ascii=False)
    firm_info_json  = json.dumps(firm_info, ensure_ascii=False)
    industries_json = json.dumps(industries, ensure_ascii=False)
    large_cap_json  = json.dumps(sorted(large_cap_codes), ensure_ascii=False)

    # 已通過但尚未舉行法說會的公司（即時資料只追「當下這一季」，依日期切換）
    #   4/1–6/30→Q1、7/1–9/30→Q2、10/1–12/31→Q3、1/1–3/31→前一年年報/Q4
    _td = date.today()
    _y = _td.year
    if   4  <= _td.month <= 6:  _valid_periods = {f'{_y} Q1'}
    elif 7  <= _td.month <= 9:  _valid_periods = {f'{_y} Q2'}
    elif 10 <= _td.month <= 12: _valid_periods = {f'{_y} Q3'}
    else:                       _valid_periods = {f'{_y - 1} 年報', f'{_y - 1} Q4'}
    _td_str = _td.strftime('%Y-%m-%d')
    _is_cur = ex['說明財報期'].isin(_valid_periods) if '說明財報期' in ex.columns else pd.Series(False, index=ex.index)
    cur = ex[_is_cur].copy()
    # 待法說：當季完全沒法說（連排定都沒有）
    pending_no_ir_df = cur[cur['法說日'].isna()].sort_values('日期', ascending=False).copy()
    # 即將法說：當季法說已排定、今天(含)之後還沒開（法說日 >= 今天）
    _future = cur['法說日'].notna() & (cur['法說日'].astype(str) >= _td_str)
    pending_has_ir_df = cur[_future].sort_values('法說日').copy()

    # 即將法說詳情：法說日/時間用分析結果；地點、是否新爬從 upcoming 補
    upcoming_by_code: dict = {}
    for _ev in upcoming_all:
        _c = str(_ev.get('co_code', '')).strip()
        if _c:
            upcoming_by_code.setdefault(_c, []).append(_ev)
    def _up_match(code, dt):
        for ev in upcoming_by_code.get(str(code), []):
            if str(ev.get('date', '')).replace('/', '-') == str(dt):
                return ev
        return {}
    if not pending_has_ir_df.empty:
        pending_has_ir_df['即將法說日']   = pending_has_ir_df['法說日']
        pending_has_ir_df['即將法說時間'] = pending_has_ir_df['召開法人說明會時間'].fillna('')
        _evs = [_up_match(c, d) for c, d in zip(pending_has_ir_df['公司代號'], pending_has_ir_df['法說日'])]
        pending_has_ir_df['即將法說地點'] = [ev.get('location', '') for ev in _evs]
        pending_has_ir_df['即將法說新']   = [
            f"{c}|{ev.get('date','')}|{ev.get('time','')}" in _new_ev_keys
            for c, ev in zip(pending_has_ir_df['公司代號'], _evs)]

    print(f'  當季 {sorted(_valid_periods)}：待法說 {len(pending_no_ir_df):,} 筆　即將法說 {len(pending_has_ir_df):,} 筆')

    def _df_to_json(df):
        df = df.rename(columns=_KEYMAP)
        return json.dumps(df.where(df.notna(), other=None).to_dict(orient='records'), ensure_ascii=False)

    pending_ir_json  = _df_to_json(pending_no_ir_df)
    upcoming_ir_json = _df_to_json(pending_has_ir_df)

    html = (HTML_TEMPLATE
            .replace('__DATA__', data_json)
            .replace('__FIRM_INFO__', firm_info_json)
            .replace('__INDUSTRIES__', industries_json)
            .replace('__LARGE_CAP__', large_cap_json)
            .replace('__PENDING_IR__', pending_ir_json)
            .replace('__UPCOMING_IR__', upcoming_ir_json)
            .replace('__ETF981A__', json.dumps(etf981a_holdings, ensure_ascii=False))
            .replace('__ETF981A_DATE__', etf981a_date)
            .replace('__ETF981A_METRICS__', json.dumps(etf981a_metrics, ensure_ascii=False))
            .replace('__ETF981A_PREV__', json.dumps(etf981a_prev, ensure_ascii=False))
            .replace('__ETF981A_PREV_DATE__', etf981a_prev_date)
            .replace('__ETF981A_NAMES__', json.dumps(etf981a_names, ensure_ascii=False))
            .replace('__FUTURES__', json.dumps(futures_codes, ensure_ascii=False))
            .replace('__ETF981A_NAV_DELTA__', json.dumps(etf981a_nav_delta, ensure_ascii=False)))
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
  padding:10px 12px 10px 16px;margin:3px 0;
  color:#93c5fd;font-size:14px;cursor:pointer;
  border-radius:10px;display:flex;align-items:center;gap:6px;
  transition:.15s;border-left:3px solid transparent;
  line-height:1.5;
}
.ci:hover{background:#112233;color:#bfdbfe;}
.ci.active{background:#1e3a5f;border-left-color:#3b82f6;color:#fff;}
.ci-code{font-weight:800;font-size:14px;min-width:38px;}
.ci-name{font-size:12px;color:#4b7aa8;font-weight:600;}
.ci.active .ci-name{color:#93c5fd;}

/* ── MAIN ── */
#main{flex:1;overflow-y:auto;padding:48px 56px;transition:padding .25s;}
body.sb-off #main{padding-left:76px;}
#main::-webkit-scrollbar{width:5px;}
#main::-webkit-scrollbar-thumb{background:#bfdbfe;border-radius:5px;}

/* home grid */
#home{display:block;}
#home-title{font-size:28px;font-weight:900;color:#1e3a5f;margin-bottom:10px;line-height:1.4;}
#home-sub{font-size:15px;color:#6b8fc7;font-weight:700;margin-bottom:32px;line-height:1.6;}

/* 00981A info card */
#etf981a-card{
  display:none;margin-bottom:24px;
  background:#eef4ff;
  border:2px solid #bfdbfe;
  border-radius:16px;padding:16px 22px;
  box-shadow:0 2px 10px rgba(59,130,246,.08);
  font-family:'Nunito',sans-serif;
}
#etf981a-card.visible{display:block;}
#etf981a-card-head{
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:14px;
}
#etf981a-card-head .title{font-size:14px;font-weight:900;color:#1e3a5f;letter-spacing:.3px;}
#etf981a-card-head .date-tag{font-size:11px;color:#6b8fc7;font-weight:700;
  background:rgba(59,130,246,.08);padding:3px 10px;border-radius:8px;}
#etf981a-metrics{display:flex;gap:12px;}
.etf-metric{
  flex:1;background:#1e3a5f;border-radius:12px;
  padding:10px 14px;text-align:center;border:1px solid #2d5a8a;
}
.etf-metric .label{font-size:10px;color:#93c5fd;font-weight:700;margin-bottom:6px;letter-spacing:.3px;}
.etf-metric .value{font-size:20px;font-weight:900;line-height:1;color:#fff;}
.etf-metric .unit{font-size:10px;color:#93c5fd;font-weight:700;margin-top:3px;}
.etf-metric .value.pos{color:#f87171;}
.etf-metric .value.neg{color:#06402b;}
.etf-metric .value.neutral{color:#fff;}
.etf-metric .delta{font-size:10px;font-weight:800;margin-top:2px;line-height:1;}
.etf-metric .delta.up{color:#f87171;}
.etf-metric .delta.dn{color:#06402b;}
#etf981a-toggle{margin-top:12px;text-align:center;cursor:pointer;
  font-size:11px;color:#2563eb;font-weight:700;letter-spacing:.3px;
  padding:6px;border-radius:8px;border:1px solid #dbeafe;
  background:#fff;transition:background .15s;user-select:none;}
#etf981a-toggle:hover{background:#eff6ff;}
#etf981a-toggle .arrow{display:inline-block;transition:transform .2s;margin-left:4px;}
#etf981a-toggle.open .arrow{transform:rotate(180deg);}
#etf981a-diff{display:none;margin-top:12px;border-top:1px solid rgba(0,0,0,.1);padding-top:12px;}
#etf981a-diff-title{font-size:11px;color:#1e293b;font-weight:700;margin-bottom:8px;}
#etf981a-diff table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px;}
#etf981a-diff th{text-align:left;padding:8px 12px;color:#1e293b;font-size:10px;font-weight:700;
  border-bottom:1px solid rgba(0,0,0,.1);white-space:nowrap;background:#f8fafc;}
#etf981a-diff td{padding:8px 12px;border-bottom:1px solid rgba(0,0,0,.06);
  vertical-align:middle;white-space:nowrap;color:#1e293b;background:#fff;}
#etf981a-diff tr:last-child td{border-bottom:none;}
#etf981a-diff .diff-type{font-weight:900;font-size:14px;text-align:center;}
#etf981a-diff .diff-code{font-weight:900;}
#etf981a-diff .diff-name{opacity:.9;}
#etf981a-diff .diff-w{font-weight:700;font-family:monospace;font-size:11px;}
#etf981a-diff .diff-arrow{color:#64748b;font-size:10px;}

/* deadline timeline */
#dl-section{margin-bottom:40px;}
#dl-title{font-size:14px;font-weight:800;color:#1e3a5f;margin-bottom:16px;display:flex;align-items:center;gap:6px;}
#dl-row{display:flex;gap:12px;flex-wrap:wrap;}
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
  gap:20px;
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
  display:flex;align-items:center;gap:14px;
  padding:20px 24px;cursor:pointer;
  transition:background .15s;
}
.ind-card-head:hover{background:#f0f6ff;}
.ind-card.open .ind-card-head{background:#eff6ff;border-bottom:2px solid #dbeafe;}
.ic-emoji{font-size:28px;flex-shrink:0;}
.ic-name{font-size:16px;font-weight:800;color:#1e3a5f;flex:1;line-height:1.4;}
.ic-cnt{font-size:12px;font-weight:700;color:#fff;background:#002366;padding:3px 12px;border-radius:10px;flex-shrink:0;}
.ic-arrow{font-size:12px;color:#93c5fd;flex-shrink:0;transition:transform .2s;}
.ind-card.open .ic-arrow{transform:rotate(180deg);}
.ind-card-body{display:none;padding:18px 22px;flex-wrap:wrap;gap:10px;}
.ind-card.open .ind-card-body{display:flex;}
.co-chip{
  padding:7px 16px;border-radius:10px;
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

/* ── Floating shortcut buttons ── */
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
#upcoming-btn{
  position:fixed;top:14px;right:200px;z-index:999;
  padding:8px 16px;border-radius:12px;
  background:#2A5470;border:none;color:#B0E2FF;
  font-size:13px;font-weight:800;cursor:pointer;
  display:flex;align-items:center;gap:8px;
  box-shadow:0 2px 10px rgba(0,0,0,.3);
  font-family:'Nunito',sans-serif;transition:.2s;
}
#upcoming-btn:hover{background:#1e3a5f;color:#bfdbfe;}
#upcoming-cnt{background:#16a34a;color:#fff;border-radius:8px;padding:1px 8px;font-size:11px;font-weight:900;}

/* ── IR Board pages ── */
.ir-board{
  display:none;position:fixed;inset:0;z-index:900;
  flex-direction:column;overflow:hidden;
  font-family:'Nunito',sans-serif;
}
.ir-board.visible{display:flex;}
.ir-board-head{
  flex-shrink:0;
  padding:12px 24px;display:flex;align-items:center;gap:12px;
}
.ir-board-back{
  background:rgba(255,255,255,.15);border:none;cursor:pointer;
  font-size:13px;font-weight:800;padding:6px 14px;border-radius:10px;
  font-family:'Nunito',sans-serif;white-space:nowrap;transition:.15s;
}
.ir-board-back:hover{background:rgba(255,255,255,.28);}
.ir-board-title{font-size:15px;font-weight:900;flex:1;text-align:center;}
.ir-board-spacer{width:72px;}
.ir-board-search{flex-shrink:0;padding:10px 24px 8px;}
.ir-board-search input{
  width:100%;padding:9px 16px;border-radius:12px;
  background:rgba(255,255,255,.12);border:2px solid rgba(255,255,255,.2);
  color:#f1f5f9;font-size:14px;font-family:'Nunito',sans-serif;outline:none;box-sizing:border-box;
}
.ir-board-search input::placeholder{color:rgba(255,255,255,.45);}
.ir-board-search input:focus{border-color:rgba(255,255,255,.4);}
.ir-board-scroll{flex:1;overflow-y:auto;}
.ir-board-scroll::-webkit-scrollbar{width:5px;}
.ir-board-scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);border-radius:5px;}
.ir-board-body{padding:8px 24px 36px;}
.ir-board-body table{width:100%;border-collapse:collapse;}
#upcoming-board{background:linear-gradient(180deg,#1d3f5c 0%,#16304a 100%);}
#upcoming-board .ir-board-head{background:#2A5470;}
#upcoming-board .ir-board-back{color:#B0E2FF;}
#upcoming-board .ir-board-title{color:#B0E2FF;}
#upcoming-board #u-meta{color:#93c5fd;}
#pending-board{background:linear-gradient(180deg,#0d2137 0%,#122840 100%);}
#pending-board .ir-board-head{background:#0d2137;}
#pending-board .ir-board-back{color:#60a5fa;}
#pending-board .ir-board-title{color:#60a5fa;}
#pending-board #p-meta{color:#93c5fd;}

/* Search bar in sections */
.panel-search-bar{padding:10px 28px 0;}
.panel-search-bar input{
  width:100%;padding:8px 14px;border:2px solid #93c5fd;border-radius:12px;
  background:#fff;color:#1e3a5f;font-size:14px;font-family:'Nunito',sans-serif;outline:none;
  transition:.2s;box-sizing:border-box;
}
.panel-search-bar input:focus{border-color:#2A5470;}
.panel-search-bar input::placeholder{color:#93c5fd;}

/* Tables */
#p-meta,#u-meta{font-size:14px;font-weight:700;margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;}
#u-meta{color:#2A5470;}
#p-meta{color:#6b8fc7;}
#ptable{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;background:#fff;
  border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(59,130,246,.1);}
#ptable th{
  padding:16px 22px;background:#f8fafc;color:#1e293b;
  font-size:12px;font-weight:600;letter-spacing:0.5px;text-align:left;
  cursor:pointer;user-select:none;white-space:nowrap;
  line-height:1.6;border-bottom:2px solid #e2e8f0;
}
#ptable th:hover{background:#f1f5f9;color:#0f172a;}
#ptable th.asc::after{content:' ▲';}
#ptable th.desc::after{content:' ▼';}
#ptable td{padding:14px 22px;color:#1e293b;border-bottom:1px solid #e2e8f0;line-height:1.8;font-weight:500;letter-spacing:0.3px;}
#ptable tr:last-child td{border-bottom:none;}
#ptable tr:hover td{background:#f8fafc !important;}
#utable{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;background:#fff;
  border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(42,84,112,.1);}
#utable th{
  padding:16px 22px;background:#f8fafc;color:#1e293b;
  font-size:12px;font-weight:600;letter-spacing:0.5px;text-align:left;
  cursor:pointer;user-select:none;white-space:nowrap;
  line-height:1.6;border-bottom:2px solid #e2e8f0;
}
#utable th:hover{background:#f1f5f9;color:#0f172a;}
#utable th.asc::after{content:' ▲';}
#utable th.desc::after{content:' ▼';}
#utable td{padding:14px 22px;color:#1e293b;border-bottom:1px solid #e2e8f0;line-height:1.8;font-weight:500;letter-spacing:0.3px;}
#utable tr:last-child td{border-bottom:none;}
#utable tr:hover td{background:#f8fafc !important;}
.new-badge{
  display:inline-block;background:#ef4444;color:#fff;
  font-size:10px;font-weight:900;letter-spacing:.3px;
  padding:1px 6px;border-radius:6px;margin-left:5px;
  vertical-align:middle;line-height:1.5;
}

.etf981a-code{font-weight:900;}
.etf981a-weight{display:block;font-size:10px;color:#f97316;font-weight:700;margin-top:1px;line-height:1.3;}
#p-meta,#u-meta{display:flex;justify-content:space-between;align-items:center;}
.etf981a-label{font-size:11px;color:#f97316;font-weight:700;}
.table-legend{display:flex;gap:24px;margin-bottom:16px;padding:10px 0;flex-wrap:wrap;}
.legend-item{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:700;}
.legend-dot{width:16px;height:16px;border-radius:4px;flex-shrink:0;}
.legend-orange{background:#fff5ee;border:1.5px solid #f97316;}
.legend-blue{background:#e0f2fe;border:1.5px solid #0369a1;}
.legend-text{color:#6b8fc7;}
.legend-green{background:#ecfdf5;border:1.5px solid #059669;}
.etf981a-bar{display:none;flex-wrap:wrap;align-items:center;gap:6px;
  margin-bottom:12px;padding:8px 12px;border-radius:10px;
  background:rgba(249,115,22,.08);border:1px solid rgba(249,115,22,.2);}
.etf981a-bar-lbl{font-size:11px;font-weight:800;color:#fb923c;white-space:nowrap;margin-right:2px;}
.etf981a-chip{font-size:12px;font-weight:900;color:#f97316;
  background:rgba(249,115,22,.15);border:1px solid rgba(249,115,22,.35);
  padding:2px 9px;border-radius:6px;white-space:nowrap;cursor:default;}

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
#qs{display:flex;flex-direction:column;gap:28px;}
.qc{background:#fff;border-radius:18px;box-shadow:0 4px 18px rgba(59,130,246,.1);overflow:hidden;border:2px solid #dbeafe;transition:.2s;}
.qc:hover{box-shadow:0 6px 24px rgba(59,130,246,.18);}
.qh{padding:18px 28px;font-weight:900;font-size:18px;color:#1e3a5f;display:flex;align-items:center;gap:10px;background:linear-gradient(90deg,#eff6ff,#fff);border-bottom:2px solid #dbeafe;line-height:1.6;}
.qh-icon{font-size:22px;}
.qbody{padding:22px 28px 26px;display:flex;flex-direction:column;gap:16px;line-height:1.7;}
.qcol{flex:1;min-width:200px;}
.qst{font-size:12px;font-weight:800;letter-spacing:.3px;margin-bottom:10px;padding:5px 14px;border-radius:20px;display:inline-flex;align-items:center;gap:5px;}
.st-b{background:#fef3c7;color:#b45309;border:1.5px solid #fcd34d;}
.st-a{background:#d1fae5;color:#065f46;border:1.5px solid #6ee7b7;}
.st-s{background:#dbeafe;color:#1d4ed8;border:1.5px solid #93c5fd;}

table{width:100%;border-collapse:separate;border-spacing:0;font-size:14px;background:#fff;}
th{text-align:left;padding:14px 18px;color:#1e293b;font-size:12px;font-weight:800;border-bottom:2px solid #e2e8f0;line-height:1.6;background:#f8fafc;}
td{padding:12px 18px;color:#1e293b;border-bottom:1px solid #e2e8f0;font-size:14px;line-height:1.8;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#f8fafc;}
.dn{color:#b45309;font-weight:800;font-size:15px;}
.dp{color:#059669;font-weight:800;font-size:15px;}
.dz{color:#1d4ed8;font-weight:800;font-size:15px;}
.empty{color:#93c5fd;font-size:13px;font-style:italic;padding:8px 0;font-weight:700;}
</style>
</head>
<body>
<button id="sb-toggle" onclick="toggleSb()" title="展開/收合側欄">☰</button>
<button id="upcoming-btn" onclick="openIRBoard('upcoming')">📅 即將法說 <span id="upcoming-cnt">0</span></button>
<button id="pending-btn" onclick="openIRBoard('pending')">📋 待法說 <span id="pending-cnt">0</span></button>
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
    <div id="etf981a-card">
      <div id="etf981a-card-head">
        <span class="title">📈 00981A 統一台股增長主動式ETF基金</span>
        <span class="date-tag" id="etf981a-date-tag"></span>
      </div>
      <div id="etf981a-metrics">
        <div class="etf-metric">
          <div class="label">🔄 差異組數</div>
          <div class="value" id="etf-diff">—</div>
          <div class="unit">組</div>
        </div>
        <div class="etf-metric">
          <div class="label">💰 基金淨資產</div>
          <div class="value neutral" id="etf-nav-total">—</div>
          <div class="delta" id="etf-nav-total-delta"></div>
          <div class="unit">元</div>
        </div>
        <div class="etf-metric">
          <div class="label">📊 淨值</div>
          <div class="value neutral" id="etf-nav-unit">—</div>
          <div class="delta" id="etf-nav-unit-delta"></div>
          <div class="unit">NT$/unit</div>
        </div>
      </div>
      <div id="etf981a-toggle" onclick="toggleEtfDiff()" style="display:none">
        📋 成分股差異 <span id="etf981a-diff-cnt"></span><span class="arrow">▾</span>
      </div>
      <div id="etf981a-diff">
        <div id="etf981a-diff-title">與前日成分股差異 <span id="etf981a-prev-date-label" style="opacity:.65"></span></div>
        <table><thead><tr>
          <th style="text-align:center">變動</th>
          <th>代號</th><th>名稱</th><th>前日權重</th><th style="text-align:center">差異</th><th>今日權重</th>
        </tr></thead><tbody id="etf981a-diff-body"></tbody></table>
      </div>
    </div>
    <div id="dl-section">
      <div id="dl-title">📅 台股財報截止日</div>
      <div id="dl-row"></div>
    </div>
    <div id="ind-grid"></div>

  </div>

  <div id="upcoming-board" class="ir-board">
    <div class="ir-board-head">
      <button class="ir-board-back" onclick="closeIRBoard()">← 返回</button>
      <span class="ir-board-title">📅 即將舉行法說會</span>
      <div class="ir-board-spacer"></div>
    </div>
    <div class="ir-board-search">
      <input id="upcoming-search" type="text" placeholder="🔍 搜尋代號或名稱…" oninput="renderUpcoming()">
    </div>
    <div class="ir-board-scroll">
      <div class="ir-board-body">
        <div id="u-meta" style="font-size:12px;font-weight:700;margin-bottom:10px;"></div>
        <div id="u-981a-bar" class="etf981a-bar"></div>
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
  </div>

  <div id="pending-board" class="ir-board">
    <div class="ir-board-head">
      <button class="ir-board-back" onclick="closeIRBoard()">← 返回</button>
      <span class="ir-board-title">📋 尚未舉行法說會</span>
      <div class="ir-board-spacer"></div>
    </div>
    <div class="ir-board-search">
      <input id="pending-search" type="text" placeholder="🔍 搜尋代號或名稱…" oninput="renderPending()">
    </div>
    <div class="ir-board-scroll">
      <div class="ir-board-body">
        <div id="p-meta" style="font-size:12px;font-weight:700;margin-bottom:10px;"></div>
        <div id="p-981a-bar" class="etf981a-bar"></div>
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

// 短鍵還原：序列化時用 A,B,C… 縮小檔案，載入時還原成中文鍵（下游渲染碼不變）
const _KM={A:'公司代號',B:'公司名稱',C:'日期',D:'法說日',E:'通過到法說天數',F:'主旨',G:'召開法人說明會時間',H:'財報期別',I:'財報年度',J:'財報錨點',K:'前後',L:'距截止日交易日',M:'法說到截止交易日',N:'市場',O:'說明財報期',P:'法人說明會擇要訊息',Q:'法說時段',R:'早上法說日',S:'早上法說時間',T:'下午法說日',U:'下午法說時間',V:'法說清單',W:'即將法說日',X:'即將法說時間',Y:'即將法說地點',Z:'即將法說新'};
function _rh(r){const o={};for(const k in r)o[_KM[k]||k]=r[k];const v=o['法說清單'];if(Array.isArray(v))o['法說清單']=v.map(s=>({日期:s.d,時間:s.t,時段:s.s,主辦:s.o,類型:s.y}));return o;}
const RECORDS    = __DATA__.map(_rh);
const FIRM_INFO  = __FIRM_INFO__;
const INDUSTRIES = __INDUSTRIES__;
const LARGE_CAP  = new Set(__LARGE_CAP__);
const PENDING_IR  = __PENDING_IR__.map(_rh);
const UPCOMING_IR = __UPCOMING_IR__.map(_rh);
const ETF981A         = __ETF981A__;
const ETF981A_DATE    = "__ETF981A_DATE__";
const ETF981A_METRICS = __ETF981A_METRICS__;
const ETF981A_PREV      = __ETF981A_PREV__;
const ETF981A_PREV_DATE = "__ETF981A_PREV_DATE__";
const ETF981A_NAMES     = __ETF981A_NAMES__;
const FUTURES           = new Set(__FUTURES__);
const ETF981A_NAV_DELTA = __ETF981A_NAV_DELTA__;

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
  document.getElementById('upcoming-board').classList.remove('visible');
  document.getElementById('pending-board').classList.remove('visible');
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
          // 法說清單：每季（時段×主辦）各第一場
          const _segC = s => s.時段 === '下午' ? '#0369a1' : (s.時段 === '早上' ? '#c2410c' : '#64748b');
          const _brkC = b => b === '外資' ? '#7c3aed' : (b === '內資' ? '#0891b2' : (b === '自辦' ? '#16a34a' : '#94a3b8'));
          const sessions = Array.isArray(r['法說清單']) ? r['法說清單'] : [];
          const sessHTML = sessions.length
            ? sessions.map(s => `<div style="font-size:11px;line-height:1.5;white-space:nowrap">
                <span style="color:${_segC(s)};font-weight:700">${s.時段 || '—'}</span>
                <span style="background:${_brkC(s.主辦)}22;color:${_brkC(s.主辦)};padding:0 4px;border-radius:3px;margin:0 3px;font-weight:700">${s.主辦 || ''}</span>
                <span style="color:#0369a1;font-weight:700">${s.日期}</span> ${s.時間 || ''}</div>`).join('')
            : (r['法說日'] ? `<span style="color:#0369a1;font-weight:700">${r['法說日']}</span>` : '<span style="color:#cbd5e1">—</span>');
          return `<tr>
            <td>${r['日期'] || ''}</td>
            <td>${sessHTML}</td>
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

function openIRBoard(which) {
  document.getElementById('home').style.display   = 'none';
  document.getElementById('detail').style.display = 'none';
  document.getElementById('upcoming-board').classList.toggle('visible', which === 'upcoming');
  document.getElementById('pending-board').classList.toggle('visible',  which === 'pending');
  window.scrollTo(0, 0);
}
function closeIRBoard() {
  document.getElementById('upcoming-board').classList.remove('visible');
  document.getElementById('pending-board').classList.remove('visible');
  document.getElementById('home').style.display = 'block';
}
function sortP(col) {
  if (pSortCol === col) pSortAsc = !pSortAsc;
  else { pSortCol = col; pSortAsc = true; }
  renderPending();
}
function etfPriority(code) {
  if (Object.prototype.hasOwnProperty.call(ETF981A, code)) {
    const w = parseFloat((ETF981A[code]||'').replace('%',''));
    return isNaN(w) ? -0.001 : -w;   // 成分股：負權重，愈大愈前
  }
  return FUTURES.has(code) ? 1 : 2;  // 有期貨 → 其他
}
function weightDelta(code) {
  if (!Object.prototype.hasOwnProperty.call(ETF981A_PREV, code)) return '';
  const curr = parseFloat((ETF981A[code]||'').replace('%',''));
  const prev = parseFloat((ETF981A_PREV[code]||'').replace('%',''));
  if (isNaN(curr) || isNaN(prev)) return '';
  const d = Math.round((curr - prev) * 100) / 100;
  if (Math.abs(d) < 0.005) return '';
  const sign = d > 0 ? '+' : '';
  const col  = d > 0 ? '#f87171' : '#06402b';
  return `<span style="color:${col};font-size:10px;font-weight:800;margin-left:3px">【${sign}${d.toFixed(2)}%】</span>`;
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
    let primary;
    if (typeof va === 'number' && typeof vb === 'number')
      primary = pSortAsc ? va - vb : vb - va;
    else
      primary = pSortAsc
        ? String(va).localeCompare(String(vb), 'zh-TW')
        : String(vb).localeCompare(String(va), 'zh-TW');
    if (primary !== 0) return primary;
    return etfPriority(String(a['公司代號']||'').trim()) - etfPriority(String(b['公司代號']||'').trim());
  });
  document.querySelectorAll('#ptable th[data-col]').forEach(th => {
    th.classList.remove('asc','desc');
    if (th.dataset.col === pSortCol) th.classList.add(pSortAsc ? 'asc' : 'desc');
  });
  const _p981aLabel = ETF981A_DATE ? `<span class="etf981a-label">📊 00981A 截至 ${ETF981A_DATE}</span>` : '';
  document.getElementById('p-meta').innerHTML = `<span>共 ${data.length} 筆（總計 ${PENDING_IR.length} 筆）</span>${_p981aLabel}`;
  const _pSeen = new Set();
  const _p981aChips = data.filter(r => {
    const c = String(r['公司代號']||'').trim();
    if (!Object.prototype.hasOwnProperty.call(ETF981A, c) || _pSeen.has(c)) return false;
    _pSeen.add(c); return true;
  });
  const _pBar = document.getElementById('p-981a-bar');
  if (_p981aChips.length > 0) {
    _pBar.style.display = 'flex';
    _pBar.innerHTML = `<span class="etf981a-bar-lbl">📊 成分股（${_p981aChips.length}）：</span>`
      + _p981aChips.map(r => {
          const c = String(r['公司代號']||'').trim();
          const hasFut = FUTURES.has(c);
          const nm = hasFut ? `<b>${r['公司名稱']||c}</b>` : (r['公司名稱']||c);
          const fw = hasFut ? '' : 'font-weight:400;';
          return `<span class="etf981a-chip" style="${fw}">${nm}（${ETF981A[c]}）${weightDelta(c)}</span>`;
        }).join('');
  } else {
    _pBar.style.display = 'none';
  }
  // 添加表格圖例
  const pLegendHtml = `<div class="table-legend">
    <div class="legend-item">
      <span class="legend-text"><b style="color:#1e3a5f;font-weight:800">粗體名稱</b> = 有個股期</span>
    </div>
    <div class="legend-item">
      <span class="legend-dot legend-orange"></span>
      <span class="legend-text">橘色名稱 = 00981A 成分股</span>
    </div>
    <div class="legend-item">
      <span class="legend-dot legend-blue"></span>
      <span class="legend-text">藍色代號 = 大型股/金融</span>
    </div>
  </div>`;
  const ptableParent = document.getElementById('ptable').parentElement;
  const existingLegend = ptableParent.querySelector('.table-legend');
  if (existingLegend) existingLegend.remove();
  document.getElementById('ptable').insertAdjacentHTML('beforebegin', pLegendHtml);

  document.getElementById('ptbody').innerHTML = data.map(r => {
    const td = r['距截止日交易日'];
    const code = String(r['公司代號']||'').trim();
    const isLarge = LARGE_CAP.has(code);
    const is981a  = Object.prototype.hasOwnProperty.call(ETF981A, code);
    const hasFut  = FUTURES.has(code);
    const mkt = r['市場'] || '';
    const mktHtml = mkt ? `<span class="${mkt.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${mkt}</span>` : '';
    const subj = (r['主旨'] || '').replace(/"/g,'&quot;');
    const weightHtml = is981a ? `<span class="etf981a-weight">權重：${ETF981A[code]}</span>` : '';
    const codeStyle = isLarge ? 'color:#1d4ed8' : '';   // 藍色代號 = 大型股/金融
    // 橘色名稱 = 成分股；粗體名稱 = 有個股期（可黑可橘，兩者獨立）
    const nameEl = `<span style="${is981a?'color:#f97316;':''}${hasFut?'font-weight:800;':''}">${r['公司名稱']||''}</span>`;
    return `<tr>
      <td><strong style="${codeStyle}">${r['公司代號']||''}</strong></td>
      <td>${nameEl}${weightHtml}</td>
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
    let primary;
    if (typeof va === 'number' && typeof vb === 'number')
      primary = uSortAsc ? va - vb : vb - va;
    else
      primary = uSortAsc
        ? String(va).localeCompare(String(vb), 'zh-TW')
        : String(vb).localeCompare(String(va), 'zh-TW');
    if (primary !== 0) return primary;
    return etfPriority(String(a['公司代號']||'').trim()) - etfPriority(String(b['公司代號']||'').trim());
  });
  document.querySelectorAll('#utable th[data-col]').forEach(th => {
    th.classList.remove('asc','desc');
    if (th.dataset.col === uSortCol) th.classList.add(uSortAsc ? 'asc' : 'desc');
  });
  const _u981aLabel = ETF981A_DATE ? `<span class="etf981a-label">📊 00981A 截至 ${ETF981A_DATE}</span>` : '';
  document.getElementById('u-meta').innerHTML = `<span>共 ${data.length} 筆（總計 ${UPCOMING_IR.length} 筆）</span>${_u981aLabel}`;
  const _uSeen = new Set();
  const _u981aChips = data.filter(r => {
    const c = String(r['公司代號']||'').trim();
    if (!Object.prototype.hasOwnProperty.call(ETF981A, c) || _uSeen.has(c)) return false;
    _uSeen.add(c); return true;
  });
  const _uBar = document.getElementById('u-981a-bar');
  if (_u981aChips.length > 0) {
    _uBar.style.display = 'flex';
    _uBar.innerHTML = `<span class="etf981a-bar-lbl">📊 成分股（${_u981aChips.length}）：</span>`
      + _u981aChips.map(r => {
          const c = String(r['公司代號']||'').trim();
          const hasFut = FUTURES.has(c);
          const nm = hasFut ? `<b>${r['公司名稱']||c}</b>` : (r['公司名稱']||c);
          const fw = hasFut ? '' : 'font-weight:400;';
          return `<span class="etf981a-chip" style="${fw}">${nm}（${ETF981A[c]}）${weightDelta(c)}</span>`;
        }).join('');
  } else {
    _uBar.style.display = 'none';
  }
  // 添加表格圖例
  const uLegendHtml = `<div class="table-legend">
    <div class="legend-item">
      <span class="legend-text"><b style="color:#1e3a5f;font-weight:800">粗體名稱</b> = 有個股期</span>
    </div>
    <div class="legend-item">
      <span class="legend-dot legend-orange"></span>
      <span class="legend-text">橘色名稱 = 00981A 成分股</span>
    </div>
    <div class="legend-item">
      <span class="legend-dot legend-blue"></span>
      <span class="legend-text">藍色代號 = 大型股/金融</span>
    </div>
  </div>`;
  const utableParent = document.getElementById('utable').parentElement;
  const existingULegend = utableParent.querySelector('.table-legend');
  if (existingULegend) existingULegend.remove();
  document.getElementById('utable').insertAdjacentHTML('beforebegin', uLegendHtml);

  document.getElementById('utbody').innerHTML = data.map(r => {
    const code    = String(r['公司代號']||'').trim();
    const isLarge = LARGE_CAP.has(code);
    const isNew   = r['即將法說新'] === true;
    const is981a  = Object.prototype.hasOwnProperty.call(ETF981A, code);
    const hasFut  = FUTURES.has(code);
    const mkt = r['市場'] || '';
    const mktHtml = mkt ? `<span class="${mkt.includes('TWSE') ? 'tag-twse' : 'tag-otc'}">${mkt}</span>` : '';
    const subj = (r['主旨'] || '').replace(/"/g,'&quot;');
    const weightHtml = is981a ? `<span class="etf981a-weight">權重：${ETF981A[code]}</span>` : '';
    const codeStyleU = isLarge ? 'color:#1d4ed8' : '';   // 藍色代號 = 大型股/金融
    // 橘色名稱 = 成分股；粗體名稱 = 有個股期（可黑可橘，兩者獨立）
    const nameElU = `<span style="${is981a?'color:#f97316;':''}${hasFut?'font-weight:800;':''}">${r['公司名稱']||''}</span>`;
    return `<tr>
      <td><strong style="${codeStyleU}">${r['公司代號']||''}</strong>${isNew ? '<span class="new-badge">NEW</span>' : ''}</td>
      <td>${nameElU}${weightHtml}</td>
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
  renderUpcoming();
  renderPending();
  // 00981A 卡片
  const m = ETF981A_METRICS;
  const hasMetrics = m && (m.diff_groups || m.nav_per_unit || m.total_nav);
  if (hasMetrics) {
    document.getElementById('etf981a-card').classList.add('visible');
    if (ETF981A_DATE) document.getElementById('etf981a-date-tag').textContent = `截至 ${ETF981A_DATE} 資料`;
    if (m.diff_groups) {
      const el = document.getElementById('etf-diff');
      el.textContent = m.diff_groups;
      el.className = 'value ' + (m.diff_sign === 'pos' ? 'pos' : 'neg');
    }
    if (m.total_nav) document.getElementById('etf-nav-total').textContent = m.total_nav;
    if (m.nav_per_unit) document.getElementById('etf-nav-unit').textContent = m.nav_per_unit;
    // NAV 前日差異小字
    const nd = ETF981A_NAV_DELTA || {};
    function setDelta(elId, pct) {
      if (!pct) return;
      const el = document.getElementById(elId);
      if (!el) return;
      const isUp = pct.startsWith('+');
      el.textContent = pct;
      el.className = 'delta ' + (isUp ? 'up' : 'dn');
    }
    setDelta('etf-nav-total-delta', nd.total_nav_pct);
    setDelta('etf-nav-unit-delta',  nd.nav_unit_pct);
  }
  // 成分股差異
  if (ETF981A_PREV && Object.keys(ETF981A_PREV).length > 0 && Object.keys(ETF981A).length > 0) {
    const upcomingSet = new Set(UPCOMING_IR.map(r => String(r['公司代號']||'').trim()));
    const pendingSet  = new Set(PENDING_IR.map(r => String(r['公司代號']||'').trim()));
    const histSet     = new Set(RECORDS.map(r => String(r['公司代號']||'').trim()));
    const nameMap     = Object.assign({}, ETF981A_NAMES);
    RECORDS.forEach(r => { const c=String(r['公司代號']||'').trim(); if(c&&!nameMap[c]) nameMap[c]=r['公司名稱']||''; });
    UPCOMING_IR.forEach(r => { const c=String(r['公司代號']||'').trim(); if(c) nameMap[c]=r['公司名稱']||nameMap[c]||''; });
    PENDING_IR.forEach(r => { const c=String(r['公司代號']||'').trim(); if(c) nameMap[c]=r['公司名稱']||nameMap[c]||''; });

    const diffItems = [];
    for (const code of Object.keys(ETF981A)) {
      if (!Object.prototype.hasOwnProperty.call(ETF981A_PREV, code))
        diffItems.push({code, type:'add', prevW:null, currW:ETF981A[code]});
    }
    for (const code of Object.keys(ETF981A_PREV)) {
      if (!Object.prototype.hasOwnProperty.call(ETF981A, code))
        diffItems.push({code, type:'remove', prevW:ETF981A_PREV[code], currW:null});
    }
    for (const code of Object.keys(ETF981A)) {
      if (Object.prototype.hasOwnProperty.call(ETF981A_PREV, code) && ETF981A_PREV[code] !== ETF981A[code])
        diffItems.push({code, type:'change', prevW:ETF981A_PREV[code], currW:ETF981A[code]});
    }

    if (diffItems.length > 0) {
      if (ETF981A_PREV_DATE) document.getElementById('etf981a-prev-date-label').textContent = `vs ${ETF981A_PREV_DATE}`;
      const tbody = document.getElementById('etf981a-diff-body');
      // 添加成分股差異圖例
      const diffLegendHtml = `<div style="margin-bottom:12px;padding:8px 0;display:flex;gap:20px;flex-wrap:wrap;">
        <div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;">
          <span style="background:#fff5ee;border:1px solid #fed7aa;width:16px;height:16px;border-radius:3px;display:inline-block;"></span>
          <span style="color:#6b8fc7">橘色 = 即將法說</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;">
          <span style="background:#e0f2fe;border:1px solid #a5d8ff;width:16px;height:16px;border-radius:3px;display:inline-block;"></span>
          <span style="color:#6b8fc7">淺藍色 = 待法說</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;">
          <span style="background:#ecfdf5;border:1px solid #a7f3d0;width:16px;height:16px;border-radius:3px;display:inline-block;"></span>
          <span style="color:#6b8fc7">綠色 = 歷史紀錄</span>
        </div>
      </div>`;
      const existingDiffLegend = document.getElementById('etf981a-diff').querySelector('.etf-diff-legend');
      if (!existingDiffLegend) {
        document.getElementById('etf981a-diff-title').insertAdjacentHTML('afterend', `<div class="etf-diff-legend">${diffLegendHtml}</div>`);
      }
      diffItems.forEach(({code, type, prevW, currW}) => {
        let color, bg;
        if      (upcomingSet.has(code)) { color='#f97316'; bg='#fff5ee'; }    // 即將法說 = 淺橘色背景
        else if (pendingSet.has(code))  { color='#0369a1'; bg='#e0f2fe'; }    // 待法說 = 淺藍色背景
        else if (histSet.has(code))     { color='#059669'; bg='#ecfdf5'; }    // 歷史紀錄 = 淡綠色背景
        else                            { color='#1e293b'; bg='#fff'; }        // 其他 = 白色背景
        const prefix = type==='add' ? '＋' : type==='remove' ? '－' : '↕';
        const name = nameMap[code] || '';
        const tr = document.createElement('tr');
        tr.style.background = bg;
        // 計算差異欄
        let deltaCell = '<td style="text-align:center;color:#475569">—</td>';
        if (type === 'change') {
          const pv = parseFloat((prevW||'').replace('%',''));
          const cv = parseFloat((currW||'').replace('%',''));
          if (!isNaN(pv) && !isNaN(cv)) {
            const d = Math.round((cv - pv) * 100) / 100;
            const sign = d > 0 ? '+' : '';
            const dc = d > 0 ? '#f87171' : '#06402b';
            const arrow = d > 0 ? '↑' : '↓';
            deltaCell = `<td style="text-align:center;color:${dc};font-weight:800;font-size:12px;white-space:nowrap">${arrow} ${sign}${d.toFixed(2)}%</td>`;
          }
        } else if (type === 'add') {
          deltaCell = `<td style="text-align:center;color:#f87171;font-weight:800;font-size:12px">NEW</td>`;
        } else {
          deltaCell = `<td style="text-align:center;color:#06402b;font-weight:800;font-size:12px">OUT</td>`;
        }
        tr.innerHTML = `<td class="diff-type" style="color:${color}">${prefix}</td>`
                     + `<td class="diff-code" style="color:${color}">${code}</td>`
                     + `<td class="diff-name" style="color:${color}">${name}</td>`
                     + `<td class="diff-w" style="color:${color}">${type==='add'?'—':prevW}</td>`
                     + deltaCell
                     + `<td class="diff-w" style="color:${color}">${type==='remove'?'—':currW}</td>`;
        tbody.appendChild(tr);
      });
      const btn = document.getElementById('etf981a-toggle');
      btn.style.display = 'block';
      document.getElementById('etf981a-diff-cnt').textContent = `（${diffItems.length} 筆）`;
    }
  }
});
function toggleEtfDiff() {
  const diff = document.getElementById('etf981a-diff');
  const btn  = document.getElementById('etf981a-toggle');
  const open = diff.style.display !== 'none' && diff.style.display !== '';
  diff.style.display = open ? 'none' : 'block';
  btn.classList.toggle('open', !open);
}
</script>
</body>
</html>"""


if __name__ == '__main__':
    main()
