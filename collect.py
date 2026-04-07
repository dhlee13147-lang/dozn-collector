"""
더즌(462860) 일간 주가 데이터 수집기 v4.5
===========================================
수정 사항:
  - 뉴스 수집: BeautifulSoup 기반 네이버 뉴스 최신순 크롤링 (요약/언론사 포함)
  - 수급/거래대금: 핀업 HTML 구조 변경 대비 패턴 보강
  - 데이터 소스: 핀업(Finup), pykrx, DART, 네이버뉴스
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
from pykrx import stock as krx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
TICKERS = {
    "더즌":        "462860",
    "헥토파이낸셜": "234340",
    "쿠콘":        "294570",
}
DART_CORP_CODES = {
    "더즌":        "01615947",
    "헥토파이낸셜": "00669540",
    "쿠콘":        "00798833",
}
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.finup.co.kr/",
}

# ─────────────────────────────────────────
# 날짜 헬퍼
# ─────────────────────────────────────────
def latest_trading_day(today: date) -> date:
    d = today
    if d.weekday() == 5: d -= timedelta(days=1)
    elif d.weekday() == 6: d -= timedelta(days=2)
    now_kst = datetime.utcnow() + timedelta(hours=9)
    if (now_kst.hour, now_kst.minute) < (15, 30):
        d -= timedelta(days=1)
        while d.weekday() >= 5: d -= timedelta(days=1)
    return d

def strdate(d: date) -> str: return d.strftime("%Y%m%d")
def display_date(d: date) -> str:
    days = ["월","화","수","목","금","토","일"]
    return d.strftime(f"%Y-%m-%d({days[d.weekday()]})")

# ─────────────────────────────────────────
# 파싱 헬퍼
# ─────────────────────────────────────────
def parse_amt(text: str) -> int:
    total = 0
    m_eok = re.search(r'([\d,]+)억', text)
    m_man = re.search(r'([\d,]+)만', text)
    if m_eok: total += int(m_eok.group(1).replace(',', '')) * 100_000_000
    if m_man: total += int(m_man.group(1).replace(',', '')) * 10_000
    if not m_eok and not m_man and text.replace(',','').isdigit():
        total = int(text.replace(',',''))
    return total

def parse_int(text: str) -> int: return int(re.sub(r'[^\d]', '', text) or '0')
def parse_signed_int(text: str) -> int:
    text = text.strip()
    sign = -1 if '-' in text else 1
    return sign * int(re.sub(r'[^\d]', '', text) or '0')

# ─────────────────────────────────────────
# 1. 핀업 수집 (거래대금 및 수급 패턴 강화)
# ─────────────────────────────────────────
def fetch_finup(ticker: str) -> dict:
    url = f"https://finance.finup.co.kr/Stock/{ticker}"
    result = {"시가": 0, "고가": 0, "저가": 0, "거래량": 0, "거래대금": 0, "기준일시": "", "history": [], "_source": "finup"}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        html = resp.text
        soup = BeautifulSoup(html, 'html.parser')

        # 기준일시
        dt_m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*기준', html)
        if dt_m: result["기준일시"] = dt_m.group(1).strip()

        # 거래대금 (강화된 패턴)
        amt_m = re.search(r'거래대금\(원\)\s*</th>\s*<td>\s*<span[^>]*>([\d,억만\s]+)</span>', html)
        if amt_m: result["거래대금"] = parse_amt(amt_m.group(1).strip())

        # 수급 이력 데이터 (JSON 데이터 영역 혹은 테이블 파싱)
        # 핀업의 일별 시세 테이블 row 추출
        rows = re.findall(r'(\d{4}-\d{2}-\d{2})\s*\|\s*([\d,]+)\s*\|\s*[\d,]+\s*\|\s*[+-][\d.]%\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)', html)
        for row in rows[:10]:
            result["history"].append({
                "날짜": row[0], "종가": parse_int(row[1]),
                "개인": parse_signed_int(row[2]), "외국인": parse_signed_int(row[3]), "기관": parse_signed_int(row[4])
            })
    except Exception as e:
        print(f"    [WARN] 핀업 오류 ({ticker}): {e}")
    return result

# ─────────────────────────────────────────
# 2. pykrx OHLCV
# ─────────────────────────────────────────
def fetch_ohlcv(ticker: str, trade_day: date) -> dict:
    ds = strdate(trade_day)
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            return {
                "시가": int(row.get("시가", 0)), "고가": int(row.get("고가", 0)),
                "저가": int(row.get("저가", 0)), "종가": int(row.get("종가", 0)),
                "거래량": int(row.get("거래량", 0)), "등락률": float(row.get("등락률", 0.0)),
            }
    except Exception as e:
        print(f"    [WARN] pykrx 오류 ({ticker}): {e}")
    return {}

# ─────────────────────────────────────────
# 3. 기술적 지표 (MA/RSI/BB/OBV/MACD)
# ─────────────────────────────────────────
def fetch_history_for_indicators(ticker: str, days: int = 60) -> list:
    today = date.today()
    start = today - timedelta(days=days * 2)
    try:
        df = krx.get_market_ohlcv(strdate(start), strdate(today), ticker)
        if df.empty: return []
        records = []
        for dt, row in df.iterrows():
            records.append({
                "날짜": dt.strftime("%Y-%m-%d"), "시가": int(row.get("시가", 0)),
                "고가": int(row.get("고가", 0)), "저가": int(row.get("저가", 0)),
                "종가": int(row.get("종가", 0)), "거래량": int(row.get("거래량", 0)),
            })
        return records[-days:]
    except: return []

def calc_ema(values: list, period: int) -> list:
    if len(values) < period: return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]: ema.append(v * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema

def calc_technical_indicators(history: list) -> dict:
    if not history: return {}
    closes = [r["종가"] for r in history]
    volumes = [r["거래량"] for r in history]
    result = {}
    for n in [5, 10, 20, 60]:
        if len(closes) >= n: result[f"MA{n}"] = round(sum(closes[-n:]) / n)
    if len(closes) >= 15:
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        gains, losses = [max(d,0) for d in deltas], [abs(min(d,0)) for d in deltas]
        ag, al = sum(gains[-14:])/14, sum(losses[-14:])/14
        rsi = 100.0 if al == 0 else round(100 - 100/(1 + ag/al), 2)
        result["RSI14"] = rsi
        result["RSI14_signal"] = "과매수 ⚠️" if rsi >= 70 else "과매도 📉" if rsi <= 30 else "중립"
    if len(closes) >= 20:
        w = closes[-20:]; ma = sum(w)/20; std = (sum((c-ma)**2 for c in w)/20)**0.5
        bbu, bbl, bbm = round(ma+2*std), round(ma-2*std), round(ma)
        result.update({"BB_upper": bbu, "BB_mid": bbm, "BB_lower": bbl})
        cur = closes[-1]
        result["BB_signal"] = f"상단 돌파 ⚠️" if cur >= bbu else f"하단 이탈 📉" if cur <= bbl else "정상 범위"
    return result

# ─────────────────────────────────────────
# 4. DART 공시
# ─────────────────────────────────────────
def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=2)
    params = {"crtfc_key": dart_key, "corp_code": corp_code, "bgn_de": strdate(start), "end_de": strdate(end), "page_count": "10"}
    url = f"{DART_LIST_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") == "000":
            return [{"날짜": i["rcept_dt"], "보고서": i["report_nm"], "URL": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"} for i in resp.get("list", [])]
    except: pass
    return []

# ─────────────────────────────────────────
# 5. 뉴스 수집 (강화된 네이버 크롤러)
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 15) -> list:
    encoded_query = urllib.parse.quote(query)
    # 최신순(sort=1), 최근 1일(nso=p:1d)
    url = f"https://search.naver.com/search.naver?where=news&query={encoded_query}&sm=tab_opt&sort=1&nso=so:dd,p:1d"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        news_list = soup.select('ul.list_news > li.bx')

        for news in news_list[:max_items]:
            title_tag = news.select_one('a.news_tit')
            if not title_tag: continue
            
            # 언론사 정보
            press_tag = news.select_one('a.info.press')
            press = press_tag.get_text(strip=True).replace("언론사 선정", "") if press_tag else ""
            
            # 시간 정보
            time_tag = news.select_one('span.info')
            time_text = time_tag.get_text(strip=True) if time_tag else ""

            # 요약 정보
            desc_tag = news.select_one('div.news_dsc')
            desc = desc_tag.get_text(strip=True) if desc_tag else ""

            items.append({
                "제목": title_tag.get('title') or title_tag.get_text(),
                "언론사": press,
                "시간": time_text,
                "요약": desc[:150],
                "링크": title_tag['href'],
            })
    except Exception as e:
        print(f"    [WARN] 뉴스 오류 ('{query}'): {e}")
    return items

# ─────────────────────────────────────────
# 6. 전체 수집 실행
# ─────────────────────────────────────────
def collect_all() -> dict:
    today = date.today()
    trade_day = latest_trading_day(today)
    dart_key = os.getenv("DART_API_KEY", "")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 데이터 수집 시작...")

    result = {
        "_collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_trade_date": trade_day.strftime("%Y-%m-%d"),
        "주가": {}, "수급": {}, "기본정보": {}, "이동평균": {}, "피어": {}, "공시": {}, "뉴스": {},
    }

    # 더즌 데이터
    t = TICKERS["더즌"]
    finup = fetch_finup(t)
    ohlcv = fetch_ohlcv(t, trade_day)

    result["주가"] = {
        "시가": ohlcv.get("시가") or finup.get("시가", 0),
        "고가": ohlcv.get("고가") or finup.get("고가", 0),
        "저가": ohlcv.get("저가") or finup.get("저가", 0),
        "종가": ohlcv.get("종가") or (finup["history"][0]["종가"] if finup.get("history") else 0),
        "거래량": ohlcv.get("거래량") or finup.get("거래량", 0),
        "거래대금": finup.get("거래대금", 0),
        "등락률": ohlcv.get("등락률", 0.0),
    }

    if finup.get("history"):
        latest = finup["history"][0]
        result["수급"] = {"개인": latest.get("개인", 0), "외국인": latest.get("외국인", 0), "기관": latest.get("기관", 0)}

    # 지표 및 피어
    history = fetch_history_for_indicators(t)
    result["이동평균"] = calc_technical_indicators(history)

    for name, pt in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]), ("쿠콘", TICKERS["쿠콘"])]:
        p = fetch_ohlcv(pt, trade_day)
        result["피어"][name] = {"종가": p.get("종가", 0), "등락률": p.get("등락률", 0.0)}

    # 공시 및 뉴스
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else []
    
    for name, q in [("더즌", "더즌 462860"), ("헥토파이낸셜", "헥토파이낸셜 주가"), ("쿠콘", "쿠콘 주가")]:
        result["뉴스"][name] = fetch_news(q)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 완료")
    return result

# ─────────────────────────────────────────
# 7. 텔레그램 전송
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    p, s, ma = data["주가"], data["수급"], data["이동평균"]
    chg = p.get("등락률", 0)
    arrow = "📈" if chg > 0 else ("📉" if chg < 0 else "➡️")
    amt_str = f"{p.get('거래대금', 0)/100_000_000:.1f}억" if p.get('거래대금', 0) >= 100_000_000 else "-"

    lines = [
        f"📊 *더즌(462860) 일간 리포트* — {data['_trade_date']}",
        "",
        f"*{arrow} 주가 요약*",
        f"  종가: {p.get('종가',0):,}원 ({chg:+.2f}%)",
        f"  거래량: {p.get('거래량',0):,}주  대금: {amt_str}",
        "",
        "*📐 기술적 지표*",
        f"  RSI14: {ma.get('RSI14','-')} ({ma.get('RSI14_signal','')})",
        f"  볼린저: {ma.get('BB_signal','-')}",
        "",
        "*👥 당일 수급*",
        f"  개인: {s.get('개인',0):+,}  외인: {s.get('외국인',0):+,}  기관: {s.get('기관',0):+,}",
        "",
        "*📋 주요 공시*",
    ]
    any_disc = False
    for name, discs in data["공시"].items():
        for d in discs:
            any_disc = True
            lines.append(f"  [{name}] {d['보고서']} \n  {d['URL']}")
    if not any_disc: lines.append("  - 오늘 공시 없음")

    lines.append("\n*📰 주요 뉴스*")
    for name, articles in data["뉴스"].items():
        if articles:
            lines.append(f"  ▸ {name}")
            for a in articles[:3]: # 종목당 상위 3개만
                lines.append(f"    - {a['제목']} ({a['언론사']})")
    
    return "\n".join(lines)

def send_telegram(text: str, json_data: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(text)
        return
    base = f"https://api.telegram.org/bot{token}"
    requests.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True})
    jb = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    requests.post(f"{base}/sendDocument", data={"chat_id": chat_id, "caption": f"JSON data - {json_data['_trade_date']}"}, files={"document": (f"dozen_{json_data['_trade_date']}.json", jb)})

def main():
    data = collect_all()
    text = format_telegram(data)
    send_telegram(text, data)

if __name__ == "__main__":
    main()
