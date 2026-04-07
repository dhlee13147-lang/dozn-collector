"""
더즌(462860) 일간 주가 데이터 수집기
- KRX 공식 데이터 (pykrx)
- DART 공시 (OpenAPI)
- 네이버 뉴스 크롤링
- 텔레그램 봇 전송
- 매일 16:00 KST 실행 (구글 트리거)

환경변수 (GitHub Secrets / GCP Secret Manager):
  TELEGRAM_BOT_TOKEN   텔레그램 봇 토큰
  TELEGRAM_CHAT_ID     수신 채팅 ID
  DART_API_KEY         DART OpenAPI 키 (https://opendart.fss.or.kr)
"""

import os
import json
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta

import requests
from pykrx import stock as krx

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
TICKERS = {
    "더즌":        "462860",
    "헥토파이낸셜": "234340",
    "쿠콘":        "294570",
}

# DART corp_code (더즌 = 상장시 DART에서 부여된 고유번호)
DART_CORP_CODES = {
    "더즌":        "01615947",   # DART에서 확인한 실제 corp_code
    "헥토파이낸셜": "00669540",
    "쿠콘":        "00798833",
}

# 최신순 + 1일 필터 — 사용자 지정 URL
NAVER_NEWS_URL = "https://search.naver.com/search.naver?where=news&query={query}&sm=tab_opt&sort=1&nso=so%3Add%2Cp%3A1d"
DART_LIST_URL  = "https://opendart.fss.or.kr/api/list.json"

# ─────────────────────────────────────────
# 날짜 헬퍼
# ─────────────────────────────────────────
def latest_trading_day(today: date) -> date:
    """오늘이 주말이면 가장 최근 금요일 반환"""
    if today.weekday() == 5:   # 토
        return today - timedelta(days=1)
    elif today.weekday() == 6: # 일
        return today - timedelta(days=2)
    return today

def strdate(d: date) -> str:
    return d.strftime("%Y%m%d")

def display_date(d: date) -> str:
    days = ["월", "화", "수", "목", "금", "토", "일"]
    return d.strftime(f"%Y-%m-%d({days[d.weekday()]})")


# ─────────────────────────────────────────
# 1. KRX 주가 데이터 (pykrx)
# ─────────────────────────────────────────
def fetch_price(ticker: str, trade_day: date) -> dict:
    """종가·시가·고가·저가·거래량·거래대금·등락률 수집
    
    pykrx 구조 주의:
      get_market_ohlcv(from,to,ticker) → 시가/고가/저가/종가/거래량/등락률 (거래대금 없음!)
      get_market_cap(from,to,ticker)   → 시가총액/거래량/거래대금/상장주식수
      거래대금은 반드시 get_market_cap에서 별도 수집
    """
    ds = strdate(trade_day)
    result = {}

    # OHLCV (거래대금 없음)
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            result.update({
                "시가":   int(row["시가"]),
                "고가":   int(row["고가"]),
                "저가":   int(row["저가"]),
                "종가":   int(row["종가"]),
                "거래량": int(row["거래량"]),
                "등락률": float(row.get("등락률", 0.0)),
            })
    except Exception as e:
        result["_ohlcv_error"] = str(e)

    # 거래대금 + 시가총액: get_market_cap에서 수집
    try:
        df_cap = krx.get_market_cap(ds, ds, ticker)
        if not df_cap.empty:
            row = df_cap.iloc[-1]
            result["거래대금"] = int(row["거래대금"])
            result["시가총액"] = int(row["시가총액"])
    except Exception as e:
        result["_cap_error"] = str(e)

    return result


def fetch_fundamentals(ticker: str, trade_day: date) -> dict:
    """PER·PBR·시가총액 수집
    
    pykrx 실제 컬럼명:
      get_market_cap(날짜, 날짜, 티커) 반환:
        날짜, 시가총액, 거래량, 거래대금, 상장주식수
      get_market_fundamental(날짜, 날짜, 티커) 반환:
        날짜, BPS, PER, PBR, EPS, DIV, DPS
    """
    ds = strdate(trade_day)
    result = {}
    try:
        # 시가총액: get_market_cap은 (fromdate, todate, ticker) 순서
        df_cap = krx.get_market_cap(ds, ds, ticker)
        if not df_cap.empty:
            row = df_cap.iloc[-1]
            result["시가총액"] = int(row.get("시가총액", 0))
            result["상장주식수"] = int(row.get("상장주식수", 0))
    except Exception as e:
        result["_cap_error"] = str(e)

    try:
        # PER/PBR: get_market_fundamental은 (fromdate, todate, ticker) 순서
        df_fund = krx.get_market_fundamental(ds, ds, ticker)
        if not df_fund.empty:
            row = df_fund.iloc[-1]
            per = float(row.get("PER", 0))
            pbr = float(row.get("PBR", 0))
            result["PER"] = per if per > 0 else "-"
            result["PBR"] = pbr if pbr > 0 else "-"
    except Exception as e:
        result["_fund_error"] = str(e)

    return result


def fetch_trading_volume(ticker: str, trade_day: date) -> dict:
    """투자자별 수급(개인·외국인·기관) 순매수 수집

    pykrx 실제 반환 구조:
      get_market_trading_volume_by_date(from, to, ticker, on="순매수")
      컬럼: 기관합계 / 기타법인 / 개인 / 외국인합계 / 전체
      인덱스: 날짜 (DatetimeIndex)
      => row["개인"] 방식으로 접근 (iloc[-1] 후 컬럼명 직접 접근)
    """
    ds = strdate(trade_day)
    result = {}
    try:
        df = krx.get_market_trading_volume_by_date(ds, ds, ticker, on="순매수")
        if not df.empty:
            row = df.iloc[-1]
            cols = df.columns.tolist()
            # 개인 컬럼 찾기 (정확한 컬럼명 보장)
            개인_col   = next((c for c in cols if c == "개인"), None)
            외국인_col = next((c for c in cols if "외국인" in c), None)
            기관_col   = next((c for c in cols if "기관" in c and "합계" in c), None)

            result["개인"]   = int(row[개인_col])   if 개인_col   else 0
            result["외국인"] = int(row[외국인_col]) if 외국인_col else 0
            result["기관"]   = int(row[기관_col])   if 기관_col   else 0
            result["_supply_cols"] = cols  # 디버깅용
    except Exception as e:
        result["_supply_error"] = str(e)
    return result


def fetch_history(ticker: str, days: int = 60) -> list:
    """최근 N거래일 OHLCV 이력 수집
    - 기술적 지표 계산에 충분한 데이터 확보를 위해 기본 60일
    - MACD(26일 EMA), 볼린저밴드(20일), RSI(14일), OBV 모두 커버
    """
    today = date.today()
    # 넉넉하게 days*2 전부터 (공휴일·주말 고려)
    start = today - timedelta(days=days * 2)
    try:
        df = krx.get_market_ohlcv(strdate(start), strdate(today), ticker)
        if df.empty:
            return []
        records = []
        for dt, row in df.iterrows():
            records.append({
                "날짜":   dt.strftime("%Y-%m-%d"),
                "시가":   int(row.get("시가", 0)),
                "고가":   int(row.get("고가", 0)),
                "저가":   int(row.get("저가", 0)),
                "종가":   int(row.get("종가", 0)),
                "거래량": int(row.get("거래량", 0)),
            })
        return records[-days:]
    except Exception:
        return []


def calc_ema(values: list, period: int) -> list:
    """지수이동평균(EMA) 계산"""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]   # 초기값: SMA
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    # 앞부분 padding (None)
    return [None] * (period - 1) + ema


def calc_technical_indicators(history: list) -> dict:
    """RSI·볼린저밴드·OBV·MACD 계산
    
    모두 표준 공식 기반, 외부 라이브러리 없이 순수 파이썬으로 계산
    """
    if not history:
        return {}

    closes  = [r["종가"]   for r in history]
    volumes = [r["거래량"] for r in history]
    highs   = [r["고가"]   for r in history]
    lows    = [r["저가"]   for r in history]
    result  = {}

    # ── 이동평균 (MA5 / MA10 / MA20 / MA60) ──────────────────────
    for n in [5, 10, 20, 60]:
        if len(closes) >= n:
            result[f"MA{n}"] = round(sum(closes[-n:]) / n)

    # ── RSI(14) ───────────────────────────────────────────────────
    # 공식: RS = 평균상승폭 / 평균하락폭, RSI = 100 - 100/(1+RS)
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        # 최초 14일 단순평균
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs  = avg_gain / avg_loss
            rsi = round(100 - (100 / (1 + rs)), 2)

        result["RSI14"] = rsi
        # 해석
        if rsi >= 70:
            result["RSI14_signal"] = "과매수 ⚠️"
        elif rsi <= 30:
            result["RSI14_signal"] = "과매도 📉 (반등 가능)"
        else:
            result["RSI14_signal"] = "중립"

    # ── 볼린저 밴드 (20일, 2σ) ────────────────────────────────────
    # 공식: 중심선(MA20), 상단(MA20+2σ), 하단(MA20-2σ)
    bb_period = 20
    if len(closes) >= bb_period:
        window = closes[-bb_period:]
        ma20   = sum(window) / bb_period
        std    = (sum((c - ma20) ** 2 for c in window) / bb_period) ** 0.5

        bb_upper = round(ma20 + 2 * std)
        bb_lower = round(ma20 - 2 * std)
        bb_mid   = round(ma20)
        current  = closes[-1]
        bb_width = round((bb_upper - bb_lower) / bb_mid * 100, 2)  # 밴드폭(%)

        result["BB_upper"]  = bb_upper
        result["BB_mid"]    = bb_mid
        result["BB_lower"]  = bb_lower
        result["BB_width"]  = bb_width

        # 위치 해석
        if current >= bb_upper:
            result["BB_signal"] = f"상단 돌파 ⚠️ ({current:,} ≥ {bb_upper:,})"
        elif current <= bb_lower:
            result["BB_signal"] = f"하단 이탈 📉 ({current:,} ≤ {bb_lower:,})"
        elif current > bb_mid:
            pct = round((current - bb_mid) / (bb_upper - bb_mid) * 100)
            result["BB_signal"] = f"중심선 위 (상단까지 {100-pct}% 여유)"
        else:
            pct = round((bb_mid - current) / (bb_mid - bb_lower) * 100)
            result["BB_signal"] = f"중심선 아래 (하단까지 {100-pct}% 여유)"

        if bb_width < 5:
            result["BB_squeeze"] = "밴드 수축 — 큰 움직임 예고 ⚡"

    # ── OBV (On-Balance Volume) ────────────────────────────────────
    # 공식: 종가 상승일 → OBV += 거래량, 하락일 → OBV -= 거래량
    # 절댓값보다 방향(추세)이 중요
    if len(closes) >= 2:
        obv = 0
        obv_series = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
            obv_series.append(obv)

        obv_current = obv_series[-1]
        obv_5d_ago  = obv_series[-6] if len(obv_series) >= 6 else obv_series[0]
        obv_trend   = obv_current - obv_5d_ago

        result["OBV"]       = obv_current
        result["OBV_5d_change"] = obv_trend

        # 주가와 OBV 방향 비교 (다이버전스 감지)
        price_5d_change = closes[-1] - closes[-6] if len(closes) >= 6 else 0
        if price_5d_change > 0 and obv_trend > 0:
            result["OBV_signal"] = "주가↑ OBV↑ — 상승 신뢰도 높음 ✅"
        elif price_5d_change > 0 and obv_trend < 0:
            result["OBV_signal"] = "주가↑ OBV↓ — 상승 신뢰도 낮음 ⚠️ (다이버전스)"
        elif price_5d_change < 0 and obv_trend < 0:
            result["OBV_signal"] = "주가↓ OBV↓ — 하락 신뢰도 높음 📉"
        elif price_5d_change < 0 and obv_trend > 0:
            result["OBV_signal"] = "주가↓ OBV↑ — 하락 신뢰도 낮음 (저점 매집 가능성)"
        else:
            result["OBV_signal"] = "보합"

    # ── MACD (12/26/9) ─────────────────────────────────────────────
    # 공식: MACD = EMA12 - EMA26, 시그널 = MACD의 EMA9
    if len(closes) >= 35:  # 26 + 9 최소 필요
        ema12_series = calc_ema(closes, 12)
        ema26_series = calc_ema(closes, 26)

        # 두 EMA가 모두 유효한 구간만
        macd_series = []
        for e12, e26 in zip(ema12_series, ema26_series):
            if e12 is not None and e26 is not None:
                macd_series.append(round(e12 - e26, 2))

        if len(macd_series) >= 9:
            signal_series = calc_ema(macd_series, 9)
            signal_valid  = [s for s in signal_series if s is not None]

            if signal_valid:
                macd_val    = macd_series[-1]
                signal_val  = round(signal_valid[-1], 2)
                histogram   = round(macd_val - signal_val, 2)

                result["MACD"]      = macd_val
                result["MACD_signal"] = signal_val
                result["MACD_hist"]   = histogram

                # 이전 히스토그램과 비교 (골든크로스/데드크로스)
                if len(macd_series) >= 2 and len(signal_valid) >= 2:
                    prev_hist = macd_series[-2] - signal_valid[-2]
                    if prev_hist <= 0 and histogram > 0:
                        result["MACD_cross"] = "골든크로스 📈 (매수 신호)"
                    elif prev_hist >= 0 and histogram < 0:
                        result["MACD_cross"] = "데드크로스 📉 (매도 신호)"
                    elif histogram > 0:
                        result["MACD_cross"] = "MACD > 시그널 (강세 유지)"
                    else:
                        result["MACD_cross"] = "MACD < 시그널 (약세 유지)"

    return result


# ─────────────────────────────────────────
# 2. DART 공시 수집
# ─────────────────────────────────────────
def fetch_dart_disclosures(corp_code: str, dart_key: str, days: int = 1) -> list:
    """최근 N일 공시 목록 수집"""
    end   = date.today()
    start = end - timedelta(days=days)
    params = {
        "crtfc_key": dart_key,
        "corp_code": corp_code,
        "bgn_de":    strdate(start),
        "end_de":    strdate(end),
        "page_no":   "1",
        "page_count": "10",
    }
    url = DART_LIST_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "000":
            return [
                {
                    "날짜":   item.get("rcept_dt", ""),
                    "보고서": item.get("report_nm", ""),
                    "URL":    f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no','')}",
                }
                for item in data.get("list", [])
            ]
    except Exception as e:
        return [{"_error": str(e)}]
    return []


# ─────────────────────────────────────────
# 3. 네이버 뉴스 크롤링
# ─────────────────────────────────────────
def fetch_naver_news(query: str, max_items: int = 20) -> list:
    """네이버 뉴스 수집 — 최신순, 1일 이내, 필터 없이 전부 수집
    
    Claude 스킬에서 광고/중복/무의미 기사를 걸러냄
    URL: 사용자 지정 최신순 1일 필터 URL
    """
    url = NAVER_NEWS_URL.format(query=urllib.parse.quote(query))
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://search.naver.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    items = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")

        # 제목 + 링크
        titles = re.findall(
            r'class="news_tit"[^>]*title="([^"]+)"[^>]*href="([^"]+)"',
            html
        )
        # 언론사
        press_list = re.findall(r'class="info press">([^<]+)<', html)
        # 날짜/시간
        times = re.findall(r'class="info"[^>]*>\s*([^<]*(?:\d+분|\d+시간|어제|\d{4}\.\d{2}\.\d{2})[^<]*)<', html)
        # 요약
        descs = re.findall(r'class="dsc_txt_wrap">([^<]{10,300})<', html)

        for i, (title, link) in enumerate(titles[:max_items]):
            items.append({
                "제목":   title,
                "언론사": press_list[i].strip() if i < len(press_list) else "",
                "시간":   times[i].strip()      if i < len(times)      else "",
                "요약":   descs[i].strip()[:150] if i < len(descs)     else "",
                "링크":   link,
            })
    except Exception as e:
        items.append({"_error": str(e)})
    return items


# ─────────────────────────────────────────
# 4. 전체 수집 실행
# ─────────────────────────────────────────
def collect_all() -> dict:
    today     = date.today()
    trade_day = latest_trading_day(today)
    dart_key  = os.getenv("DART_API_KEY", "")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 시작 — {display_date(trade_day)}")

    result = {
        "_collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_trade_date":   trade_day.strftime("%Y-%m-%d"),
        "_report_type":  "daily",
        "주가":   {},
        "수급":   {},
        "기본정보": {},
        "이동평균": {},
        "피어":   {},
        "공시":   {},
        "뉴스":   {},
    }

    # ── 더즌 주가 ──────────────────────────
    ticker = TICKERS["더즌"]
    print("  ▶ 더즌 주가 수집...")
    result["주가"]    = fetch_price(ticker, trade_day)
    result["수급"]    = fetch_trading_volume(ticker, trade_day)
    result["기본정보"] = fetch_fundamentals(ticker, trade_day)

    print("  ▶ 이동평균 및 기술적 지표 계산...")
    history = fetch_history(ticker, days=60)
    indicators = calc_technical_indicators(history)
    result["이동평균"] = indicators
    result["이동평균"]["history_60d"] = history   # 스킬에서 참조용

    # ── 피어 그룹 ────────────────────────
    for name, t in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]),
                    ("쿠콘",        TICKERS["쿠콘"])]:
        print(f"  ▶ {name} 주가 수집...")
        p = fetch_price(t, trade_day)
        result["피어"][name] = {
            "종가":   p.get("종가", 0),
            "등락률": p.get("등락률", 0.0),
            "거래량": p.get("거래량", 0),
        }
        time.sleep(0.3)   # KRX 요청 간격

    # ── DART 공시 ────────────────────────
    print("  ▶ DART 공시 수집...")
    for name, corp_code in DART_CORP_CODES.items():
        if dart_key:
            result["공시"][name] = fetch_dart_disclosures(corp_code, dart_key, days=1)
        else:
            result["공시"][name] = [{"_note": "DART_API_KEY 미설정"}]
        time.sleep(0.5)

    # ── 네이버 뉴스 (필터 없이 전부 수집, 스킬에서 정리) ──────
    print("  ▶ 뉴스 수집...")
    news_queries = {
        "더즌":        "더즌 462860",
        "헥토파이낸셜": "헥토파이낸셜 주가",
        "쿠콘":        "쿠콘 주가",
    }
    for name, query in news_queries.items():
        result["뉴스"][name] = fetch_naver_news(query, max_items=20)
        time.sleep(0.5)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 완료")
    return result


# ─────────────────────────────────────────
# 5. 텔레그램 포맷 & 전송
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    """Claude 스킬 입력용 포맷으로 텔레그램 메시지 생성"""
    d  = data["_trade_date"]
    p  = data["주가"]
    s  = data["수급"]
    ma = data["이동평균"]
    info = data["기본정보"]

    # 등락 이모지
    chg = p.get("등락률", 0)
    arrow = "📈" if chg > 0 else ("📉" if chg < 0 else "➡️")

    # 거래대금 표시 (원 단위 → 억 단위 변환, 소수점 1자리)
    trade_amt = p.get('거래대금', 0)
    if trade_amt >= 100000000:
        trade_amt_str = f"{trade_amt / 100000000:.1f}억"
    elif trade_amt > 0:
        trade_amt_str = f"{trade_amt // 10000}만"
    else:
        trade_amt_str = "0억"

    # 시가총액
    mktcap = info.get('시가총액', 0)
    mktcap_str = f"{mktcap // 100000000:,}억" if mktcap > 0 else "-"

    lines = [
        f"📊 *더즌(462860) 일간 주가 리포트* — {d}",
        "",
        f"*{arrow} 주가 요약*",
        f"  종가: {p.get('종가',0):,}원  ({chg:+.2f}%)",
        f"  시가: {p.get('시가',0):,}  고가: {p.get('고가',0):,}  저가: {p.get('저가',0):,}",
        f"  거래량: {p.get('거래량',0):,}주  거래대금: {trade_amt_str}",
        "",
        "*📐 이동평균*",
        f"  MA5:  {ma.get('MA5','-'):,}" if ma.get('MA5') else "  MA5: 계산 중",
        f"  MA10: {ma.get('MA10','-'):,}" if ma.get('MA10') else "  MA10: 계산 중",
        f"  MA20: {ma.get('MA20','-'):,}" if ma.get('MA20') else "  MA20: 계산 중",
        "",
        "*📊 기술적 지표*",
        f"  RSI(14): {ma.get('RSI14','-')} — {ma.get('RSI14_signal','')}",
        f"  볼린저: 상단 {ma.get('BB_upper','-'):,} / 중심 {ma.get('BB_mid','-'):,} / 하단 {ma.get('BB_lower','-'):,}" if ma.get('BB_upper') else "  볼린저: 계산 중",
        f"    → {ma.get('BB_signal','')}",
        f"    → {ma.get('BB_squeeze','')}" if ma.get('BB_squeeze') else "",
        f"  OBV: {ma.get('OBV_signal','-')}",
        f"  MACD: {ma.get('MACD','-')} / 시그널: {ma.get('MACD_signal','-')} — {ma.get('MACD_cross','')}",
        "",
        "*👥 수급*",
        f"  개인: {s.get('개인',0):+,}  외국인: {s.get('외국인',0):+,}  기관: {s.get('기관',0):+,}",
        "",
        "*🏢 기본정보*",
        f"  시가총액: {mktcap_str}  PER: {info.get('PER','-')}  PBR: {info.get('PBR','-')}",
    ]

    # 피어
    lines += ["", "*🔗 피어 그룹*"]
    for name, peer in data["피어"].items():
        c = peer.get("등락률", 0)
        em = "▲" if c > 0 else ("▼" if c < 0 else "─")
        lines.append(f"  {name}: {peer.get('종가',0):,}원 {em}{abs(c):.2f}%")

    # 공시
    lines += ["", "*📋 오늘 공시*"]
    any_disc = False
    for name, discs in data["공시"].items():
        real = [d for d in discs if "_error" not in d and "_note" not in d]
        if real:
            any_disc = True
            for disc in real:
                lines.append(f"  [{name}] {disc.get('보고서','')} — {disc.get('URL','')}")
    if not any_disc:
        lines.append("  해당 없음")

    # 뉴스 (언론사·시간 포함, 전체 첨부 — Claude 스킬에서 정리)
    lines += ["", "*📰 오늘 뉴스 (원본 전체 — Claude가 정리)*"]
    any_news = False
    for name, articles in data["뉴스"].items():
        real = [a for a in articles if "_error" not in a]
        if real:
            any_news = True
            lines.append(f"  ▸ *{name}* ({len(real)}건)")
            for a in real:
                press = a.get("언론사", "")
                t     = a.get("시간", "")
                press_str = f"[{press}]" if press else ""
                time_str  = f" {t}"      if t     else ""
                lines.append(f"    {press_str}{time_str} {a.get('제목','')}")
                lines.append(f"    {a.get('링크','')}")
    if not any_news:
        lines.append("  해당 없음")

    lines += [
        "",
        "─────────────────────",
        "📌 _Claude 스킬 사용법_",
        "_아래 JSON을 Claude 채팅에 붙여넣고_",
        "_\"일간 리포트 노션에 올려줘\" 입력_",
    ]

    return "\n".join(lines)


def send_telegram(text: str, json_data: dict):
    """텔레그램으로 메시지 + JSON 전송"""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 콘솔 출력으로 대체")
        print("\n" + "="*60)
        print(text)
        print("\n[JSON 데이터]")
        print(json.dumps(json_data, ensure_ascii=False, indent=2))
        return

    base_url = f"https://api.telegram.org/bot{token}"

    # 1) 포맷된 요약 메시지 전송
    resp = requests.post(f"{base_url}/sendMessage", json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })
    if resp.status_code != 200:
        print(f"텔레그램 메시지 전송 실패: {resp.text}")

    # 2) JSON 데이터를 파일로 전송 (Claude 스킬용)
    json_bytes = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    resp2 = requests.post(f"{base_url}/sendDocument", data={
        "chat_id": chat_id,
        "caption": "📎 Claude 스킬 입력용 JSON — 이 파일을 Claude 채팅에 붙여넣으세요",
    }, files={
        "document": (f"dozen_{json_data['_trade_date']}.json", json_bytes, "application/json")
    })
    if resp2.status_code != 200:
        print(f"텔레그램 파일 전송 실패: {resp2.text}")
    else:
        print("✅ 텔레그램 전송 완료")


# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────
def main():
    data = collect_all()
    text = format_telegram(data)
    send_telegram(text, data)

    # 로컬 백업 저장
    out_path = f"output/dozen_{data['_trade_date']}.json"
    os.makedirs("output", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 저장: {out_path}")


if __name__ == "__main__":
    main()
