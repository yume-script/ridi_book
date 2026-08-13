# -*- coding: utf-8 -*-
"""
BookOasis 플러그인 진입점.

가이드(guide_plugins.md) 3장의 폴더 기반 규격에 따라,
plugins/metadata/ridi_book/ 폴더가 로드될 때 ridi_book.py 안의
Provider 클래스를 이 패키지의 이름공간으로 노출한다.
"""

from .ridi_book import RidiBookMetadataProvider

__all__ = ['RidiBookMetadataProvider']
