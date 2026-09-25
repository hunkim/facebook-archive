# facebook-archive

Sung Kim(@hunkims)의 Facebook 글과 사진 아카이브. 원본은 Facebook "내 정보 다운로드"(JSON)이고, 여기 있는 Markdown이 기준 사본이다.

## 구조

| 경로 | 내용 |
|---|---|
| [TIMELINE.md](TIMELINE.md) | 연도별 전체 목차 |
| `posts/YYYY/*.md` | 글 하나에 파일 하나 (front matter: date, images, links, places, tags) |
| `albums/*.md` | 앨범 |
| `media/YYYY/` | 이미지 (동영상은 제외) |
| `index.jsonl` | 글 하나에 한 줄. LLM 컨텍스트나 검색에 쓴다 |

## 업데이트 방법

1. Facebook → 계정 센터 → 내 정보 다운로드 (JSON, 미디어 화질 높음)
2. 받은 zip 파일(여러 개면 전부)을 이 폴더에 넣는다. `*.zip`과 `raw/`는 git에서 제외된다
3. 변환:

```bash
python3 scripts/fb_to_md.py --clean
```
