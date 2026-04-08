"""
더즌(462860) 일간 주가 데이터 수집기 v7
=========================================
데이터 소스 및 날짜 기준:
  모든 당일 데이터는 네이버 금융 크롤링 → 항상 현재 날짜 기준
  기술적 지표(MA/RSI/BB/OBV/MACD)만 pykrx 이력 계산

  frgn 페이지  → 날짜 확정 + OHLCV + 기관/외국인 순매매량
  시장요약 페이지 → 거래대금 + 시가총액 + PER + PBR

  날짜 결정 로직:
    frgn 페이지 첫 번째 행의 날짜 = 오늘 기준 최신 거래일
    (휴장일이면 자동으로 직전 거래일이 첫 번째 행에 표시됨)

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
# 헬퍼
# ─────────────────────────────────────────
def strdate(d: date) -> str:
    return d.strftime("%Y%m%d")

def display_date(d: date) -> str:
    days = ["월","화","수","목","금","토","일"]
    return d.strftime(f"%Y-%m-%d({days[d.weekday()]})")

def to_int(td) -> int:
    """BeautifulSoup td 태그에서 부호 포함 정수 추출"""
    if not td:
        return 0
    t = td.get_text(strip=True).replace(",", "").replace("+", "")
    try:
        return int(t)
    except:
        return 0

def naver_date_to_iso(naver_date: str) -> str:
    """'2026.04.07' → '2026-04-07'"""
    return naver_date.replace(".", "-")


# ─────────────────────────────────────────
# 1. 네이버 frgn — 날짜 확정 + OHLCV + 수급
# ─────────────────────────────────────────
def fetch_frgn(ticker: str) -> dict:
    """네이버 frgn 페이지에서 최신 거래일 데이터 수집

    컬럼 매핑 (로컬 테스트 확인):
      tds[0]: 날짜 (gray03 span)
      tds[1]: 종가
      tds[2]: 전일비  ← em 태그 포함, span 인덱싱 불가 → td 직접 접근
      tds[3]: 등락률
      tds[4]: 거래량
      tds[5]: 기관 순매매량
      tds[6]: 외국인 순매매량
      tds[7]: 외국인 보유주수
      tds[8]: 외국인 보유율

    개인 = 거래량 - 기관 - 외국인 (추정)
    반환값의 '날짜'가 이 수집기의 기준 거래일이 됨
    """
    url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        table = None
        for t in soup.find_all("table"):
            if t.find("td", class_="tc"):
                table = t
                break
        if not table:
            print(f"    [ERROR] frgn 테이블 미발견")
            return {}

        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 7:
                continue
            date_span = tds[0].find("span", class_="gray03")
            if not date_span:
                continue

            naver_date = date_span.text.strip()          # "2026.04.07"
            종가   = to_int(tds[1])
            거래량 = to_int(tds[4])
            기관   = to_int(tds[5])
            외국인 = to_int(tds[6])
            개인   = 거래량 - 기관 - 외국인

            result = {
                "날짜":   naver_date_to_iso(naver_date),  # 기준 거래일
                "종가":   종가,
                "거래량": 거래량,
                "기관":   기관,
                "외국인": 외국인,
                "개인":   개인,
            }
            print(f"    frgn 날짜={naver_date} 종가={종가:,} 기관={기관:+,} 외국인={외국인:+,}")
            return result

    except Exception as e:
        print(f"    [ERROR] frgn: {e}")
    return {}


# ─────────────────────────────────────────
# 2. 네이버 main — 시가/고가/저가/등락률 (오늘 기준)
# ─────────────────────────────────────────
def fetch_naver_main(ticker: str) -> dict:
    """네이버 금융 메인 페이지에서 시가/고가/저가/등락률 수집
    
    pykrx 이력의 마지막 행을 쓰지 않고 여기서 직접 가져옴
    → frgn 날짜와 동일한 기준(오늘/최신 거래일)
    """
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        result = {}
        # _nowVal = 현재가(종가), open/high/low는 구분자로 파싱
        for label, key in [("시가", "시가"), ("고가", "고가"), ("저가", "저가")]:
            tag = soup.find("span", id=f"_{label}")
            if not tag:
                # 텍스트 기반 fallback
                th = soup.find("em", string=label)
                if th:
                    tag = th.find_next("span")
            if tag:
                val = tag.get_text(strip=True).replace(",", "")
                try:
                    result[key] = int(val)
                except:
                    pass

        # 등락률
        rate = soup.find("span", id="_change_rate")
        if not rate:
            rate = soup.find("span", class_=lambda c: c and "change_rate" in c)
        if rate:
            try:
                result["등락률"] = float(rate.get_text(strip=True)
                                        .replace("%","").replace("+","").replace(",",""))
            except:
                pass

        if result:
            print(f"    main: 시가={result.get('시가',0):,} 고가={result.get('고가',0):,} "
                  f"저가={result.get('저가',0):,} 등락률={result.get('등락률',0):.2f}%")
        else:
            print("    [WARN] main 파싱 실패 — 구조 변경 가능성")
        return result

    except Exception as e:
        print(f"    [ERROR] main: {e}")
        return {}


# ─────────────────────────────────────────
# 3. 네이버 시장요약 — 거래대금·시가총액·PER·PBR
# ─────────────────────────────────────────
def fetch_naver_market_sum(ticker: str) -> dict:
    """네이버 시장요약(sise_market_sum)에서 거래대금·시가총액·PER·PBR 수집

    컬럼 매핑 (로컬 테스트 확인):
      cols[7]:  거래대금 (백만원)
      cols[9]:  시가총액 (억원)
      cols[10]: PER
      cols[11]: PBR
    """
    session = requests.Session()
    session.headers.update(HEADERS)

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

                def pn(text):
                    try: return float(text.strip().replace(",",""))
                    except: return 0.0

                amt    = pn(cols[7].text)   # 거래대금 (백만원)
                mktcap = pn(cols[9].text)   # 시가총액 (억원)
                per    = pn(cols[10].text)  # PER
                pbr    = pn(cols[11].text)  # PBR

                print(f"    시장요약 p{page}: 거래대금={amt:.0f}백만 시총={mktcap:.0f}억 PER={per} PBR={pbr}")
                return {
                    "거래대금": int(amt * 1_000_000),
                    "시가총액": int(mktcap * 100_000_000),
                    "PER": per if per > 0 else "-",
                    "PBR": pbr if pbr > 0 else "-",
                }
        except Exception as e:
            print(f"    [ERROR] 시장요약 p{page}: {e}")
            break

    print(f"    [WARN] 시장요약에서 {ticker} 미발견")
    return {}


# ─────────────────────────────────────────
# 3. 네이버 frgn — 피어 그룹 (종가·등락률)
# ─────────────────────────────────────────
def fetch_frgn_peer(ticker: str) -> dict:
    """피어 종목의 종가·등락률만 frgn에서 수집"""
    url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = None
        for t in soup.find_all("table"):
            if t.find("td", class_="tc"):
                table = t
                break
        if not table:
            return {}
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 4:
                continue
            if not tds[0].find("span", class_="gray03"):
                continue
            종가 = to_int(tds[1])
            # 등락률: tds[3]에서 텍스트 추출
            등락 = tds[3].get_text(strip=True).replace("%","").replace("+","")
            try:
                등락률 = float(등락)
            except:
                등락률 = 0.0
            거래량 = to_int(tds[4])
            return {"종가": 종가, "등락률": 등락률, "거래량": 거래량}
    except Exception as e:
        print(f"    [ERROR] 피어 frgn ({ticker}): {e}")
    return {}


# ─────────────────────────────────────────
# 4. pykrx — 기술적 지표용 이력 (날짜 무관)
# ─────────────────────────────────────────
def fetch_history(ticker: str, days: int = 60) -> list:
    """최근 60거래일 OHLCV 이력 (MA/RSI/BB/OBV/MACD 계산용)
    pykrx는 이 목적으로만 사용
    """
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
# 5. 기술적 지표 계산
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

    for n in [5, 10, 20, 60]:
        if len(closes) >= n:
            result[f"MA{n}"] = round(sum(closes[-n:]) / n)

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

    if len(closes) >= 20:
        w   = closes[-20:]
        ma  = sum(w)/20
        std = (sum((c-ma)**2 for c in w)/20)**0.5
        bbu, bbl, bbm = round(ma+2*std), round(ma-2*std), round(ma)
        cur = closes[-1]
        result.update({"BB_upper":bbu,"BB_mid":bbm,"BB_lower":bbl,
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
                result.update({"MACD":mv,"MACD_signal":sv,"MACD_hist":hist})
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
# 6. DART 공시
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
# 7. 뉴스
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 20) -> list:
    base = "https://search.naver.com/search.naver"
    params = urllib.parse.urlencode({
        "where":"news","query":query,
        "sm":"tab_opt","sort":"1","nso":"so:dd,p:1d",
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
# 8. 전체 수집
# ─────────────────────────────────────────
def collect_all() -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 시작")

    t = TICKERS["더즌"]
    dart_key = os.getenv("DART_API_KEY", "")

    # ── Step 1: frgn에서 날짜·OHLCV·수급 ──────────────
    print("  ▶ frgn (날짜 확정 + OHLCV + 수급)...")
    frgn = fetch_frgn(t)
    if not frgn:
        print("  [ERROR] frgn 수집 실패 — 수집 중단")
        return {}

    trade_date = frgn["날짜"]   # 네이버 기준 최신 거래일 (휴장일이면 전 거래일)
    print(f"  기준 거래일: {trade_date}")

    result = {
        "_collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_trade_date":   trade_date,
        "_report_type":  "daily",
        "주가": {
            "종가":   frgn["종가"],
            "거래량": frgn["거래량"],
            "시가": 0, "고가": 0, "저가": 0, "등락률": 0.0,
            "거래대금": 0,
        },
        "수급": {
            "기관":   frgn["기관"],
            "외국인": frgn["외국인"],
            "개인":   frgn["개인"],
        },
        "기본정보": {},
        "이동평균": {},
        "피어": {},
        "공시": {},
        "뉴스": {},
    }

    # ── Step 2: 시장요약에서 거래대금·시가총액·PER·PBR ──
    print("  ▶ 시장요약 (거래대금·시총·PER·PBR)...")
    market = fetch_naver_market_sum(t)
    result["주가"]["거래대금"]    = market.get("거래대금", 0)
    result["기본정보"]["시가총액"] = market.get("시가총액", 0)
    result["기본정보"]["PER"]     = market.get("PER", "-")
    result["기본정보"]["PBR"]     = market.get("PBR", "-")

    # ── Step 3: 기술적 지표 (pykrx 이력 계산) ──────────
    print("  ▶ 기술적 지표 계산 (pykrx 60일 이력)...")
    history    = fetch_history(t, days=60)
    indicators = calc_technical_indicators(history)
    result["이동평균"] = {**indicators, "history_60d": history}

    print(f"    MA5={indicators.get('MA5','?'):,} RSI={indicators.get('RSI14','?')}")

    # ── Step 4: 시가/고가/저가/등락률 (네이버 main) ────
    # pykrx 이력 마지막 행 대신 네이버 main에서 직접 가져옴
    # → frgn 날짜와 동일한 기준 보장
    print("  ▶ 시가·고가·저가·등락률 (네이버 main)...")
    main_data = fetch_naver_main(t)
    result["주가"]["시가"]   = main_data.get("시가",   0)
    result["주가"]["고가"]   = main_data.get("고가",   0)
    result["주가"]["저가"]   = main_data.get("저가",   0)
    result["주가"]["등락률"] = main_data.get("등락률", 0.0)

    # ── Step 5: 피어 (frgn) ─────────────────────────────
    for name, pt in [("헥토파이낸셜", TICKERS["헥토파이낸셜"]),
                     ("쿠콘",         TICKERS["쿠콘"])]:
        print(f"  ▶ {name} (frgn)...")
        p = fetch_frgn_peer(pt)
        result["피어"][name] = {
            "종가":   p.get("종가", 0),
            "등락률": p.get("등락률", 0.0),
            "거래량": p.get("거래량", 0),
        }
        time.sleep(0.3)

    # ── Step 6: DART ─────────────────────────────────────
    print("  ▶ DART 공시...")
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else [{"_note":"DART_API_KEY 미설정"}]
        time.sleep(0.3)

    # ── Step 7: 뉴스 ─────────────────────────────────────
    print("  ▶ 뉴스 수집...")
    for name, q in [("더즌","더즌 462860"),("헥토파이낸셜","헥토파이낸셜 주가"),("쿠콘","쿠콘 주가")]:
        result["뉴스"][name] = fetch_news(q, max_items=20)
        time.sleep(0.5)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 완료 — 기준일: {trade_date}")
    return result


# ─────────────────────────────────────────
# 9. 텔레그램 포맷 & 전송
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
        f"  기관: {s.get('기관',0):+,}  외국인: {s.get('외국인',0):+,}  개인: {s.get('개인',0):+,} *(개인 추정)*",
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
    if not data:
        print("수집 실패")
        return
    text = format_telegram(data)
    send_telegram(text, data)
    os.makedirs("output", exist_ok=True)
    path = f"output/dozen_{data['_trade_date']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 저장: {path}")

if __name__ == "__main__":
    main()
