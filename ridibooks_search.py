#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ridibooks_search.py
--------------------
https://ridibooks.com/comics/ebook (리디북스 만화) 에서 책을 검색하고,
검색 결과 리스트에서 사용자가 선택한 책의 메타데이터를 저장하는 스크립트.

사용법:
    python ridibooks_search.py                      # 실행 후 검색어를 입력받음
    python ridibooks_search.py "원피스"               # 검색어를 인자로 바로 전달
    python ridibooks_search.py "원피스" --tab ALL     # 만화(COMIC) 탭이 아닌 전체 검색
    python ridibooks_search.py "원피스" --format yaml # JSON 대신 YAML로 저장
    python ridibooks_search.py "원피스" --out ./meta  # 저장 폴더 지정 (기본: ./ridibooks_metadata)

필요 패키지:
    pip install requests beautifulsoup4
    (YAML로 저장하려면) pip install pyyaml

주의:
    - 리디북스는 공식 오픈 API를 제공하지 않으므로, 이 스크립트는 검색결과/상세페이지
      HTML을 파싱(스크래핑)합니다. 리디북스 페이지 구조가 바뀌면 파싱 로직(특히
      extract_search_results, extract_book_detail)을 함께 수정해야 할 수 있습니다.
    - 개인적/비상업적 용도로 적당한 요청 간격을 두고 사용하세요.
    - 성인 인증이 필요한 작품은 비로그인 상태에서 일부 정보가 제한될 수 있습니다.
"""

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://ridibooks.com"
SEARCH_URL = BASE_URL + "/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": BASE_URL + "/",
}

# tab 파라미터: COMIC(만화), WEBTOON(웹툰), WEBNOVEL(웹소설), BOOK(도서), LIGHT_NOVEL(라이트노벨)
DEFAULT_TAB = "COMIC"

# 403(WAF/봇 차단) 대응: curl_cffi가 설치되어 있으면 브라우저 TLS 핑거프린트를
# 흉내내는 curl_cffi를 우선 사용하고, 없으면 requests.Session으로 폴백한다.
# 설치: pip install curl_cffi
try:
    from curl_cffi import requests as _http  # type: ignore
    _USING_CURL_CFFI = True
except ImportError:
    import requests as _http  # type: ignore
    _USING_CURL_CFFI = False

_session = None


def _get_session():
    global _session
    if _session is not None:
        return _session

    if _USING_CURL_CFFI:
        _session = _http.Session(impersonate="chrome124")
    else:
        _session = _http.Session()
        _session.headers.update(HEADERS)
        # 홈페이지를 먼저 방문해 쿠키(클리어런스 등)를 확보한 뒤 실제 요청을 진행
        try:
            _session.get(BASE_URL + "/", timeout=15)
            time.sleep(0.5)
        except Exception:
            pass
    return _session


def fetch(url: str, params: dict | None = None) -> BeautifulSoup:
    """URL을 요청하고 BeautifulSoup 객체로 반환."""
    session = _get_session()
    if _USING_CURL_CFFI:
        resp = session.get(url, params=params, headers=HEADERS, timeout=15)
    else:
        resp = session.get(url, params=params, timeout=15)

    if resp.status_code == 403:
        raise RuntimeError(
            "403 Forbidden: 리디북스가 봇으로 판단해 요청을 차단했습니다.\n"
            + ("  curl_cffi를 이미 사용 중인데도 차단되었습니다. IP 평판(데이터센터/호스팅 IP) 문제일 수 있습니다.\n"
               if _USING_CURL_CFFI else
               "  'pip install curl_cffi' 로 설치 후 다시 실행하면 브라우저 TLS 핑거프린트를 흉내내어 우회될 수 있습니다.\n")
            + "  그래도 안 되면 요청 빈도를 낮추거나(초당 1회 이하), 다른 네트워크(가정용 회선 등)에서 시도해보세요."
        )
    resp.raise_for_status()
    if not _USING_CURL_CFFI:
        resp.encoding = resp.apparent_encoding or "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def search_books(query: str, tab: str = DEFAULT_TAB, page: int = 1, limit: int = 20, debug: bool = False):
    """
    검색어로 리디북스를 검색해 (book_id, title, author, publisher, url) 리스트를 반환.
    tab이 None/""/"ALL"이면 전체 카테고리에서 검색.
    """
    params = {"q": query, "page": page}
    if tab and tab.upper() != "ALL":
        params["tab"] = tab.upper()

    soup = fetch(SEARCH_URL, params=params)

    if debug:
        debug_path = Path("./ridibooks_debug_search.html")
        debug_path.write_text(str(soup), encoding="utf-8")
        print(f"[debug] 검색결과 HTML 저장: {debug_path.resolve()}")

    results = extract_search_results(soup, limit=limit)

    if not results and debug:
        # 검색결과 페이지에 /books/ 링크 자체가 있는지 확인 (파싱 문제 vs 진짜 0건 구분)
        raw_links = re.findall(r'/books/\d+', str(soup))
        print(f"[debug] HTML 내 /books/ 링크 개수(원시): {len(raw_links)}")

    return results


def extract_search_results(soup: BeautifulSoup, limit: int = 20):
    """
    검색결과 페이지 HTML에서 책 정보를 추출.
    책 상세 링크(/books/{id})를 기준으로 카드를 식별하고,
    같은 카드 안에서 제목/저자/출판사 텍스트를 최대한 근접 요소에서 찾는다.
    """
    results = {}
    order = []

    book_links = soup.select('a[href^="/books/"]')

    for a in book_links:
        href = a.get("href", "")
        m = re.match(r"^/books/(\d+)", href)
        if not m:
            continue
        book_id = m.group(1)

        text = a.get_text(strip=True)
        if book_id not in results:
            results[book_id] = {
                "book_id": book_id,
                "title": "",
                "author": "",
                "publisher": "",
                "url": urljoin(BASE_URL, f"/books/{book_id}"),
            }
            order.append(book_id)

        # 텍스트가 있는 링크(커버 이미지 링크는 보통 텍스트가 비어있음) 중
        # 가장 먼저 나오는 것을 제목으로 채택.
        # 주의: 검색결과 페이지에서는 책 소개 문구도 같은 /books/{id} 링크로
        # 감싸져 있는 경우가 있어, "가장 긴 텍스트"를 기준으로 삼으면
        # 소개 문구를 제목으로 잘못 채택하게 된다. 따라서 "처음 등장하는
        # 비어있지 않은 텍스트"를 제목으로 고정한다.
        if text and not results[book_id]["title"]:
            results[book_id]["title"] = text

    # 저자/출판사 정보 보강.
    # - 만화/웹툰: 저자가 /author/{id} 링크
    # - 일반 도서(BOOK): 저자가 /search?q={이름} 링크 (출판사와 같은 패턴이며, 출판사는
    #   /search?q=출판사:{이름} 형태로 구분됨)
    # 카드 컨테이너를 정확히 특정하기 어려우므로, 같은 <li>/<div> 조상 내에서 탐색한다.
    for book_id in order:
        anchor = soup.select_one(f'a[href^="/books/{book_id}"]')
        if not anchor:
            continue
        container = anchor.find_parent(["li", "div"])
        depth = 0
        while container is not None and depth < 4:
            all_links = container.find_all("a", href=True)

            authors = []
            publisher = ""
            for link in all_links:
                href = link.get("href", "")
                text = unescape(link.get_text(strip=True))
                if not text:
                    continue

                if href.startswith("/author/"):
                    authors.append(text)
                elif href.startswith("/search?q="):
                    is_publisher = "출판사" in href or "%EC%B6%9C%ED%8C%90%EC%82%AC" in href
                    if is_publisher and not publisher:
                        publisher = text
                    elif not is_publisher:
                        authors.append(text)

            if authors or publisher:
                if authors and not results[book_id]["author"]:
                    # 중복 제거하면서 순서 유지
                    results[book_id]["author"] = ", ".join(dict.fromkeys(authors))
                if publisher and not results[book_id]["publisher"]:
                    results[book_id]["publisher"] = publisher
                break
            container = container.find_parent(["li", "div"])
            depth += 1

    ordered_results = [results[bid] for bid in order if results[bid]["title"]]
    return ordered_results[:limit]


def extract_book_detail(soup: BeautifulSoup, url: str) -> dict:
    """책 상세 페이지에서 메타데이터를 추출."""

    def meta(name: str, attr: str = "name") -> str:
        tag = soup.find("meta", attrs={attr: name})
        return unescape(tag["content"].strip()) if tag and tag.get("content") else ""

    title = meta("og:title", "property") or meta("title")
    description = meta("og:description", "property") or meta("description")
    cover = meta("og:image", "property")
    isbn = meta("books:isbn")
    canonical_tag = soup.find("link", rel="canonical")
    canonical_url = canonical_tag["href"] if canonical_tag and canonical_tag.get("href") else url

    # 카테고리 (예: 컴퓨터/IT > 개발/프로그래밍)
    category_links = soup.select('a[href^="/category/"]')
    category = " > ".join(unescape(a.get_text(strip=True)) for a in category_links if a.get_text(strip=True))

    # 저자 (og:title 근처 "저자" 표시가 있는 링크들)
    author_links = soup.select('a[href^="/author/"]')
    authors = list(dict.fromkeys(unescape(a.get_text(strip=True)) for a in author_links if a.get_text(strip=True)))

    # 출판사 (검색 링크 중 "출판사:" 포함)
    publisher = ""
    for a in soup.select('a[href^="/search?q="]'):
        href = a.get("href", "")
        if "%EC%B6%9C%ED%8C%90%EC%82%AC" in href or "출판사:" in href:
            publisher = unescape(a.get_text(strip=True))
            break

    # 본문 텍스트 전체에서 파일 정보/ISBN/출간일 등 보조 추출
    page_text = soup.get_text("\n", strip=True)

    def find_after(label: str) -> str:
        m = re.search(re.escape(label) + r"\s*\n?\s*([^\n]+)", page_text)
        return m.group(1).strip() if m else ""

    isbn = isbn or find_after("ISBN")
    file_format = ""
    fmt_match = re.search(r"\b(EPUB|PDF)\b", page_text)
    if fmt_match:
        file_format = fmt_match.group(1)

    pages_match = re.search(r"(\d+)\s*쪽", page_text)
    pages = pages_match.group(1) if pages_match else ""

    ebook_date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*전자책\s*출간", page_text)
    ebook_release_date = ebook_date_match.group(1) if ebook_date_match else ""

    price_match = re.search(r"판매가\s*\n?\s*([\d,]+원)", page_text)
    price = price_match.group(1) if price_match else ""

    rating_match = re.search(r"books:rating:value\"\s*content=\"([\d.]+)\"", str(soup))
    rating = meta("books:rating:value") or (rating_match.group(1) if rating_match else "")

    return {
        "book_id": re.search(r"/books/(\d+)", canonical_url).group(1)
        if re.search(r"/books/(\d+)", canonical_url) else "",
        "title": title,
        "authors": authors,
        "publisher": publisher,
        "category": category,
        "isbn": isbn,
        "description": description,
        "cover_image": cover,
        "file_format": file_format,
        "pages": pages,
        "ebook_release_date": ebook_release_date,
        "price": price,
        "rating": rating,
        "source_url": canonical_url,
    }


def choose_book(candidates: list[dict]) -> dict:
    """검색 결과를 번호와 함께 출력하고 사용자 선택을 입력받음."""
    if not candidates:
        print("검색 결과가 없습니다.")
        sys.exit(1)

    print(f"\n검색 결과 {len(candidates)}건:\n")
    for i, book in enumerate(candidates, start=1):
        title = book["title"]
        if len(title) > 60:
            title = title[:57] + "..."
        author = book.get("author") or "-"
        publisher = book.get("publisher") or "-"
        print(f"[{i:2}] {title}")
        print(f"      저자: {author}  |  출판사: {publisher}")

    while True:
        choice = input("\n저장할 책 번호를 입력하세요 (0=취소): ").strip()
        if choice == "0":
            print("취소되었습니다.")
            sys.exit(0)
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        print("올바른 번호를 입력해주세요.")


def save_metadata(meta: dict, out_dir: Path, fmt: str = "json"):
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", meta.get("title") or meta.get("book_id", "unknown"))
    filename = f"{meta.get('book_id', 'unknown')}_{safe_title}"[:100]

    if fmt == "yaml":
        try:
            import yaml
        except ImportError:
            print("PyYAML이 설치되어 있지 않아 JSON으로 저장합니다. (pip install pyyaml)")
            fmt = "json"

    if fmt == "yaml":
        path = out_dir / f"{filename}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(meta, f, allow_unicode=True, sort_keys=False)
    else:
        path = out_dir / f"{filename}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    return path


def main():
    parser = argparse.ArgumentParser(description="리디북스 검색 및 메타데이터 저장 스크립트")
    parser.add_argument("query", nargs="?", help="검색어")
    parser.add_argument("--tab", default=DEFAULT_TAB,
                         help="검색 탭: COMIC(기본, 만화) / WEBTOON / WEBNOVEL / BOOK / LIGHT_NOVEL / ALL")
    parser.add_argument("--limit", type=int, default=20, help="검색 결과 최대 표시 개수 (기본 20)")
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="저장 형식 (기본 json)")
    parser.add_argument("--out", default="./ridibooks_metadata", help="저장 폴더 (기본 ./ridibooks_metadata)")
    parser.add_argument("--debug", action="store_true",
                         help="검색결과 HTML을 파일로 저장하고 진단 정보를 출력 (파싱 실패 원인 확인용)")
    args = parser.parse_args()

    query = args.query or input("검색어를 입력하세요: ").strip()
    if not query:
        print("검색어가 비어 있습니다.")
        sys.exit(1)

    print(f'"{query}" 검색 중... (tab={args.tab})')
    candidates = search_books(query, tab=args.tab, limit=args.limit, debug=args.debug)

    chosen = choose_book(candidates)
    print(f"\n'{chosen['title']}' 상세 정보를 가져오는 중...")

    detail_soup = fetch(chosen["url"])
    meta = extract_book_detail(detail_soup, chosen["url"])

    out_path = save_metadata(meta, Path(args.out), fmt=args.format)
    print(f"\n메타데이터를 저장했습니다: {out_path}")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
