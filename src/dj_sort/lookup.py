from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dj_sort.genres import GenreMap
from dj_sort.paths import normalize_key, normalize_text

USER_AGENT = "dj-sort/0.1 (+https://local)"
LOCAL_ENV_PATH = Path(".env")


STYLE_HINTS = {
    "2-step": "UK Garage",
    "acid": "Acid House/Techno",
    "ambient": "Ambient",
    "bass music": "Bass",
    "bassline": "Bassline House",
    "breakbeat": "Breakbeat",
    "breaks": "Breaks",
    "chicago": "Chicago House",
    "deep house": "Deep House",
    "disco": "Disco",
    "downtempo": "Downtempo",
    "drum n bass": "Drum & Bass",
    "drum and bass": "Drum & Bass",
    "dubstep": "Dubstep",
    "electro house": "Electro House",
    "electro": "Electro",
    "experimental": "Experimental",
    "footwork": "Juke",
    "future garage": "Future Bass",
    "garage house": "UK Garage",
    "garage": "UK Garage",
    "hardcore": "Hardcore Techno",
    "hip hop": "Hip-Hop",
    "house": "House",
    "idm": "IDM",
    "juke": "Juke",
    "leftfield": "Experimental",
    "tech house": "Tech House",
    "techno": "Techno",
    "trap": "Trap",
    "uk garage": "UK Garage",
}

GENERIC_EXTERNAL_TERMS = {"dance", "electronic", "electronica", "club"}
PRIORITY_STYLE_HINTS = [
    ("uk garage", "UK Garage"),
    ("2-step", "UK Garage"),
    ("acid", "Acid House/Techno"),
    ("breakbeat", "Breakbeat"),
    ("breaks", "Breaks"),
    ("deep house", "Deep House"),
    ("tech house", "Tech House"),
    ("electro house", "Electro House"),
    ("electro", "Electro"),
    ("ambient", "Ambient"),
    ("idm", "IDM"),
    ("dubstep", "Dubstep"),
    ("techno", "Techno"),
]


@dataclass(frozen=True)
class LookupSuggestion:
    genre: str
    confidence: str
    source: str
    terms: tuple[str, ...]
    notes: str = ""


def suggest_genre_for_track(
    artist: str,
    title: str,
    genre_map: GenreMap,
    *,
    discogs_token: str | None = None,
    sleep_seconds: float = 1.1,
    musicbrainz_fallback: bool = True,
) -> LookupSuggestion | None:
    """Return a best-effort external genre suggestion without modifying local files."""
    _load_local_env()
    cleaned_artist = normalize_text(artist)
    cleaned_title = _strip_key_suffix(normalize_text(title))
    if not cleaned_artist or not cleaned_title:
        return None

    discogs_auth = _discogs_auth(discogs_token)
    if discogs_auth:
        suggestion = _discogs_suggestion(cleaned_artist, cleaned_title, genre_map, discogs_auth)
        if suggestion:
            time.sleep(sleep_seconds)
            return suggestion
        time.sleep(sleep_seconds)

    if musicbrainz_fallback:
        return _musicbrainz_suggestion(cleaned_artist, cleaned_title, genre_map)
    return None


def _discogs_suggestion(artist: str, title: str, genre_map: GenreMap, auth: dict[str, str]) -> LookupSuggestion | None:
    params = {"artist": artist, "track": title, "type": "release", "per_page": "5"}
    params.update(auth.get("params", {}))
    data = _json_get(
        f"https://api.discogs.com/database/search?{urlencode(params)}",
        headers={**auth.get("headers", {}), "User-Agent": USER_AGENT},
    )
    if not data:
        return None

    candidates = []
    for result in data.get("results", [])[:5]:
        terms = [*(result.get("style") or []), *(result.get("genre") or [])]
        if not terms:
            continue
        score = _match_score(result.get("title") or "", artist, title)
        candidates.append((score, tuple(str(term) for term in terms), result.get("title") or ""))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda item: item[0])
    score, terms, matched_title = candidates[0]
    genre = _best_local_genre(terms, genre_map)
    if not genre:
        return None
    confidence = "high" if score >= 2 else "medium"
    return LookupSuggestion(genre=genre, confidence=confidence, source="discogs", terms=terms, notes=f"matched {matched_title}")


def _musicbrainz_suggestion(artist: str, title: str, genre_map: GenreMap) -> LookupSuggestion | None:
    query = f'artist:"{artist}" AND recording:"{title}"'
    params = urlencode({"query": query, "fmt": "json", "limit": 5, "inc": "tags"})
    data = _json_get(f"https://musicbrainz.org/ws/2/recording/?{params}", headers={"User-Agent": USER_AGENT})
    if not data:
        return None

    candidates = []
    for recording in data.get("recordings", [])[:5]:
        terms = [tag.get("name") for tag in recording.get("tags", []) if tag.get("name")]
        if not terms:
            continue
        score = int(recording.get("score") or 0)
        candidates.append((score, tuple(str(term) for term in terms), recording.get("title") or ""))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda item: item[0])
    score, terms, matched_title = candidates[0]
    genre = _best_local_genre(terms, genre_map)
    if not genre:
        return None
    confidence = "medium" if score >= 90 else "low"
    return LookupSuggestion(genre=genre, confidence=confidence, source="musicbrainz", terms=terms, notes=f"matched {matched_title}")


def _best_local_genre(terms: tuple[str, ...], genre_map: GenreMap) -> str | None:
    useful_terms = [term for term in terms if normalize_key(term) not in GENERIC_EXTERNAL_TERMS]
    normalized_terms = [normalize_key(term) for term in useful_terms]
    for hint, genre in PRIORITY_STYLE_HINTS:
        normalized_hint = normalize_key(hint)
        if any(normalized_hint == term or normalized_hint in term for term in normalized_terms):
            return genre
    for term in useful_terms:
        resolved = genre_map.resolve(term, "Uncategorizable")
        if resolved.mapped and resolved.canonical_genre:
            return resolved.canonical_genre
    for hint, genre in STYLE_HINTS.items():
        normalized_hint = normalize_key(hint)
        if any(normalized_hint == term or normalized_hint in term for term in normalized_terms):
            return genre
    return None


def _match_score(result_title: str, artist: str, title: str) -> int:
    normalized_result = normalize_key(_strip_key_suffix(result_title))
    score = 0
    if normalize_key(artist) in normalized_result:
        score += 1
    if normalize_key(title) in normalized_result:
        score += 2
    return score


def _strip_key_suffix(title: str) -> str:
    return normalize_text(title.rsplit(" - ", 1)[0]) if " - " in title else title


def _json_get(url: str, *, headers: dict[str, str]) -> dict | None:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed metadata API hosts
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def discogs_token_available() -> bool:
    _load_local_env()
    return bool(_discogs_auth(None))


def _discogs_auth(token: str | None) -> dict[str, dict[str, str]] | None:
    token = token or os.environ.get("DISCOGS_TOKEN")
    if token:
        return {"headers": {"Authorization": f"Discogs token={token}"}, "params": {}}
    key = os.environ.get("DISCOGS_CONSUMER_KEY")
    secret = os.environ.get("DISCOGS_CONSUMER_SECRET")
    if key and secret:
        return {"headers": {}, "params": {"key": key, "secret": secret}}
    return None


def _load_local_env(path: Path = LOCAL_ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
