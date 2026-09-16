"""Classification bullish/bearish des titres de news crypto via un LLM local
(Qwen2.5-1.5B fine-tune, quantifie GGUF), charge une seule fois au process.

Le modele est un classifieur binaire par titre, pas un juge du marche global
(cf. market_sentiment.py pour le Fear & Greed Index) : chaque titre RSS/
CryptoPanic deja recupere par market_news.py est passe au modele, qui
repond bullish ou bearish. Le score par actif est la moyenne des derniers
titres classifies pour cet actif (+1 bullish, -1 bearish).

Chargement paresseux et tolerant aux pannes : si le fichier .gguf n'existe
pas (chemin non configure, pas encore depose sur le Pi) ou si le chargement
echoue, get_news_sentiment_score() retourne toujours None plutot que de
faire planter le cycle de trading -- ce signal est un bonus, pas une
dependance critique.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a crypto market sentiment classifier. "
    "Given a news headline, respond with exactly one word: bullish or bearish."
)

_llm = None
_load_attempted = False
_load_failed = False


def _build_prompt(title: str) -> str:
    return (
        "Classifie le sentiment crypto de ce titre de news en un seul mot : "
        "bullish ou bearish.\n"
        f"Titre : {title}\n"
        "Sentiment :"
    )


def _get_llm(model_path: str | None):
    global _llm, _load_attempted, _load_failed

    if _llm is not None:
        return _llm
    if _load_failed:
        return None
    if not model_path:
        return None
    if not Path(model_path).is_file():
        if not _load_attempted:
            log.warning("Modele de sentiment introuvable: %s (signal news desactive)", model_path)
        _load_attempted = True
        _load_failed = True
        return None

    try:
        from llama_cpp import Llama
    except ImportError:
        log.warning("llama-cpp-python non installe (signal news desactive)")
        _load_failed = True
        return None

    try:
        _llm = Llama(
            model_path=model_path,
            n_ctx=256,
            n_threads=4,
            verbose=False,
        )
        log.info("Modele de sentiment news charge: %s", model_path)
    except Exception:
        log.exception("Echec du chargement du modele de sentiment (signal news desactive)")
        _load_failed = True
        return None

    _load_attempted = True
    return _llm


def classify_title(title: str, model_path: str | None) -> str | None:
    """Retourne 'bullish', 'bearish' ou None (modele indisponible ou reponse
    ambigue -- le titre est alors simplement exclu du score, pas force)."""
    llm = _get_llm(model_path)
    if llm is None:
        return None

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_prompt(title)},
    ]
    try:
        resp = llm.create_chat_completion(messages=messages, max_tokens=5, temperature=0.0)
        text = resp["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        log.exception("Erreur d'inference sentiment pour le titre: %r", title[:80])
        return None

    if re.search(r"\bbullish\b", text):
        return "bullish"
    if re.search(r"\bbearish\b", text):
        return "bearish"
    return None


def get_news_sentiment_score(titles: list[str], model_path: str | None, max_titles: int = 5) -> float | None:
    """Moyenne des classifications (+1/-1) des derniers `max_titles` titres
    pour un actif. None si le modele est indisponible ou si aucun titre n'a
    pu etre classifie (plutot qu'un 0.0 qui laisserait croire a un sentiment
    neutre mesure, et pas a une absence de mesure)."""
    if not titles:
        return None

    labels = [classify_title(t, model_path) for t in titles[:max_titles]]
    scored = [1.0 if lab == "bullish" else -1.0 for lab in labels if lab is not None]
    if not scored:
        return None
    return sum(scored) / len(scored)
