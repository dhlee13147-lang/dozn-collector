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
    if not td:
        return 0
    t = td.get_text(strip=True).replace(",", "").replace("+", "")
    try:
        return int(t)
    except:
        return 0

def naver_date_to_iso(naver_date: str) -> str:
    return naver_date.replace(".", "-")


# ─────────────────────────────────────────
# 1. 네이버 frgn — 데이터 수집
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
            return {}

        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 7:
                continue
            date_span = tds[0].find("span", class_="gray03")
            if not date_span:
                continue

            naver_date = date_span.text.strip()
            종가   = to_int(tds[1])
            거래량 = to_int(tds[4])
            기관   = to_int(tds[5])
            외국인 = to_int(tds[6])
            개인   = 거래량 - 기관 - 외국인

            return {
                "날짜":   naver_date_to_iso(naver_date),
                "종가":   종가,
                "거래량": 거래량,
                "기관":   기관,
                "외국인": 외국인,
                "개인":   개인,
            }
    except:
        return {}
    return {}


# ─────────────────────────────────────────
# 2. 네이버 main — 실시간 데이터
# ─────────────────────────────────────────
def parse_num_spans(em_tag) -> int:
    if not em_tag:
        return 0
    parts = []
    for span in em_tag.find_all("span"):
        cls = span.get("class", [])
        cls_str = " ".join(cls) if isinstance(cls, list) else cls
        if "shim" in cls_str: continue
        if "jum" in cls_str: break
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
            result["종가"] = parse_num_spans(no_today.find("em"))

        label_map = {"sp_txt3": "시가", "sp_txt4": "고가", "sp_txt5": "저가", "sp_txt10": "거래대금_백만"}
        for cls, key in label_map.items():
            span = soup.find("span", class_=cls)
            if not span: continue
            td = span.find_parent("td") or span.find_parent("p")
            if not td: continue
            val = parse_num_spans(td.find("em"))
            if val > 0: result[key] = val

        if "거래대금_백만" in result:
            result["거래대금"] = result.pop("거래대금_백만") * 1_000_000

        no_exday = soup.find("p", class_="no_exday")
        if no_exday:
            ems = no_exday.find_all("em")
            if len(ems) >= 2:
                rate_em = ems[1]
                parts = []
                for span in rate_em.find_all("span"):
                    c_list = span.get("class", [])
                    c_str = " ".join(c_list)
                    if any(x in c_str for x in ["per", "ico", "blind"]): continue
                    if "jum" in c_str: parts.append(".")
                    elif any(c.startswith("no") for c in c_list): parts.append(span.get_text(strip=True))
                try:
                    r_str = "".join(parts)
                    result["등락률"] = -float(r_str) if "no_dn" in rate_em.get("class", []) else float(r_str)
                except: pass
        return result
    except:
        return {}


# ─────────────────────────────────────────
# 3. 네이버 시장요약 — 시가총액 등
# ─────────────────────────────────────────
def fetch_naver_market_sum(ticker: str) -> dict:
    session = requests.Session()
    session.headers.update(HEADERS)
    field_url = "https://finance.naver.com/sise/field_submit.naver?menu=market_sum&returnUrl=http://finance.naver.com/sise/sise_market_sum.naver?sosok=1&fieldIds=market_sum&fieldIds=per&fieldIds=pbr&fieldIds=amount"
    try: session.get(field_url, timeout=10)
    except: pass

    for page in range(1, 51):
        try:
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page={page}"
            resp = session.get(url, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="type_2")
            if not table: break
            for row in table.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) < 12: continue
                link = cols[1].find("a")
                if link and ticker in link.get("href", ""):
                    def pn(t): 
                        try: return float(t.strip().replace(",",""))
                        except: return 0.0
                    return {
                        "거래대금": int(pn(cols[7].text) * 1_000_000),
                        "시가총액": int(pn(cols[9].text) * 100_000_000),
                        "PER": cols[10].text.strip(),
                        "PBR": cols[11].text.strip()
                    }
        except: break
    return {}


# ─────────────────────────────────────────
# 4. 피어/이력/지표 (v7 원본 로직 유지)
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
                result["거래량"] = parse_num_spans(span.find_parent("td").find("em"))
        except: pass
    return {"종가": result.get("종가", 0), "등락률": result.get("등락률", 0.0), "거래량": result.get("거래량", 0)}

def fetch_history(ticker: str, days: int = 60) -> list:
    today = date.today()
    start = today - timedelta(days=days * 2)
    try:
        df = krx.get_market_ohlcv(strdate(start), strdate(today), ticker)
        records = []
        for dt, row in df.iterrows():
            records.append({
                "날짜": dt.strftime("%Y-%m-%d"), "시가": int(row["시가"]), "고가": int(row["고가"]),
                "저가": int(row["저가"]), "종가": int(row["종가"]), "거래량": int(row["거래량"])
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
    closes = [r["종가"] for r in history]; volumes = [r["거래량"] for r in history]
    result = {}
    for n in [5, 10, 20, 60]:
        if len(closes) >= n: result[f"MA{n}"] = round(sum(closes[-n:]) / n)
    if len(closes) >= 15:
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        ag = sum(max(d,0) for d in deltas[-14:]) / 14
        al = sum(abs(min(d,0)) for d in deltas[-14:]) / 14
        rsi = 100.0 if al == 0 else round(100 - 100/(1+ag/al), 2)
        result["RSI14"] = rsi
        result["RSI14_signal"] = ("과매수 ⚠️" if rsi >= 70 else "과매도 📉 (반등 가능)" if rsi <= 30 else "중립")
    if len(closes) >= 20:
        w = closes[-20:]; ma = sum(w)/20; std = (sum((c-ma)**2 for c in w)/20)**0.5
        bbu, bbl, bbm = round(ma+2*std), round(ma-2*std), round(ma); cur = closes[-1]
        result.update({"BB_upper":bbu,"BB_mid":bbm,"BB_lower":bbl, "BB_width":round((bbu-bbl)/bbm*100,2)})
        result["BB_signal"] = (f"상단 돌파 ⚠️" if cur >= bbu else f"하단 이탈 📉" if cur <= bbl else f"중심선 {'위' if cur > bbm else '아래'}")
    if len(closes) >= 2:
        obv, series = 0, [0]
        for i in range(1, len(closes)):
            obv += volumes[i] if closes[i]>closes[i-1] else (-volumes[i] if closes[i]<closes[i-1] else 0)
            series.append(obv)
        p5c = closes[-1]-closes[-6] if len(closes)>=6 else 0
        p5o = series[-1]-series[-6] if len(series)>=6 else 0
        result["OBV_signal"] = ("주가↑ OBV↑ — 상승 신뢰도 높음 ✅" if p5c>0 and p5o>0 else "주가↓ OBV↓ — 하락 신뢰도 높음 📉" if p5c<0 and p5o<0 else "보합")
    if len(closes) >= 35:
        e12, e26 = calc_ema(closes, 12), calc_ema(closes, 26)
        macd = [round(a-b,2) for a,b in zip(e12,e26) if a and b]
        if len(macd) >= 9:
            sig = calc_ema(macd, 9); mv, sv = macd[-1], round(sig[-1], 2); hist = round(mv-sv, 2)
            result.update({"MACD":mv,"MACD_signal":sv,"MACD_cross":("골든크로스 📈" if macd[-2]-sig[-2]<=0 and hist>0 else "데드크로스 📉" if macd[-2]-sig[-2]>=0 and hist<0 else "강세 유지" if hist>0 else "약세 유지")})
    return result


# ─────────────────────────────────────────
# 6. DART/뉴스 (날짜 타겟팅 반영)
# ─────────────────────────────────────────
def fetch_dart(corp_code: str, dart_key: str) -> list:
    end, start = date.today(), date.today() - timedelta(days=1)
    params = {"crtfc_key": dart_key, "corp_code": corp_code, "bgn_de": strdate(start), "end_de": strdate(end), "page_count":"10"}
    url = DART_LIST_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r: data = json.loads(r.read())
        if data.get("status") == "000":
            return [{"날짜": i["rcept_dt"], "보고서": i["report_nm"], "URL": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"} for i in data.get("list", [])]
    except: pass
    return []

def fetch_news_from_github_csv(repo: str, csv_path: str = "sent_news.csv", keywords: list = None, target_date: str = None) -> dict:
    if keywords is None: keywords = ["더즌", "dozn", "헥토파이낸셜", "쿠콘"]
    filter_date = target_date if target_date else date.today().isoformat()
    result = {kw: [] for kw in keywords}; result["기타"] = []
    try:
        raw_url = f"https://raw.githubusercontent.com/{repo}/main/{csv_path}"
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dozen-collector"})
        with urllib.request.urlopen(req, timeout=15) as r: csv_text = r.read().decode("utf-8-sig", errors="replace")
        import csv as csv_mod, io
        reader = csv_mod.reader(io.StringIO(csv_text))
        for row in reader:
            if len(row) < 3 or row[2].strip() != filter_date: continue
            u, t = row[0].strip(), row[1].strip()
            if "media.naver.com/press" in u or len(t) < 10: continue
            matched = False
            for kw in keywords:
                if kw in t or kw.lower() in u.lower():
                    result[kw].append({"제목": t, "링크": u}); matched = True; break
            if not matched: result["기타"].append({"제목": t, "링크": u})
    except: pass
    return result


# ─────────────────────────────────────────
# 8. 전체 수집 (날짜 기준 분리 핵심 로직)
# ─────────────────────────────────────────
def collect_all() -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 수집 시작")
    t = TICKERS["더즌"]
    dart_key = os.getenv("DART_API_KEY", "")

    # 리포트 기준일은 무조건 오늘(4/8)로 확정
    report_today = date.today().isoformat()

    # Step 1: frgn에서 수급 수집 (데이터 기준일 별도 관리)
    frgn = fetch_frgn(t)
    if not frgn: return {}
    supply_date = frgn["날짜"]

    result = {
        "_collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_trade_date":   report_today, # 리포트 제목용 (4/8)
        "_supply_date":  supply_date,  # 수급 지표 옆 표시용 (4/7 등)
        "주가": {"종가": frgn["종가"], "거래량": frgn["거래량"], "시가": 0, "고가": 0, "저가": 0, "등락률": 0.0, "거래대금": 0},
        "수급": {"기관": frgn["기관"], "외국인": frgn["외국인"], "개인": frgn["개인"]},
        "기본정보": {}, "이동평균": {}, "피어": {}, "공시": {}, "뉴스": {},
    }

    # Step 2~4: 실시간 및 기술적 지표 (원본 로직 그대로 호출)
    main_data = fetch_naver_main(t); market = fetch_naver_market_sum(t)
    if main_data.get("종가"): result["주가"]["종가"] = main_data["종가"]
    result["주가"].update({"시가": main_data.get("시가", 0), "고가": main_data.get("고가", 0), "저가": main_data.get("저가", 0), "등락률": main_data.get("등락률", 0.0), "거래대금": main_data.get("거래대금", 0) or market.get("거래대금", 0)})
    result["기본정보"] = market
    history = fetch_history(t); result["이동평균"] = calc_technical_indicators(history)

    # Step 5~7: 피어/공시/뉴스
    for name, pt in TICKERS.items():
        if name != "더즌": result["피어"][name] = fetch_peer(pt)
    for name, corp in DART_CORP_CODES.items():
        result["공시"][name] = fetch_dart(corp, dart_key) if dart_key else []
    
    news_repo = os.getenv("NEWS_REPO", "")
    if news_repo:
        csv_news = fetch_news_from_github_csv(repo=news_repo, target_date=report_today)
        result["뉴스"]["더즌"] = csv_news.get("더즌", []) + csv_news.get("dozn", [])
        result["뉴스"]["기타"] = csv_news.get("기타", [])
    
    return result


# ─────────────────────────────────────────
# 9. 텔레그램 포맷 및 메인
# ─────────────────────────────────────────
def format_telegram(data: dict) -> str:
    d, sd = data["_trade_date"], data["_supply_date"]
    p, s, ma, info = data["주가"], data["수급"], data["이동평균"], data["기본정보"]
    
    lines = [
        f"📊 *더즌(462860) 일간 주가 리포트* — {d}",
        "",
        f"*💰 주가 요약*",
        f"  종가: {p['종가']:,}원 ({p['등락률']:+.2f}%)",
        f"  거래량: {p['거래량']:,}주  거래대금: {p['거래대금']/100_000_000:.1f}억",
        "",
        f"*📐 이동평균 및 지표*",
        f"  MA5: {ma.get('MA5',0):,}  RSI14: {ma.get('RSI14','-')} ({ma.get('RSI14_signal','')})",
        f"  볼린저: {ma.get('BB_signal','')}",
        f"  MACD: {ma.get('MACD_cross','')}",
        "",
        f"*👥 수급 (기준일: {sd})*", # 수급 기준일 명시
        f"  기관: {s['기관']:+,}  외국인: {s['외국인']:+,}  개인: {s['개인']:+,}",
        "",
        f"*📰 오늘 뉴스 ({d} 수집분)*"
    ]
    for name, articles in data["뉴스"].items():
        if articles:
            lines.append(f"  ▸ *{name}*")
            for a in articles[:3]: lines.append(f"    - {a['제목']}\n    {a['링크']}")
    return "\n".join(lines)

def send_telegram(text: str, json_data: dict):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id: return
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
