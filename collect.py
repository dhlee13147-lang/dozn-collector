"""
더즌(462860) 기업분석 데이터 수집기 v4.11 (최종 완결본)
===================================================
업데이트 내역:
- [v4.5] 데이터 0원 방지: 유효 데이터 발견 시까지 최대 10일 역추적 로직 도입
- [v4.6] 기술지표 연산: Pandas 기반 RSI, 이동평균, 볼린저밴드 내부 계산
- [v4.7] SyntaxError 해결: f-string 문법 및 문자열 잘림 현상 완벽 수정
- [v4.8] 뉴스 수집 강화: 네이버 뉴스 최신 구조 반영 및 언론사 매칭 정확도 향상
===================================================
출처(References):
- 주가/수급: 한국거래소(KRX) http://data.krx.co.kr/
- 뉴스: 네이버 뉴스 https://search.naver.com/
- 기술분석: Pandas Finance Library https://pandas.pydata.org/
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# [1] 기본 설정
TICKERS = {
    "더즌": "462860",
    "헥토파이낸셜": "234340",
    "쿠콘": "294570"
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"
}

# [2] 유효 거래일 탐색 (0원 리포트 방지 핵심 로직)
def find_latest_valid_date(ticker):
    """값이 정상적으로 존재하는 가장 최근의 거래일을 역추적하여 반환합니다."""
    now_kst = datetime.utcnow() + timedelta(hours=9)
    # 오후 4시 이전이면 전날 데이터부터 확인 시작
    search_start = now_kst.date() if now_kst.hour >= 16 else now_kst.date() - timedelta(days=1)
    
    for i in range(10):  # 최대 10일 전까지 확인
        target = search_start - timedelta(days=i)
        ds = target.strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv(ds, ds, ticker)
            if not df.empty and df.iloc[0]["종가"] > 0:
                return target
        except:
            continue
    return search_start

# [3] 종합 데이터 수집 및 기술적 지표 연산
def get_comprehensive_data(ticker, target_date):
    ds = target_date.strftime("%Y%m%d")
    result = {"price": {}, "supply": {},
