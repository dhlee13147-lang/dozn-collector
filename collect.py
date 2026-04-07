"""
더즌(462860) 기업분석 데이터 수집기 v4.21
===================================================
- 명세서 반영: ACC_TRDVAL(거래대금), ACC_TRDVOL(거래량) 직접 매핑
- 오류 수정: 127라인 조건문 및 78라인 뉴스 URL 잘림 현상 해결
- 수집 보장: 데이터 누락 시 0으로 치환 및 유효 거래일 자동 탐색
- 전송 로직: 기존에 성공했던 텔레그램 바이너리 전송 방식 적용
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

# [2] 유효 거래일 탐색 (명세서상 2010-01-04 데이터부터 제공 확인)
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

# [3] 데이터 수집 (명세서 필드명 ACC_TRDVAL, ACC_TRDVOL 적용)
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {"close": 0, "rate": 0.0, "vol": 0, "amt": 0}, "supply": {"ant": 0, "foreigner": 0, "inst": 0}, "tech": {}}
    try:
        # 시세 및 거래량/대금 수집
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0].to_dict()
            # 명세서 필드명 직접 매핑
            vol = row.get("ACC_TRDVOL") or row.get("거래량") or 0
            amt = row.get("ACC_TRDVAL") or row.get("거래대금") or 0
            
            res["price"] = {
                "close": int(row.get("종가") or row.get("TDD_CLSPRC") or 0), 
                "rate": float(row.get("등락률") or row.get("FLUC_RT") or 0.0), 
                "vol": int(vol), 
                "amt": int(amt)
            }
        
        # 투자자별 수급 수집
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0].to_dict()
            res["supply"] = {
                "ant": int(inv.get("개인") or 0), 
                "foreigner": int(inv.get("외국인") or 0), 
                "inst": int(inv.get("기관합계") or 0)
            }

        # 기술적 지표 연산 (Pandas 기반)
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

# [4] 뉴스 수집 (SyntaxError 방지 구조)
def fetch_news_list(query):
    news_items = []
    try:
        base_url = "https://search.naver.com/search.naver"
        params = {"where": "news", "query": query, "sort": "1"}
        target_url = base_url + "?" + urllib.parse.urlencode(params)
        
        resp = requests.get(target_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for item in soup.select('ul.list_news > li.bx')[:3]:
            t_tag, p_tag = item.select_one('a.news_tit'), item.select_one('a.info.press')
            if t_tag:
                title = t_tag.get_text(strip=True)
                press = p_tag.get_text(strip=True) if p_tag else "뉴스"
                news_items.append(f"    - {title} ({press})")
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
    target_ticker = TICKERS["더즌"]
    v_date = find_latest_valid_date(target_ticker)
    main_data = get_comprehensive_data(target_ticker, v_date)
    peer_results = {n: get_comprehensive_data(c, v_date) for n, c in TICKERS.items() if n != "더즌"}
    news_results = {n: fetch_news_list(f"{n} 주가") for n in TICKERS.keys()}

    p, s, t = main_data["price"], main_data["supply"], main_data["tech"]
    status = "📈" if p['rate'] > 0 else "📉" if p['rate'] < 0 else "➡️"
    
    # RSI 판정 로직 단순화 (에러 방지)
    rsi_val = t.get('rsi', 0)
    rsi_sig = "중립"
    if rsi_val > 70: rsi_sig = "과매수"
    elif rsi_val < 30: rsi_sig = "과매도"

    report = [
        f"📊 *더
