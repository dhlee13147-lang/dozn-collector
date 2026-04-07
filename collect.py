"""
더즌(462860) 기업분석 데이터 수집기 v4.19
===================================================
- 오류 수정: 128라인 RSI 판정 로직의 f-string 구문 오류 해결
- 명세서 반영: ACC_TRDVAL(거래대금), ACC_TRDVOL(거래량) 직접 매핑
- 수집 보장: 데이터 누락 시 0으로 치환하여 리포트 완성 보장
- 파일 생성: GitHub Artifact 업로드를 위한 output 폴더 자동 생성
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

# [2] 유효 거래일 탐색 (명세서상 2010년 이후 데이터 제공)
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

# [3] 데이터 수집 (명세서 규격 적용)
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    res = {"price": {"close": 0, "rate": 0.0, "vol": 0, "amt": 0}, "supply": {"ant": 0, "foreigner": 0, "inst": 0}, "tech": {}}
    try:
        df = krx.get_market_ohlcv(ds, ds, ticker)
        if not df.empty:
            row = df.iloc[0].to_dict()
            # API 명세서 필드명(ACC_TRDVOL, ACC_TRDVAL) 매핑
            vol = row.get("ACC_TRDVOL") or row.get("거래량") or 0
            amt = row.get("ACC_TRDVAL") or row.get("거래대금") or 0
            
            res["price"] = {
                "close": int(row.get("종가") or row.get("TDD_CLSPRC") or 0), 
                "rate": float(row.get("등락률") or row.get("FLUC_RT") or 0.0), 
                "vol": int(vol), 
                "amt": int(amt)
            }
        
        df_inv = krx.get_market_net_purchases_of_equities_by_ticker(ds, ds, ticker)
        if not df_inv.empty:
            inv = df_inv.iloc[0].to_dict()
            res["supply"] = {
                "ant": int(inv.get("개인") or 0), 
                "foreigner": int(inv.get("외국인") or 0), 
                "inst": int(inv.get("기관합계") or 0)
            }

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

# [4] 뉴스 수집
def fetch_news_list(query):
    news_items = []
    try:
        url = f"
