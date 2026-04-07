"""
더즌(462860) 일간 주가 데이터 수집기 v4
===========================================
데이터 소스 전략 (GitHub Actions 검증 완료):
  - OHLCV + 수급 + 거래대금: 핀업 (finance.finup.co.kr) — requests HTML 파싱
  - 기술적 지표 (MA/RSI/BB/OBV/MACD): pykrx 이력 → 로컬 계산
  - 공시: DART OpenAPI
  - 뉴스: 네이버 뉴스 (수정된 URL)

환경변수 (GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  DART_API_KEY  (opendart.fss.or.kr — 무료)
"""

import os, json, re, time, urllib.request, urllib.parse
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
DART_CORP_CODES = {
    "더즌":        "01615947",
    "헥토파이낸셜": "00669540",
    "쿠콘":        "00798833",
}
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.finup.co.kr/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ─────────────────────────────────────────
# 날짜 헬퍼
# ─────────────────────────────────────────
def latest_trading_day(today: date) -> date:
    """거래일 계산 (KST 기준)

    pykrx/핀업 수급 데이터 제공 기준 (pykrx README):
      - 당일 최종 매매내역은 오후 6시(18:00 KST) 이후 제공
      - 18:00 이전 실행 시 → 전 거래일 데이터 조회
      - 18:00 이후 실행 시 → 당일 데이터 조회

    실행 시간별 동작:
      평일 18:00 이전 → 전 거래일 (월요일이면 금요일)
      평일 18:00 이후 → 당일
      토/일 → 직전 금요일 (시간 무관)
    """
    d = today
    now_kst = datetime.utcnow() + timedelta(hours=9)

    if d.weekday() >= 5:
        # 주말(토/일) → 직전 금요일, 시간 무관
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    else:
        # 평일: KST 18:00 이전이면 전 거래일
        if now_kst.hour < 18:
            d -= timedelta(days=1)
            # 월요일 18시 이전 → 금요일로
            while d.weekday() >= 5:
                d -= timedelta(days=1)

    return d

def strdate(d: date) -> str:
    return d.strftime("%Y%m%d")

def display_date(d: date) -> str:
    days = ["월","화","수","목","금","토","일"]
    return d.strftime(f"%Y-%m-%d({days[d.weekday()]})")

# ─────────────────────────────────────────
# 핀업 파싱 헬퍼
# ─────────────────────────────────────────
def parse_amt(text: str) -> int:
    """'150억 4,400만' 형태 → 원 단위 정수 변환"""
    total = 0
    m_eok = re.search(r'([\d,]+)억', text)
    m_man = re.search(r'([\d,]+)만', text)
    if m_eok:
        total += int(m_eok.group(1).replace(',', '')) * 100_000_000
    if m_man:
        total += int(m_man.group(1).replace(',', '')) * 10_000
    return total

def parse_int(text: str) -> int:
    """'4,173,334' → 4173334"""
    return int(re.sub(r'[^\d]', '', text) or '0')

def parse_signed_int(text: str) -> int:
    """'-88,871' → -88871, '+64,611' → 64611"""
    text = text.strip()
    sign = -1 if text.startswith('-') else 1
    return sign * int(re.sub(r'[^\d]', '', text) or '0')

# ─────────────────────────────────────────
# 1. 핀업에서 주가 + 수급 수집
# ─────────────────────────────────────────
def fetch_finup(ticker: str) -> dict:
    """핀업에서 시가/고가/저가/거래량/거래대금 + 10일 시세/수급 파싱

    README 분석 결과:
    - pykrx adjusted=True(기본값) → 네이버 fchart → 거래대금 컬럼 없음
    - pykrx 수급 API → KRX 직접 호출 → GitHub Actions IP 차단
    - 따라서 거래대금·수급 모두 핀업에서 직접 수집
    
    패턴 설계 원칙:
    - 원본 HTML / 마크다운 변환 텍스트 / 탭 구분 등 모든 형식에 대응하는 통합 패턴 사용
    - 거래대금: "거래대금(원)" 이후 "X억 Y만" 형식 추출 (어떤 구분자든 대응)
    - 수급: 날짜+숫자7개 통합 패턴 (HTML 태그 / 마크다운 테이블 / 탭 모두 대응)
    """
    url = f"https://finance.finup.co.kr/Stock/{ticker}"
    result = {
        "시가": 0, "고가": 0, "저가": 0,
        "거래량": 0, "거래대금": 0,
        "기준일시": "",
        "history": [],
        "_source": "finup",
    }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        html = resp.text

        # ── 기준일시 ──────────────────────────────
        dt_m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*기준", html)
        if dt_m:
            result["기준일시"] = dt_m.group(1).strip()

        # ── 시가/고가/저가 ─────────────────────────
        # 원본 HTML: <td>시가</td>...<td>3,220</td> 또는 텍스트: "시가\n  3,220"
        for label in ["시가", "고가", "저가"]:
            m = re.search(rf"{label}[^\d]{{1,50}}?([\d,]{{4,}})", html, re.DOTALL)
            if m:
                val = parse_int(m.group(1))
                if val > 0:
                    result[label] = val

        # ── 거래량 ────────────────────────────────
        # "거래량" 다음 숫자 (쉼표 포함, 최소 6자리 이상 = 수십만 이상)
        vol_m = re.search(r"거래량[^\d]{1,50}?([\d,]{6,})", html, re.DOTALL)
        if vol_m:
            result["거래량"] = parse_int(vol_m.group(1))

        # ── 거래대금 ──────────────────────────────
        # "거래대금(원)" 이후 "X억 Y만" 패턴 — 어떤 HTML 구조든 대응
        amt_m = re.search(
            r"거래대금\(원\)[^\d억만]{0,50}?([\d,]+억[\d\s,만]*)",
            html, re.DOTALL
        )
        if amt_m:
            result["거래대금"] = parse_amt(amt_m.group(1))

        # ── 10일 시세 + 수급 테이블 ────────────────
        # 통합 패턴: 날짜 + 숫자 7개 (구분자 무관 — HTML태그/마크다운/탭 모두 대응)
        num_sep = r"[^\d-]+"
        unified = (
            r"(202\d-\d{2}-\d{2})" + num_sep +  # 날짜
            r"([\d,]+)" + num_sep +                 # 종가
            r"([\d,]+)" + num_sep +                 # 전일비
            r"([+-][\d.]+%)" + num_sep +            # 등락률
            r"([+-]?[\d,]+)" + num_sep +            # 개인
            r"([+-]?[\d,]+)" + num_sep +            # 외국인
            r"([+-]?[\d,]+)"                        # 기관
        )
        rows = re.findall(unified, html)
        for row in rows[:10]:
            result["history"].append({
                "날짜":   row[0],
                "종가":   parse_int(row[1]),
                "전일비": parse_int(row[2]),
                "등락률": row[3],
                "개인":   parse_signed_int(row[4]),
                "외국인": parse_signed_int(row[5]),
                "기관":   parse_signed_int(row[6]),
            })

    except Exception as e:
        result["_error"] = str(e)
        print(f"    [WARN] 핀업 오류 ({ticker}): {e}")

    return result


# ─────────────────────────────────────────
# 2. pykrx OHLCV (종가·등락률)
# ─────────────────────────────────────────
def fetch_ohlcv(ticker: str, trade_day: date) -> dict:
    """pykrx로 당일 OHLCV. 내부적으로 네이버 fchart 사용."""
    ds = strdate(trade_day)
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            return {
                "시가":   int(row.get("시가", 0)),
                "고가":   int(row.get("고가", 0)),
                "저가":   int(row.get("저가", 0)),
                "종가":   int(row.get("종가", 0)),
                "거래량": int(row.get("거래량", 0)),
                "등락률": float(row.get("등락률", 0.0)),
            }
    except Exception as e:
        print(f"    [WARN] pykrx OHLCV 오류 ({ticker}): {e}")
    return {}


# ─────────────────────────────────────────
# 3. 기술적 지표 계산
# ─────────────────────────────────────────
def fetch_history_for_indicators(ticker: str, days: int = 60) -> list:
    """pykrx로 60일 이력 (MA/RSI/BB/OBV/MACD 계산용)"""
    today = date.today()
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
    except Exception as e:
        print(f"    [WARN] 이력 수집 오류: {e}")
        return []

def calc_ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema

def calc_technical_indicators(history: list) -> dict:
    if not history:
        return {}
    closes  = [r["종가"]   for r in history]
    volumes = [r["거래량"] for r in history]
    result  = {}

    # MA
    for n in [5, 10, 20, 60]:
        if len(closes) >= n:
            result[f"MA{n}"] = round(sum(closes[-n:]) / n)

    # RSI(14)
    if len(closes) >= 15:
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d,0) for d in deltas]
        losses = [abs(min(d,0)) for d in deltas]
        ag, al = sum(gains[-14:])/14, sum(losses[-14:])/14
        rsi = 100.0 if al == 0 else round(100 - 100/(1 + ag/al), 2)
        result["RSI14"] = rsi
        result["RSI14_signal"] = (
            "과매수 ⚠️" if rsi >= 70 else
            "과매도 📉 (반등 가능)" if rsi <= 30 else "중립"
        )

    # 볼린저밴드(20일, 2σ)
    if len(closes) >= 20:
        w   = closes[-20:]
        ma  = sum(w)/20
        std = (sum((c-ma)**2 for c in w)/20)**0.5
        bbu, bbl, bbm = round(ma+2*std), round(ma-2*std), round(ma)
        cur = closes[-1]
        result.update({"BB_upper": bbu, "BB_mid": bbm, "BB_lower": bbl,
                        "BB_width": round((bbu-bbl)/bbm*100, 2)})
        if cur >= bbu:
            result["BB_signal"] = f"상단 돌파 ⚠️ ({cur:,} ≥ {bbu:,})"
        elif cur <= bbl:
            result["BB_signal"] = f"하단 이탈 📉 ({cur:,} ≤ {bbl:,})"
        elif cur > bbm:
            result["BB_signal"] = f"중심선 위 (상단까지 {round((bbu-cur)/(bbu-bbm)*100)}% 여유)"
        else:
            result["BB_signal"] = f"중심선 아래 (하단까지 {round((cur-bbl)/(bbm-bbl)*100)}% 여유)"
        if result["BB_width"] < 5:
            result["BB_squeeze"] = "밴드 수축 — 큰 움직임 예고 ⚡"

    # OBV
    if len(closes) >= 2:
        obv, series = 0, [0]
        for i in range(1, len(closes)):
            obv += volumes[i] if closes[i]>closes[i-1] else (-volumes[i] if closes[i]<closes[i-1] else 0)
            series.append(obv)
        p5c = closes[-1]-closes[-6] if len(closes)>=6 else 0
        p5o = series[-1]-series[-6] if len(series)>=6 else 0
        result["OBV"] = series[-1]
        result["OBV_signal"] = (
            "주가↑ OBV↑ — 상승 신뢰도 높음 ✅" if p5c>0 and p5o>0 else
            "주가↑ OBV↓ — 다이버전스 ⚠️"      if p5c>0 and p5o<0 else
            "주가↓ OBV↓ — 하락 신뢰도 높음 📉" if p5c<0 and p5o<0 else
            "주가↓ OBV↑ — 저점 매집 가능성"   if p5c<0 and p5o>0 else "보합"
        )

    # MACD(12/26/9)
    if len(closes) >= 35:
        e12 = calc_ema(closes, 12)
        e26 = calc_ema(closes, 26)
        macd = [round(a-b,2) for a,b in zip(e12,e26) if a and b]
        if len(macd) >= 9:
            sig = calc_ema(macd, 9)
            sv_list = [s for s in sig if s is not None]
            if sv_list:
                mv, sv = macd[-1], round(sv_list[-1], 2)
                hist = round(mv - sv, 2)
                result.update({"MACD": mv, "MACD_signal": sv, "MACD_hist": hist})
                if len(macd)>=2 and len(sv_list)>=2:
                    ph = macd[-2] - sv_list[-2]
                    result["MACD_cross"] = (
                        "골든크로스 📈 (매수 신호)" if ph<=0 and hist>0 else
                        "데드크로스 📉 (매도 신호)" if ph>=0 and hist<0 else
                        "MACD > 시그널 (강세 유지)" if hist>0 else
                        "MACD < 시그널 (약세 유지)"
                    )
    return result


# ─────────────────────────────────────────
# 4. DART 공시
# ─────────────────────────────────────────
def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=1)
    params = {
        "crtfc_key": dart_key, "corp_code": corp_code,
        "bgn_de": strdate(start), "end_de": strdate(end),
        "page_no": "1", "page_count": "10",
    }
    url = DART_LIST_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "000":
            return [
                {"날짜": i["rcept_dt"], "보고서": i["report_nm"],
                 "URL": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"}
                for i in data.get("list", [])
            ]
    except Exception as e:
        return [{"_error": str(e)}]
    return []


# ─────────────────────────────────────────
# 5. 뉴스
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 20) -> list:
    """네이버 뉴스 — 최신순, 1일 이내"""
    # URL을 직접 조합 (인코딩 문제 방지)
    base = "https://search.naver.com/search.naver"
    params = urllib.parse.urlencode({
        "where": "news",
        "query": query,
        "sm": "tab_opt",
        "sort": "1",
        "nso": "so:dd,p:1d",
    })
    url = f"{base}?{params}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://search.naver.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    items = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        titles = re.findall(r'class="news_tit"[^>]*title="([^"]+)"[^>]*href="([^"]+)"', html)
        press  = re.findall(r'class="info press">([^<]+)<', html)
        times  = re.findall(r'class="info"[^>]*>\s*([^<]*(?:\d+분|\d+시간|어제|\d{4}\.\d{2}\.\d{2})[^<]*)<', html)
        descs  = re.findall(r'class="dsc_txt_wrap">([^<]{10,300})<', html)
        for i, (title, link) in enumerate(titles[:max_items]):
            items.append({
                "제목":   title,
                "언론사": press[i].strip() if i < len(press) else "",
                "시간":   times[i].strip() if i < len(times) else "",
                "요약":   descs[i].strip()[:150] if i < len(descs) else "",
                "링크":   link,
            })
        print(f"    뉴스 {len(items)}건 수집 ('{query}')")
    except Exception as e:
        items.append({"_error": str(e)})
        print(f"    [WARN] 뉴스 오류 ('{query}'): {e}")
    return items


# ─────────────────────────────────────────
# 6. 전체 수집
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
        "주가": {}, "수급": {}, "기본정보": {},
        "이동평균": {}, "피어": {}, "공시": {}, "뉴스": {},
    }

    # ── 더즌: 핀업 (시가/고가/저가/거래량/거래대금/수급) ──
    t = TICKERS["더즌"]
    print("  ▶ 더즌 — 핀업 수집...")
    finup = fetch_finup(t)
    print(f"    기준: {finup.get('기준일시','?')} | 거래대금: {finup.get('거래대금',0):,}원 | 이력: {len(finup.get('history',[]))}건")

    # ── 더즌: pykrx (당일 종가/등락률 확인용) ──
    print("  ▶ 더즌 — pykrx OHLCV...")
    ohlcv = fetch_ohlcv(t, trade_day)
    print(f"    종가: {ohlcv.get('종가','?')} 등락률: {ohlcv.get('등락률','?')}%")

    # 데이터 병합 (핀업 우선, pykrx 보완)
    result["주가"] = {
        "시가":   finup.get("시가")   or ohlcv.get("시가", 0),
        "고가":   finup.get("고가")   or ohlcv.get("고가", 0),
        "저가":   finup.get("저가")   or ohlcv.get("저가", 0),
        "종가":   ohlcv.get("종가")   or (finup["history"][0]["종가"] if finup.get("history") else 0),
        "거래량": finup.get("거래량") or ohlcv.get("거래량", 0),
        "거래대금": finup.get("거래대금", 0),
        "등락률": ohlcv.get("등락률", 0.0),
    }

    # 수급: 핀업 history 최신 행
    if finup.get("history"):
        latest = finup["history"][0]
        result["수급"] = {
            "개인":   latest.get("개인", 0),
            "외국인": latest.get("외국인", 0),
            "기관":   latest.get("기관", 0),
        }
        result["기본정보"]["기준일시"] = finup.get("기준일시", "")

    # ── 기술적 지표 ──────────────────────
    print("  ▶ 기술적 지표 계산...")
    history    = fetch_history_for_indicators(t, days=60)
    indicators = calc_technical_indicators(history)
    result["이동평균"] = {**indicators, "history_60d": history}
    print(f"    MA5={indicators.get('MA5','?')} RSI={indicators.get('RSI14','?')}")

    # ── 피어: pykrx ──────────────────────
    for name, pt in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]),
                     ("쿠콘",         TICKERS["쿠콘"])]:
        print(f"  ▶ {name}...")
        p = fetch_ohlcv(pt, trade_day)
        result["피어"][name] = {
            "종가":   p.get("종가", 0),
            "등락률": p.get("등락률", 0.0),
            "거래량": p.get("거래량", 0),
        }
        time.sleep(0.3)

    # ── DART ─────────────────────────────
    print("  ▶ DART 공시...")
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else [{"_note":"DART_API_KEY 미설정"}]
        time.sleep(0.3)

    # ── 뉴스 ─────────────────────────────
    print("  ▶ 뉴스 수집...")
    for name, q in [
        ("더즌",        "더즌 462860"),
        ("헥토파이낸셜", "헥토파이낸셜 주가"),
        ("쿠콘",        "쿠콘 주가"),
    ]:
        result["뉴스"][name] = fetch_news(q, max_items=20)
        time.sleep(0.5)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 완료")
    return result


# ─────────────────────────────────────────
# 7. 텔레그램 포맷 & 전송
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    d    = data["_trade_date"]
    p    = data["주가"]
    s    = data["수급"]
    ma   = data["이동평균"]
    chg  = p.get("등락률", 0)
    arrow = "📈" if chg > 0 else ("📉" if chg < 0 else "➡️")

    amt = p.get("거래대금", 0)
    amt_str = f"{amt/100_000_000:.1f}억" if amt >= 100_000_000 else (f"{amt//10_000}만" if amt > 0 else "-")

    lines = [
        f"📊 *더즌(462860) 일간 주가 리포트* — {d}",
        "",
        f"*{arrow} 주가 요약*",
        f"  종가: {p.get('종가',0):,}원  ({chg:+.2f}%)",
        f"  시가: {p.get('시가',0):,}  고가: {p.get('고가',0):,}  저가: {p.get('저가',0):,}",
        f"  거래량: {p.get('거래량',0):,}주  거래대금: {amt_str}",
        "",
        "*📐 이동평균*",
        (f"  MA5: {ma['MA5']:,}  MA10: {ma['MA10']:,}  MA20: {ma['MA20']:,}"
         if ma.get('MA5') else "  이동평균 계산 중"),
        "",
        "*📊 기술적 지표*",
        f"  RSI(14): {ma.get('RSI14','-')} — {ma.get('RSI14_signal','')}",
        (f"  볼린저: 상단 {ma['BB_upper']:,} / 중심 {ma['BB_mid']:,} / 하단 {ma['BB_lower']:,}"
         if ma.get('BB_upper') else "  볼린저: 계산 중"),
        f"    → {ma.get('BB_signal','')}" if ma.get('BB_signal') else "",
        f"    → {ma.get('BB_squeeze','')}" if ma.get('BB_squeeze') else "",
        f"  OBV: {ma.get('OBV_signal','-')}",
        f"  MACD: {ma.get('MACD','-')} / 시그널: {ma.get('MACD_signal','-')} — {ma.get('MACD_cross','')}",
        "",
        "*👥 수급 (핀업 기준)*",
        f"  개인: {s.get('개인',0):+,}  외국인: {s.get('외국인',0):+,}  기관: {s.get('기관',0):+,}",
        "",
        "*🔗 피어 그룹*",
    ]
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

    # 뉴스
    lines += ["", "*📰 오늘 뉴스 (전체 — Claude가 정리)*"]
    any_news = False
    for name, articles in data["뉴스"].items():
        real = [a for a in articles if "_error" not in a]
        if real:
            any_news = True
            lines.append(f"  ▸ *{name}* ({len(real)}건)")
            for a in real:
                press = f"[{a['언론사']}]" if a.get('언론사') else ""
                t     = f" {a['시간']}"   if a.get('시간') else ""
                lines.append(f"    {press}{t} {a.get('제목','')}")
                lines.append(f"    {a.get('링크','')}")
    if not any_news:
        lines.append("  해당 없음")

    lines += [
        "", "─────────────────────",
        "📌 _Claude 스킬 사용법_",
        "_JSON 파일을 Claude 채팅에 붙여넣고_",
        "_\"일간 리포트 노션에 올려줘\" 입력_",
    ]
    return "\n".join(l for l in lines if l is not None)


def send_telegram(text: str, json_data: dict):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("\n" + "="*60)
        print(text)
        print("\n[JSON]")
        print(json.dumps(json_data, ensure_ascii=False, indent=2))
        return
    base = f"https://api.telegram.org/bot{token}"
    r1 = requests.post(f"{base}/sendMessage", json={
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    if r1.status_code != 200:
        print(f"메시지 전송 실패: {r1.text}")
    jb = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    r2 = requests.post(f"{base}/sendDocument", data={
        "chat_id": chat_id,
        "caption": f"📎 Claude 스킬 입력용 — {json_data['_trade_date']}",
    }, files={"document": (f"dozen_{json_data['_trade_date']}.json", jb, "application/json")})
    if r2.status_code != 200:
        print(f"파일 전송 실패: {r2.text}")
    else:
        print("✅ 텔레그램 전송 완료")


# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────
def main():
    data = collect_all()
    text = format_telegram(data)
    send_telegram(text, data)
    os.makedirs("output", exist_ok=True)
    path = f"output/dozen_{data['_trade_date']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 저장: {path}")

if __name__ == "__main__":
    main()

# ─────────────────────────────────────────
# README 주요 발견사항 (pykrx 가이드 기반)
# ─────────────────────────────────────────
# 1. get_market_ohlcv(adjusted=True, 기본값):
#    → 네이버 fchart 사용 → 거래대금 컬럼 없음 (시가/고가/저가/종가/거래량/등락률만)
#    → GitHub Actions OK ✅
#
# 2. get_market_ohlcv(adjusted=False):
#    → KRX 직접 호출 → 거래대금 있음 but GitHub Actions IP 차단 ❌
#
# 3. get_market_trading_volume/value_by_date:
#    → KRX 직접 호출 (ISIN 조회 선행 필요) → GitHub Actions IP 차단 ❌
#    → README: "당일자 최종 매매내역은 오후 6시 이후에 제공"
#
# 4. 해결책:
#    - 거래대금·수급: 핀업(finance.finup.co.kr) 직접 크롤링
#    - 시가/고가/저가/종가/거래량: pykrx (네이버 fchart 경유)
#    - 기술적 지표(MA/RSI/BB/OBV/MACD): 60일 이력으로 로컬 계산
