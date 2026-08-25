"""Language code normalization to ISO 639-3 (matches FE.Library convention).

Anna's Archive sources use a mix of full English names (zlib: "german"),
ISO 639-1 ("de"), and ISO 639-3 ("deu"). We map everything to ISO 639-3 so
values are directly comparable with FE.Library's `language_utils`.
"""

from __future__ import annotations

# name / iso639-1 / iso639-2(b,t) -> ISO 639-3
_TO_ISO639_3: dict[str, str] = {
    # german
    "de": "deu",
    "ger": "deu",
    "deu": "deu",
    "german": "deu",
    "deutsch": "deu",
    # english
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    # french
    "fr": "fra",
    "fre": "fra",
    "fra": "fra",
    "french": "fra",
    "français": "fra",
    "franzosisch": "fra",
    # spanish
    "es": "spa",
    "spa": "spa",
    "spanish": "spa",
    "español": "spa",
    "spanisch": "spa",
    # italian
    "it": "ita",
    "ita": "ita",
    "italian": "ita",
    "italienisch": "ita",
    # dutch
    "nl": "nld",
    "dut": "nld",
    "nld": "nld",
    "dutch": "nld",
    "niederlandisch": "nld",
    # russian
    "ru": "rus",
    "rus": "rus",
    "russian": "rus",
    "russisch": "rus",
    # portuguese
    "pt": "por",
    "por": "por",
    "portuguese": "por",
    # polish
    "pl": "pol",
    "pol": "pol",
    "polish": "pol",
    "polnisch": "pol",
    # chinese
    "zh": "zho",
    "chi": "zho",
    "zho": "zho",
    "chinese": "zho",
    "mandarin": "zho",
    # japanese
    "ja": "jpn",
    "jpn": "jpn",
    "japanese": "jpn",
    # korean
    "ko": "kor",
    "kor": "kor",
    "korean": "kor",
    # arabic
    "ar": "ara",
    "ara": "ara",
    "arabic": "ara",
    # turkish
    "tr": "tur",
    "tur": "tur",
    "turkish": "tur",
    # swedish
    "sv": "swe",
    "swe": "swe",
    "swedish": "swe",
    # norwegian
    "no": "nor",
    "nor": "nor",
    "nob": "nob",
    "nno": "nno",
    "norwegian": "nor",
    # danish
    "da": "dan",
    "dan": "dan",
    "danish": "dan",
    # finnish
    "fi": "fin",
    "fin": "fin",
    "finnish": "fin",
    # czech
    "cs": "ces",
    "cze": "ces",
    "ces": "ces",
    "czech": "ces",
    # hungarian
    "hu": "hun",
    "hun": "hun",
    "hungarian": "hun",
    # romanian
    "ro": "ron",
    "rum": "ron",
    "ron": "ron",
    "romanian": "ron",
    # greek
    "el": "ell",
    "gre": "ell",
    "ell": "ell",
    "greek": "ell",
    # hebrew
    "he": "heb",
    "heb": "heb",
    "hebrew": "heb",
    # hindi
    "hi": "hin",
    "hin": "hin",
    "hindi": "hin",
    # indonesian
    "id": "ind",
    "ind": "ind",
    "indonesian": "ind",
    # ukrainian
    "uk": "ukr",
    "ukr": "ukr",
    "ukrainian": "ukr",
    # bulgarian
    "bg": "bul",
    "bul": "bul",
    "bulgarian": "bul",
    # croatian / serbian
    "hr": "hrv",
    "hrv": "hrv",
    "croatian": "hrv",
    "sr": "srp",
    "srp": "srp",
    "serbian": "srp",
    # slovak / slovenian
    "sk": "slk",
    "slo": "slk",
    "slk": "slk",
    "slovak": "slk",
    "sl": "slv",
    "slv": "slv",
    "slovenian": "slv",
    # vietnamese
    "vi": "vie",
    "vie": "vie",
    "vietnamese": "vie",
    # thai
    "th": "tha",
    "tha": "tha",
    "thai": "tha",
    # persian
    "fa": "fas",
    "fas": "fas",
    "per": "fas",
    "persian": "fas",
    "farsi": "fas",
    # latin
    "la": "lat",
    "lat": "lat",
    "latin": "lat",
    # multiple / unknown markers used by some sources
    "mul": "mul",
    "multiple": "mul",
}

# BCP-47 region subcodes like "en-us" resolve to the base language.
_REGION_PREFIX_RE = __import__("re").compile(r"^([a-z]{2,3})[-_]")


def language_to_iso639_3(value: str | list | tuple | None) -> str | None:
    """Map a language name or code to ISO 639-3; pass through unknown 3-letter codes.

    Returns None for empty/implausible input. Mirrors FE.Library behavior of
    accepting unmapped 3-letter alphabetic codes as-is. IA metadata sometimes
    sends lists (e.g. ["eng"]); the first usable entry wins.
    """
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if str(v).strip()), None)
    if not value:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    if key in _TO_ISO639_3:
        return _TO_ISO639_3[key]
    match = _REGION_PREFIX_RE.match(key)
    if match:
        base = match.group(1)
        if base in _TO_ISO639_3:
            return _TO_ISO639_3[base]
    if len(key) == 3 and key.isalpha():
        return key
    return None
