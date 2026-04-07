"""
더즌(462860) 기업분석 데이터 수집기 v4.18
===================================================
- 필드 매핑: 명세서 기반 ACC_TRDVAL, ACC_TRDVOL 강제 매핑
- 오류 방지: 데이터 누락 시 프로그램 중단 없이 0으로 치환하여 리포트 완성
- 파일 생성: GitHub Artifact 업로드를 위한 output 폴더 및 파일 생성 로직 강화
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
            if not df.empty: return target
        except: continue
    return search_start

# [3] 데이터 수집 (명세서 필드명 대응 강화)
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {"close": 0, "rate": 0.0, "vol": 0, "amt": 0}, "supply": {"ant": 0, "foreigner": 0, "inst": 0}, "tech": {}}
    try:
        # 시세 정보 수집
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0].to_dict() # 명세서 필드 대조를 위해 딕셔너리 변환
            
            # 명세서 필드명(ACC_TRDVOL, ACC_TRDVAL) 우선 추출
            vol = row.get("ACC_TRDVOL") or row.get("거래량") or 0
            amt = row.get("ACC_TRDVAL") or row.get("거래대금") or 0
            
            res["price"] = {
                "close": int(row.get("종가") or row.get("TDD_CLSPRC") or 0), 
                "rate": float(row.get("등락률") or row.get("FLUC_RT") or 0.0), 
                "vol": int(vol), 
                "amt": int(amt)
            }
        
        # 수급 정보 수집
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0].to_dict()
            res["supply"] = {
                "ant": int(inv.get("개인") or 0), 
                "foreigner": int(inv.get("외국인") or 0), 
                "inst": int(inv.get("기관합계") or 0)
            }

        # 기술적 지표 계산
        start_ds = (target_date - timedelta(days=150)).strftime("%Y%m%d")
        df_h = krx.get_market_ohlcv(start_ds, ds, ticker)
        if not df_h.empty:
            c = df_h['종가']
            ma5, ma20 = c.rolling(5).mean().iloc[-1], c.rolling(20).mean().iloc[-1]
            diff = c.diff(); up, down = diff.where(diff > 0, 0), -diff.where(diff < 0, 0)
            rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
            std = c.rolling(20).std().iloc[-1]
            res["tech"] = {"ma5": int(ma5), "ma20": int(ma20), "rsi": round(rsi, 2), "bb_u": int(ma20 + 2*std), "bb_l": int(ma20 - 2*std)}
    except Exception as e:
        print(f"⚠️ 데이터 수집 중 오류: {e}")
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

# [5] 텔레그램 전송
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
    ticker_dozen = TICKERS["더즌"]
    v_date = find_latest_valid_date(ticker_dozen)
    
    main_data = get_comprehensive_data(ticker_dozen, v_date)
    peer_results = {n: get_comprehensive_data(c, v_date) for n, c in TICKERS.items() if n != "더즌"}
    news_results = {n: fetch_news_list(f"{n} 주가") for n in TICKERS.keys()}

    p, s, t = main_data["price"], main_data["supply"], main_data["tech"]
    status = "📈" if p['rate'] > 0 else "📉" if p['rate'] < 0 else "➡️"
    
    report = [
        f"📊 *더즌({ticker_dozen}) 기업분석 리포트* — {v_date.strftime('%Y-%m-%d')}",
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
        f"  • RSI(14): {t.get('rsi', 0)} ({'과매수' if t.get('rsi', 0) > 70 else '과매도' if t.get('rsi', 0)
