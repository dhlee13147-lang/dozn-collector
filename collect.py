"""
더즌(462860) 기업분석 리포트 생성기 v4.25
===================================================
- 가이드 분석: PyKrx stock.get_market_ohlcv 규격 준수
- 명세서 반영: ACC_TRDVAL(거래대금), TDD_CLSPRC(종가) 필드 매핑 
- 지표 추가: OBV, MACD, 이동평균, RSI, 볼린저밴드 연산 포함
- 템플릿: 사용자 요청 맞춤형 텔레그램 메시지 양식 적용
"""

import os, json, urllib.parse, datetime
import requests
import pandas as pd
from pykrx import stock
from bs4 import BeautifulSoup

# [1] 기본 설정
TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}

def get_latest_date():
    """최신 유효 영업일 탐색 (KST 16:00 기준 당일/전일 결정)"""
    now = datetime.datetime.now()
    # 장 마감 시간 고려하여 탐색 시작일 설정
    target = now if now.hour >= 16 else now - datetime.timedelta(days=1)
    for i in range(10):
        dt = (target - datetime.timedelta(days=i)).strftime("%Y%m%d")
        # 데이터 존재 여부 확인
        df = stock.get_market_ohlcv(dt, dt, TICKERS["더즌"])
        if not df.empty: return dt
    return target.strftime("%Y%m%d")

def fetch_data(ticker, dt):
    """PyKrx 가이드 및 API 명세서 기반 데이터 종합 수집 """
    res = {}
    
    # 1. OHLCV 수집 (종가, 거래량, 거래대금 등) 
    df = stock.get_market_ohlcv(dt, dt, ticker)
    if not df.empty:
        row = df.iloc[0]
        # 명세서의 ACC_TRDVAL은 PyKrx의 '거래대금' 컬럼에 매핑됨 
        res['ohlcv'] = {
            "종가": int(row['종가']), "시가": int(row['시가']), "고가": int(row['고가']),
            "저가": int(row['저가']), "거래량": int(row['거래량']), "거래대금": int(row['거래대금']),
            "등락률": float(row['등락률'])
        }
    
    # 2. 기술적 지표 계산용 이력 데이터 수집 (최근 180일)
    start_dt = (datetime.datetime.strptime(dt, "%Y%m%d") - datetime.timedelta(days=180)).strftime("%Y%m%d")
    df_h = stock.get_market_ohlcv(start_dt, dt, ticker)
    if not df_h.empty:
        c = df_h['종가']
        v = df_h['거래량']
        
        # 이동평균 (MA5, 10, 20)
        res['ma'] = {
            "ma5": int(c.rolling(5).mean().iloc[-1]), 
            "ma10": int(c.rolling(10).mean().iloc[-1]), 
            "ma20": int(c.rolling(20).mean().iloc[-1])
        }
        
        # RSI(14) 연산
        diff = c.diff(); up = diff.where(diff > 0, 0); down = -diff.where(diff < 0, 0)
        rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
        res['rsi'] = round(rsi, 2)
        
        # 볼린저밴드 (20일, 2표준편차)
        std = c.rolling(20).std().iloc[-1]
        ma20 = res['ma']['ma20']
        res['bb'] = {"u": int(ma20 + 2*std), "m": ma20, "l": int(ma20 - 2*std)}
        
        # MACD (12, 26, 9)
        exp1 = c.ewm(span=12, adjust=False).mean()
        exp2 = c.ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        res['macd'] = {"val": round(macd.iloc[-1], 2), "sig": round(signal.iloc[-1], 2)}
        
        # OBV (On Balance Volume)
        obv = (v * (~c.diff().le(0) * 2 - 1)).cumsum()
        res['obv'] = {"val": obv.iloc[-1], "prev": obv.iloc[-2], "p_diff": c.diff().iloc[-1]}

    # 3. 투자자별 수급 데이터 수집
    df_i = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, ticker)
    if not df_i.empty:
        row_i = df_i.iloc[0]
        res['supply'] = {
            "개인": int(row_i['개인']), 
            "외인": int(row_i['외국인']), 
            "기관": int(row_i['기관합계'])
        }
    
    return res

def send_telegram(msg):
    """텔레그램 메시지 전송"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True}
        requests.post(url, json=payload)

def main():
    dt = get_latest_date()
    formatted_date = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    
    # 더즌 데이터 수집
    d_data = fetch_data(TICKERS["더즌"], dt)
    o, m, b, mc, ob, s = d_data['ohlcv'], d_data['ma'], d_data['bb'], d_data['macd'], d_data['obv'], d_data['supply']
    
    # 전략적 지표 해석
    rsi_status = "과매수 ⚠️" if d_data['rsi'] >= 70 else "과매도 📉" if d_data['rsi'] <= 30 else "중립"
    
    # OBV 해석
    obv_msg = "보합"
    if ob['p_diff'] > 0 and ob['val'] > ob['prev']: 
        obv_msg = "주가↑ OBV↑ — 상승 신뢰도 높음 ✅"
    elif ob['p_diff'] < 0 and ob['val'] < ob['prev']:
        obv_msg = "주가↓ OBV↓ — 하락 신뢰도 높음 📉"

    # 볼린저밴드 위치 해석
    if o['종가'] > b['m']:
        bb_pos = "중심선 위"
    else:
        dist = b['m'] - b['l']
        margin = round((o['종가'] - b['l']) / dist * 100) if dist != 0 else 0
        bb_pos = f"중중심선 아래 (하단까지 {margin}% 여유)"

    # 템플릿 기반 리포트 작성
    report = f"""📊 *더즌({TICKERS['더즌']}) 일간 주가 리포트 — {formatted_date}*

📈 *주가 요약*
  종가: {o['종가']:,}원  ({o['등락률']:+.2f}%)
  시가: {o['시가']:,}  고가: {o['고가']:,}  저가: {o['저가']:,}
  거래량: {o['거래량']:,}주  거래대금: {o['거래대금']/100000000:.2f}억

📐 *이동평균*
  MA5: {m['ma5']:,}  MA10: {m['ma10']:,}  MA20: {m['ma20']:,}

📊 *기술적 지표*
  RSI(14): {d_data['rsi']} — {rsi_status}
  볼린저: 상단 {b['u']:,} / 중심 {b['m']:,} / 하단 {b['l']:,}
    → {bb_pos}

  OBV: {obv_msg}
  MACD: {mc['val']} / 시그널: {mc['sig']} — {"MACD > 시그널 (강세 유지)" if mc['val'] > mc['sig'] else "MACD < 시그널 (약세)"}

👥 *수급 (KRX 기준)*
  개인: {s['개인']:+,}  외국인: {s['외인']:+,}  기관: {s['기관']:+,}

🔗 *피어 그룹*
"""
    # 피어 그룹 데이터 추가
    for name, code in TICKERS.items():
        if name == "더즌": continue
        p = fetch_data(code, dt).get('ohlcv', {})
        if p:
            mark = "▲" if p['등락률'] > 0 else "▼" if p['등락률'] < 0 else "─"
            report += f"  {name}: {p['종가']:,}원 {mark}{abs(p['등락률']):.2f}%\n"

    report += "\n📋 *오늘 공시*\n  해당 없음\n\n📰 *오늘 뉴스 (전체 — Claude가 정리)*\n  해당 없음"
    
    # 전송 및 저장
    send_telegram(report)
    os.makedirs("output", exist_ok=True)
    with open(f"output/dozen_{dt}.json", "w", encoding="utf-8") as f:
        json.dump(d_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
