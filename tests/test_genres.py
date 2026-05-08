from dj_sort.genres import GenreMap


def test_genre_map_resolves_case_insensitive_alias() -> None:
    genre_map = GenreMap({"Drum and Bass": "Drum & Bass"})

    result = genre_map.resolve(" drum AND   bass ", "Missing Genre")

    assert result.canonical_genre == "Drum & Bass"
    assert result.mapped is True
    assert result.missing is False


def test_genre_map_handles_missing_genre() -> None:
    genre_map = GenreMap({})

    result = genre_map.resolve(None, "Missing Genre")

    assert result.canonical_genre == "Missing Genre"
    assert result.missing is True


def test_genre_map_load_supports_explicit_whitelist_entries(tmp_path) -> None:
    genre_map_path = tmp_path / "genres.yaml"
    genre_map_path.write_text(
        "genres:\n  House Music: House\n  Techno:\n",
        encoding="utf-8",
    )

    genre_map = GenreMap.load(genre_map_path)

    assert genre_map.resolve("House Music", "Missing Genre").canonical_genre == "House"
    assert genre_map.resolve("Techno", "Missing Genre").canonical_genre == "Techno"
    assert genre_map.is_whitelisted("House") is True
    assert genre_map.is_whitelisted("Techno") is True
    assert genre_map.is_whitelisted("Ambient") is False
