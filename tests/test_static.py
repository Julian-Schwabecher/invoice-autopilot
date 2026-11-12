"""The one-file page (static/index.html) has no build step and no browser test, so these checks catch the
mistakes that would otherwise only show up on screen: a call to an endpoint that does not exist, a script that
does not parse, an element the script looks for that is not in the page, or something loaded from the internet."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import main

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPTS = re.findall(r"<script>(.*?)</script>", HTML, re.S)
JS = SCRIPTS[0] if SCRIPTS else ""

_TOKEN = re.compile(r"""\s*(?:'((?:[^'\\]|\\.)*)'|(encodeURIComponent\([^()]*\))|\(([^()]*)\))\s*""")


def api_calls() -> list[tuple[str, str]]:
    """(method, path) for every api('/api/...') call in the page script. Parts that are filled in at run time
    (an encoded invoice id, an optional query string) become {x}; the query string is dropped."""
    found = []
    for m in re.finditer(r"\bapi\(\s*(?='/api/)", JS):
        pos, path = m.end(), ""
        while True:
            t = _TOKEN.match(JS, pos)
            if not t:
                break
            if t.group(1) is not None:
                path += t.group(1)
            elif t.group(2):
                path += "{x}"          # an encoded invoice id; a (...) group is only ever an optional query string
            pos = t.end()
            if JS[pos:pos + 1] != "+":
                break
            pos += 1
        method = "POST" if JS[pos:pos + 1] == "," else "GET"
        found.append((method, path.split("?")[0]))
    return found


def _route_matches(method: str, path: str) -> bool:
    probe = path.replace("{x}", "X")
    for r in main.app.routes:
        rx = re.sub(r"\{[^}]+\}", "[^/]+", getattr(r, "path", ""))
        if re.fullmatch(rx, probe) and method in (getattr(r, "methods", None) or ()):
            return True
    return False


def test_the_page_has_exactly_one_script_and_it_is_read():
    assert len(SCRIPTS) == 1 and len(JS) > 5000


def test_nothing_is_loaded_from_the_internet():
    assert "<script src" not in HTML and "<link " not in HTML.replace('<link rel="icon"', "")
    assert not re.search(r"""(?:src|srcset)=["']?https?://""", HTML)
    assert not re.search(r"@import|url\(\s*['\"]?https?:", HTML)


def test_every_endpoint_the_page_calls_exists():
    calls = api_calls()
    assert len(calls) >= 15, calls   # if the parser above stops finding calls, fail loudly instead of passing empty
    missing = [c for c in calls if not _route_matches(*c)]
    assert not missing, f"The page calls endpoints that do not exist (or use the wrong method): {missing}"


def test_the_new_features_call_the_new_endpoints():
    paths = {p for _, p in api_calls()}
    for p in ("/api/nudges", "/api/safe-mode", "/api/log", "/api/events", "/api/demo/seed", "/api/health",
              "/api/invoices/{x}/qr", "/api/invoices/{x}/record-test-payment", "/api/invoices/{x}/remind"):
        assert p in paths, p


def test_all_network_calls_go_through_the_one_helper_that_shows_plain_errors():
    assert len(re.findall(r"\bfetch\(", JS)) == 1


def test_every_element_the_script_looks_up_exists():
    ids = set(re.findall(r"""\$\('([A-Za-z][\w-]*)'\)""", JS))
    assert len(ids) > 40
    missing = sorted(i for i in ids if f'id="{i}"' not in HTML)
    assert not missing, f"The script looks for elements that are not in the page: {missing}"


def test_ids_in_the_page_are_unique():
    ids = re.findall(r'\sid="([^"$]+)"', HTML.split("<script>")[0])
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, dupes


def test_only_https_links_are_ever_opened_from_server_data():
    assert "function safeUrl" in JS and "/^https:\\/\\//i" in JS
    assert "safeUrl(inv.view_url)" in JS and "safeUrl(r.view_url)" in JS


def test_the_page_follows_the_light_and_dark_setting_and_fits_a_phone():
    assert "prefers-color-scheme:dark" in HTML and 'data-theme="dark"' in HTML
    assert 'name="viewport"' in HTML and "max-width:480px" in HTML


def test_changing_text_is_announced_to_screen_readers():
    for needle in ('id="toasts"', 'id="nstatus"', 'id="cfpreview"', 'id="demomsg"'):
        assert needle in HTML
    assert re.search(r'id="toasts"[^>]*aria-live="polite"', HTML)


def test_the_live_list_does_not_announce_every_refresh():
    assert not re.search(r'id="list"[^>]*aria-live', HTML)
    assert re.search(r'id="updated"[^>]*aria-live="off"', HTML)


def test_the_page_polls_only_while_visible():
    assert "document.hidden" in JS and "visibilitychange" in JS and "POLL_MS = 8000" in JS


def _function_source(name: str) -> str:
    start = JS.index(f"function {name}(")
    depth, i = 0, JS.index("{", start)
    for j in range(i, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[j], 0)
        if depth == 0:
            return JS[start:j + 1]
    raise AssertionError(name)


def test_partly_paid_is_decided_from_the_numbers_not_from_the_label():
    for name in ("cardHeadHtml", "openPayConfirm"):
        assert "kind === 'partial'" not in _function_source(name), name
        assert "isPartlyPaid(" in _function_source(name), name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_overdue_partly_paid_invoice_counts_as_partly_paid(tmp_path):
    f = tmp_path / "check.js"
    f.write_text(_function_source("isPartlyPaid") + """
const rows = [
  [{kind: 'overdue', total: '300.00', amount_due: '120.00'}, true],    // late and partly paid (the label says overdue)
  [{kind: 'partial', total: '300.00', amount_due: '120.00'}, true],
  [{kind: 'waiting', total: '300.00', amount_due: '300.00'}, false],
  [{kind: 'overdue', total: '300.00', amount_due: '300.00'}, false],
  [{kind: 'paid', total: '300.00', amount_due: '0.00'}, false],
  [{kind: 'cancelled', total: '300.00', amount_due: '120.00'}, false],
  [{kind: 'overdue', total: '300.00', amount_due: '0.00'}, false],
  [{kind: 'overdue', total: '300.00'}, false],
  [{kind: 'overdue', total: '300.00', amount_due: null}, false],
  [null, false],
];
let bad = 0;
for (const [inv, want] of rows) if (isPartlyPaid(inv) !== want) { bad++; console.log('wrong for', JSON.stringify(inv)); }
process.exit(bad ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run([shutil.which("node"), str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_marking_paid_in_preview_only_mode_shows_the_preview_and_does_not_mark_the_card_paid():
    src = _function_source("doPayTest")
    assert "r.preview" in src and "previewBox(" in src
    assert src.index("r.preview") < src.index("kind: 'paid'")        # decided before the card is patched to Paid
    assert "Show me the preview" in _function_source("openPayConfirm")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_parses(tmp_path):
    f = tmp_path / "page.js"
    f.write_text(JS, encoding="utf-8")
    r = subprocess.run([shutil.which("node"), "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
