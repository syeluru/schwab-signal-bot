from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetCompatibility:
    underlying: str
    expected_family: str
    trigger_symbol: str


# Root-symbol family mapping for signals.
# Extend this as you add supported option underlyings.
UNDERLYING_TO_FUTURES_ROOTS: dict[str, set[str]] = {
    # US equity index ETFs / indexes
    'SPY': {'/ES'},
    'SPX': {'/ES'},
    '$SPX': {'/ES'},
    'QQQ': {'/NQ'},
    'NDX': {'/NQ'},
    '$NDX': {'/NQ'},
    'IWM': {'/RTY'},
    'RUT': {'/RTY'},
    '$RUT': {'/RTY'},
    'DIA': {'/YM'},
    'DJX': {'/YM'},
    '$DJX': {'/YM'},

    # Commodity / rates / FX proxies or direct products if ever supported
    'GLD': {'/GC'},
    'SLV': {'/SI'},
    'USO': {'/CL'},
    'TLT': {'/ZB'},
    'IEF': {'/ZN'},
    'FXE': {'/6E'},
    'BITO': {'/BTC'},
    'IBIT': {'/BTC'},
    'FBTC': {'/BTC'},
    'ARKB': {'/BTC'},
    'COPX': {'/HG'},
    'CPER': {'/HG'},
}


class AssetMappingError(ValueError):
    pass


def normalize_futures_root(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol.startswith('/'):
        return symbol
    # Root is slash + letters/digits up to first non-alnum after slash.
    root = ['/']
    for ch in symbol[1:]:
        if ch.isalnum():
            root.append(ch)
        else:
            break
    return ''.join(root)


def validate_underlying_trigger_pair(underlying: str, trigger_symbol: str) -> AssetCompatibility:
    normalized_underlying = underlying.strip().upper()
    normalized_trigger = normalize_futures_root(trigger_symbol)
    allowed = UNDERLYING_TO_FUTURES_ROOTS.get(normalized_underlying)
    if not allowed:
        raise AssetMappingError(
            f'Unsupported underlying {normalized_underlying!r}: no futures mapping configured yet'
        )
    if normalized_trigger not in allowed:
        raise AssetMappingError(
            f'Incompatible signal: {normalized_underlying} must use one of {sorted(allowed)}, '
            f'not {normalized_trigger}'
        )
    return AssetCompatibility(
        underlying=normalized_underlying,
        expected_family=sorted(allowed)[0],
        trigger_symbol=normalized_trigger,
    )
