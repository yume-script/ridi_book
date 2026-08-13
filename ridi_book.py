# -*- coding: utf-8 -*-
"""
BookOasis metadata plugin: 리디북스 도서검색 (RIDI Books)

리디북스 검색결과 페이지(https://ridibooks.com/search?...)는 Next.js
앱이며, 페이지에 `<script id="__NEXT_DATA__" type="application/json">`로
그 페이지가 필요로 하는 데이터가 통째로 JSON으로 박혀 있습니다. HTML을
CSS 클래스로 긁는 대신 이 JSON을 직접 파싱하므로, 사이트의 시각적
마크업(class 이름 등)이 바뀌어도 이 JSON 스키마 자체가 유지되는 한
plugin이 계속 동작합니다.

핵심 계약(kyobobook/nlk_book 플러그인 개발 과정에서 확인된 실제 동작):
  - search(db_type, query)는 {'success':..., 'items':...} 로 감싸지 않고
    아이템 딕셔너리로 이루어진 "평범한 list"를 그대로 반환해야 한다.
  - 아이템 딕셔너리 키: title / author / publisher / description / isbn /
    cover / link / source / pubDate.
  - apply()가 실제로 쓰는 books 테이블 컬럼: title, author, publisher,
    summary, link, release_date, isbn, cover_image, cover_updated_at.

== 알려진 한계 ==
리디북스 검색결과 JSON에는 ISBN과 출간일(pubDate)이 포함되어 있지
않습니다(전자책/웹소설/웹툰 위주 플랫폼이라 서지 데이터가 종이책만큼
표준화되어 있지 않은 것으로 보입니다). 이 버전은 검색결과 JSON에서
얻을 수 있는 정보(제목/저자(역할 포함)/출판사/전체 책소개/평점/가격/
표지/링크)만으로 구성하며, isbn/pubDate는 빈 값으로 둡니다. 표지는
책 ID로 CDN URL을 직접 구성합니다: https://img.ridicdn.net/cover/<ID>/large
(검색결과의 시리즈 썸네일 URL 패턴에서 확인).
"""

import hashlib
import json
import logging
import os
import re
import traceback
from io import BytesIO
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, urljoin

import requests

from plugins.metadata.base import BaseMetadataProvider

SEARCH_BASE_URL = "https://ridibooks.com/search"
COVER_URL_TEMPLATE = "https://img.ridicdn.net/cover/{book_id}/{size}"
BOOK_LINK_TEMPLATE = "https://ridibooks.com/books/{book_id}"

REQUEST_TIMEOUT = 15
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}

DEFAULTS = {
    "MAX_RESULTS": 10,
    "SEARCH_TAB": "BOOK",
    "INCLUDE_ADULT": False,
    "ENABLE_LOGGING": False,
}

PRIMARY_AUTHOR_ROLES = ("AUTHOR",)

# ----------------------------------------------------------------------
# 디버그 로깅 (kyobobook/nlk_book 플러그인과 동일한 관례)
# ----------------------------------------------------------------------
_LOG_FILE_NAME = 'ridi_book_debug.log'
_logger = logging.getLogger('bookoasis.ridi_book')
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

if not _logger.handlers:
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _LOG_FILE_NAME)
        _handler = RotatingFileHandler(
            log_path, maxBytes=512 * 1024, backupCount=3, encoding='utf-8')
        _handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        _logger.addHandler(_handler)
    except Exception:
        _fallback = logging.StreamHandler()
        _fallback.setFormatter(logging.Formatter('[ridi_book] %(levelname)s %(message)s'))
        _logger.addHandler(_fallback)


# ----------------------------------------------------------------------
# __NEXT_DATA__ 추출 및 book -> item 변환
# ----------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL)


def _extract_next_data(html):
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _extract_books(next_data):
    """__NEXT_DATA__에서 SearchBookListWithTab 셀의 books 배열을 찾는다."""
    try:
        cells = (next_data["props"]["pageProps"]["gridData"]
                  ["riGrid"]["grid"]["cells"])
    except (KeyError, TypeError):
        return []

    for cell in cells:
        if cell.get("type") == "SearchBookListWithTab":
            payload = cell.get("cell__SearchBookListWithTab") or {}
            return payload.get("books") or []
    return []


def _book_to_item(entry, get_all_authors):
    book = entry.get("book") or {}
    book_id = str(book.get("id") or entry.get("id") or "").strip()
    if not book_id:
        return None

    title = ((book.get("title") or {}).get("main") or "").strip()
    if not title:
        return None

    authors = book.get("authors") or []
    if get_all_authors:
        names = [a.get("name", "").strip() for a in authors if a.get("name")]
    else:
        names = [a.get("name", "").strip() for a in authors
                  if a.get("name") and (a.get("role") in PRIMARY_AUTHOR_ROLES)]
        if not names and authors:
            # 전부 번역가/삽화가 등 부차 역할뿐이면 첫 번째 인물로 대체
            first_name = authors[0].get("name", "").strip()
            if first_name:
                names = [first_name]

    publisher = ((book.get("publicationInfo") or {}).get("name") or "").strip()
    description = ((book.get("introduction") or {}).get("description") or "").strip()

    price_info = ((book.get("priceInfo") or {}).get("purchase") or {})
    selling_price = price_info.get("sellingPrice")
    full_price = price_info.get("fullPrice")
    if selling_price is not None:
        price_line = f"정가 {full_price}원" if full_price and full_price != selling_price else ""
        price_text = f"판매가 {selling_price}원" + (f" ({price_line})" if price_line else "")
        description = (description + "\n\n[가격] " + price_text) if description else "[가격] " + price_text

    categories = book.get("categories") or []
    tags = [c.get("name", "").strip() for c in categories if c.get("name")]

    cover_url = COVER_URL_TEMPLATE.format(book_id=book_id, size="xxlarge")
    link = BOOK_LINK_TEMPLATE.format(book_id=book_id)

    item = {
        "title": title,
        "author": ", ".join(names),
        "publisher": publisher,
        "description": description,
        "isbn": "",  # 리디북스 검색결과에는 ISBN이 노출되지 않음
        "cover": cover_url,
        "link": link,
        "source": "리디북스",
    }
    if tags:
        item["tags"] = tags
    return item


class RidiBookMetadataProvider(BaseMetadataProvider):
    """BookOasis 리디북스(RIDI) 도서검색 플러그인"""

    id = "ridi_book"
    name = "리디북스 도서검색"
    is_searchable = True

    config_schema = [
        {
            "key": "MAX_RESULTS",
            "label": "최대 검색결과 개수",
            "type": "number",
            "default": DEFAULTS["MAX_RESULTS"],
        },
        {
            "key": "SEARCH_TAB",
            "label": "검색 대상 (BOOK=도서, ALL=전체, COMIC=만화, "
                     "WEBTOON=웹툰, WEBNOVEL=웹소설, LIGHT_NOVEL=라이트노벨)",
            "type": "text",
            "default": DEFAULTS["SEARCH_TAB"],
        },
        {
            "key": "INCLUDE_ADULT",
            "label": "성인 콘텐츠 포함",
            "type": "checkbox",
            "default": DEFAULTS["INCLUDE_ADULT"],
        },
        {
            "key": "ENABLE_LOGGING",
            "label": "디버그 로그 남기기 (plugins/metadata/ridi_book/ridi_book_debug.log)",
            "type": "checkbox",
            "default": DEFAULTS["ENABLE_LOGGING"],
        },
    ]

    # 자동 업데이트 계약: raw_base_url의 <org>/<repo>/<branch>는
    # 실제 배포할 GitHub 저장소 경로로 교체해서 사용하세요.
    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": (
            "https://raw.githubusercontent.com/<org>/<repo>/<branch>"
            "/plugins/metadata/ridi_book"
        ),
        "files": ["ridi_book.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # ------------------------------------------------------------------
    # 디버그 로깅 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _logging_enabled(cfg):
        return bool(cfg.get('ENABLE_LOGGING', DEFAULTS['ENABLE_LOGGING']))

    @classmethod
    def _log(cls, cfg, level, msg, *args):
        if not cls._logging_enabled(cfg):
            return
        _logger.log(level, msg, *args)

    @classmethod
    def _log_exception(cls, cfg, msg, *args):
        if not cls._logging_enabled(cfg):
            return
        _logger.error(msg, *args)
        _logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # 공통 계약: search / apply
    # ------------------------------------------------------------------

    def search(self, db_type, query):
        print(f"[RidiBookMetadataProvider] search called db_type={db_type!r} query={query!r}")
        cfg = self.get_plugin_config(db_type, default={})
        self._log(cfg, logging.INFO, '=== search() 시작: query=%r ===', query)

        q = str(query or '').strip()
        if not q:
            print("[RidiBookMetadataProvider] empty query, returning []")
            return []

        max_results = self._as_int(cfg.get('MAX_RESULTS'), DEFAULTS['MAX_RESULTS'])
        max_results = max(1, min(30, max_results))
        get_all_authors = bool(cfg.get('GET_ALL_AUTHORS', False))
        include_adult = bool(cfg.get('INCLUDE_ADULT', DEFAULTS['INCLUDE_ADULT']))
        search_tab = (cfg.get('SEARCH_TAB') or DEFAULTS['SEARCH_TAB']).strip().upper()

        try:
            html = self._fetch_search_page(q, search_tab, include_adult, cfg)
        except Exception as e:
            print(f"[RidiBookMetadataProvider] search page fetch FAILED: {e!r}")
            print(traceback.format_exc())
            self._log_exception(cfg, '검색 요청 중 예외 발생: query=%r', q)
            return []

        if not html:
            print("[RidiBookMetadataProvider] search page response was empty")
            self._log(cfg, logging.WARNING, '검색 응답이 비어 있습니다.')
            return []

        next_data = _extract_next_data(html)
        if next_data is None:
            print("[RidiBookMetadataProvider] __NEXT_DATA__ 추출/파싱 실패 — 페이지 구조가 바뀌었을 수 있음")
            self._log(cfg, logging.WARNING,
                       '__NEXT_DATA__를 찾지 못했거나 JSON 파싱에 실패했습니다.')
            return []

        books = _extract_books(next_data)
        print(f"[RidiBookMetadataProvider] parsed {len(books)} book(s) from __NEXT_DATA__")
        self._log(cfg, logging.INFO, '__NEXT_DATA__에서 책 %d건 발견', len(books))

        items = []
        for entry in books:
            if len(items) >= max_results:
                break
            book = entry.get("book") or {}
            if not include_adult and book.get("isAdultOnly"):
                continue
            item = _book_to_item(entry, get_all_authors)
            if item:
                items.append(item)

        print(f"[RidiBookMetadataProvider] search returning {len(items)} item(s)")
        self._log(cfg, logging.INFO, '=== search() 종료: %d건 반환 ===', len(items))
        return items

    def _fetch_search_page(self, query, search_tab, include_adult, cfg):
        params = {
            "q": query,
            "adult_exclude": "n" if include_adult else "y",
            "page": "1",
        }
        if search_tab and search_tab != "ALL":
            params["tab"] = search_tab

        query_string = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        url = f"{SEARCH_BASE_URL}?{query_string}"
        print(f"[RidiBookMetadataProvider] fetching {url}")
        self._log(cfg, logging.DEBUG, '검색 요청 URL: %s', url)

        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        print(f"[RidiBookMetadataProvider] response status={resp.status_code} "
              f"content-type={resp.headers.get('Content-Type')}")
        self._log(cfg, logging.DEBUG, '응답 status=%s Content-Type=%s',
                   resp.status_code, resp.headers.get('Content-Type'))
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        raw = resp.text
        print(f"[RidiBookMetadataProvider] response body length: {len(raw)} chars")
        self._log(cfg, logging.DEBUG, '응답 본문 길이: %d자', len(raw))
        return raw

    def apply(self, db_type, book_id, item_data):
        print(f"[RidiBookMetadataProvider] apply called db_type={db_type!r} book_id={book_id!r}")
        cfg = self.get_plugin_config(db_type, default={})
        self._log(cfg, logging.INFO, '=== apply() 시작: book_id=%s ===', book_id)
        if self._logging_enabled(cfg):
            self._log(cfg, logging.DEBUG, 'item_data: %r', item_data)

        if not item_data:
            self._log(cfg, logging.WARNING, 'item_data가 비어 있어 적용을 건너뜁니다.')
            return False, '적용할 메타데이터가 없습니다.'

        try:
            gateway = self.get_db_gateway(db_type)
        except Exception as e:
            print(f"[RidiBookMetadataProvider] get_db_gateway FAILED: {e!r}")
            self._log_exception(cfg, 'get_db_gateway 실패: book_id=%s', book_id)
            return False, 'DB 연결 실패: %s' % e

        def _try_pragma():
            info = gateway.fetch_all("PRAGMA table_info(books)")
            return [row['name'].lower() for row in info] if info else []

        def _try_show_columns():
            info = gateway.fetch_all("SHOW COLUMNS FROM books")
            return [row['Field'].lower() for row in info] if info else []

        columns = []
        for attempt in (_try_pragma, _try_show_columns):
            try:
                columns = attempt()
                if columns:
                    break
            except Exception as e:
                print(f"[RidiBookMetadataProvider] column introspection attempt failed: {e!r}")
                continue
        self._log(cfg, logging.DEBUG, 'books 테이블 컬럼: %r', columns)

        cover_rel_path = None
        cover_url = item_data.get('cover')
        if cover_url and 'cover_image' in columns:
            cover_rel_path = self._download_cover(gateway, book_id, cover_url, cfg)

        set_parts = []
        params = []

        def _add(col, value):
            if col in columns or not columns:
                set_parts.append('%s = ?' % col)
                params.append(value)

        if item_data.get('title'):
            _add('title', item_data['title'])
        if item_data.get('author'):
            _add('author', item_data['author'])
        if item_data.get('publisher'):
            _add('publisher', item_data['publisher'])
        if item_data.get('description') and 'summary' in columns:
            _add('summary', item_data['description'])
        if item_data.get('link') and 'link' in columns:
            _add('link', item_data['link'])
        if item_data.get('pubDate') and 'release_date' in columns:
            _add('release_date', item_data['pubDate'])
        if item_data.get('isbn') and 'isbn' in columns:
            _add('isbn', item_data['isbn'])
        if cover_rel_path:
            _add('cover_image', cover_rel_path)
            if 'cover_updated_at' in columns:
                set_parts.append('cover_updated_at = CURRENT_TIMESTAMP')

        if not set_parts:
            self._log(cfg, logging.WARNING, '적용 가능한 필드가 없습니다. item_data=%r', item_data)
            return False, '적용할 메타데이터가 없습니다.'

        try:
            sql = 'UPDATE books SET %s WHERE id = ?' % ', '.join(set_parts)
            self._log(cfg, logging.DEBUG, 'SQL: %s / params=%r', sql, params + [book_id])
            gateway.execute(sql, params + [book_id])
            print(f"[RidiBookMetadataProvider] apply() DB update OK for book_id={book_id!r}")
            self._log(cfg, logging.INFO, 'apply() 성공: book_id=%s', book_id)
            return True, '[리디북스] 정보가 성공적으로 적용되었습니다.'
        except Exception as e:
            print(f"[RidiBookMetadataProvider] apply() DB update FAILED: {e!r}")
            self._log_exception(cfg, 'apply() 중 DB 반영 실패: book_id=%s', book_id)
            return False, '메타데이터 적용 실패: %s' % e

    def _download_cover(self, gateway, book_id, cover_url, cfg):
        """표지 이미지를 내려받아 covers/<library_id>/ 아래에 webp로 저장하고,
        DB의 cover_image 컬럼에 넣을 상대경로를 반환한다. Pillow가 없거나
        실패하면 None을 반환한다(적용 자체는 계속 진행됨)."""
        try:
            from PIL import Image
        except ImportError:
            self._log(cfg, logging.WARNING,
                       'Pillow가 설치되어 있지 않아 표지 다운로드를 건너뜁니다. '
                       'requirements.txt에 Pillow를 추가하고 플러그인을 재설치하세요.')
            return None

        try:
            book = gateway.fetch_one(
                'SELECT file_path, library_id FROM books WHERE id = ?', (book_id,))
            if not book:
                return None
            file_path = book['file_path'] if 'file_path' in book.keys() else book.get('file_path')
            library_id = book['library_id'] if 'library_id' in book.keys() else book.get('library_id')

            base_dir = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
            covers_dir = os.path.join(base_dir, 'covers', str(library_id))
            os.makedirs(covers_dir, exist_ok=True)

            name_for_hash = os.path.basename(file_path) if file_path else str(book_id)
            book_hash = hashlib.md5(name_for_hash.encode('utf-8')).hexdigest()
            cover_filename = 'book_%s.webp' % book_hash
            dest_path = os.path.join(covers_dir, cover_filename)

            resp = requests.get(cover_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            with Image.open(BytesIO(resp.content)) as img:
                img.save(dest_path, 'WEBP', quality=95)

            self._log(cfg, logging.DEBUG, '표지 저장 완료: %s', dest_path)
            return '%s/%s' % (library_id, cover_filename)
        except Exception:
            self._log_exception(cfg, '표지 다운로드/저장 실패: book_id=%s url=%s', book_id, cover_url)
            return None

    @staticmethod
    def _as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
