"""
더즌(462860) 기업분석 데이터 수집기 v4.23
===================================================
- 오류 수정: pip 설치 오류 해결 (financedatareader 소문자 적용)
- 명세서 반영: ACC_TRDVAL(거래대금), ACC_TRDVOL(거래량) 데이터 매핑 
- 데이터 소스: FinanceDataReader + pykrx 하이브리드
- 전송 로직: 기존 성공했던 텔레그램 바이너리 전송 방식 적용
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock as krx
from bs4 import BeautifulSoup

# [1] 설정
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"}

# [2] 유효 거래일 탐색
def find_latest_valid_date(ticker):
    now_kst = datetime.utcnow() + timedelta(hours=9)
    end_date = now_kst.date() if now_kst.hour >= 16 else now_kst.date() - timedelta(days=1)
    start_date = end_date - timedelta(days=10)
    try:
        df = fdr.DataReader(ticker, start_date, end_date)
        if not df.empty:
            return df.index[-1].to_pydatetime().date()
    except: pass
    return end_date

# [3] 데이터 수집 (명세서 규격 ACC_TRDVAL, ACC_TRDVOL 반영)
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {"close": 0, "rate": 0.0, "vol": 0, "amt": 0}, "supply": {"ant": 0, "foreigner": 0, "inst": 0}, "tech": {}}
    try:
        # fdr 시세 수집 (거래량: ACC_TRDVOL, 거래대금: ACC_TRDVAL 대응) 
        df_price = fdr.DataReader(ticker, target_date, target_date)
        if not df_price.empty:
            row = df_price.iloc[0]
            res["price"] = {
                "close": int(row.get("Close", 0)),
                "rate": float(row.get("Change", 0.0) * 100),
                "vol": int(row.get("Volume", 0)), # Spec: ACC_TRDVOL 
                "amt": int(row.get("Amount", 0))  # Spec: ACC_TRDVAL 
            }

        # pykrx 수급 수집
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0]
            res["supply"] = {
                "ant": int(inv.get("개인", 0)),
                "foreigner": int(inv.get("외국인", 0)),
                "inst": int(inv.get("기관합계", 0))
            }

        # 기술적 지표 연산
        start_ds = (target_date - timedelta(days=150)).strftime("%Y-%m-%d")
        df_h = fdr.DataReader(ticker, start_ds, target_date.strftime("%Y-%m-%d"))
        if not df_h.empty:
            c = df_h['Close']
            ma5, ma20 = c.rolling(5).mean().iloc[-1], c.rolling(20).mean().iloc[-1]
            diff = c.diff(); up, down = diff.where(diff > 0, 0), -diff.where(diff < 0, 0)
            rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
            std = c.rolling(20).std().iloc[-1]
            res["tech"] = {"ma5": int(ma5), "ma20": int(ma20), "rsi": round(rsi, 2), "bb_u": int(ma20 + 2*std), "bb_l": int(ma20 - 2*std)}
    except: pass
    return res

# [4] 뉴스 수집
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

# [5] 텔레그램 전송 (기존 성공 로직)
def send_telegram(text: str, json_data: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id: return
    base_url = f"https://api.telegram.org/bot{token}"
    requests.post(f"{base_url}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True})
    jb = json.dumps(json_data, ensure_ascii=False, indent=2).encode("utf-8")
    t_str = json_data.get("trade_date", "output")
    requests.post(f"{base_url}/sendDocument", data={"chat_id": chat_id}, files={"document": (f"dozen_{t_str}.json", jb)})

# [6] 메인 실행
def main():
    target_ticker = TICKERS["더즌"]
    v_date = find_latest_valid_date(target_ticker)
    main_data = get_comprehensive_data(target_ticker, v_date)
    peer_results = {n: get_comprehensive_data(c, v_date) for n, c in TICKERS.items() if n != "더즌"}
    news_results = {n: fetch_news_list(f"{n} 주가") for n in TICKERS.keys()}

    p, s, t = main_data["price"], main_data["supply"], main_data["tech"]
    status = "📈" if p['rate'] > 0 else "📉" if p['rate'] < 0 else "➡️"
    
    rsi_val = t.get('rsi', 0)
    rsi_sig = "과매수" if rsi_val > 70 else "과매도" if rsi_val < 30 else "중립"

    report = [
        f"📊 *더즌({target_ticker}) 기업분석 리포트* — {v_date.strftime('%Y-%m-%d')}",
        "",
        f"*{status} 가격 및 거래 지표*",
        f"  • 종가: {p['close']:,}원 ({p['rate']:+.2f}%)",
        f"  • 거래량: {p['vol']:,}주 / 대금: {p['amt']/100000000:.2f}억",
        "",
        f"*👥 투자자별 수급 (단위: 주)*",
        f"  • 개인: {s['ant']:+,} | 외인: {s['foreigner']:+,} | 기관: {s['inst']:+,}",
        "",
        f"*📐 기술적 지표*",
        f"  • 이동평균: MA5({t.get('ma5', 0):,}) | MA20({t.get('ma20', 0):,})",
        f"  • RSI(14): {rsi_val} ({rsi_sig})",
        f"  • 볼린저밴드: 상단 {t.get('bb_u', 0):,} / 하단 {t.get('bb_l', 0):,}",
        "",
        "*🔗 피어 그룹 비교*"
    ]
    for name, data in peer_results.items():
        pp = data.get("price", {})
        report.append(f"  • {name}: {pp.get('close', 0):,}원 ({pp.get('rate', 0.0):+.2f}%)")

    report.append("\n*📰 종목별 최신 뉴스*")
    for name, n_list in news_results.items():
        if n_list:
            report.append(f"  ▸ {name}"); report.extend(n_list)

    full_text = "\n".join(report)
    json_out = {"trade_date": v_date.strftime("%Y-%m-%d"), "main": main_data, "peers": peer_results, "news": news_results}
    
    send_telegram(full_text, json_out)
    os.makedirs("output", exist_ok=True)
    with open(f"output/dozen_{v_date.strftime('%Y%m%d')}.json", "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2)
    print("✅ 모든 작업 완료")

if __name__ == "__main__":
    main()
