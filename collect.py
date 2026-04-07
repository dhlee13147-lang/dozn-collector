"""
더즌(462860) 기업분석 데이터 수집기 v4.12
===========================================
- SyntaxError 방지를 위한 괄호 구조 간소화
- 데이터 0원 방지: 유효 날짜 자동 역추적 로직
- 기술적 지표: RSI, MA, BB 내부 계산
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# [1] 설정
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"}

# [2] 유효 거래일 탐색
def find_latest_valid_date(ticker):
    now_kst = datetime.utcnow() + timedelta(hours=9)
    search_start = now_kst.date() if now_kst.hour >= 16 else now_kst.date() - timedelta(days=1)
    for i in range(10):
        target = search_start - timedelta(days=i)
        ds = target.strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv(ds, ds, ticker)
            if not df.empty and df.iloc[0]["종가"] > 0:
                return target
        except:
            continue
    return search_start

# [3] 데이터 수집 및 지표 연산
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {}, "supply": {}, "tech": {}}
    try:
        # 가격 정보
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0]
            res["price"] = {"close": int(row["종가"]), "rate": float(row["등락률"]), "vol": int(row["거래량"]), "amt": int(row["거래대금"])}
        
        # 수급 정보
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0]
            res["supply"] = {"ant": int(inv["개인"]), "foreigner": int(inv["외국인"]), "inst": int(inv["기관합계"])}

        # 기술적 지표 (150일 데이터)
        start_ds = (target_date - timedelta(days=150)).strftime("%Y%m%d")
        df_h = krx.get_market_ohlcv(start_ds, ds, ticker)
        if not df_h.empty:
            ma5 = df_h['종가'].rolling(5).mean().iloc[-1]
            ma20 = df_h['종가'].rolling(20).mean().iloc[-1]
            diff = df_h['종가'].diff()
            up = diff.where(diff > 0, 0)
            down = -diff.where(diff < 0, 0)
            rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
            std = df_h['종가'].rolling(20).std().iloc[-1]
            res["tech"] = {"ma5": int(ma5), "ma20": int(ma20), "rsi": round(rsi, 2), "bb_u": int(ma20 + 2*std), "bb_l": int(ma20 - 2*std)}
    except:
        pass
    return res

# [4] 뉴스 수집
def fetch_news_list(query):
    news_items = []
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={urllib.parse.quote(query)}&sort=1"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for item in soup.select('ul.list_news > li.bx')[:3]:
            t_tag = item.select_one('a.news_tit')
            p_tag = item.select_one('a.info.press')
            if t_tag:
                news_items.append(f"    - {t_tag.get_text(strip=True)} ({p_tag.get_text(strip=True) if p_tag else '네이버'})")
    except:
        pass
    return news_items

# [5] 메인 실행 및 전송
def main():
    target_ticker = TICKERS["더즌"]
    v_date = find_latest_valid_date(target_ticker)
    
    m_data = get_comprehensive_data(target_ticker, v_date)
    p_results = {n: get_comprehensive_data(c, v_date) for n, c in TICKERS.items() if n != "더즌"}

    p, s, t = m_data["price"], m_data["supply"], m_data["tech"]
    if not p or p.get("close", 0) == 0:
        print("데이터 확인 불가"); return

    icon = "📈" if p['rate'] > 0 else "📉" if p['rate'] < 0 else "➡️"
    msg = [
        f"📊 *더즌({target_ticker}) 기업분석 리포트*",
        f"기준 일자: {v_date.strftime('%Y-%m-%d')}",
        "",
        f"*{icon} 주가 동향*",
        f"  • 종가: {p['close']:,}원 ({p['rate']:+.2f}%)",
        f"  • 거래량: {p['vol']:,}주 / 대금: {p['amt']/100000000:.1f}억",
        "",
        f"*👥 투자자별 수급 (주)*",
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
    for q_name in TICKERS.keys():
        n_list = fetch_news_list(f"{q_name} 주가")
        if n_list:
            msg.append(f"  ▸ {q_name}")
            msg.extend(n_list)

    full_text = "\n".join(msg)
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": full_text, "parse_mode": "Markdown", "disable_web_page_preview": True})
    else:
        print(full_text)

if __name__ == "__main__":
    main()
