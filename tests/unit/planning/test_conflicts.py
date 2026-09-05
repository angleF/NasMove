from nasmove.planning.conflicts import allocate_name


def test_conflict_suffix_preserves_extension() -> None:
    occupied = {"movie.mov", "movie (1).mov"}

    assert allocate_name("movie.mov", occupied) == "movie (2).mov"


def test_conflict_key_uses_nfc_and_casefold() -> None:
    occupied = {"Café.txt"}

    assert allocate_name("cafe\u0301.TXT", occupied) == "cafe\u0301 (1).TXT"
