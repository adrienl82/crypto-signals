"""Agregation de news crypto, filtrees par actif suivi.

Deux sources complementaires :
- Flux RSS de medias crypto etablis (gratuits, publics, sans cle) : bonne
  couverture generale, filtree cote code par mots-cles (nom/ticker de
  l'actif) puisque ces flux ne sont pas segmentes par crypto.
- CryptoPanic (optionnel, necessite un compte + token gratuit) : filtrage
  natif par devise et vote bullish/bearish/important de la communaute,
  plus precis mais plus limite en requetes.

Les deux sont mis en cache (TTL) pour ne pas spammer les flux/l'API a
chaque appel -- la news n'a pas besoin d'etre plus fraiche que ca.
"""
from __future__ import annotations

import logging
import time

import feedparser
import requests

log = logging.getLogger(__name__)

RSS_FEEDS = {
    "Cointelegraph": "https://cointelegraph.com/rss",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoinist": "https://bitcoinist.com/feed/",
    "NewsBTC": "https://www.newsbtc.com/feed/",
    "U.Today": "https://u.today/rss.php",
    "Bitcoin.com": "https://news.bitcoin.com/feed/",
    "AMBCrypto": "https://ambcrypto.com/feed/",
    "CryptoNews": "https://cryptonews.com/news/feed/",
    "BeInCrypto": "https://beincrypto.com/feed/",
    "The Defiant": "https://thedefiant.io/api/feed",
    "Protos": "https://protos.com/feed/",
    "Watcher.Guru": "https://watcher.guru/news/feed",
    "The Daily Hodl": "https://dailyhodl.com/feed/",
    # CryptoSlate, CryptoBriefing et CryptoPotato exclus : proteges par un
    # challenge Cloudflare (403 constant ou page de challenge JS servie en
    # HTTP 200 par intermittence, ce qui casse le parsing XML).
}

# mots-cles de reconnaissance par entity_key (en minuscules, matches sur titre+resume)
ASSET_KEYWORDS = {
    "btc": ["bitcoin", "btc"],
    "eth": ["ethereum", "eth"],
    "xrp": ["xrp", "ripple"],
}

_RSS_TTL_SECONDS = 20 * 60  # 20 min : la news n'a pas besoin d'etre plus fraiche
_CP_TTL_SECONDS = 20 * 60

_rss_cache: dict = {"data": None, "fetched_at": 0.0}
_cp_cache: dict = {"data": None, "fetched_at": 0.0}


def fetch_rss_news(asset_keywords: dict[str, list[str]] | None = None,
                    max_items_per_asset: int = 5, force: bool = False) -> dict[str, list[dict]]:
    """Interroge les flux RSS et retourne {entity_key: [items...]}, tries du
    plus recent au plus ancien, limites a max_items_per_asset par actif."""
    asset_keywords = asset_keywords or ASSET_KEYWORDS
    now = time.time()
    if not force and _rss_cache["data"] is not None and now - _rss_cache["fetched_at"] < _RSS_TTL_SECONDS:
        raw_items = _rss_cache["data"]
    else:
        raw_items = []
        seen_titles: set[str] = set()
        for source, url in RSS_FEEDS.items():
            try:
                resp = requests.get(url, timeout=10, headers={"User-Agent": "crypto-paper-trader/1.0"})
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                for entry in parsed.entries[:30]:
                    title = entry.get("title", "")
                    title_key = title.strip().lower()
                    if not title or title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)
                    raw_items.append({
                        "title": title,
                        "summary": entry.get("summary", ""),
                        "url": entry.get("link", ""),
                        "source": source,
                        "published": entry.get("published", entry.get("updated", "")),
                    })
            except Exception:
                log.warning("Flux RSS indisponible: %s (%s)", source, url)
        _rss_cache["data"] = raw_items
        _rss_cache["fetched_at"] = now

    by_asset: dict[str, list[dict]] = {key: [] for key in asset_keywords}
    for item in raw_items:
        haystack = (item["title"] + " " + item["summary"]).lower()
        for key, keywords in asset_keywords.items():
            if any(kw in haystack for kw in keywords) and len(by_asset[key]) < max_items_per_asset:
                by_asset[key].append({k: v for k, v in item.items() if k != "summary"})
    return by_asset


def fetch_cryptopanic_news(token: str | None, currencies: list[str],
                            max_items: int = 10, force: bool = False) -> list[dict] | None:
    """Retourne les dernieres news CryptoPanic filtrees par devises (tickers,
    ex: ["BTC","ETH","XRP"]), ou None si aucun token n'est configure (feature
    desactivee proprement, pas une erreur)."""
    if not token:
        return None

    now = time.time()
    if not force and _cp_cache["data"] is not None and now - _cp_cache["fetched_at"] < _CP_TTL_SECONDS:
        return _cp_cache["data"]

    try:
        resp = requests.get(
            "https://cryptopanic.com/api/developer/v2/posts/",
            params={"auth_token": token, "currencies": ",".join(currencies), "public": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[:max_items]
        items = [{
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": (r.get("source") or {}).get("title", ""),
            "published": r.get("published_at", ""),
            "currencies": [c.get("code") for c in (r.get("currencies") or [])],
            "votes": r.get("votes", {}),
        } for r in results]
        _cp_cache["data"] = items
        _cp_cache["fetched_at"] = now
        return items
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.warning("CryptoPanic indisponible: HTTP %s", status)
        return _cp_cache["data"]
    except Exception as exc:
        log.warning("CryptoPanic indisponible: %s", exc)
        return _cp_cache["data"]
