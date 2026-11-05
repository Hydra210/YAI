from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_shell_has_no_promotional_taglines():
    html = (ROOT / "web/dashboard.html").read_text().lower()
    js = (ROOT / "web/dashboard.js").read_text().lower()

    for phrase in (
        "little things matter",
        "make this space yours",
        "a little snapshot of your server",
        "your server settings, all in one place",
        "choose a section to make it your own",
        "thoughtful settings for a better space",
    ):
        assert phrase not in html
        assert phrase not in js
    assert "sidebar-bottom" not in html
    assert "welcome-tag" not in js
    assert "✳" not in js


def test_dashboard_dirty_state_uses_plain_text_without_status_dots():
    js = (ROOT / "web/dashboard.js").read_text()
    css = (ROOT / "web/dashboard.css").read_text()

    assert "status.replaceChildren(el('span', 'status-dot')" not in js
    assert "All changes saved" in js
    assert "nav-unsaved" in js
    assert "unsaved-label" in js
    assert ".status-dot" not in css
    assert ".nav-dirty" not in css
    assert ".mini-dirty" not in css
    assert ".welcome-tag" not in css
