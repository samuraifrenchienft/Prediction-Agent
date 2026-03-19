"""
Edge Agent — Specialist market scanners.
Each scanner detects pricing gaps in a specific domain.
"""
from .weather_scanner import scan_weather_markets, WeatherGap
from .crypto_scanner  import scan_crypto_markets,  CryptoGap
from .econ_scanner    import scan_econ_markets,     EconGap

__all__ = [
    "scan_weather_markets", "WeatherGap",
    "scan_crypto_markets",  "CryptoGap",
    "scan_econ_markets",    "EconGap",
]
