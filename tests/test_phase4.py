"""Phase 4 production React asset preflight, before old process replacement."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require_dashboard_build(root):
    from annie_runtime.__main__ import require_dashboard_build as check

    return check(root)


def test_real_build_is_available():
    dashboard = require_dashboard_build(ROOT)
    assert (dashboard / "index.html").is_file()
    assert (dashboard / "assets").is_dir()


def test_missing_build_fails_before_replacing_running_runtime(tmp_path):
    try:
        require_dashboard_build(tmp_path)
    except ValueError as exc:
        assert "React build missing" in str(exc)
    else:
        raise AssertionError("missing React build must fail")


def test_incomplete_build_cannot_silently_fall_back_to_old_dashboard(tmp_path):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/missing.js"></script>', encoding="utf-8")
    try:
        require_dashboard_build(tmp_path)
    except ValueError as exc:
        assert "React build incomplete" in str(exc)
    else:
        raise AssertionError("incomplete React build must fail")
