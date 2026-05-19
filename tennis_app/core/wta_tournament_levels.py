from __future__ import annotations

import re
import unicodedata

# Based on the official WTA Tour calendar and tournament pages on wtatennis.com.
# The aliases intentionally include sponsorless city forms because live providers
# often expose shortened names instead of the official branded title.
_OFFICIAL_WTA_LEVEL_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("PM", "Qatar TotalEnergies Open", (
        "qatar totalenergies open", "doha",
    )),
    ("PM", "Dubai Duty Free Tennis Championships", (
        "dubai duty free tennis championships", "dubai",
    )),
    ("PM", "BNP Paribas Open", (
        "bnp paribas open", "indian wells",
    )),
    ("PM", "Miami Open", (
        "miami open", "miami",
    )),
    ("PM", "Mutua Madrid Open", (
        "mutua madrid open", "wta madrid", "madrid",
    )),
    ("PM", "Internazionali BNL d'Italia", (
        "internazionali bnl d italia", "italian open", "wta rome", "rome",
    )),
    ("PM", "National Bank Open presented by Rogers", (
        "national bank open presented by rogers",
        "national bank open",
        "canadian open",
        "toronto",
        "montreal",
    )),
    ("PM", "Cincinnati Open", (
        "cincinnati open", "western southern open", "cincinnati",
    )),
    ("PM", "China Open", (
        "china open", "beijing",
    )),
    ("PM", "Wuhan Open", (
        "wuhan open", "wuhan",
    )),
    ("P", "Brisbane International", (
        "brisbane international", "brisbane",
    )),
    ("P", "Adelaide International", (
        "adelaide international", "adelaide",
    )),
    ("P", "Mubadala Abu Dhabi Open", (
        "mubadala abu dhabi open", "abu dhabi open", "abu dhabi",
    )),
    ("P", "Upper Austria Ladies Linz", (
        "upper austria ladies linz", "linz open", "linz",
    )),
    ("P", "Merida Open Akron", (
        "merida open akron", "merida open", "merida",
    )),
    ("P", "Credit One Charleston Open", (
        "credit one charleston open", "charleston open", "charleston",
    )),
    ("P", "Porsche Tennis Grand Prix", (
        "porsche tennis grand prix", "stuttgart",
    )),
    ("P", "Internationaux de Strasbourg presented by Mammotion", (
        "internationaux de strasbourg presented by mammotion",
        "internationaux de strasbourg",
        "strasbourg",
    )),
    ("P", "The HSBC Championships", (
        "the hsbc championships", "queens", "queen s", "london",
    )),
    ("P", "Berlin Tennis Open", (
        "berlin tennis open", "vanda pharmaceuticals berlin tennis open", "berlin",
    )),
    ("P", "Bad Homburg Open", (
        "bad homburg open powered by solarwatt", "bad homburg open", "bad homburg",
    )),
    ("P", "Mubadala DC Open", (
        "mubadala dc open", "washington dc", "washington",
    )),
    ("P", "Abierto GNP Seguros", (
        "abierto gnp seguros", "monterrey",
    )),
    ("P", "Guadalajara Open presentado por Santander", (
        "guadalajara open presentado por santander", "guadalajara open", "guadalajara",
    )),
    ("P", "Singapore Tennis Open", (
        "singapore tennis open", "singapore",
    )),
    ("P", "Ningbo Open", (
        "ningbo open", "ningbo",
    )),
    ("P", "Toray Pan Pacific Open Tennis", (
        "toray pan pacific open tennis", "pan pacific open", "tokyo",
    )),
    ("W", "ASB Classic", (
        "asb classic", "auckland",
    )),
    ("W", "Hobart International", (
        "hobart international", "hobart",
    )),
    ("W", "Transylvania Open", (
        "transylvania open", "cluj napoca", "cluj",
    )),
    ("W", "ATX Open", (
        "atx open", "austin",
    )),
    ("W", "Copa Colsanitas", (
        "copa colsanitas", "bogota",
    )),
    ("W", "Open Capfinances Rouen Metropole", (
        "open capfinances rouen metropole", "rouen",
    )),
    ("W", "Grand Prix SAR La Princesse Lalla Meryem", (
        "grand prix son altesse royale la princesse lalla meryem",
        "princesse lalla meryem",
        "rabat",
    )),
    ("W", "Libema Open", (
        "libema open", "s hertogenbosch", "hertogenbosch",
    )),
    ("W", "Nottingham Open", (
        "lexus nottingham open", "nottingham open", "nottingham",
    )),
    ("W", "Eastbourne Open", (
        "lexus eastbourne open", "eastbourne open", "eastbourne",
    )),
    ("W", "Athens Open", (
        "athens open", "athens",
    )),
    ("W", "Iasi Open", (
        "unicredit iasi open", "iasi open", "iasi",
    )),
    ("W", "Prague Open", (
        "livesport prague open", "prague open", "prague",
    )),
    ("W", "Hamburg Ladies Open", (
        "msc hamburg ladies open", "hamburg ladies open", "hamburg",
    )),
    ("W", "The Memphis Classic", (
        "the memphis classic", "memphis",
    )),
    ("W", "SP Open", (
        "sp open", "sao paulo",
    )),
    ("W", "Korea Open", (
        "korea open", "seoul",
    )),
    ("W", "Kinoshita Group Japan Open", (
        "kinoshita group japan open", "osaka",
    )),
    ("W", "Guangzhou Open", (
        "guangzhou open", "guangzhou",
    )),
    ("W", "Chennai Open", (
        "chennai open", "chennai",
    )),
    ("W", "Hong Kong Tennis Open", (
        "prudential hong kong tennis open", "hong kong tennis open", "hong kong",
    )),
)

_HISTORICAL_WTA_LEVEL_RULES: tuple[tuple[frozenset[int], str, tuple[str, ...]], ...] = (
    (
        frozenset((2024, 2025)),
        "P",
        (
            "korea open",
            "seoul",
        ),
    ),
    (
        frozenset((2022,)),
        "PM",
        (
            "guadalajara",
            "guadalajara open",
            "guadalajara open akron",
            "guadalajara open presentado por santander",
        ),
    ),
    (
        frozenset(range(2009, 2021)),
        "PM",
        (
            "indian wells",
            "bnp paribas open",
            "miami",
            "miami open",
            "madrid",
            "wta madrid",
            "mutua madrid open",
            "beijing",
            "china open",
        ),
    ),
    (
        frozenset((2009, 2010, 2011, 2015, 2017, 2019)),
        "PM",
        (
            "dubai",
            "dubai tennis championships",
            "dubai duty free tennis championships",
        ),
    ),
    (
        frozenset((2012, 2013, 2014, 2016, 2018, 2020)),
        "PM",
        (
            "doha",
            "qatar open",
            "qatar totalenergies open",
        ),
    ),
    (
        frozenset(range(2009, 2021)),
        "PM",
        (
            "rome",
            "wta rome",
            "italian open",
            "internazionali bnl d italia",
            "internazionali bnl d'italia",
            "cincinnati",
            "cincinnati open",
            "western southern open",
            "canada",
            "canadian open",
            "national bank open",
            "national bank open presented by rogers",
            "toronto",
            "montreal",
        ),
    ),
    (
        frozenset(range(2009, 2014)),
        "PM",
        (
            "tokyo",
            "pan pacific open",
            "toray pan pacific open tennis",
        ),
    ),
    (
        frozenset(range(2014, 2021)),
        "PM",
        (
            "wuhan",
            "wuhan open",
        ),
    ),
    (
        frozenset((2012, 2013, 2018, 2020)),
        "P",
        (
            "dubai",
            "dubai tennis championships",
            "dubai duty free tennis championships",
        ),
    ),
    (
        frozenset((2019,)),
        "P",
        (
            "doha",
            "qatar open",
            "qatar totalenergies open",
        ),
    ),
)

_ITF_OR_LOWER_WTA_RE = re.compile(r"\bw(?:15|25|35|50|60|75|80|100|125)\b")
_CHALLENGER_SUFFIX_RE = re.compile(r"\bch\b")


def _normalize_name(name: str | None) -> str:
    if not isinstance(name, str):
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _coerce_year(year: str | int | None) -> int | None:
    if year in (None, ""):
        return None
    try:
        text = str(year).strip()
        if len(text) >= 4:
            return int(text[:4])
        return int(text)
    except (TypeError, ValueError):
        return None


def _is_non_main_wta_event(normalized_name: str) -> bool:
    if not normalized_name:
        return True
    if _ITF_OR_LOWER_WTA_RE.search(normalized_name):
        return True
    return (
        "challenger" in normalized_name
        or "wta 125" in normalized_name
        or _CHALLENGER_SUFFIX_RE.search(normalized_name) is not None
    )


def _infer_historical_wta_level(normalized_name: str, year: int | None) -> str | None:
    if year is None:
        return None
    for rule_years, level, aliases in _HISTORICAL_WTA_LEVEL_RULES:
        if year in rule_years and any(alias in normalized_name for alias in aliases):
            return level
    return None


def infer_wta_level_from_name(name: str | None, year: str | int | None = None) -> str | None:
    normalized = _normalize_name(name)
    if not normalized:
        return None
    if _is_non_main_wta_event(normalized):
        return None
    historical_level = _infer_historical_wta_level(normalized, _coerce_year(year))
    if historical_level:
        return historical_level
    for level, _official_name, aliases in _OFFICIAL_WTA_LEVEL_RULES:
        if any(alias in normalized for alias in aliases):
            return level
    return None


def official_wta_tournament_name(name: str | None) -> str | None:
    normalized = _normalize_name(name)
    if not normalized:
        return None
    if _ITF_OR_LOWER_WTA_RE.search(normalized):
        return None
    if (
        "challenger" in normalized
        or "wta 125" in normalized
        or _CHALLENGER_SUFFIX_RE.search(normalized) is not None
    ):
        return None
    for _level, official_name, aliases in _OFFICIAL_WTA_LEVEL_RULES:
        if any(alias in normalized for alias in aliases):
            return official_name
    return None