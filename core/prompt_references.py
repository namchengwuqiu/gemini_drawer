"""从提示词正文识别明确的多张参考图要求。"""

import re
from functools import lru_cache
from unicodedata import normalize


_ZH_DIGITS = "一二三四五六七八九"
_ZH_NUMBER = rf"(?:[1-9][0-9]?|[{_ZH_DIGITS}]?十[{_ZH_DIGITS}]?|[{_ZH_DIGITS}两俩])"
_EN_NUMBERS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
_EN_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")
_EN_IMAGE = r"(?:images?|photos?|pictures?)"
_EN_REFERENCE = r"(?:reference|input|source|uploaded|provided)"

_INDEXED_IMAGES = re.compile(
    rf"(?<![构绘插竖横出])(?:图(?:片|像)?|照片)[ \t]*#?[ \t]*"
    rf"(?P<label>{_ZH_NUMBER}(?:[ \t]*[/~、][ \t]*{_ZH_NUMBER})*)"
    rf"(?![0-9{_ZH_DIGITS}十]|次元|维|视图|[ \t]*:[ \t]*[0-9])"
    rf"|第\s*(?:{_ZH_NUMBER}\s*(?:到|至|~|-)\s*第?\s*)?"
    rf"(?P<ordinal>{_ZH_NUMBER})\s*(?:张|幅)?\s*(?:参考|输入|上传|示例|原始)?(?:图(?:片|像)?|照片)"
    rf"|\b{_EN_IMAGE}[ \t_]*#?[ \t]*(?P<english>[1-9][0-9]?)\b"
    rf"(?![ \t]*:[ \t]*[0-9])"
    rf"|\b(?P<english_ordinal>{'|'.join(_EN_ORDINALS)})\s+(?:{_EN_REFERENCE}\s+)?{_EN_IMAGE}\b",
    re.IGNORECASE,
)

_COUNTED_IMAGES = re.compile(
    rf"(?P<qualified>{_ZH_NUMBER}|多|数)\s*[张幅]\s*(?:参考|输入|上传|提供的)\s*(?:图(?:片|像)?|照片)"
    rf"|(?:上传|提供|输入|参考|融合|合并|结合|基于)\s*(?:了|的|这)?\s*"
    rf"(?P<supplied>{_ZH_NUMBER}|多|数)\s*[张幅]\s*(?:图(?:片|像)?|照片)"
    rf"|(?P<as_reference>{_ZH_NUMBER}|多|数)\s*[张幅]\s*(?:图(?:片|像)?|照片)\s*(?:作为|用作|作|来做)(?:角色|外观|外貌)?参考"
    rf"|\b(?P<english>[1-9][0-9]?|{'|'.join(_EN_NUMBERS)}|multiple|several)\s+"
    rf"{_EN_REFERENCE}\s+(?:images|photos|pictures)\b",
    re.IGNORECASE,
)
_NUMBER_TOKEN = re.compile(_ZH_NUMBER)
_NUMBER_VALUES = {
    **{number: i for i, number in enumerate(_ZH_DIGITS, 1)},
    **{number: i for i, number in enumerate(_EN_NUMBERS, 1)},
    **{number: i for i, number in enumerate(_EN_ORDINALS, 1)},
    **dict.fromkeys(("两", "俩", "多", "数", "multiple", "several"), 2),
}


def _number_value(number: str) -> int:
    if number.isascii() and number.isdecimal():
        return int(number)
    if "十" in number:
        tens, ones = number.split("十")
        return 10 * _NUMBER_VALUES.get(tens, 1) + _NUMBER_VALUES.get(ones, 0)
    return _NUMBER_VALUES[number]


@lru_cache(maxsize=4096)
def minimum_reference_images(prompt: str) -> int:
    """返回正文能推断出的最少张数。没有多图依据时按单图绘图处理。"""
    text = normalize("NFKC", prompt).lower()
    minimum = 1
    for match in _INDEXED_IMAGES.finditer(text):
        if labels := match["label"]:
            for number in _NUMBER_TOKEN.findall(labels):
                minimum = max(minimum, _number_value(number))
        else:
            minimum = max(minimum, _number_value(match[match.lastgroup]))
    for match in _COUNTED_IMAGES.finditer(text):
        minimum = max(minimum, _number_value(match[match.lastgroup]))
    return minimum
