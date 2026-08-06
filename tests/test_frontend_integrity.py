"""Frontend integrity guard — keeps every button working, permanently.

Fails the suite if any future edit to static/index.html:
  1. references an event-handler function that is not defined (the exact class
     of bug that killed New Opportunity/Invoice/Quotation for weeks),
  2. breaks JavaScript syntax (one SyntaxError = every button dead),
  3. adds a sidebar page with no PAGES entry (dead nav item),
  4. re-introduces an unguarded JSON.parse on record data.

Run with the rest of the suite:  .venv\\Scripts\\python.exe -m pytest tests -q
"""
import os, re, shutil, subprocess, tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, "static", "index.html")

with open(INDEX, encoding="utf-8") as f:
    HTML = f.read()

# Identifiers that may legitimately appear in handler attributes undefined.
JS_BUILTINS = {"event", "confirm", "prompt", "alert", "parseInt", "parseFloat",
               "String", "Number", "encodeURIComponent", "setTimeout", "fetch",
               "function", "if", "for"}


def _defined_functions():
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", HTML))
    defined |= set(re.findall(r"(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", HTML))
    return defined


def test_every_event_handler_is_defined():
    handlers = set()
    for attr in re.findall(r'on(?:click|change|input|keydown|blur|submit)="([^"]+)"', HTML):
        # bare calls only — `.method()` calls are object members, not globals
        for name in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", attr):
            handlers.add(name)
    missing = sorted(handlers - _defined_functions() - JS_BUILTINS)
    assert not missing, (
        f"Buttons reference undefined functions: {missing}. "
        "Define them in static/index.html or the buttons will silently die."
    )


def test_javascript_syntax_is_valid():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — syntax check skipped")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
    assert js.strip(), "No <script> block found in index.html"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(js)
        path = tmp.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True)
        assert r.returncode == 0, (
            "JavaScript SyntaxError — this kills EVERY button in the app:\n"
            + r.stderr.decode("utf-8", errors="replace")[:800]
        )
    finally:
        os.unlink(path)


def test_every_nav_page_has_a_renderer():
    nav_pages = set(re.findall(r'data-page="([\w-]+)"', HTML))
    assert nav_pages, "No sidebar nav items found"
    m = re.search(r"const PAGES = \{(.*?)\};", HTML, re.S)
    assert m, "PAGES registry not found"
    pages_src = m.group(1)
    registered = set(re.findall(r"([\w]+)\s*[:,]", pages_src)) | set(
        re.findall(r"(\w+)\s*:", pages_src))
    missing = sorted(p for p in nav_pages if p not in pages_src)
    assert not missing, f"Sidebar items with no page renderer: {missing}"


def test_no_unguarded_json_parse_on_record_data():
    # JSON.parse directly on record fields (co.x, r.x, lead.x…) crashed the
    # View button when a field held a plain string. Data parses must go
    # through safeJSON() or sit inside a try block on the same line.
    bad = []
    for i, line in enumerate(HTML.split("\n"), 1):
        for mm in re.finditer(r"JSON\.parse\(\s*(\w+)\.(\w+)", line):
            var = mm.group(1)
            if var in ("localStorage", "sessionStorage", "JSON", "window"):
                continue
            if "try" in line or "safeJSON" in line:
                continue
            bad.append(f"line {i}: JSON.parse({var}.{mm.group(2)}…)")
    assert not bad, (
        "Unguarded JSON.parse on record data (use safeJSON() instead): " + "; ".join(bad)
    )


def test_global_error_net_present():
    assert "window.addEventListener('error'" in HTML
    assert "unhandledrejection" in HTML
    assert "function safeJSON" in HTML


def test_chart_tooltips_use_registry_not_inline_payloads():
    # Tooltip HTML contains quotes; inlining it into onmousemove attributes
    # broke the markup (raw code leaked into the page + SyntaxError toast).
    # Payloads must go through chTipRef()/CH_TIPS, never inline strings.
    assert "chTipMove(event,'" not in HTML, (
        "Inline chart-tooltip payload found — use chTipMove(event,CH_TIPS[chTipRef(...)]) instead"
    )
    assert "CH_TIPS[" in HTML and "function chTipRef" in HTML
