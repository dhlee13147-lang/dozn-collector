"""
더즌(462860) 기업분석 데이터 수집기 v4.8
===========================================
수정 사항:
1. 데이터 누락 차단: 종가가 0원일 경우, 데이터가 있는 날짜를 찾을 때까지 최대 10일 역추적
2. 뉴스 수집 우회: 네이버 뉴스 차단을 피하기 위한 헤더 강화 및 파싱 로직 수정
3. 기술적 지표 정밀화: Pandas 기반 내부 계산으로 외부 사이트 의존도 제로화
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# 설정 (TICKER 및 DART)
# ─────────────────────────────────────────
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}
DART_CODES = {"더즌": "01615947", "헥토파이낸셜": "00669540", "쿠콘": "00798833"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Cache-Control": "max-age=0",
}

# ─────────────────────────────────────────
# 데이터가 있는 최근 거래일 찾기 (핵심 로직)
# ─────────────────────────────────────────
def find_latest_valid_date(ticker):
    """데이터가 존재할 때까지 과거 날짜를 하나씩 확인합니다."""
    # 한국 시간(KST) 기준 현재 날짜 설정
    now_kst = datetime.utcnow() + timedelta(hours=9)
    # 오후 4시 이전이면 전날부터 체크 시작
    search_start = now_kst.date() if now_kst.hour >= 16 else now_kst.date() - timedelta(days=1)
    
    for i in range(10):  # 최근 10일간 역추적
        target = search_start - timedelta(days=i)
        ds = target.strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv(ds, ds, ticker)
            if not df.empty and df.iloc[0]["종가"] > 0:
                print(f"✅ 유효 데이터 발견 날짜: {ds}")
                return target
        except:
            continue
    return search_start

# ─────────────────────────────────────────
# 종합 데이터 수집 함수
# ─────────────────────────────────────────
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    result = {"price": {}, "supply": {}, "tech": {}}
    
    try:
        # 1. 시세/거래량/거래대금
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0]
            result["price"] = {
                "close": int(row["종가"]), "open": int(row["시가"]),
                "high": int(row["고가"]), "low": int(row["저가"]),
                "vol": int(row["거래량"]), "amt": int(row["거래대금"]),
                "chg_rate": float(row["등락률"])
            }

        # 2. 투자자별 수급
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0]
            result["supply"] = {
                "ant": int(inv["개인"]), "foreigner": int(inv["외국인"]), "inst": int(inv["기관합계"])
            }

        # 3. 기술적 지표 (최근 120일치 기반)
        start_ds = (target_date - timedelta(days=150)).strftime("%Y%m%d")
        df_h = krx.get_market_ohlcv(start_ds, ds, ticker)
        if not df_h.empty:
            # 이동평균
            ma5 = df_h['종가'].rolling(5).mean().iloc[-1]
            ma20 = df_h['종가'].rolling(20).mean().iloc[-1]
            # RSI
            diff = df_h['종가'].diff()
            up, down = diff.copy(), diff.copy()
            up[up < 0] = 0; down[down > 0] = 0
            avg_gain = up.ewm(com=13, adjust=False).mean()
            avg_loss = down.abs().ewm(com=13, adjust=False).mean()
            rsi = 100 - (100 / (1 + avg_gain/avg_loss)).iloc[-1]
            # 볼린저밴드
            std = df_h['종가'].rolling(20).std().iloc[-1]
            
            result["tech"] = {
                "ma5": round(ma5), "ma20": round(ma20), "rsi": round(rsi, 2),
                "bb_u": round(ma20 + 2*std), "bb_l": round(ma20 - 2*std)
            }
    except Exception as e:
        print(f"❌ 데이터 수집 중 오류: {e}")
    return result

# ─────────────────────────────────────────
# 뉴스 수집 (네이버 우회 로직 강화)
# ─────────────────────────────────────────
def fetch_news_expert(query):
    news_list = []
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={urllib.parse.quote(query)}&sort=1"
        req = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(req.text, 'html.parser')
        
        # 네이버 뉴스 검색 결과 영역 파싱
        for item in soup.select('ul.list_news > li.bx')[:3]:
            title_tag = item.select_one('a.news_tit')
            press_tag = item.select_one('a.info.press')
            if title_tag:
                title = title_tag.get_text(strip=True)
                press = press_tag.get_text(strip=True).replace("언론사 선정", "") if press_tag else "뉴스"
                news_list.append(f"    - {title} ({press})")
    except Exception as e:
        print(f"❌ 뉴스 수집 오류 ({query}): {e}")
    return news_list

# ─────────────────────────────────────────
# 메인 실행 및 텔레그램 포맷팅
# ─────────────────────────────────────────
def main():
    main_name = "더즌"
    main_ticker = TICKERS[main_name]
    
    # 1. 데이터가 유효한 날짜 찾기
    valid_date = find_latest_valid_date(main_ticker)
    
    # 2. 전 종목 데이터 수집
    main_data = get_comprehensive_data(main_ticker, valid_date)
    peer_results = {}
    for name, code in TICKERS.items():
        if name != main_name:
            peer_results[name] = get_comprehensive_data(code, valid_date)

    # 3. 메시지 작성
    p, s, t = main_data["price"], main_data["supply"], main_data["tech"]
    if not p or p.get("close", 0) == 0:
        print("최종적으로 데이터를 찾지 못했습니다."); return

    status_icon = "📈" if p['chg_rate'] > 0 else "📉" if p['chg_rate'] < 0 else "➡️"
    
    msg = [
        f"📊 *{main_name}({main_ticker}) 기업분석 리포트*",
        f"분석 기준: {valid_date.strftime('%Y-%m-%d')} (장 마감 데이터)",
        "",
        f"*{status_icon} 가격 및 거래량*",
        f"  • 종가: {p['close']:,}원 ({p['chg_rate']:+.2f}%)",
        f"  • 시/고/저: {p['open']:,}/{p['high']:,}/{p['low']:,}",
        f"  • 거래량: {p['vol']:,}주 / 대금: {p['amt']/100000000:.1f}억",
        "",
        f"*👥 투자자별 수급 (주)*",
        f"  • 개인: {s.get('ant',0):+,} | 외인: {s.get('foreigner',0):+,} | 기관: {s.get('inst',0):+,}",
        "",
        f"*📐 기술적 분석*",
        f"  • 이동평균: MA5({t.get('ma5',0):,}) | MA20({t.
