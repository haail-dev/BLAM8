from pathlib import Path

from app.main import static_rev


def test_rev_is_stable_and_follows_the_static_files(tmp_path: Path):
    (tmp_path / "a.js").write_text("one")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.css").write_text("two")
    first = static_rev(tmp_path)
    assert first == static_rev(tmp_path) and len(first) == 7
    (tmp_path / "a.js").write_text("one!")
    changed = static_rev(tmp_path)
    assert changed != first
    (tmp_path / "a.js").write_text("one")
    assert static_rev(tmp_path) == first                # back to the same bytes: the same URLs


def test_rev_notices_a_rename_and_a_new_file(tmp_path: Path):
    (tmp_path / "a.js").write_text("x")
    base = static_rev(tmp_path)
    (tmp_path / "a.js").rename(tmp_path / "b.js")
    assert static_rev(tmp_path) != base
    (tmp_path / "c.js").write_text("y")
    assert static_rev(tmp_path) not in (base,)


def test_the_real_static_dir_hashes_quickly():
    import time
    started = time.monotonic()
    assert static_rev()
    assert time.monotonic() - started < 1.0
