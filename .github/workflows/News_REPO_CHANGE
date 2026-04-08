# news-release 저장소 수정 사항

## 1. save_sent_article 함수에 날짜 컬럼 추가

기존 코드 (`news_release.py`):
```python
def save_sent_article(url, title):
    with open(csv_file, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([url, title])
```

변경 후:
```python
from datetime import datetime  # 상단에 추가

def save_sent_article(url, title):
    today = datetime.now().strftime("%Y-%m-%d")
    with open(csv_file, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([url, title, today])
```

## 2. 변경 후 CSV 형식
```
url, title, date
https://..., "기사 제목", 2026-04-08
```

## 3. dozen-collector GitHub Secrets 추가
```
NEWS_REPO = "본인계정/news-release저장소명"
```
예: NEWS_REPO = "lee/news-release"

## 4. 동작 방식
- collect.py가 매일 18:00 KST 실행
- sent_news.csv에서 오늘 날짜(date 컬럼)인 행만 필터링
- 더즌/dozn/헥토파이낸셜/쿠콘 키워드 분류
- 나머지는 "기타"로 Claude 스킬에 전달 → 스킬이 관련성 판단
