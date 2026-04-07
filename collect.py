"""
더즌(462860) 일간 기업분석 데이터 수집기 v4.7
===========================================
주요 특징:
1. 데이터 누락 방지: 값이 0일 경우 실제 거래가 있었던 최근 날짜를 자동 탐색
2. 뉴스 수집 최적화: 최신 네이버 뉴스 검색 구조 반영
3. 지표 해석 강화: 경영전략 직무에 적합한 기술적 지표 자동 판정
"""

import os, json, re, time, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
import requests
import pandas as pd
from pykrx import stock as krx
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
# [1] 기본 설정
# ─────────────────────────────────────────
TICKERS = {
    "더즌": "462860",
    "헥토파이낸셜": "234340",
    "쿠콘": "294570"
}
DART_CORP_CODES = {
    "더즌": "01615947",
    "헥토파이낸셜": "00669540",
    "쿠콘": "00798833"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

# ─────────────────────────────────────────
# [2] 날짜 및 데이터 무결성 로직
# ─────────────────────────────────────────
def get_verified_trade_date(ticker):
    """값이 정상적으로 존재하는 가장 최근의 거래일을 찾아냅니다."""
    now_kst = datetime.utcnow() + timedelta(hours=9)
    # 장 마감(
