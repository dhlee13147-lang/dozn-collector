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

NAVER_NEWS_URL = "https://search.naver.com/search.naver?where=news&query={query}&sort=1&pd=4&ds={ds}&de={de}"
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
    """종가·시가·고가·저가·거래량·거래대금·등락률 수집"""
    ds = strdate(trade_day)
    result = {}
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            result.update({
                "시가":    int(row.get("시가", 0)),
                "고가":    int(row.get("고가", 0)),
                "저가":    int(row.get("저가", 0)),
                "종가":    int(row.get("종가", 0)),
                "거래량":  int(row.get("거래량", 0)),
                "거래대금": int(row.get("거래대금", 0)),
                "등락률":  float(row.get("등락률", 0.0)),
            })
    except Exception as e:
        result["_price_error"] = str(e)
    return result


def fetch_fundamentals(ticker: str, trade_day: date) -> dict:
    """PER·PBR·시가총액 수집"""
    ds = strdate(trade_day)
    result = {}
    try:
        df_cap = krx.get_market_cap(ds, ds, ticker)
        if not df_cap.empty:
            row = df_cap.iloc[-1]
            result["시가총액"] = int(row.get("시가총액", 0))

        df_fund = krx.get_market_fundamental(ds, ds, ticker)
        if not df_fund.empty:
            row = df_fund.iloc[-1]
            result["PER"] = float(row.get("PER", 0))
            result["PBR"] = float(row.get("PBR", 0))
    except Exception as e:
        result["_fund_error"] = str(e)
    return result


def fetch_trading_volume(ticker: str, trade_day: date) -> dict:
    """투자자별 수급(개인·외국인·기관) 수집"""
    ds = strdate(trade_day)
    result = {}
    try:
        df = krx.get_market_trading_volume_by_date(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[-1]
            result["개인"]   = int(row.get("개인", 0))
            result["외국인"] = int(row.get("외국인", 0))
            result["기관"]   = int(row.get("기관합계", row.get("기관", 0)))
    except Exception as e:
        result["_supply_error"] = str(e)
    return result


def fetch_history(ticker: str, days: int = 20) -> list:
    """최근 N거래일 종가 이력 수집 (이동평균 계산용)"""
    today = date.today()
    # 넉넉하게 30일 전부터
    start = today - timedelta(days=days * 2)
    try:
        df = krx.get_market_ohlcv(strdate(start), strdate(today), ticker)
        if df.empty:
            return []
        records = []
        for dt, row in df.iterrows():
            records.append({
                "날짜": dt.strftime("%Y-%m-%d"),
                "종가": int(row.get("종가", 0)),
                "거래량": int(row.get("거래량", 0)),
            })
        return records[-days:]   # 최근 N일만
    except Exception:
        return []


def calc_moving_averages(history: list) -> dict:
    """5일·10일·20일 이동평균 계산"""
    result = {}
    closes = [r["종가"] for r in history if r["종가"] > 0]
    for n in [5, 10, 20]:
        if len(closes) >= n:
            result[f"MA{n}"] = round(sum(closes[-n:]) / n)
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
def fetch_naver_news(query: str, today: date, max_items: int = 5) -> list:
    """네이버 뉴스 당일 기사 수집"""
    ds = today.strftime("%Y.%m.%d")
    url = NAVER_NEWS_URL.format(
        query=urllib.parse.quote(query),
        ds=ds, de=ds
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.naver.com",
    }
    items = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")

        # 제목 추출
        titles = re.findall(
            r'class="news_tit"[^>]*title="([^"]+)"[^>]*href="([^"]+)"',
            html
        )
        # 요약 추출
        descs = re.findall(r'class="dsc_txt_wrap">([^<]{10,200})<', html)

        for i, (title, link) in enumerate(titles[:max_items]):
            desc = descs[i].strip() if i < len(descs) else ""
            items.append({
                "제목": title,
                "요약": desc[:100] + "..." if len(desc) > 100 else desc,
                "링크": link,
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

    print("  ▶ 이동평균 계산...")
    history = fetch_history(ticker, days=20)
    result["이동평균"] = calc_moving_averages(history)
    result["이동평균"]["history_20d"] = history   # 스킬에서 참조용

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

    # ── 네이버 뉴스 ──────────────────────
    print("  ▶ 뉴스 수집...")
    for name in ["더즌", "헥토파이낸셜", "쿠콘"]:
        query = f"{name} 주식" if name != "더즌" else "더즌 462860"
        result["뉴스"][name] = fetch_naver_news(query, today, max_items=3)
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

    lines = [
        f"📊 *더즌(462860) 일간 주가 리포트* — {d}",
        "",
        f"*{arrow} 주가 요약*",
        f"  종가: {p.get('종가',0):,}원  ({chg:+.2f}%)",
        f"  시가: {p.get('시가',0):,}  고가: {p.get('고가',0):,}  저가: {p.get('저가',0):,}",
        f"  거래량: {p.get('거래량',0):,}주  거래대금: {p.get('거래대금',0)//100000000:,}억",
        "",
        "*📐 이동평균*",
        f"  MA5:  {ma.get('MA5','-'):,}" if ma.get('MA5') else "  MA5: 계산 중",
        f"  MA10: {ma.get('MA10','-'):,}" if ma.get('MA10') else "  MA10: 계산 중",
        f"  MA20: {ma.get('MA20','-'):,}" if ma.get('MA20') else "  MA20: 계산 중",
        "",
        "*👥 수급*",
        f"  개인: {s.get('개인',0):+,}  외국인: {s.get('외국인',0):+,}  기관: {s.get('기관',0):+,}",
        "",
        "*🏢 기본정보*",
        f"  시가총액: {info.get('시가총액',0)//100000000:,}억  PER: {info.get('PER','-')}  PBR: {info.get('PBR','-')}",
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

    # 뉴스
    lines += ["", "*📰 오늘 뉴스*"]
    any_news = False
    for name, articles in data["뉴스"].items():
        real = [a for a in articles if "_error" not in a]
        if real:
            any_news = True
            for a in real:
                lines.append(f"  [{name}] {a.get('제목','')}")
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
