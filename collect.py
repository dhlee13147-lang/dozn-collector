"""
더즌(462860) 일간 주가 데이터 수집기 v8
=========================================
데이터 소스 및 날짜 기준:
  모든 당일 데이터는 네이버 금융 크롤링 → 항상 현재 날짜 기준
  기술적 지표(MA/RSI/BB/OBV/MACD)만 pykrx 이력 계산

  frgn 페이지  → OHLCV + 기관/외국인 순매매량 (수급 기준일 별도 관리)
  시장요약 페이지 → 거래대금 + 시가총액 + PER + PBR

  날짜 결정 로직 (수정):
    리포트 기준일 = 실행 시점(오늘) 무조건 고정
    수급 데이터 기준일 = frgn 페이지 첫 번째 행의 날짜 표시

환경변수 (GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  DART_API_KEY
  NEWS_REPO (예: "dhlee13147-lang/my-news-bot")
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
# 1. 네이버 frgn — OHLCV + 수급 (날짜 추출 포함)
# ─────────────────────────────────────────
def fetch_frgn(ticker: str) -> dict:
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
                "날짜":   naver_date_to_iso(naver_date),  # 수급 데이터의 실제 기준일
                "종가":   종가,
                "거래량": 거래량,
                "기관":   기관,
                "외국인": 외국인,
                "개인":   개인,
            }
            return result

    except Exception as e:
        print(f"    [ERROR] frgn: {e}")
    return {}


# ─────────────────────────────────────────
# 2. 네이버 main — 시가/고가/저가/등락률 (오늘 기준)
# ─────────────────────────────────────────
def parse_num_spans(em_tag) -> int:
    if not em_tag:
        return 0
    parts = []
    for span in em_tag.find_all("span"):
        cls = span.get("class", [])
        cls_str = " ".join(cls) if isinstance(cls, list) else cls
        if "shim" in cls_str:
            continue
        if "jum" in cls_str:
            break
        if any(c.startswith("no") for c in (cls if isinstance(cls, list) else [cls])):
            parts.append(span.get_text(strip=True))
    try:
        return int("".join(parts))
    except:
        return 0


def fetch_naver_main(ticker: str) -> dict:
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    result = {}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        no_today = soup.find("p", class_="no_today")
        if no_today:
            em = no_today.find("em")
            val = parse_num_spans(em)
            if val > 0:
                result["종가"] = val

        label_map = {
            "sp_txt3":  "시가",
            "sp_txt4":  "고가",
            "sp_txt5":  "저가",
            "sp_txt10": "거래대금_백만",
        }
        for cls, key in label_map.items():
            span = soup.find("span", class_=cls)
            if not span:
                continue
            td = span.find_parent("td") or span.find_parent("p")
            if not td:
                continue
            em = td.find("em")
            val = parse_num_spans(em)
            if val > 0:
                result[key] = val

        if "거래대금_백만" in result:
            result["거래대금"] = result.pop("거래대금_백만") * 1_000_000

        no_exday = soup.find("p", class_="no_exday")
        if no_exday:
            ems = no_exday.find_all("em")
            if len(ems) >= 2:
                rate_em = ems[1]
                parts = []
                for span in rate_em.find_all("span"):
                    cls_list = span.get("class", [])
                    cls_str  = " ".join(cls_list)
                    if any(x in cls_str for x in ["per", "ico", "blind"]):
                        continue
                    if "jum" in cls_str:
                        parts.append(".")
                        continue
                    if any(c.startswith("no") for c in cls_list):
                        parts.append(span.get_text(strip=True))
                try:
                    rate_str = "".join(parts)
                    if "no_dn" in rate_em.get("class", []):
                        result["등락률"] = -float(rate_str)
                    else:
                        result["등락률"] = float(rate_str)
                except:
                    pass
        return result
    except Exception as e:
        print(f"    [ERROR] main: {e}")
        return {}


# ─────────────────────────────────────────
# 3. 네이버 시장요약 — 거래대금·시가총액·PER·PBR
# ─────────────────────────────────────────
def fetch_naver_market_sum(ticker: str) -> dict:
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
    except:
        pass

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
                code = link.get("href", "").split("code=")[-1].strip()
                if code != ticker:
                    continue

                def pn(text):
                    try: return float(text.strip().replace(",",""))
                    except: return 0.0

                return {
                    "거래대금": int(pn(cols[7].text) * 1_000_000),
                    "시가총액": int(pn(cols[9].text) * 100_000_000),
                    "PER": cols[10].text.strip() if pn(cols[10].text) > 0 else "-",
                    "PBR": cols[11].text.strip() if pn(cols[11].text) > 0 else "-",
                }
        except:
            break
    return {}


# ─────────────────────────────────────────
# 3. 네이버 frgn — 피어 그룹
# ─────────────────────────────────────────
def fetch_peer(ticker: str) -> dict:
    result = fetch_naver_main(ticker)
    if not result.get("거래량"):
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            span = soup.find("span", class_="sp_txt9")
            if span:
                em = span.find_parent("td").find("em")
                result["거래량"] = parse_num_spans(em)
        except:
            pass
    return {
        "종가":   result.get("종가",   0),
        "등락률": result.get("등락률", 0.0),
        "거래량": result.get("거래량", 0),
    }


# ─────────────────────────────────────────
# 4. pykrx — 기술적 지표용 이력
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
    except:
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
        result["RSI14_signal"] = ("과매수 ⚠️" if rsi >= 70 else "과매도 📉" if rsi <= 30 else "중립")

    if len(closes) >= 20:
        w = closes[-20:]; ma = sum(w)/20
        std = (sum((c-ma)**2 for c in w)/20)**0.5
        bbu, bbl, bbm = round(ma+2*std), round(ma-2*std), round(ma)
        cur = closes[-1]
        result.update({"BB_upper":bbu,"BB_mid":bbm,"BB_lower":bbl})
        result["BB_signal"] = (f"상단 돌파 ⚠️" if cur >= bbu else f"하단 이탈 📉" if cur <= bbl else "중심선 부근")

    if len(closes) >= 2:
        obv, series = 0, [0]
        for i in range(1, len(closes)):
            obv += volumes[i] if closes[i]>closes[i-1] else (-volumes[i] if closes[i]<closes[i-1] else 0)
            series.append(obv)
        p5c = closes[-1]-closes[-6] if len(closes)>=6 else 0
        p5o = series[-1]-series[-6] if len(series)>=6 else 0
        result["OBV_signal"] = ("상승 신뢰도 높음 ✅" if p5c>0 and p5o>0 else "하락 신뢰도 높음 📉" if p5c<0 and p5o<0 else "보합")

    if len(closes) >= 35:
        e12, e26 = calc_ema(closes, 12), calc_ema(closes, 26)
        macd = [round(a-b,2) for a,b in zip(e12,e26) if a and b]
        if len(macd) >= 9:
            sig = calc_ema(macd, 9); mv, sv = macd[-1], sig[-1]
            result.update({"MACD":mv,"MACD_signal":sv})
            result["MACD_cross"] = ("골든크로스 📈" if macd[-2]<=sig[-2] and mv>sv else "데드크로스 📉" if macd[-2]>=sig[-2] and mv<sv else "추세 유지")
    return result


# ─────────────────────────────────────────
# 6. DART 공시
# ─────────────────────────────────────────
def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=1)
    params = {"crtfc_key": dart_key, "corp_code": corp_code, "bgn_de": strdate(start), "end_de": strdate(end), "page_count":"5"}
    url = DART_LIST_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "000":
            return [{"보고서": i["report_nm"], "URL": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"} for i in data.get("list", [])]
    except: pass
    return []


# ─────────────────────────────────────────
# 7. 뉴스 (전달받은 target_date 기준 필터링)
# ─────────────────────────────────────────
def fetch_news_from_github_csv(repo: str, csv_path: str = "sent_news.csv", keywords: list = None, target_date: str = None) -> dict:
    if keywords is None:
        keywords = ["더즌", "dozn", "헥토파이낸셜", "쿠콘"]

    # [수정] target_date(리포트 기준일)가 들어오면 해당 날짜 기사만 추출
    filter_date = target_date if target_date else date.today().isoformat()
    result = {kw: [] for kw in keywords}; result["기타"] = []

    try:
        raw_url = f"https://raw.githubusercontent.com/{repo}/main/{csv_path}"
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dozen-collector"})
        with urllib.request.urlopen(req, timeout=15) as r:
            csv_text = r.read().decode("utf-8-sig", errors="replace")

        import csv as csv_mod, io
        reader = csv_mod.reader(io.StringIO(csv_text))

        for row in reader:
            if len(row) < 3: continue
            url_val, title, row_date = row[0].strip(), row[1].strip(), row[2].strip()

            # [핵심] CSV에 저장된 날짜와 리포트 기준일이 다르면 스킵
            if row_date != filter_date:
                continue

            # 필터링 및 분류
            if "media.naver.com/press" in url_val or len(title) < 10: continue

            matched = False
            for kw in keywords:
                if kw in title or kw.lower() in url_val.lower():
                    result[kw].append({"제목": title, "링크": url_val}); matched = True; break
            if not matched: result["기타"].append({"제목": title, "링크": url_val})

        print(f"    GitHub CSV ({filter_date}): 매칭 뉴스 수집 완료")
    except Exception as e:
        print(f"    [ERROR] GitHub CSV: {e}")
    return result


# ─────────────────────────────────────────
# 8. 전체 수집 (날짜 기준 분리)
# ─────────────────────────────────────────
def collect_all() -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 시작")
    t = TICKERS["더즌"]
    dart_key = os.getenv("DART_API_KEY", "")

    # [핵심 수정] 리포트 전체의 기준일은 실행 시점인 '오늘'로 강제 확정
    report_today = date.today().isoformat() 

    # Step 1: frgn에서 수급 데이터 수집 (날짜는 별도로 기록)
    frgn = fetch_frgn(t)
    if not frgn:
        print("  [ERROR] frgn 수집 실패"); return {}

    supply_date = frgn["날짜"] # 수급 데이터 실제 기준일 (4/7 등)
    print(f"  리포트 기준일: {report_today} | 수급 데이터 기준일: {supply_date}")

    result = {
        "_collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_trade_date":   report_today, # [중요] 리포트 헤더 및 뉴스 필터용 (4/8)
        "_supply_date":  supply_date,  # 수급 지표 옆에 표시할 날짜 (4/7)
        "주가": {
            "종가": frgn["종가"], "거래량": frgn["거래량"], "시가": 0, "고가": 0, "저가": 0, "등락률": 0.0, "거래대금": 0,
        },
        "수급": {"기관": frgn["기관"], "외국인": frgn["외국인"], "개인": frgn["개인"]},
        "기본정보": {}, "이동평균": {}, "피어": {}, "공시": {}, "뉴스": {},
    }

    # Step 2~3: 실시간 데이터 (오늘 8일 데이터)
    main_data = fetch_naver_main(t)
    if main_data.get("종가"): result["주가"]["종가"] = main_data["종가"]
    result["주가"].update({
        "시가": main_data.get("시가", 0), "고가": main_data.get("고가", 0), "저가": main_data.get("저가", 0),
        "등락률": main_data.get("등락률", 0.0), "거래대금": main_data.get("거래대금", 0)
    })

    market = fetch_naver_market_sum(t)
    result["주가"]["거래대금"] = main_data.get("거래대금", 0) or market.get("거래대금", 0)
    result["기본정보"] = market

    # Step 4: 기술적 지표
    history = fetch_history(t)
    indicators = calc_technical_indicators(history)
    result["이동평균"] = indicators

    # Step 5~6: 피어 및 공시
    for name, pt in TICKERS.items():
        if name != "더즌": result["피어"][name] = fetch_peer(pt)
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else []

    # Step 7: 뉴스 (리포트 기준일인 4/8 뉴스를 CSV에서 추출)
    news_repo = os.getenv("NEWS_REPO", "")
    if news_repo:
        csv_news = fetch_news_from_github_csv(repo=news_repo, target_date=report_today)
        result["뉴스"]["더즌"] = csv_news.get("더즌", []) + csv_news.get("dozn", [])
        result["뉴스"]["기타"] = csv_news.get("기타", [])

    return result


# ─────────────────────────────────────────
# 9. 텔레그램 포맷 & 전송
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    d, sd = data["_trade_date"], data["_supply_date"]
    p, s, ma, info = data["주가"], data["수급"], data["이동평균"], data["기본정보"]
    
    arrow = "📈" if p['등락률'] > 0 else "📉" if p['등락률'] < 0 else "➡️"
    amt_str = f"{p['거래대금']/100_000_000:.1f}억"
    cap_str = f"{info.get('시가총액', 0)//100_000_000:,}억"

    lines = [
        f"📊 *더즌(462860) 일간 주가 리포트* — {d}",
        "",
        f"*{arrow} 주가 요약*",
        f"  종가: {p['종가']:,}원 ({p['등락률']:+.2f}%)",
        f"  시가: {p['시가']:,} 고가: {p['고가']:,} 저가: {p['저가']:,}",
        f"  거래량: {p['거래량']:,}주  거래대금: {amt_str}",
        "",
        "*📐 이동평균*",
        f"  MA5: {ma.get('MA5',0):,}  MA20: {ma.get('MA20',0):,}",
        "",
        "*📊 기술적 지표*",
        f"  RSI(14): {ma.get('RSI14','-')} — {ma.get('RSI14_signal','')}",
        f"  볼린저: {ma.get('BB_signal','')}",
        f"  OBV: {ma.get('OBV_signal','-')}",
        f"  MACD: {ma.get('MACD_cross','')}",
        "",
        "*👥 수급*",
        f"  기준일: {sd}", # 수급 데이터 기준일 명시
        f"  기관: {s['기관']:+,}  외국인: {s['외국인']:+,}  개인: {s['개인']:+,}",
        "",
        "*🏢 기본정보*",
        f"  시가총액: {cap_str}  PER: {info.get('PER','-')}  PBR: {info.get('PBR','-')}",
        "",
        "*📰 오늘 뉴스 (4/8 수집분)*"
    ]
    
    for name, articles in data["뉴스"].items():
        if articles:
            lines.append(f"  ▸ *{name}* ({len(articles)}건)")
            for a in articles[:3]: lines.append(f"    - {a['제목']}\n    {a['링크']}")

    lines += ["", "─────────────────────", "📌 _Claude 스킬 입력용 JSON 파일 참조_"]
    return "\n".join(lines)


def send_telegram(text: str, json_data: dict):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id: print(text); return
    base = f"https://api.telegram.org/bot{token}"
    requests.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True})
    jb = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    requests.post(f"{base}/sendDocument", data={"chat_id": chat_id, "caption": f"📎 Claude 스킬용 — {json_data['_trade_date']}"}, files={"document": (f"dozen_{json_data['_trade_date']}.json", jb, "application/json")})


def main():
    data = collect_all()
    if data:
        text = format_telegram(data)
        send_telegram(text, data)

if __name__ == "__main__":
    main()
