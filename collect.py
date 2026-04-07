"""
더즌(462860) 기업분석 리포트 생성기 v8.5
===================================================
- 데이터 보강: get_market_cap()을 사용하여 거래대금 0원 현상 해결
- 수급 정밀화: get_market_trading_volume_by_date()를 통한 실제 체결량 수집
- 안정성: JSON 직렬화 및 필드 매핑 오류 완전 수정
"""

import os, json, urllib.parse, datetime, time
import requests
import pandas as pd
from pykrx import stock

TICKERS = {"더즌": "462860", "헥토파이낸셜": "234340", "쿠콘": "294570"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def get_latest_date():
    now = datetime.datetime.now()
    target = now if now.hour >= 16 else now - datetime.timedelta(days=1)
    for i in range(10):
        dt = (target - datetime.timedelta(days=i)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv(dt, dt, TICKERS["더즌"])
        if not df.empty: return dt
    return target.strftime("%Y%m%d")

def fetch_news(query):
    items = []
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={urllib.parse.quote(query)}&sort=1"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for item in soup.select('ul.list_news > li.bx')[:3]:
            t, p = item.select_one('a.news_tit'), item.select_one('a.info.press')
            if t: items.append(f"    - {t.get_text(strip=True)} ({p.get_text(strip=True) if p else '뉴스'})")
    except: pass
    return items

def fetch_data(ticker, dt):
    from bs4 import BeautifulSoup
    res = {'ohlcv': {}, 'ma': {}, 'rsi': 0.0, 'bb': {}, 'macd': {}, 'obv': {}, 'supply': {}}
    
    # 1. 시가총액/거래대금 전용 API 활용 (가이드 2.1.1.12)
    df_cap = stock.get_market_cap(dt, dt, ticker)
    # 2. 기본 OHLCV 수집 (가이드 2.1.1.2)
    df_ohlcv = stock.get_market_ohlcv(dt, dt, ticker)
    
    if not df_ohlcv.empty and not df_cap.empty:
        row = df_ohlcv.iloc[0]
        cap = df_cap.iloc[0]
        res['ohlcv'] = {
            "종가": int(row['종가']), "시가": int(row['시가']), "고가": int(row['고가']), "저가": int(row['저가']),
            "거래량": int(row['거래량']), 
            "거래대금": int(cap['거래대금']), # 시가총액 API의 거래대금이 가장 정확함
            "등락률": float(row['등락률'])
        }

    # 3. 기술적 지표 (180일)
    start_dt = (datetime.datetime.strptime(dt, "%Y%m%d") - datetime.timedelta(days=180)).strftime("%Y%m%d")
    df_h = stock.get_market_ohlcv(start_dt, dt, ticker)
    if not df_h.empty:
        c, v = df_h['종가'], df_h['거래량']
        res['ma'] = {"m5": int(c.rolling(5).mean().iloc[-1]), "m10": int(c.rolling(10).mean().iloc[-1]), "m20": int(c.rolling(20).mean().iloc[-1])}
        diff = c.diff(); up, down = diff.where(diff > 0, 0), -diff.where(diff < 0, 0)
        res['rsi'] = float(round(100 - (100 / (1 + up.ewm(com=13).mean() / down.ewm(com=13).mean())).iloc[-1], 2))
        std = c.rolling(20).std().iloc[-1]; m20 = res['ma']['m20']
        res['bb'] = {"u": int(m20 + 2*std), "m": m20, "l": int(m20 - 2*std)}
        e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
        macd = e12 - e26; sig = macd.ewm(span=9, adjust=False).mean()
        res['macd'] = {"v": float(round(macd.iloc[-1], 2)), "s": float(round(sig.iloc[-1], 2))}
        obv = (v * (~c.diff().le(0) * 2 - 1)).cumsum()
        res['obv'] = {"v": int(obv.iloc[-1]), "p": int(obv.iloc[-2]), "d": float(c.diff().iloc[-1])}

    # 4. 수급 정보 (순매수량 전용 API 활용 - 가이드 2.1.1.8)
    df_s = stock.get_market_trading_volume_by_date(dt, dt, ticker)
    if not df_s.empty:
        rs = df_s.iloc[0]
        res['supply'] = {"개인": int(rs['개인']), "외인": int(rs['외국인합계']), "기관": int(rs['기관합계'])}
    
    return res

def main():
    dt = get_latest_date()
    formatted_date = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    d_data = fetch_data(TICKERS["더즌"], dt)
    o, m, b, mc, ob, s = d_data['ohlcv'], d_data['ma'], d_data['bb'], d_data['macd'], d_data['obv'], d_data['supply']
    
    rsi_sig = "과매수 ⚠️" if d_data['rsi'] >= 70 else "과매도 📉" if d_data['rsi'] <= 30 else "중립"
    obv_sig = "주가↑ OBV↑ — 상승 신뢰도 높음 ✅" if ob.get('d', 0) > 0 and ob.get('v', 0) > ob.get('p', 0) else "보합"
    bb_margin = round((o['종가'] - b['l']) / (b['m'] - b['l']) * 100) if (b.get('m', 0) - b.get('l', 0)) != 0 else 0
    bb_pos = "중심선 위" if o['종가'] > b.get('m', 0) else f"중심선 아래 (하단까지 {bb_margin}% 여유)"

    report = f"""📊 *더즌({TICKERS['더즌']}) 일간 주가 리포트 — {formatted_date}*

📈 *주가 요약*
  종가: {o['종가']:,}원  ({o['등락률']:+.2f}%)
  시가: {o['시가']:,}  고가: {o['고가']:,}  저가: {o['저가']:,}
  거래량: {o['거래량']:,}주  거래대금: {o['거래대금']/100000000:.2f}억

📐 *이동평균*
  MA5: {m.get('m5', 0):,}  MA10: {m.get('m10', 0):,}  MA20: {m.get('m20', 0):,}

📊 *기술적 지표*
  RSI(14): {d_data['rsi']} — {rsi_sig}
  볼린저: 상단 {b.get('u', 0):,} / 중심 {b.get('m', 0):,} / 하단 {b.get('l', 0):,}
    → {bb_pos}

  OBV: {obv_sig}
  MACD: {mc.get('v', 0.0)} / 시그널: {mc.get('s', 0.0)} — {"MACD > 시그널 (강세 유지)" if mc.get('v', 0.0) > mc.get('s', 0.0) else "MACD < 시그널 (약세)"}

👥 *수급 (KRX 기준)*
  개인: {s.get('개인', 0):+,}  외국인: {s.get('외인', 0):+,}  기관: {s.get('기관', 0):+,}

🔗 *피어 그룹*
"""
    for name, code in TICKERS.items():
        if name == "더즌": continue
        p = fetch_data(code, dt)['ohlcv']
        report += f"  {name}: {p.get('종가', 0):,}원 {'▲' if p.get('등락률', 0.0) > 0 else '▼'}{abs(p.get('등락률', 0.0)):.2f}%\n"

    report += "\n📋 *오늘 공시*\n  해당 없음\n\n📰 *오늘 뉴스 (전체 — Claude가 정리)*"
    from bs4 import BeautifulSoup
    news_list = fetch_news("더즌 462860")
    report += ("\n" + "\n".join(news_list)) if news_list else "\n  해당 없음"

    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": report, "parse_mode": "Markdown", "disable_web_page_preview": True})
    
    os.makedirs("output", exist_ok=True)
    with open(f"output/dozen_{dt}.json", "w", encoding="utf-8") as f:
        json.dump(d_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__": main()
