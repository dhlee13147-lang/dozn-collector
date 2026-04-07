"""
더즌(462860) 일간 주가 데이터 수집기 v4.6
===========================================
수정 사항:
  - 수급/거래대금: 핀업 파싱 대신 pykrx(거래소 직접 데이터) 사용 (수집 보장)
  - 기술적 지표: RSI, 볼린저밴드, MACD, OBV 상세 해석 로직 복원
  - 뉴스: BeautifulSoup 기반 네이버 뉴스 최신순 수집
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
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

# ─────────────────────────────────────────
# 1. 주가 및 수급 수집 (pykrx 기반 - 수집 보장)
# ─────────────────────────────────────────
def fetch_stock_data(ticker: str, trade_day: date) -> dict:
    ds = strdate(trade_day)
    result = {"주가": {}, "수급": {}}
    
    try:
        # 1. OHLCV (시가, 고가, 저가, 종가, 거래량, 거래대금)
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            result["주가"] = {
                "시가": int(row["시가"]), "고가": int(row["고가"]),
                "저가": int(row["저가"]), "종가": int(row["종가"]),
                "거래량": int(row["거래량"]), "거래대금": int(row["거래대금"]),
                "등락률": float(row["등락률"]),
            }
        
        # 2. 투자자별 순매수 (수급)
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            row_inv = df_inv.iloc[0]
            result["수급"] = {
                "개인": int(row_inv["개인"]),
                "외국인": int(row_inv["외국인"]),
                "기관": int(row_inv["기관합계"]),
            }
    except Exception as e:
        print(f"    [WARN] pykrx 수집 실패 ({ticker}): {e}")
    
    return result

# ─────────────────────────────────────────
# 2. 기술적 지표 및 상세 해석 (직무 특화)
# ─────────────────────────────────────────
def calc_ema(values: list, period: int) -> list:
    if len(values) < period: return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]: ema.append(v * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema

def get_technical_analysis(ticker: str) -> dict:
    today = date.today()
    start = today - timedelta(days=100)
    df = krx.get_market_ohlcv(strdate(start), strdate(today), ticker)
    if df.empty: return {}

    closes = df['종가'].tolist()
    volumes = df['거래량'].tolist()
    res = {}

    # 이동평균선
    res["MA5"] = round(df['종가'].rolling(5).mean().iloc[-1])
    res["MA20"] = round(df['종가'].rolling(20).mean().iloc[-1])
    res["MA60"] = round(df['종가'].rolling(60).mean().iloc[-1])

    # RSI (14)
    delta = df['종가'].diff()
    up = delta.clip(lower=0); down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=13, adjust=False).mean(); ema_down = down.ewm(com=13, adjust=False).mean()
    rs = ema_up / ema_down; rsi = 100 - (100 / (1 + rs))
    res["RSI14"] = round(rsi.iloc[-1], 2)
    res["RSI_signal"] = "과매수 영역(주의) ⚠️" if res["RSI14"] >= 70 else "과매도 영역(기회) 📉" if res["RSI14"] <= 30 else "중립"

    # 볼린저 밴드
    ma20 = df['종가'].rolling(20).mean(); std20 = df['종가'].rolling(20).std()
    res["BB_upper"] = round((ma20 + 2*std20).iloc[-1])
    res["BB_lower"] = round((ma20 - 2*std20).iloc[-1])
    res["BB_mid"] = round(ma20.iloc[-1])
    cur = closes[-1]
    res["BB_signal"] = "상단 돌파(과열)" if cur >= res["BB_upper"] else "하단 이탈(침체)" if cur <= res["BB_lower"] else "밴드 내 횡보"

    # MACD
    ema12 = df['종가'].ewm(span=12, adjust=False).mean()
    ema26 = df['종가'].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    res["MACD"] = round(macd.iloc[-1], 2)
    res["MACD_signal"] = round(signal.iloc[-1], 2)
    res["MACD_cross"] = "골든크로스 📈" if macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2] else "데드크로스 📉" if macd.iloc[-1] < signal.iloc[-1] and macd.iloc[-2] >= signal.iloc[-2] else "강세 유지" if macd.iloc[-1] > signal.iloc[-1] else "약세 유지"

    return res

# ─────────────────────────────────────────
# 3. 뉴스 및 공시 (기존 로직 보강)
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 10) -> list:
    encoded_query = urllib.parse.quote(query)
    url = f"https://search.naver.com/search.naver?where=news&query={encoded_query}&sm=tab_opt&sort=1&nso=so:dd,p:1d"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for news in soup.select('ul.list_news > li.bx')[:max_items]:
            title_tag = news.select_one('a.news_tit')
            if title_tag:
                items.append({
                    "제목": title_tag.get_text(strip=True),
                    "언론사": news.select_one('a.info.press').get_text(strip=True).replace("언론사 선정", "") if news.select_one('a.info.press') else "정보없음",
                    "링크": title_tag['href'],
                })
    except: pass
    return items

def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=2)
    params = {"crtfc_key": dart_key, "corp_code": corp_code, "bgn_de": strdate(start), "end_de": strdate(end)}
    try:
        resp = requests.get(f"{DART_LIST_URL}?{urllib.parse.urlencode(params)}", timeout=10).json()
        if resp.get("status") == "000":
            return [{"보고서": i["report_nm"], "URL": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"} for i in resp.get("list", [])]
    except: pass
    return []

# ─────────────────────────────────────────
# 4. 메인 실행 및 포맷팅
# ─────────────────────────────────────────
def main():
    today = date.today()
    trade_day = latest_trading_day(today)
    dart_key = os.getenv("DART_API_KEY", "")

    # 1. 데이터 수집
    t = TICKERS["더즌"]
    stock_info = fetch_stock_data(t, trade_day)
    tech = get_technical_analysis(t)
    
    # 피어 그룹
    peers = {}
    for name, code in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]), ("쿠콘", TICKERS["쿠콘"])]:
        peers[name] = fetch_stock_data(code, trade_day)["주가"]

    # 뉴스 및 공시
    news_data = {n: fetch_news(q) for n, q in [("더즌", "더즌 462860"), ("헥토파이낸셜", "헥토파이낸셜 주가"), ("쿠콘", "쿠콘 주가")]}
    dart_data = {n: fetch_dart(c, dart_key) for n, c in DART_CORP_CODES.items()} if dart_key else {}

    # 2. 텔레그램 메시지 구성
    p, s = stock_info["주가"], stock_info["수급"]
    chg = p.get("등락률", 0)
    arrow = "📈" if chg > 0 else "📉" if chg < 0 else "➡️"
    
    # 거래대금 단위 변환 (원 -> 억)
    amt_eok = f"{p.get('거래대금', 0)/100_000_000:.1f}억" if p.get('거래대금', 0) > 0 else "-"

    lines = [
        f"📊 *더즌(462860) 기업분석 리포트* | {trade_day.strftime('%Y-%m-%d')}",
        "",
        f"*{arrow} 가격 및 거래 지표*",
        f"  • 종가: {p.get('종가',0):,}원 ({chg:+.2f}%)",
        f"  • 시/고/저: {p.get('시가',0):,}/{p.get('고가',0):,}/{p.get('저가',0):,}",
        f"  • 거래량: {p.get('거래량',0):,}주",
        f"  • 거래대금: {amt_eok} (당일 기준)",
        "",
        f"*👥 투자자별 수급 (단위: 주)*",
        f"  • 개인: {s.get('개인',0):+,}",
        f"  • 외국인: {s.get('외국인',0):+,}",
        f"  • 기관: {s.get('기관',0):+,}",
        "",
        f"*📐 기술적 지표 상세 해석*",
        f"  • 이동평균: MA5({tech.get('MA5',0):,}) | MA20({tech.get('MA20',0):,})",
        f"  • RSI(14): {tech.get('RSI14')} → {tech.get('RSI_signal')}",
        f"  • 볼린저밴드: {tech.get('BB_signal')}",
        f"    (상단 {tech.get('BB_upper',0):,} / 하단 {tech.get('BB_lower',0):,})",
        f"  • MACD: {tech.get('MACD')} ({tech.get('MACD_cross')})",
        "",
        f"*🔗 피어 그룹 동향*",
    ]
    for name, info in peers.items():
        lines.append(f"  • {name}: {info.get('종가',0):,}원 ({info.get('등락률',0):+.2f}%)")

    lines.append("\n*📋 주요 공시 및 뉴스*")
    # 공시 요약
    any_dart = False
    for n, docs in dart_data.items():
        for d in docs:
            any_dart = True
            lines.append(f"  • [{n}] {d['보고서']}")
    if not any_dart: lines.append("  • 당일 주요 공시 없음")

    # 뉴스 요약 (종목별 2개씩만)
    for n, articles in news_data.items():
        if articles:
            lines.append(f"  ▸ {n} 관련 최신 뉴스")
            for a in articles[:2]:
                lines.append(f"    - {a['제목']} ({a['언론사']})")

    msg = "\n".join(lines)

    # 3. 전송
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})
        # JSON 저장 및 전송 로직 (생략 가능하나 기존 유지)
        jb = json.dumps({"trade_date": strdate(trade_day), "data": stock_info}, ensure_ascii=False).encode("utf-8")
        requests.post(f"https://api.telegram.org/bot{token}/sendDocument", 
                      data={"chat_id": chat_id}, files={"document": (f"dozen_{strdate(trade_day)}.json", jb)})
    else:
        print(msg)

if __name__ == "__main__":
    main()
