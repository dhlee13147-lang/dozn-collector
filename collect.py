"""
더즌(462860) 일간 주가 데이터 수집기 v6
=========================================
데이터 소스:
  OHLCV + 등락률     : pykrx (네이버 fchart 경유)
  거래대금/시가총액   : 네이버 금융 시장요약 (sise_market_sum) ← 로컬 테스트 확인
  PER / PBR          : 네이버 금융 시장요약 (동일)
  기술적 지표        : pykrx 60일 이력 → 로컬 계산
  공시               : DART OpenAPI
  뉴스               : 네이버 뉴스

수급(개인/외국인/기관): KRX 엔드포인트 차단으로 미제공 → "-" 표기

환경변수 (GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  DART_API_KEY
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
from bs4 import BeautifulSoup
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ─────────────────────────────────────────
# 날짜 헬퍼
# ─────────────────────────────────────────
def latest_trading_day(today: date) -> date:
    """거래일 계산 (KST 기준)
    - 주말 → 직전 금요일 (시간 무관)
    - 평일 18:00 이전 → 전 거래일
    - 평일 18:00 이후 → 당일
    """
    d = today
    now_kst = datetime.utcnow() + timedelta(hours=9)
    if d.weekday() >= 5:
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    else:
        if now_kst.hour < 18:
            d -= timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
    return d

def strdate(d: date) -> str:
    return d.strftime("%Y%m%d")

def display_date(d: date) -> str:
    days = ["월","화","수","목","금","토","일"]
    return d.strftime(f"%Y-%m-%d({days[d.weekday()]})")

def parse_number(text: str) -> float:
    """쉼표 제거 후 숫자 변환. 실패 시 0.0"""
    try:
        return float(text.replace(",", "").strip())
    except:
        return 0.0


# ─────────────────────────────────────────
# 1. pykrx — OHLCV (거래량·등락률)
# ─────────────────────────────────────────
def fetch_ohlcv(ticker: str, trade_day: date) -> dict:
    """pykrx: 시가/고가/저가/종가/거래량/등락률
    ※ 거래대금은 네이버 시장요약에서 별도 수집
    """
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
        print(f"    [WARN] OHLCV 빈 DataFrame ({ticker})")
    except Exception as e:
        print(f"    [ERROR] OHLCV ({ticker}): {e}")
    return {}


# ─────────────────────────────────────────
# 2. 네이버 시장요약 — 거래대금·시가총액·PER·PBR
# ─────────────────────────────────────────
def fetch_naver_market_sum(ticker: str) -> dict:
    """네이버 금융 시장요약(sise_market_sum)에서 특정 종목 검색
    
    로컬 테스트 확인된 컬럼 매핑 (더즌 기준):
      cols[6]  거래량
      cols[7]  거래대금 (백만원)
      cols[8]  상장주식수 (천주)
      cols[9]  시가총액 (억원)
      cols[10] PER
      cols[11] PBR
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # 세션 쿠키 설정 (필드 선택: 거래대금·PER·PBR·시가총액)
    field_url = (
        "https://finance.naver.com/sise/field_submit.naver"
        "?menu=market_sum"
        "&returnUrl=http://finance.naver.com/sise/sise_market_sum.naver?sosok=1"
        "&fieldIds=market_sum"
        "&fieldIds=listed_stock_cnt"
        "&fieldIds=amount"
        "&fieldIds=per"
        "&fieldIds=pbr"
        "&fieldIds=quant"
    )
    try:
        session.get(field_url, timeout=10)
    except Exception as e:
        print(f"    [WARN] 필드 설정 실패: {e}")

    # 코스닥(sosok=1) 전 페이지 순회 — 더즌은 9페이지에서 발견됨
    for page in range(1, 51):
        try:
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page={page}"
            resp = session.get(url, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="type_2")
            if not table:
                break

            for row in table.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) < 12:
                    continue
                link = cols[1].find("a")
                if not link:
                    continue
                href = link.get("href", "")
                code = href.split("code=")[-1].strip() if "code=" in href else ""
                if code != ticker:
                    continue

                # 발견
                amt_million  = parse_number(cols[7].text)   # 거래대금 (백만원)
                mktcap_eok   = parse_number(cols[9].text)   # 시가총액 (억원)
                per          = parse_number(cols[10].text)  # PER
                pbr          = parse_number(cols[11].text)  # PBR

                result = {
                    "거래대금": int(amt_million * 1_000_000),         # 원 단위
                    "시가총액": int(mktcap_eok  * 100_000_000),       # 원 단위
                    "PER":     per if per > 0 else "-",
                    "PBR":     pbr if pbr > 0 else "-",
                }
                print(f"    발견 p{page}: 거래대금={amt_million:.0f}백만 시총={mktcap_eok:.0f}억 PER={per} PBR={pbr}")
                return result

        except Exception as e:
            print(f"    [ERROR] 시장요약 p{page}: {e}")
            break

    print(f"    [WARN] 네이버 시장요약에서 {ticker} 미발견")
    return {}


# ─────────────────────────────────────────
# 3. pykrx — 60일 이력 (기술적 지표용)
# ─────────────────────────────────────────
def fetch_history(ticker: str, days: int = 60) -> list:
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
        print(f"    [ERROR] 이력: {e}")
        return []


# ─────────────────────────────────────────
# 4. 기술적 지표 계산
# ─────────────────────────────────────────
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
        ag = sum(max(d,0) for d in deltas[-14:]) / 14
        al = sum(abs(min(d,0)) for d in deltas[-14:]) / 14
        rsi = 100.0 if al == 0 else round(100 - 100/(1+ag/al), 2)
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
        result.update({"BB_upper":bbu, "BB_mid":bbm, "BB_lower":bbl,
                        "BB_width":round((bbu-bbl)/bbm*100,2)})
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
                hist = round(mv-sv, 2)
                result.update({"MACD":mv, "MACD_signal":sv, "MACD_hist":hist})
                if len(macd)>=2 and len(sv_list)>=2:
                    ph = macd[-2]-sv_list[-2]
                    result["MACD_cross"] = (
                        "골든크로스 📈 (매수 신호)" if ph<=0 and hist>0 else
                        "데드크로스 📉 (매도 신호)" if ph>=0 and hist<0 else
                        "MACD > 시그널 (강세 유지)" if hist>0 else
                        "MACD < 시그널 (약세 유지)"
                    )
    return result


# ─────────────────────────────────────────
# 5. DART 공시
# ─────────────────────────────────────────
def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=1)
    params = {
        "crtfc_key": dart_key, "corp_code": corp_code,
        "bgn_de": strdate(start), "end_de": strdate(end),
        "page_no":"1", "page_count":"10",
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
# 6. 뉴스
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 20) -> list:
    base = "https://search.naver.com/search.naver"
    params = urllib.parse.urlencode({
        "where": "news", "query": query,
        "sm": "tab_opt", "sort": "1", "nso": "so:dd,p:1d",
    })
    url = f"{base}?{params}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
        print(f"    뉴스 {len(items)}건 (\'{query}\')")
    except Exception as e:
        items.append({"_error": str(e)})
        print(f"    [WARN] 뉴스 오류: {e}")
    return items


# ─────────────────────────────────────────
# 7. 전체 수집
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
        "주가": {}, "수급": {"개인":"-","외국인":"-","기관":"-"},
        "기본정보": {}, "이동평균": {}, "피어": {}, "공시": {}, "뉴스": {},
    }

    t = TICKERS["더즌"]

    # ── OHLCV (pykrx) ──────────────────────────────
    print("  ▶ OHLCV (pykrx)...")
    ohlcv = fetch_ohlcv(t, trade_day)
    print(f"    종가={ohlcv.get('종가','?'):,} 등락률={ohlcv.get('등락률','?'):.2f}%")

    # ── 거래대금·시가총액·PER·PBR (네이버 시장요약) ──
    print("  ▶ 거래대금·시가총액·PER·PBR (네이버 시장요약)...")
    market = fetch_naver_market_sum(t)

    # 병합
    result["주가"] = {
        **ohlcv,
        "거래대금": market.get("거래대금", 0),
    }
    result["기본정보"] = {
        "시가총액": market.get("시가총액", 0),
        "PER":      market.get("PER", "-"),
        "PBR":      market.get("PBR", "-"),
    }

    # ── 기술적 지표 ────────────────────────────────
    print("  ▶ 기술적 지표 계산...")
    history    = fetch_history(t, days=60)
    indicators = calc_technical_indicators(history)
    result["이동평균"] = {**indicators, "history_60d": history}
    print(f"    MA5={indicators.get('MA5','?'):,} RSI={indicators.get('RSI14','?')}")

    # ── 피어 (pykrx OHLCV) ────────────────────────
    for name, pt in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]),
                     ("쿠콘",         TICKERS["쿠콘"])]:
        print(f"  ▶ {name}...")
        p = fetch_ohlcv(pt, trade_day)
        result["피어"][name] = {
            "종가":   p.get("종가",0),
            "등락률": p.get("등락률",0.0),
            "거래량": p.get("거래량",0),
        }
        time.sleep(0.3)

    # ── DART ──────────────────────────────────────
    print("  ▶ DART 공시...")
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else [{"_note":"DART_API_KEY 미설정"}]
        time.sleep(0.3)

    # ── 뉴스 ──────────────────────────────────────
    print("  ▶ 뉴스 수집...")
    for name, q in [("더즌","더즌 462860"),("헥토파이낸셜","헥토파이낸셜 주가"),("쿠콘","쿠콘 주가")]:
        result["뉴스"][name] = fetch_news(q, max_items=20)
        time.sleep(0.5)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 완료")
    return result


# ─────────────────────────────────────────
# 8. 텔레그램 포맷 & 전송
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    d    = data["_trade_date"]
    p    = data["주가"]
    s    = data["수급"]
    ma   = data["이동평균"]
    info = data["기본정보"]
    chg  = p.get("등락률", 0)
    arrow = "📈" if chg > 0 else ("📉" if chg < 0 else "➡️")

    amt = p.get("거래대금", 0)
    amt_str = f"{amt/100_000_000:.1f}억" if isinstance(amt,int) and amt >= 100_000_000 else "-"
    cap = info.get("시가총액", 0)
    cap_str = f"{cap//100_000_000:,}억" if isinstance(cap,int) and cap > 0 else "-"

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
        "*👥 수급*",
        f"  개인: {s.get('개인','-')}  외국인: {s.get('외국인','-')}  기관: {s.get('기관','-')}",
        "",
        "*🏢 기본정보*",
        f"  시가총액: {cap_str}  PER: {info.get('PER','-')}  PBR: {info.get('PBR','-')}",
        "",
        "*🔗 피어 그룹*",
    ]
    for name, peer in data["피어"].items():
        c = peer.get("등락률", 0)
        em = "▲" if c > 0 else ("▼" if c < 0 else "─")
        lines.append(f"  {name}: {peer.get('종가',0):,}원 {em}{abs(c):.2f}%")

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

    lines += ["", "*📰 오늘 뉴스 (전체 — Claude가 정리)*"]
    any_news = False
    for name, articles in data["뉴스"].items():
        real = [a for a in articles if "_error" not in a]
        if real:
            any_news = True
            lines.append(f"  ▸ *{name}* ({len(real)}건)")
            for a in real:
                press = f"[{a['언론사']}]" if a.get('언론사') else ""
                t     = f" {a['시간']}"    if a.get('시간') else ""
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
