import re

_NON_ALNUM = re.compile(r'[^a-z0-9]+')
_PUNCTUATION_INSIDE_WORD = re.compile(r'(?<=\w)[.\'’](?=\w)')


def normalize_search_text(value: str) -> str:
    """Lowercases and strips punctuation so "S.R.K.R." and "SRKR" index and match identically."""
    collapsed = _PUNCTUATION_INSIDE_WORD.sub('', (value or '').lower())
    return _NON_ALNUM.sub(' ', collapsed).strip()
