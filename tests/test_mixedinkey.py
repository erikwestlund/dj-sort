from dj_sort.mixedinkey import parse_mixed_in_key_comment


def test_parse_mixed_in_key_comment_normalizes_prefix() -> None:
    info = parse_mixed_in_key_comment("08A - 123 - 7 - 10.0.155")

    assert info is not None
    assert info.key == "8A"
    assert info.bpm == "123"
    assert info.energy == "7"
    assert info.normalized_comment == "8A - 123 - 7 - 10.0.155"


def test_mixed_in_key_comment_rejects_unrelated_comment() -> None:
    assert parse_mixed_in_key_comment("dj-sort original genre: House") is None
