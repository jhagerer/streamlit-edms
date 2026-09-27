"""Render every page against the fake session. Catches errors in page code
without a Snowflake account."""

import pytest
from streamlit.testing.v1 import AppTest

PAGES = {"documents": "Documents", "upload": "Upload", "search": "Search", "trash": "Trash",
         "document": "Document", "document_types": "Document types",
         "metadata_types": "Metadata types", "tags": "Tags", "audit": "Audit trail",
         "admin": "Admin", "diagnostics": "Diagnostics"}


def app():
    return AppTest.from_file("../streamlit_app.py", default_timeout=30)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(fake, page):
    at = app().run()
    at.switch_page(f"app_pages/{page}.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == PAGES[page]


def click(buttons, label):
    matches = [b for b in buttons if b.label == label]
    assert matches, f"no button {label!r}: {[b.label for b in buttons]}"
    matches[0].click()


def test_create_tag_and_flush(fake):
    at = app().run()
    at.switch_page("app_pages/tags.py").run()
    at.text_input[0].input("Urgent")
    click(at.button, "Add")
    at.run()
    assert not at.exception, at.exception
    assert any("Urgent" == t.value for t in at.text_input), [t.value for t in at.text_input]
    assert any("1 unsaved" in w.value for w in at.sidebar.warning)

    click(at.sidebar.button, "Save to database")
    at.run()
    assert not at.exception, at.exception
    assert [r["label"] for r in fake.tables["tag_log"]] == ["Urgent"]
    assert at.sidebar.success[0].value == "All changes saved."


def test_save_to_session_then_restore_in_new_browser_session(fake):
    at = app().run()
    at.switch_page("app_pages/document_types.py").run()
    at.text_input[0].input("Invoice")
    click(at.button, "Add")
    at.run()
    click(at.sidebar.button, "Save to session")
    at.run()
    assert not at.exception, at.exception
    assert any(k.startswith("sessions/alice/") for k in fake.stage)
    assert fake.tables["document_type_log"] == []

    fresh = app().run()  # a new browser session: restore on entry
    assert not fresh.exception, fresh.exception
    assert any("1 change(s) saved to session" in m.value for m in fresh.sidebar.info)
    fresh.switch_page("app_pages/document_types.py").run()
    assert any(t.value == "Invoice" for t in fresh.text_input)


def seed(fake):
    from core import write

    u = "bob"
    rows = {
        "document_type_log": [write.document_type_row(u, "t1", "Invoice")],
        "document_log": [write.document_row(u, "create", "d1", "t1", "Invoice 42", "desc", "en")],
        "document_file_log": [write.file_row(u, document_id="d1", file_id="f1", filename="a.txt",
                                             stage_path="@doc_files/d1/f1.txt", mimetype="text/plain",
                                             size=5, checksum="x", page_count=None)],
        "metadata_type_log": [write.metadata_type_row(u, "m1", "amount", "Amount", "number", None, None)],
        "metadata_log": [write.metadata_row(u, "d1", "m1", "12.5")],
        "tag_log": [write.tag_row(u, "red", "Red", "#ff0000")],
        "tag_assignment_log": [write.tag_assignment_row(u, "d1", "red", True)],
    }
    for table, rs in rows.items():
        fake.tables[table].extend(rs)
        fake.commits[table] = 1
    fake.stage["doc_files/d1/f1.txt"] = b"hello"


def test_document_list_and_detail(fake):
    seed(fake)
    at = app().run()
    assert not at.exception, at.exception
    assert at.dataframe[0].value["label"].tolist() == ["Invoice 42"]

    at.switch_page("app_pages/document.py")
    at.query_params["doc"] = "d1"
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Invoice 42"

    click(at.button, "Move to trash")
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value.startswith("🗑️")
    click(at.sidebar.button, "Save to database")
    at.run()
    assert [r["op"] for r in fake.tables["document_log"]] == ["create", "trash"]
    assert [r["actor"] for r in fake.tables["document_log"]] == ["bob", "alice"]

    at.switch_page("app_pages/documents.py").run()
    assert not at.exception, at.exception
    assert not at.dataframe  # nothing left in the active list


def test_missing_schema_points_to_setup_and_setup_creates_it(fake):
    fake.schema_missing = True
    at = app().run()
    assert not at.exception, at.exception
    assert at.error and "Cannot reach the MiniDMS schema" in at.error[0].value

    at.switch_page("app_pages/setup.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Setup"
    click(at.button, "Run all 8 statement(s) of this step")
    at.run()
    assert not at.exception, at.exception
    assert sum(q.startswith("CREATE TABLE") for q in fake.executed) == 8

    at.switch_page("app_pages/documents.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Documents"


def test_setup_starter_definitions(fake):
    at = app().run()
    at.switch_page("app_pages/setup.py").run()
    click(at.button, "Add starter definitions")
    at.run()
    assert not at.exception, at.exception
    click(at.sidebar.button, "Save to database")
    at.run()
    assert len(fake.tables["document_type_log"]) == 4
    assert len(fake.tables["tag_log"]) == 3


def test_login_required_when_auth_is_configured(fake):
    at = app()
    at.secrets["auth"] = {"redirect_uri": "http://localhost:8501/oauth2callback",
                          "cookie_secret": "x", "client_id": "x", "client_secret": "x",
                          "server_metadata_url": "https://example.com/.well-known/openid-configuration"}
    at.run()
    assert not at.exception, at.exception
    assert [b.label for b in at.button] == ["Log in"]
    assert not fake.queries  # nothing touches Snowflake before login


def test_enter_own_credentials_when_not_connected(fake, monkeypatch):
    """No shared connection: the app points to Setup, the viewer enters a token,
    and from then on this browser session uses its own connection."""
    from conftest import FakeSession
    from core import session as core_session

    fake.offline = True
    own = FakeSession()
    opened = {}

    def fake_open(settings):
        opened["settings"] = settings
        return own, "BOB"

    monkeypatch.setattr(core_session, "open_session", fake_open)
    monkeypatch.delenv("MINIDMS_USER")

    at = app().run()
    assert "Cannot reach the MiniDMS schema" in at.error[0].value
    at.switch_page("app_pages/setup.py").run()
    assert not at.exception, at.exception
    assert any("Not connected" in w.value for w in at.warning)

    at.radio(key="conn:auth").set_value("pat").run()
    labels = {t.label: t for t in at.text_input}
    labels["Account"].input("org-acct")
    labels["User"].input("bob")
    labels["Programmatic access token"].input("tok")
    labels["Schema"].input("MINIDMS")
    click(at.button, "Connect for this browser session")
    at.run()
    assert not at.exception, at.exception
    assert opened["settings"].token == "tok" and opened["settings"].schema == "MINIDMS"
    assert any("Connected as BOB" in s.value for s in at.success)

    # The rest of the app now runs on the viewer's own connection.
    at.switch_page("app_pages/documents.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Documents"
    assert "BOB" in at.sidebar.caption[0].value
    assert own.queries and not fake.queries

    at.switch_page("app_pages/setup.py").run()
    click(at.button, "Disconnect")
    at.run()
    assert own.closed
    at.switch_page("app_pages/documents.py").run()
    assert "Cannot reach the MiniDMS schema" in at.error[0].value


def test_connection_change_blocked_with_pending_changes(fake):
    at = app().run()
    at.switch_page("app_pages/tags.py").run()
    at.text_input[0].input("Urgent")
    click(at.button, "Add")
    at.run()
    at.switch_page("app_pages/setup.py").run()
    assert not at.exception, at.exception
    assert any("before you change the connection" in w.value for w in at.warning)
    assert not [b for b in at.button if b.label == "Connect for this browser session"]
