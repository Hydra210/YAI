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


def test_dashboard_borders_are_stateful_and_keyboard_focus_remains_visible():
    css = (ROOT / "web/dashboard.css").read_text()

    assert ".panel { border: 1px solid transparent;" in css
    assert "border-color: transparent;" in css
    assert ".nav-item:hover:not(:disabled)" in css and "border-color: #444" in css
    assert ".nav-item.active" in css and "border-color: #404040" in css
    assert ".section-card:hover:not(:disabled)" in css and "border-color: #666" in css
    assert ".text-input:hover:not(:disabled)" in css and ".text-input:focus-visible" in css
    assert ":focus-visible { outline: 3px solid #fff !important;" in css


def test_rules_editor_has_read_only_sources_and_accessible_edit_controls():
    js = (ROOT / "web/dashboard.js").read_text()
    css = (ROOT / "web/dashboard.css").read_text()

    assert "field.type === 'rules_editor'" in js
    assert "function renderRulesEditor" in js
    assert "rule.text == null ? '' : rule.text" in js
    assert "Original parsed rule text (read only)" in js
    assert "rule.source_channel" in js and "rule.category" in js
    assert "'Correction'" in js
    assert "'Guidance for Yuki (optional)'" in js
    assert "'Category override'" in js
    assert "field.options" in js
    assert "correction.disabled = disabled" in js
    assert "guidance.disabled = disabled" in js
    assert "categorySelect.disabled = disabled" in js
    assert "aria-labelledby" in js and "correctionLabel.htmlFor = correctionId" in js
    assert "guidanceLabel.htmlFor = guidanceId" in js
    assert "categoryLabel.htmlFor = categoryId" in js
    assert "No parsed rules yet" in js
    assert "Stale / orphan rule" in js
    assert "clear the correction and guidance, choose “auto”, then save" in js
    assert "const updated = current.map((rule) => rule && rule.id === ruleId" in js

    # Dynamic server-provided values go through the textContent helper, never HTML parsing.
    assert "node.textContent = String(text);" in js
    assert "innerHTML" not in js
    assert "el('p', 'rule-original', rule.text == null ? '' : rule.text)" in js
    assert "el('span', 'rule-meta-value', rule.source_channel" in js
    assert "el('span', 'rule-meta-value', rule.category" in js

    assert ".rule-card {" in css and "border: 1px solid transparent" in css
    assert ".rule-card:hover, .rule-card:focus-within" in css
    assert ".rule-control-label" in css and ".rule-editor-select" in css
    assert "status-pill" not in js + css
    assert "rule-status-dot" not in js + css
    assert "rule-dot" not in js + css
