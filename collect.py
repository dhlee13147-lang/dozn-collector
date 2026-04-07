import os, json, re, time
from datetime import datetime, date, timedelta
import requests
from bs4 import BeautifulSoup # 가급적 BeautifulSoup 사용 권장

# ... (기존 설정 및 헬퍼 함수 생략) ...

# ─────────────────────────────────────────
# 1. 핀업 수급/거래대금 수집 (패턴 보강)
# ─────────────────────────────────────────
def fetch_finup(ticker: str) -> dict:
    url = f"https://finance.finup.co.kr/Stock/{ticker}"
    result = {"시가": 0, "고가": 0, "저가": 0, "거래량": 0, "거래대금": 0, "history": [], "_source": "finup"}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        html = resp.text

        # 거래대금 패턴 강화 (공백 및 단위 처리 유연화)
        amt_m = re.search(r'거래대금\(원\)\s*</th>\s*<td>\s*<span[^>]*>([\d,억만\s]+)</span>', html)
        if not amt_m: # 대안 패턴
            amt_m = re.search(r'거래대금\(원\)\s*\n\s*([\d,억만]+)', html)
        
        if amt_m:
            result["거래대금"] = parse_amt(amt_m.group(1).strip())

        # 10일 수급 테이블 (정규식 대신 구조적 접근 시뮬레이션)
        # 핀업의 경우 실제 데이터가 <table> 내 <tr>로 존재함
        # 아래는 기존 re 기반 수집을 더 유연한 공백 패턴으로 수정한 버전
        row_pattern = r'(\d{4}-\d{2}-\d{2})\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([+-]?[\d.]+%)\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)\s*\|\s*([+-]?[\d,]+)'
        rows = re.findall(row_pattern, html)
        
        for row in rows[:10]:
            result["history"].append({
                "날짜": row[0], "종가": parse_int(row[1]), "개인": parse_signed_int(row[4]),
                "외국인": parse_signed_int(row[5]), "기관": parse_signed_int(row[6]),
            })
            
    except Exception as e:
        print(f"    [WARN] 핀업 파싱 실패: {e}")
    return result

# ─────────────────────────────────────────
# 2. 뉴스 수집 (네이버 검색 결과 크롤링 응용)
# ─────────────────────────────────────────
def fetch_news(query: str, max_items: int = 15) -> list:
    """네이버 뉴스 최신순 수집 (개선된 로직)"""
    encoded_query = urllib.parse.quote(query)
    # sort=1 (최신순), pd=4 (최근 1일)
    url = f"https://search.naver.com/search.naver?where=news&query={encoded_query}&sm=tab_opt&sort=1&nso=so:dd,p:1d"
    
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        news_list = soup.select('ul.list_news > li.bx')
        
        for news in news_list[:max_items]:
            title_tag = news.select_one('a.news_tit')
            if not title_tag: continue
            
            title = title_tag.get_text(strip=True)
            link = title_tag['href']
            
            # 언론사 및 시간 정보
            press = news.select_one('a.info.press')
            press_text = press.get_text(strip=True).replace('언론사 선정', '') if press else "알수없음"
            
            time_tag = news.select_one('span.info')
            time_text = time_tag.get_text(strip=True) if time_tag else ""
            
            # 요약 내용
            desc_tag = news.select_one('div.news_dsc')
            desc = desc_tag.get_text(strip=True) if desc_tag else ""

            items.append({
                "제목": title,
                "언론사": press_text,
                "시간": time_text,
                "요약": desc[:150],
                "링크": link,
            })
            
    except Exception as e:
        print(f"    [WARN] 뉴스 수집 중 오류 발생 ('{query}'): {e}")
    return items
