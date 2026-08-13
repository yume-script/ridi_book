# 리디북스 도서검색 플러그인 (BookOasis)

리디북스(RIDI) 검색결과 페이지를 파싱해 메타데이터를 가져오는 BookOasis
플러그인입니다. kyobobook/nlk_book 플러그인 개발 과정에서 확인된 실제
계약(아래)을 그대로 따릅니다.

## 핵심 계약

- `search(db_type, query)`는 `{'success':..., 'items':...}`로 감싸지 않고
  **아이템 딕셔너리로 이루어진 평범한 `list`를 그대로 반환**해야 코어가
  화면에 결과를 표시합니다.
- 아이템 딕셔너리 키: `title` / `author` / `publisher` / `description` /
  `isbn` / `cover` / `link` / `source` (+ 선택적으로 `tags`).
- `apply()`가 실제로 쓰는 `books` 테이블 컬럼: `title`, `author`,
  `publisher`, `summary`, `link`, `release_date`, `isbn`, `cover_image`,
  `cover_updated_at`.

## 이 플러그인의 특징: HTML을 긁지 않고 `__NEXT_DATA__` JSON을 직접 파싱

리디북스 검색결과 페이지(`https://ridibooks.com/search?...`)는 Next.js
앱이며, 페이지 안에 `<script id="__NEXT_DATA__" type="application/json">`
로 그 페이지가 필요로 하는 데이터 전체가 JSON으로 그대로 박혀 있습니다.
kyobobook 플러그인처럼 CSS 클래스 기반 xpath로 화면 마크업을 긁는 대신,
이 JSON을 정규식으로 추출해 파싱합니다 — 사이트의 시각적 디자인(class
이름 등)이 바뀌어도 이 JSON 스키마 자체가 유지되는 한 계속 동작하므로
더 견고합니다.

JSON 안에서 실제로 읽는 경로:
```
props.pageProps.gridData.riGrid.grid.cells[]
  -> type == "SearchBookListWithTab"인 셀
  -> cell__SearchBookListWithTab.books[]
       -> book.title.main            (제목)
       -> book.authors[]             (이름 + role: AUTHOR/TRANSLATOR/ILLUSTRATOR 등)
       -> book.publicationInfo.name  (출판사)
       -> book.introduction.description (책소개 전문 — 검색 스니펫이 아니라 전체 소개)
       -> book.priceInfo.purchase    (정가/판매가 — 책소개 끝에 덧붙임)
       -> book.categories[]          (분류 — tags로 부가 제공)
       -> book.isAdultOnly           (성인 여부 — 설정에 따라 필터링)
       -> book.id                    (표지·링크 URL 구성에 사용)
```

## 표지 이미지

검색결과 JSON에는 일반 단행본(시리즈가 아닌 책)의 표지 URL이 직접
나오지 않지만, 시리즈가 있는 항목의 썸네일 URL 패턴
(`https://img.ridicdn.net/cover/<책ID>/large`)이 책 ID를 그대로 쓰는
것을 확인했습니다. 이 패턴을 이용해 **모든 책의 표지를 책 ID로부터
직접 구성**합니다: `https://img.ridicdn.net/cover/<책ID>/xxlarge`.

## 알려진 한계: ISBN·출간일 없음

리디북스 검색결과 JSON에는 **ISBN과 정확한 출간일(pubDate)이 포함되어
있지 않습니다.** 전자책/웹소설/웹툰이 주력인 플랫폼 특성상 서지 데이터가
종이책만큼 표준화되어 있지 않은 것으로 보입니다. 이 버전은 `isbn`/
`pubDate`를 빈 값으로 둡니다. 책 상세페이지(`https://ridibooks.com/books/
<ID>`)에도 같은 방식의 `__NEXT_DATA__` JSON이 있을 가능성이 높고, 거기에
ISBN/출간일이 있다면 상세페이지를 추가로 조회해(kyobobook처럼
`MAX_RESULTS`만큼만) 보완할 수 있습니다 — 상세페이지의 `__NEXT_DATA__`
JSON을 공유해 주시면 이 기능을 추가하겠습니다.

## 설정 항목

- **최대 검색결과 개수** (`MAX_RESULTS`, 기본 10)
- **검색 대상** (`SEARCH_TAB`, 기본 `BOOK`): `BOOK`(일반 도서) /
  `ALL`(전체) / `COMIC`(만화) / `WEBTOON`(웹툰) / `WEBNOVEL`(웹소설) /
  `LIGHT_NOVEL`(라이트노벨) 중 선택. 리디북스 검색창의 탭 필터와 동일한
  값입니다.
- **성인 콘텐츠 포함** (`INCLUDE_ADULT`, 기본 꺼짐): 꺼져 있으면
  `isAdultOnly: true`인 항목을 결과에서 제외하고, URL의
  `adult_exclude` 파라미터도 그에 맞게 보냅니다.
- **디버그 로그 남기기** (`ENABLE_LOGGING`, 기본 꺼짐): 켜면
  `plugins/metadata/ridi_book/ridi_book_debug.log`에 요청 URL/응답
  상태·길이/파싱 결과가 기록됩니다(500KB × 최대 3개 자동 순환). 핵심
  지점은 로그 설정과 무관하게 항상 `print()`로도 남아 `docker logs`로
  바로 확인할 수 있습니다.

## `apply()` 동작

`books` 테이블의 실제 컬럼을 `PRAGMA table_info(books)`(SQLite) 또는
`SHOW COLUMNS FROM books`(MariaDB)로 확인한 뒤, 존재하는 컬럼에만 값을
씁니다. 표지는 URL을 그대로 저장하지 않고 Pillow로 내려받아 webp로
변환한 후 `covers/<library_id>/book_<해시>.webp`에 저장하고 그 상대경로를
`cover_image`에 넣습니다. Pillow가 없으면 표지만 건너뛰고 나머지 필드는
정상 적용됩니다. ISBN이 빈 값이면 `isbn` 컬럼은 건드리지 않습니다
(기존 값 유지).

## 설치

1. `ridi_book/` 폴더를 BookOasis 서버의 `plugins/metadata/` 아래에 복사합니다.
2. 서버를 재시작해 `requirements.txt`(requests, Pillow)가 설치되는지 확인합니다.
3. 환경설정 > 플러그인 설정 > "리디북스 도서검색"에서 필요하면 검색
   대상/성인 콘텐츠 포함 여부를 조정하고 저장합니다.
4. `is_searchable = True`이므로 수동 메타데이터 검색 모달에 자동 노출됩니다.

## 자동 업데이트 설정

`ridi_book.py`의 `update_manifest['raw_base_url']`에 있는
`<org>/<repo>/<branch>`를 실제 배포할 GitHub 저장소 경로로 교체하세요.

## 파일 구성

```text
ridi_book/
  __init__.py         # Provider 클래스를 패키지 이름공간에 노출
  ridi_book.py         # search()/apply() 및 __NEXT_DATA__ 파싱 로직
  VERSION              # 자동 업데이트용 버전 파일
  requirements.txt     # requests, Pillow
  README.md            # 이 문서
```

## 참고 / 한계

- 이 컨테이너는 `ridibooks.com`으로 나가는 네트워크가 허용되어 있지
  않아 실제 사이트를 상대로 한 라이브 요청 테스트는 하지 못했습니다.
  대신 사용자가 실제로 제공한 검색결과 페이지의 `__NEXT_DATA__` JSON
  구조를 그대로 재현한 픽스처로 JSON 추출/도서 목록 파싱/저자 역할
  필터링(`AUTHOR`만 vs 전체)/성인 콘텐츠 필터링(포함·제외 양쪽)/
  `__NEXT_DATA__`를 못 찾는 경우의 안전한 빈 리스트 반환까지 전부
  모킹(mock) 테스트로 확인했습니다.
- ISBN/출간일 미제공에 대해서는 위 "알려진 한계" 절 참고 — 상세페이지
  구조를 공유해 주시면 보완 가능합니다.
- `SEARCH_TAB` 값은 리디북스 검색 페이지의 탭 필터 값(`BOOK`/`COMIC`/
  `WEBTOON`/`WEBNOVEL`/`LIGHT_NOVEL`)을 그대로 사용하며, 리디북스가
  이 값을 바꾸면 함께 갱신해야 합니다.
