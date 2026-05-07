from dj_sort.genres import GenreMap


def test_genre_map_resolves_case_insensitive_alias() -> None:
    genre_map = GenreMap({"Drum and Bass": "Drum & Bass"})

    result = genre_map.resolve(" drum AND   bass ", "_Needs Genre")

    assert result.canonical_genre == "Drum & Bass"
    assert result.mapped is True
    assert result.missing is False


def test_genre_map_handles_missing_genre() -> None:
    genre_map = GenreMap({})

    result = genre_map.resolve(None, "_Needs Genre")

    assert result.canonical_genre == "_Needs Genre"
    assert result.missing is True
