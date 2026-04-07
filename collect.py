"""
더즌(462860) 기업분석 데이터 수집기 v4.10
===========================================
수정 사항:
1. SyntaxError 완벽 해결: 피어 그룹 출력부의 f-string 문법 교정
2. 데이터 수집 보장: 종가 0원 방지를 위한 날짜 역추적 로직 유지
3. 뉴스 수집 안정화: 네이버 뉴스 파싱 구조 최신화
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# [1] 설정
# ─────────────────────────────────────────
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

# ─────────────────────────────────────────
# [2] 유효 거래일 탐색 (0원 리포트 방지)
# ─────────────────────────────────────────
def find_latest_valid_date(ticker):
    now_kst = datetime.utcnow() + timedelta(hours=9)
    # 16시 전이면 전일 데이터부터, 후면 당일 데이터부터 체크
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

# ─────────────────────────────────────────
# [3] 데이터 수집 및 지표 연산
# ─────────────────────────────────────────
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    result = {"price": {}, "supply": {}, "tech": {}}
    
    try:
        # 시세 데이터
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0]
            result["price"] = {
                "close": int(row["종가"]), "open": int(row["시가"]),
                "high": int(row["고가"]), "low": int(row["저가"]),
                "vol": int(row["거래량"]), "amt": int(row["거래대금"]),
                "chg_rate": float(row["등락률"])
            }

        # 수급 데이터
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0]
            result["supply"] = {
                "ant": int(inv["개인"]), "foreigner": int(inv["외국인"]), "inst": int(inv["기관합계"])
            }

        # 기술적 지표 (내부 연산)
        start_ds = (target_date - timedelta(days=150)).strftime("%Y%m%d")
        df_h = krx.get_market_ohlcv(start_ds, ds, ticker)
        if not df_h.empty:
            ma5 = df_h['종가'].rolling(5).mean().iloc[-1]
            ma20 = df_h['종가'].rolling(20).mean().iloc[-1]
            
            diff = df_h['종가'].diff()
            up, down = diff.copy(), diff.copy()
            up[up < 0] = 0; down[down > 0] = 0
            avg_gain = up.ewm(com=13, adjust=False).mean()
            avg_loss = down.abs().ewm(com=13, adjust=False).mean()
            rsi = 100 - (100 / (1 + avg_gain/avg_loss)).iloc[-1]
            
            std = df_h['종가'].rolling(20).std().iloc[-1]
            
            result["tech"] = {
                "ma5": round(ma5), "ma20": round(ma20), "rsi": round(rsi, 2),
                "bb_u": round(ma20 + 2*std), "bb_l": round(ma20 - 2*std)
            }
    except:
        pass
    return result

# ─────────────────────────────────────────
# [4] 뉴스 수집 (네이버 최신순)
# ─────────────────────────────────────────
def fetch_news_list(query):
    news_list = []
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={urllib.parse.quote(query)}&sort=1"
        req = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(req.text, 'html.parser')
        
        for item in soup.select('ul.list_news > li.bx')[:3]:
            title_tag = item.select_one('a.news_tit')
            press_tag = item.select_one('a.info.press')
            if title_tag:
                title = title_tag.get_text(strip=True)
                press = press_tag.get_text(strip=True).replace("언론사 선정", "") if press_tag else "뉴스"
                news_list.append(f"    - {title} ({press})")
    except:
        pass
    return news_list

# ─────────────────────────────────────────
# [5] 메인 및 메시지 전송
# ─────────────────────────────────────────
def main():
    main_ticker = TICKERS["더즌"]
    valid_date = find_latest_valid_date(main_ticker)
    
    # 데이터 수집 실행
    main_data = get_comprehensive_data(main_ticker, valid_date)
    peer_results = {name: get_comprehensive_data(code, valid_date) for name, code in TICKERS.items() if name != "더즌"}

    # 데이터 추출
    p = main_data["price"]
    s = main_data["supply"]
    t = main_data["tech"]

    if not p or p.get("close", 0) == 0:
        print("데이터 수집 실패"); return

    status_icon = "📈" if p['chg_rate'] > 0 else "📉" if p['chg_rate'] < 0 else "➡️"
    
    # 텔레그램 메시지 조립
    msg = [
        f"📊 *더즌({main_ticker}) 기업분석 리포트*",
        f"분석 기준: {valid_date.strftime('%Y-%m-%d')} (종가 기준)",
        "",
        f"*{status_icon} 가격 및 거래량*",
        f"  • 종가: {p['close']:,}원 ({p['chg_rate']:+.2f}%)",
        f"  • 시/고/저: {p['open']:,}/{p['high']:,}/{p['low']:,}",
        f"  • 거래량: {p['vol']:,}주 / 대금: {p['amt']/100000000:.1f}억",
        "",
        f"*👥 투자자별 수급 (단위: 주)*",
        f"  • 개인: {s.get('ant', 0):+,} | 외인: {s.get('foreigner', 0):+,} | 기관: {s.get('inst', 0):+,}",
        "",
        f"*📐 기술적 지표 상세*",
        f"  • 이동평균: MA5({t.get('ma5', 0):,}) | MA20({t.get('ma20', 0):,})",
        f"  • RSI(14): {t.get('rsi', 0)} ({'과매수' if t.get('rsi', 0) > 70 else '과매도' if t.get('rsi', 0) < 30 else '중립'})",
        f"  • 볼린저밴드: 상단 {t.get('bb_u', 0):,} / 하단 {t.get('bb_l', 0):,}",
        "",
        f"*🔗 피어 그룹 비교*",
    ]
    
    # 피어 그룹 출력 (에러 발생 지점 수정 완료)
    for name, d in peer_results.items():
        pp = d.get("price", {})
        price_val = pp.get("close", 0)
        rate_val = pp.get("chg_rate", 0.0)
        msg.append(f"  • {name}: {price_val:,}원 ({rate_val:+.2f}
