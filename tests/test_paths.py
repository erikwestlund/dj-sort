from pathlib import Path

from dj_sort.paths import ensure_unique_path, normalize_key, safe_path_part, shorten_filename_stem


def test_safe_path_part_replaces_unsafe_characters() -> None:
    assert safe_path_part('A/B: "Track"') == "A_B_ _Track_"


def test_normalize_key_casefolds_and_collapses_whitespace() -> None:
    assert normalize_key("  Drum   AND Bass ") == "drum and bass"


def test_ensure_unique_path_adds_suffix_for_occupied_path(tmp_path: Path) -> None:
    target = tmp_path / "Artist - Title.mp3"
    occupied = {target}

    unique = ensure_unique_path(target, occupied, "seed")

    assert unique != target
    assert unique.name.startswith("Artist - Title - ")
    assert unique.suffix == ".mp3"


def test_shorten_filename_stem_caps_long_names() -> None:
    stem = "A" * 300

    shortened = shorten_filename_stem(stem, max_length=80)

    assert len(shortened) <= 80
    assert shortened != stem
