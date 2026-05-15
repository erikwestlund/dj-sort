from __future__ import annotations

from dj_sort.genres import GenreMap
from dj_sort.lookup import _lookup_title_variants, _match_score


def test_match_score_preserves_artist_title_discogs_format() -> None:
    assert _match_score("Joy Orbison - Hyph Mngo", "Joy Orbison", "Hyph Mngo") == 3
    assert _match_score("Joy Orbison - Gudrun", "Joy Orbison", "Hyph Mngo") == 1


def test_lookup_title_variants_include_parenthetical_base_title() -> None:
    assert _lookup_title_variants("Hyph Mngo (Andreas Saag's House Perspective)") == (
        "Hyph Mngo (Andreas Saag's House Perspective)",
        "Hyph Mngo",
    )


def test_genre_map_resolves_discogs_house_style() -> None:
    genre_map = GenreMap({"House": "House"})
    resolved = genre_map.resolve("House", "Uncategorizable")

    assert resolved.canonical_genre == "House"
