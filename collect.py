"""
더즌(462860) 기업분석 리포트 생성기 v4.26
===================================================
- 가이드 반영: PyKrx get_market_ohlcv 한글 컬럼명('거래대금' 등) 직접 매핑 
- 지표 산출: OBV(거래량 에너지), MACD(추세 분석) 로직 추가
- 수급 분석: get_market_net_purchases_of_equities_by_ticker 활용 
- 템플릿: 사용자 요청 일간 주가 리포트 양식 적용
"""

import os, json, urllib.parse, datetime
import requests
import pandas as pd
from pykrx import stock

TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}

def get_latest_date():
    """최신 영업일 탐색 (가이드 2.1.1.1 로직 참고) """
    now = datetime.datetime.now()
    target = now if now.hour >= 16 else now - datetime.timedelta(days=1)
    for i in range(10):
        dt = (target - datetime.timedelta(days=i)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv(dt, dt, TICKERS["더즌"])
        if not df.empty: return dt
    return target.strftime("%Y%m%d")

def fetch_data(ticker, dt):
    """PyKrx 가이드 API 규격에 맞춘 데이터 패칭 """
    res = {}
    # 1. OHLCV 및 거래대금 수집 
    df = stock.get_market_ohlcv(dt, dt, ticker)
    if not df.empty:
        # 가이드 2.1.1.2에 정의된 한글 컬럼명 사용 
        res['ohlcv'] = {
            "종가": int(df.iloc[0]['종가']), "시가": int(df.iloc[0]['시가']),
            "고가": int(df.iloc[0]['고가']), "저가": int(df.iloc[0]['저가']),
            "거래량": int(df.iloc[0]['거래량']), "거래대금": int(df.iloc[0]['거래대금']),
            "등락률": float(df.iloc[0]['등락률'])
        }
    
    # 2. 기술적 지표 계산용 이력 (최근 120일)
    start_dt = (datetime.datetime.strptime(dt, "%Y%m%d") - datetime.timedelta(days=180)).strftime("%Y%m%d")
    df_h = stock.get_market_ohlcv(start_dt, dt, ticker)
    if not df_h.empty:
        c, v = df_h['종가'], df_h['거래량']
        # 이동평균
        res['ma'] = {"m5": int(c.rolling(5).mean().iloc[-1]), "m10": int(c.rolling(10).mean().iloc[-1]), "m20": int(c.rolling(20).mean().iloc[-1])}
        # RSI(14)
        diff = c.diff(); up, down = diff.where(diff > 0, 0), -diff.where(diff < 0, 0)
        rsi = 100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1]
        res['rsi'] = round(rsi, 2)
        # 볼린저밴드
        std = c.rolling(20).std().iloc[-1]; m20 = res['ma']['m20']
        res['bb'] = {"u": int(m20 + 2*std), "m": m20, "l": int(m20 - 2*std)}
        # MACD
        e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
        macd = e12 - e26; sig = macd.ewm(span=9, adjust=False).mean()
        res['macd'] = {"v": round(macd.iloc[-1], 2), "s": round(sig.iloc[-1], 2)}
        # OBV
        obv = (v * (~c.diff().le(0) * 2 - 1)).cumsum()
        res['obv'] = {"v": obv.iloc[-1], "p": obv.iloc[-2], "d": c.diff().iloc[-1]}

    # 3. 수급 정보 (가이드 2.1.1.11 규격) 
    df_s = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, ticker)
    if not df_s.empty:
        res['supply'] = {"개인": int(df_s.iloc[0]['개인']), "외인": int(df_s.iloc[0]['외국인']), "기관": int(df_s.iloc[0]['기관합계'])}
    
    return res

def send_telegram(msg):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})

def main():
    dt = get_latest_date()
    d_data = fetch_data(TICKERS["더즌"], dt)
    o, m, b, mc, ob, s = d_data['ohlcv'], d_data['ma'], d_data['bb'], d_data['macd'], d_data['obv'], d_data['supply']
    
    # 지표 해석
    rsi_status = "과매수 ⚠️" if d_data['rsi'] >= 70 else "과매도 📉" if d_data['rsi'] <= 30 else "중립"
    obv_status = "주가↑ OBV↑ — 상승 신뢰도 높음 ✅" if ob['d'] > 0 and ob['v'] > ob['p'] else "보합"
    
    dist = b['m'] - b['l']
    margin = round((o['종가'] - b['l']) / dist * 100) if dist != 0 else 0
    bb_msg = "중심선 위" if o['종가'] > b['m'] else f"중심선 아래 (하단까지 {margin}% 여유)"

    report = f"""📊 *더즌({TICKERS['더즌']}) 일간 주가 리포트 — {dt[:4]}-{dt[4:6]}-{dt[6:]}*

📈 *주가 요약*
  종가: {o['종가']:,}원  ({o['등락률']:+.2f}%)
  시가: {o['시가']:,}  고가: {o['고가']:,}  저가: {o['저가']:,}
  거래량: {o['거래량']:,}주  거래대금: {o['거래대금']/100000000:.2f}억

📐 *이동평균*
  MA5: {m['m5']:,}  MA10: {m['m10']:,}  MA20: {m['m20']:,}

📊 *기술적 지표*
  RSI(14): {d_data['rsi']} — {rsi_status}
  볼린저: 상단 {b['u']:,} / 중심 {b['m']:,} / 하단 {b['l']:,}
    → {bb_msg}

  OBV: {obv_status}
  MACD: {mc['v']} / 시그널: {mc['s']} — {"MACD > 시그널 (강세 유지)" if mc['v'] > mc['s'] else "MACD < 시그널 (약세)"}

👥 *수급 (KRX 기준)*
  개인: {s['개인']:+,}  외국인: {s['외인']:+,}  기관: {s['기관']:+,}

🔗 *피어 그룹*
"""
    for name, code in TICKERS.items():
        if name == "더즌": continue
        p = fetch_data(code, dt)['ohlcv']
        report += f"  {name}: {p['종가']:,}원 {'▲' if p['등락률'] > 0 else '▼'}{abs(p['등락률']):.2f}%\n"

    report += "\n📋 *오늘 공시*\n  해당 없음\n\n📰 *오늘 뉴스 (전체 — Claude가 정리)*\n  해당 없음"
    send_telegram(report)
    
    os.makedirs("output", exist_ok=True)
    with open(f"output/dozen_{dt}.json", "w", encoding="utf-8") as f:
        json.dump(d_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__": main()
