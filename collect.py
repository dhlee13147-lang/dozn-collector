"""
더즌(462860) 기업분석 데이터 수집기 v4.14
===================================================
- 필드명 대응: ACC_TRDVAL(거래대금) 유연 처리
- 전송 로직 복원: 기존에 성공했던 텔레그램 전송 및 파일 첨부 방식 적용
- 분석 지표: MA, RSI, 볼린저밴드 내부 연산
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

# ─────────────────────────────────────────
# 1. 날짜 및 데이터 수집 (거래대금 필드 보강)
# ─────────────────────────────────────────
def find_latest_valid_date(ticker):
    now_kst = datetime.utcnow() + timedelta(hours=9)
    search_start = now_kst.date() if now_kst.hour >= 16 else now_kst.date() - timedelta(days=1)
    for i in range(10):
        target = search_start - timedelta(days=i)
        ds = target.strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv(ds, ds, ticker)
            if not df.empty and int(df.iloc[0].get("종가", 0)) > 0:
                return target
        except: continue
    return None

def get_comprehensive_data(ticker, target_date):
    if not target_date: return {}
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {"close": 0, "rate": 0.0, "vol": 0, "amt": 0}, "supply": {"ant": 0, "foreigner": 0, "inst": 0}, "tech": {}}
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0]
            # 거래대금 필드 대응 (ACC_TRDVAL 포함)
            amt = row.get("거래대금") or row.get("ACC_TRDVAL") or row.get("거래금액") or 0
            res["price"] = {"close": int(row.get("종가", 0)), "rate": float(row.get("등락률", 0.0)), "vol": int(row.get("거래량", 0)), "amt": int(amt)}
        
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0]
            res["supply"] = {"ant": int(inv.get("개인", 0)), "foreigner": int(inv.get("외국인", 0)), "inst": int(inv.get("기관합계", 0))}

        start_ds = (target_date - timedelta(days=150)).strftime("%Y%m%d")
        df_h = krx.get_market_ohlcv(start_ds, ds, ticker)
        if not df_h.empty:
            c = df_h['종가']
            ma5, ma20 = c.rolling(5).mean().iloc[-1], c.rolling(20).mean().iloc[-1]
            diff = c.diff(); up, down = diff.where(diff > 0, 0), -diff.where(diff < 0, 0)
            rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
            std = c.rolling(20).std().iloc[-1]
            res["tech"] = {"ma5": int(ma5), "ma20": int(ma20), "rsi": round(rsi, 2), "bb_u": int(ma20 + 2*std), "bb_l": int(ma20 - 2*std)}
    except: pass
    return res

# ─────────────────────────────────────────
# 2. 뉴스 수집
# ─────────────────────────────────────────
def fetch_news_list(query):
    news_items = []
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={urllib.parse.quote(query)}&sort=1"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for item in soup.select('ul.list_news > li.bx')[:3]:
            t, p = item.select_one('a.news_tit'), item.select_one('a.info.press')
            if t: news_items.append(f"    - {t.get_text(strip=True)} ({p.get_text(strip=True) if p else '뉴스'})")
    except: pass
    return news_items

# ─────────────────────────────────────────
# 3. 메시지 포맷팅 및 전송 (기존 성공 로직 반영)
# ─────────────────────────────────────────
def format_telegram(data_package):
    v_date = data_package["date"]
    m_data = data_package["main"]
    p_results = data_package["peers"]
    news_data = data_package["news"]
    
    p, s, t = m_data["price"], m_data["supply"], m_data["tech"]
    status = "📈" if p['rate'] > 0 else "📉" if p['rate'] < 0 else "➡️"
    
    msg = [
        f"📊 *더즌({TICKERS['더즌']}) 기업분석 리포트* — {v_date.strftime('%Y-%m-%d')}",
        "",
        f"*{status} 가격 및 거래량*",
        f"  • 종가: {p['close']:,}원 ({p['rate']:+.2f}%)",
        f"  • 거래량: {p['vol']:,}주 / 대금: {p['amt']/100000000:.1f}억",
        "",
        f"*👥 투자자별 수급 (단위: 주)*",
        f"  • 개인: {s.get('ant', 0):+,} | 외인: {s.get('foreigner', 0):+,} | 기관: {s.get('inst', 0):+,}",
        "",
        f"*📐 기술적 지표*",
        f"  • 이동평균: MA5({t.get('ma5', 0):,}) | MA20({t.get('ma20', 0):,})",
        f"  • RSI(14): {t.get('rsi', 0)} ({'과매수' if t.get('rsi', 0) > 70 else '과매도' if t.get('rsi', 0) < 30 else '중립'})",
        f"  • 볼린저밴드: 상단 {t.get('bb_u', 0):,} / 하단 {t.get('bb_l', 0):,}",
        "",
        "*🔗 피어 그룹 비교*"
    ]
    for name, data in p_results.items():
        pp = data.get("price", {})
        msg.append(f"  • {name}: {pp.get('close', 0):,}원 ({pp.get('rate', 0.0):+.2f}%)")

    msg.append("\n*📰 종목별 최신 뉴스*")
    for name, n_list in news_data.items():
        if n_list:
            msg.append(f"  ▸ {name}")
            msg.extend(n_list)
            
    msg.append("\n─────────────────────")
    msg.append("📌 _Claude 스킬 사용법_")
    msg.append("_JSON 파일을 Claude 채팅에 붙여넣고_")
    msg.append("_\"일간 리포트 노션에 올려줘\" 입력_")
    
    return "\n".join(msg)

def send_telegram(text: str, json_data: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    
    if not token or not chat_id:
        print("\n" + "="*60 + "\n" + text + "\n[JSON]\n" + json.dumps(json_data, ensure_ascii=False, indent=2))
        return

    base_url = f"https://api.telegram.org/bot{token}"
    
    # 1. 메시지 전송
    r1 = requests.post(f"{base_url}/sendMessage", json={
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    })
    if r1.status_code != 200: print(f"메시지 전송 실패: {r1.text}")

    # 2. JSON 파일 전송 (기존 성공 방식: 인메모리 jb 전송)
    jb = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    trade_date_str = json_data.get("trade_date", datetime.now().strftime("%Y%m%d"))
    
    r2 = requests.post(f"{base_url}/sendDocument", data={
        "chat_id": chat_id,
        "caption": f"📎 Claude 스킬 입력용 — {trade_date_str}",
    }, files={"document": (f"dozen_{trade_date_str}.json", jb, "application/json")})
    
    if r2.status_code == 200: print("✅ 텔레그램 전송 완료")
    else: print(f"파일 전송 실패: {r2.text}")

# ─────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────
def main():
    target_ticker = TICKERS["더즌"]
    v_date = find_latest_valid_date(target_ticker)
    
    if not v_date:
        print("❌ 유효 데이터를 찾지 못했습니다."); return

    # 데이터 패키징
    main_data = get_comprehensive_data(target_ticker, v_date)
    peer_results = {n: get_comprehensive_data(c, v_date) for n, c in TICKERS.items() if n != "더즌"}
    news_results = {n: fetch_news_list(f"{n} 주가") for n in TICKERS.keys()}
    
    package = {"date": v_date, "main": main_data, "peers": peer_results, "news": news_results}
    
    # 출력 및 전송
    report_text = format_telegram(package)
    json_output = {
        "trade_date": v_date.strftime("%Y-%m-%d"),
        "main_stock": main_data,
        "peer_stocks": peer_results,
        "news": news_results
    }
    
    send_telegram(report_text, json_output)

if __name__ == "__main__":
    main()
