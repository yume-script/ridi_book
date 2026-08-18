# -*- coding: utf-8 -*-
"""
ridi_book.py
------------
BookOasis 메타데이터 플러그인 - 리디북스(ridibooks.com) 검색 연동.

BaseMetadataProvider 계약을 따른다:
    - search(db_type, query)  : 도서 후보 목록 검색 (필수)
    - apply(db_type, book_id, item_data) : 선택된 항목을 도서에 적용 (필수)
    - get_context_menu_items / run_context_menu_action : 도서 우클릭 메뉴에서
      리디북스 검색결과를 YAML로 만들어 도서 파일 옆에 사이드카로 저장하고,
      결과 내용을 그대로 응답 메시지로도 보여주는 보너스 액션 (선택)

리디북스는 공식 오픈API가 없어 검색결과/상세페이지 HTML을 파싱(스크래핑)한다.
403(WAF/봇 차단) 대응을 위해 curl_cffi가 설치되어 있으면 우선 사용하고,
없으면 requests.Session으로 폴백한다. (standalone 스크립트 ridibooks_search.py와
동일한 방식 - requirements.txt에 curl_cffi 추가를 권장)

주의:
    - 페이지 구조가 바뀌면 extract_search_results / extract_book_detail을
      함께 수정해야 할 수 있다.
    - metadata_locked=1인 도서는 apply()에서 덮어쓰지 않고 실패로 반환한다.
"""

import os
import re
import time
import urllib.parse
from html import unescape

from plugins.metadata.base import BaseMetadataProvider

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from curl_cffi import requests as _http  # 브라우저 TLS 핑거프린트 흉내 (403 우회)
    _USING_CURL_CFFI = True
except ImportError:
    import requests as _http
    _USING_CURL_CFFI = False

BASE_URL = "https://ridibooks.com"
SEARCH_URL = BASE_URL + "/search"

# 플러그인 자체 폴더 내 쓰기 가능한 위치 (도서 원본 경로는 read-only일 수 있어 여기 저장)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
YAML_EXPORT_DIR = os.path.join(PLUGIN_DIR, "yaml_exports")

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
    "Referer": BASE_URL + "/",
}


class RidiBookMetadataProvider(BaseMetadataProvider):
    """리디북스에서 도서 메타데이터를 검색/적용하는 플러그인."""

    id = "ridi_book"
    name = "리디북스"
    is_searchable = True
    config_schema = []

    def __init__(self):
        self._session = None

    # ------------------------------------------------------------------
    # 내부 HTTP 헬퍼
    # ------------------------------------------------------------------

    def _get_session(self):
        if self._session is not None:
            return self._session

        if _USING_CURL_CFFI:
            self._session = _http.Session(impersonate="chrome124")
        else:
            self._session = _http.Session()
            self._session.headers.update(HEADERS)
            try:
                self._session.get(BASE_URL + "/", timeout=15)
                time.sleep(0.3)
            except Exception:
                pass
        return self._session

    def _fetch(self, url, params=None):
        session = self._get_session()
        if _USING_CURL_CFFI:
            resp = session.get(url, params=params, headers=HEADERS, timeout=15)
        else:
            resp = session.get(url, params=params, timeout=15)

        if resp.status_code == 403:
            raise RuntimeError(
                "리디북스가 봇으로 판단해 요청을 차단했습니다(403). "
                + ("" if _USING_CURL_CFFI else "pip install curl_cffi 로 우회를 시도해보세요.")
            )
        resp.raise_for_status()
        if not _USING_CURL_CFFI:
            resp.encoding = resp.apparent_encoding or "utf-8"
        return BeautifulSoup(resp.text, "html.parser")

    # ------------------------------------------------------------------
    # 검색결과 파싱
    # ------------------------------------------------------------------

    def _extract_search_results(self, soup, limit=20):
        results = {}
        order = []

        for a in soup.select('a[href^="/books/"]'):
            href = a.get("href", "")
            m = re.match(r"^/books/(\d+)", href)
            if not m:
                continue
            book_id = m.group(1)
            text = a.get_text(strip=True)

            if book_id not in results:
                results[book_id] = {
                    "title": "",
                    "author": "",
                    "publisher": "",
                    "pubDate": "",
                    "cover": "",
                    "description": "",
                    "link": urllib.parse.urljoin(BASE_URL, f"/books/{book_id}"),
                }
                order.append(book_id)

            # 처음 등장하는 비어있지 않은 텍스트만 제목으로 채택
            # (책 소개 문구도 같은 링크로 감싸져 있어, 나중 텍스트를 덮어쓰면 안 됨)
            if text and not results[book_id]["title"]:
                results[book_id]["title"] = unescape(text)

        for book_id in order:
            anchor = soup.select_one(f'a[href^="/books/{book_id}"]')
            if not anchor:
                continue
            container = anchor.find_parent(["li", "div"])
            depth = 0
            while container is not None and depth < 4:
                all_links = container.find_all("a", href=True)
                authors, publisher = [], ""
                for link in all_links:
                    href = link.get("href", "")
                    text = unescape(link.get_text(strip=True))
                    if not text:
                        continue
                    if href.startswith("/author/"):
                        authors.append(text)
                    elif href.startswith("/search?q="):
                        is_pub = "출판사" in href or "%EC%B6%9C%ED%8C%90%EC%82%AC" in href
                        if is_pub and not publisher:
                            publisher = text
                        elif not is_pub:
                            authors.append(text)
                if authors or publisher:
                    if authors and not results[book_id]["author"]:
                        results[book_id]["author"] = ", ".join(dict.fromkeys(authors))
                    if publisher and not results[book_id]["publisher"]:
                        results[book_id]["publisher"] = publisher
                    break
                container = container.find_parent(["li", "div"])
                depth += 1

        ordered = [results[bid] for bid in order if results[bid]["title"]]
        return ordered[:limit]

    def _extract_book_detail(self, soup, url):
        def meta(name, attr="property"):
            tag = soup.find("meta", attrs={attr: name})
            return unescape(tag["content"].strip()) if tag and tag.get("content") else ""

        title = meta("og:title") or meta("title", "name")
        description = meta("og:description") or meta("description", "name")
        cover = meta("og:image")
        isbn = meta("books:isbn")

        canonical_tag = soup.find("link", rel="canonical")
        canonical_url = canonical_tag["href"] if canonical_tag and canonical_tag.get("href") else url

        category_links = soup.select('a[href^="/category/"]')
        category = " > ".join(unescape(a.get_text(strip=True)) for a in category_links if a.get_text(strip=True))

        author_links = soup.select('a[href^="/author/"]')
        authors = list(dict.fromkeys(unescape(a.get_text(strip=True)) for a in author_links if a.get_text(strip=True)))

        publisher = ""
        for a in soup.select('a[href^="/search?q="]'):
            href = a.get("href", "")
            if "%EC%B6%9C%ED%8C%90%EC%82%AC" in href or "출판사:" in href:
                publisher = unescape(a.get_text(strip=True))
                break

        page_text = soup.get_text("\n", strip=True)
        if not isbn:
            m_isbn = re.search(r"ISBN\s*\n?\s*(\d{10,13})", page_text)
            if m_isbn:
                isbn = m_isbn.group(1)

        ebook_date_match = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*전자책\s*출간", page_text)
        pub_date = ebook_date_match.group(1) if ebook_date_match else ""

        return {
            "title": title,
            "author": ", ".join(authors) if authors else "",
            "publisher": publisher,
            "pubDate": pub_date,
            "cover": cover,
            "description": description,
            "link": canonical_url,
            "isbn": isbn,
            "category": category,
        }

    # ------------------------------------------------------------------
    # BaseMetadataProvider 필수 계약
    # ------------------------------------------------------------------

    def search(self, db_type, query):
        print(f"[RidiBookMetadataProvider] search called db_type={db_type!r} query={query!r}")
        if not query or not query.strip():
            return []
        try:
            soup = self._fetch(SEARCH_URL, params={"q": query, "page": 1})
            results = self._extract_search_results(soup, limit=20)
            print(f"[RidiBookMetadataProvider] {len(results)}건 검색됨")
            return results
        except Exception:
            import traceback
            print(f"[RidiBookMetadataProvider] search 실패: {traceback.format_exc()}")
            return []

    def apply(self, db_type, book_id, item_data):
        print(f"[RidiBookMetadataProvider] apply called db_type={db_type!r} book_id={book_id!r}")
        link = (item_data or {}).get("link")
        if not link:
            return False, "적용할 항목에 상세페이지 링크(link)가 없습니다."

        try:
            gateway = self.get_db_gateway(db_type)

            row = gateway.fetch_one("SELECT metadata_locked FROM books WHERE id = ?", (book_id,))
            if row and row.get("metadata_locked"):
                return False, "메타데이터가 잠겨 있어(metadata_locked) 적용할 수 없습니다."

            detail_soup = self._fetch(link)
            detail = self._extract_book_detail(detail_soup, link)

            title = detail.get("title") or item_data.get("title") or ""
            author = detail.get("author") or item_data.get("author") or ""
            publisher = detail.get("publisher") or item_data.get("publisher") or ""
            isbn = detail.get("isbn") or ""
            cover = detail.get("cover") or item_data.get("cover") or ""
            summary = detail.get("description") or item_data.get("description") or ""
            release_date = detail.get("pubDate") or item_data.get("pubDate") or ""

            gateway.execute(
                """
                UPDATE books
                SET title = ?, author = ?, publisher = ?, isbn = ?,
                    cover_image = ?, summary = ?, release_date = ?,
                    link = ?, cover_updated_at = NOW()
                WHERE id = ?
                """,
                (title, author, publisher, isbn, cover, summary, release_date, link, book_id),
            )

            print(f"[RidiBookMetadataProvider] book_id={book_id} 메타데이터 적용 완료 (title={title!r})")
            return True, f"'{title}' 메타데이터를 적용했습니다."

        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(f"[RidiBookMetadataProvider] apply 실패: {tb}")
            return False, "메타데이터 적용 중 오류가 발생했습니다. 로그를 확인해주세요."

    # ------------------------------------------------------------------
    # 컨텍스트 메뉴 확장: 리디북스 검색 결과를 YAML로 만들어 저장/표시
    # ------------------------------------------------------------------

    def get_context_menu_items(self, db_type, context):
        print(f"[RidiBookMetadataProvider] get_context_menu_items db_type={db_type!r} context={context!r}")
        return [
            {
                "id": "export_ridi_yaml",
                "label": "리디북스 검색결과 YAML로 저장",
                "icon": "fa-solid fa-file-code",
            }
        ]

    def _build_search_query(self, db_type, context):
        book_id = (context or {}).get("book_id")
        title = (context or {}).get("book_title") or ""
        author = ""

        if book_id:
            try:
                gateway = self.get_db_gateway(db_type)
                row = gateway.fetch_one("SELECT title, author FROM books WHERE id = ?", (book_id,))
                if row:
                    title = row.get("title") or title
                    author = row.get("author") or ""
            except Exception:
                import traceback
                print(f"[RidiBookMetadataProvider] db lookup failed: {traceback.format_exc()}")

        return " ".join(p.strip() for p in [title, author] if p and str(p).strip()).strip()

    def _dump_yaml(self, data):
        """PyYAML이 있으면 그걸 쓰고, 없으면 최소한의 수동 YAML 직렬화로 폴백."""
        try:
            import yaml
            return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        except ImportError:
            return self._manual_yaml_dump(data)

    def _manual_yaml_dump(self, data, indent=0):
        """PyYAML 미설치 환경을 위한 아주 단순한 폴백 직렬화 (list[dict] 전용)."""
        lines = []
        pad = "  " * indent
        if isinstance(data, list):
            for item in data:
                sub = self._manual_yaml_dump(item, indent + 1).lstrip()
                lines.append(f"{pad}- {sub}")
        elif isinstance(data, dict):
            for i, (k, v) in enumerate(data.items()):
                v_str = "" if v is None else str(v).replace("\n", " ")
                prefix = "" if i > 0 else ""
                lines.append(f"{pad}{k}: \"{v_str}\"")
        else:
            lines.append(f"{pad}{data}")
        return "\n".join(lines)

    def run_context_menu_action(self, db_type, action_id, context):
        print(f"[RidiBookMetadataProvider] run_context_menu_action action_id={action_id!r} context={context!r}")
        if action_id != "export_ridi_yaml":
            return {"success": False, "error": f"지원하지 않는 액션입니다: {action_id}"}

        book_id = (context or {}).get("book_id")
        query = self._build_search_query(db_type, context)
        if not query:
            return {"success": False, "error": "검색할 도서 제목 정보가 없습니다."}

        try:
            results = self.search(db_type, query)
        except Exception:
            import traceback
            print(f"[RidiBookMetadataProvider] search 실패: {traceback.format_exc()}")
            return {"success": False, "error": "리디북스 검색 중 오류가 발생했습니다."}

        if not results:
            return {"success": False, "error": f"'{query}' 검색 결과가 없습니다."}

        yaml_payload = {
            "source": "ridibooks",
            "query": query,
            "book_id": book_id,
            "results": results,
        }
        yaml_text = self._dump_yaml(yaml_payload)

        saved_path = None
        try:
            os.makedirs(YAML_EXPORT_DIR, exist_ok=True)
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", query)[:80]
            filename = f"{book_id or 'unknown'}_{safe_title}.yaml"
            yaml_path = os.path.join(YAML_EXPORT_DIR, filename)
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(yaml_text)
            saved_path = yaml_path
            print(f"[RidiBookMetadataProvider] YAML 저장 완료: {yaml_path}")
        except Exception:
            import traceback
            print(f"[RidiBookMetadataProvider] YAML 파일 저장 실패 (내용은 반환됨): {traceback.format_exc()}")

        message = f"'{query}' 검색결과 {len(results)}건을 YAML로 만들었습니다."
        if saved_path:
            message += f" (저장 위치: {saved_path})"

        # 새 탭에 YAML 내용을 바로 보여주기 위해 data: URL로 인코딩.
        # (컨텍스트 메뉴 액션은 open_url 필드만 프론트엔드가 새 창으로 열어주는 것으로 확인됨 - naver_book 참고)
        data_url = "data:text/plain;charset=utf-8," + urllib.parse.quote(yaml_text)

        return {
            "success": True,
            "message": message,
            "yaml": yaml_text,
            "yaml_path": saved_path,
            "open_url": data_url,
        }
