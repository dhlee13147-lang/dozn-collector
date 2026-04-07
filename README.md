[README.md](https://github.com/user-attachments/files/26523371/README.md)
# 더즌 주가 데이터 수집기

매일 오후 4시 KST에 자동으로 실행되어 텔레그램으로 주가 데이터를 전송합니다.

## 구성

```
dozen-collector/
├── collect.py              # 메인 수집 스크립트
├── requirements.txt        # 패키지 목록
└── .github/workflows/
    └── daily_collect.yml   # GitHub Actions 자동화
```

## 데이터 소스

| 데이터 | 소스 | 비고 |
|--------|------|------|
| 시가·고가·저가·종가·거래량·거래대금 | KRX (pykrx) | 공식 거래소 데이터 |
| 수급 (개인·외국인·기관) | KRX (pykrx) | |
| 이동평균 (MA5·MA10·MA20) | KRX 데이터로 직접 계산 | |
| PER·PBR·시가총액 | KRX (pykrx) | |
| 공시 (더즌·헥토·쿠콘) | DART OpenAPI | 무료 API 키 필요 |
| 뉴스 (더즌·헥토·쿠콘) | 네이버 뉴스 | |

## GitHub 설정

### 1. 리포지토리 Secrets 등록
`Settings > Secrets and variables > Actions`에서 아래 3개 추가:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 (`@BotFather`에서 발급) |
| `TELEGRAM_CHAT_ID` | 수신할 채팅 ID (`@userinfobot`에서 확인) |
| `DART_API_KEY` | DART OpenAPI 키 ([발급 링크](https://opendart.fss.or.kr/uat/uia/egovLoginUsr.do)) |

### 2. 자동 실행 확인
- 평일 16:00 KST (07:00 UTC)에 자동 실행
- `Actions` 탭에서 수동 실행도 가능

## 로컬 테스트

```bash
# 패키지 설치
pip install -r requirements.txt

# 환경변수 설정 (테스트용 — 실제 전송 안 됨)
export DART_API_KEY="your_key"

# 실행 (텔레그램 토큰 없으면 콘솔에 출력)
python collect.py
```

## Claude 스킬 연동

1. 텔레그램에서 JSON 파일 수신
2. 파일 내용 전체 복사
3. Claude 채팅에 붙여넣기
4. `"일간 리포트 노션에 올려줘"` 입력
5. 자동으로 노션 페이지 생성

## DART corp_code 확인 방법

```
https://opendart.fss.or.kr/api/company.json?crtfc_key=YOUR_KEY&corp_code=XXXXX
```
또는 DART 홈페이지 → 기업 검색 → URL에서 corp_code 확인
