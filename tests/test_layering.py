"""core/ never imports streamlit, except core/session.py; core/ never imports
the pages; only app_pages/_state.py touches st.session_state."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_core_does_not_import_streamlit_or_pages():
    for path in (ROOT / "core").glob("*.py"):
        imports = _imports(path)
        assert "app_pages" not in imports, path
        if path.name != "session.py":
            assert "streamlit" not in imports, path


def test_only_state_module_touches_session_state():
    for path in list((ROOT / "app_pages").glob("*.py")) + [ROOT / "streamlit_app.py"]:
        if path.name == "_state.py":
            continue
        assert "session_state" not in path.read_text(), path


def test_core_session_state_only_in_session_module():
    """core/session.py keeps the viewer's own connection (not data) in
    st.session_state; no other core module may touch it."""
    for path in (ROOT / "core").glob("*.py"):
        if path.name != "session.py":
            assert "session_state" not in path.read_text(), path
