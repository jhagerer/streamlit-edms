"""Drive the real pages with Streamlit's AppTest against the SQLite-backed
fake session. Catches errors in page code and checks the user-facing flows
without a Snowflake account."""

from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from conftest import stored
from core import write

PAGES = {"documents": "Documents", "upload": "Upload", "search": "Search", "trash": "Trash",
         "document": "Document", "document_types": "Document types",
         "metadata_types": "Metadata types", "tags": "Tags", "audit": "Audit trail",
         "admin": "Admin", "diagnostics": "Diagnostics", "setup": "Setup"}


def app():
    return AppTest.from_file("../streamlit_app.py", default_timeout=30)


def click(buttons, label):
    matches = [b for b in buttons if b.label == label]
    assert matches, f"no button {label!r}: {[b.label for b in buttons]}"
    matches[0].click()


def seed(fake):
    """A document with one processed file, an enrichment with suggestions, and
    definitions — written through the real write commands."""
    write.create_definition("document_type", "bob", {"label": "Invoice"}, entity_id="t1")
    write.create_definition("document_type", "bob", {"label": "Contract"}, entity_id="t2")
    write.create_definition("metadata_type", "bob", {"name": "amount", "label": "Amount",
                                                     "data_type": "number"}, entity_id="m1")
    write.create_definition("tag", "bob", {"label": "Red", "color": "#ff0000"}, entity_id="red")
    write.create_definition("tag", "bob", {"label": "Blue", "color": "#0000ff"}, entity_id="blue")
    write.create_document("bob", document_id="d1",
                          fields={"label": "Invoice 42", "document_type_id": "t1",
                                  "description": "desc", "language": "en"},
                          files=[stored(name="a.txt")], metadata={"m1": "12.5"},
                          tag_ids=["red"])
    write.register_files("bob", [stored(name="a.txt")])
    fake.stage[stored(name="a.txt")["stage_path"].lstrip("@")] = b"hello"
    # What process_ai() would have written inside Snowflake:
    fake.conn.execute(
        "INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, content) "
        "VALUES ('t1', '2020-01-02 00:00:00.000000', 'system:ai', 'f1', 'claude-sonnet-4-5', "
        "'done', 'Invoice no. 42, total 99 EUR')")
    fake.conn.execute(
        "INSERT INTO document_enrichment (event_id, created_at, actor, file_id, document_id, model, "
        "status, summary, suggested_type_id, suggested_tag_ids, extracted_metadata) VALUES "
        "('enr1', '2020-01-02 00:00:01.000000', 'system:ai', 'f1', 'd1', 'claude-sonnet-4-5', "
        "'done', 'An invoice over 99 EUR.', 't2', '[\"blue\"]', "
        "'[{\"metadata_type_id\": \"m1\", \"value\": \"99\"}]')")
    # The request is older than the results: the file is processed, not pending.
    fake.conn.execute("UPDATE ai_request SET requested_at = '2020-01-01 00:00:00.000000'")
    fake.conn.execute("UPDATE sync_state SET seq = seq + 1 "
                      "WHERE table_name IN ('ai_request', 'document_text', 'document_enrichment')")


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(fake, page):
    seed(fake)
    at = app().run()
    at.switch_page(f"app_pages/{page}.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == PAGES[page]


def test_no_save_buttons_every_change_is_written_immediately(fake):
    at = app().run()
    assert not [b for b in at.sidebar.button if "Save" in b.label or "Discard" in b.label]
    at.switch_page("app_pages/tags.py").run()
    at.text_input[0].input("Urgent")
    click(at.button, "Add")
    at.run()
    assert not at.exception, at.exception
    assert [r["label"] for r in fake.rows("SELECT label FROM tag")] == ["Urgent"]
    assert fake.rows("SELECT entity, op, actor FROM change_log") == [
        {"entity": "tag", "op": "create", "actor": "alice"}]
    assert any(t.value == "Urgent" for t in at.text_input)          # cache reloaded
    assert not any(k.startswith("sessions/") for k in fake.stage)  # no session snapshots


def test_document_list_and_trash_restore(fake):
    seed(fake)
    at = app().run()
    assert not at.exception, at.exception
    assert at.dataframe[0].value["label"].tolist() == ["Invoice 42"]

    at.switch_page("app_pages/document.py")
    at.query_params["doc"] = "d1"
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Invoice 42"
    assert any("An invoice over 99 EUR." in i.value for i in at.info)   # AI summary shown
    click(at.button, "Move to trash")
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value.startswith("🗑️")
    assert fake.rows("SELECT status, version FROM document") == [{"status": "trashed", "version": 2}]
    click(at.button, "Restore")
    at.run()
    assert fake.rows("SELECT status, version FROM document") == [{"status": "active", "version": 3}]


def test_concurrent_edit_shows_conflict_and_writes_nothing(fake):
    seed(fake)
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    # Someone else trashes the document after this page was rendered.
    write.update_document("bob", fake.rows("SELECT * FROM document")[0], {"label": "Bob's"})
    click(at.button, "Move to trash")
    at.run()
    assert not at.exception, at.exception
    assert any("changed by someone else" in e.value for e in at.error)
    assert fake.rows("SELECT label, status, version FROM document") == [
        {"label": "Bob's", "status": "active", "version": 2}]
    assert at.title[0].value == "Bob's"                    # the page now shows the latest


def test_ai_suggestions_apply_with_provenance(fake):
    seed(fake)
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    assert not at.exception, at.exception
    apply_buttons = [b for b in at.button if b.label == "Apply"]
    assert len(apply_buttons) == 3                         # type, tags, amount
    apply_buttons[2].click()                               # metadata: amount = 99
    at.run()
    assert not at.exception, at.exception
    assert fake.rows("SELECT value, version, updated_by FROM document_metadata") == [
        {"value": "99", "version": 2, "updated_by": "alice"}]
    data = fake.rows("SELECT data FROM change_log WHERE entity = 'document_metadata' "
                     "ORDER BY event_ts DESC LIMIT 1")[0]["data"]
    assert '"source": "ai:enr1"' in data
    assert len([b for b in at.button if b.label == "Apply"]) == 2


def test_process_again_requests_ai_in_snowflake(fake):
    seed(fake)
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    before = fake.count("ai_request")
    click(at.button, "Process again")
    at.run()
    assert not at.exception, at.exception
    assert fake.count("ai_request") == before + 1
    assert not any("AI_COMPLETE" in q for q in fake.queries)   # the app never calls Cortex
    assert any("AI is processing 1 file" in i.value for i in at.sidebar.info)


def test_search_uses_text_and_summary(fake):
    seed(fake)
    at = app().run()
    at.switch_page("app_pages/search.py").run()
    at.text_input(key="search:q").input("EUR").run()
    assert not at.exception, at.exception
    assert at.dataframe[0].value["label"].tolist() == ["Invoice 42"]


def test_any_warehouse_works_but_one_is_needed(fake, monkeypatch):
    fake.warehouse_type = "STANDARD"
    at = app().run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Documents"                 # a standard warehouse is fine

    from core import read
    monkeypatch.setattr(read, "warehouse_type", lambda session=None: (None, None))
    at = app().run()
    assert any("No warehouse is in use" in e.value for e in at.error)
    assert not at.title                                     # the page did not render
    at.switch_page("app_pages/setup.py").run()              # setup still works
    assert at.title[0].value == "Setup"


def test_missing_schema_points_to_setup_and_setup_creates_it(fake):
    fake.schema_missing = True
    at = app().run()
    assert not at.exception, at.exception
    assert at.error and "Cannot reach the MiniDMS schema" in at.error[0].value

    at.switch_page("app_pages/setup.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Setup"
    click(at.button, "Run all 10 statement(s) of this step")    # hybrid tables + seed
    at.run()
    assert not at.exception, at.exception
    assert sum(q.startswith("CREATE HYBRID TABLE") for q in fake.executed) == 9
    assert any("FROM VALUES" in q for q in fake.executed)        # sync_state seed

    at.switch_page("app_pages/documents.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Documents"


def test_setup_starter_definitions(fake):
    at = app().run()
    at.switch_page("app_pages/setup.py").run()
    click(at.button, "Add starter definitions")
    at.run()
    assert not at.exception, at.exception
    assert fake.count("document_type") == 4 and fake.count("tag") == 3
    assert fake.count("metadata_type") == 4


def test_admin_ai_tab(fake):
    seed(fake)
    write.create_document("alice", document_id="d2", fields={"label": "Orphan"},
                          files=[stored(file_id="f2", document_id="d2")], metadata={}, tag_ids=[])
    fake.task_state = "suspended"
    at = app().run()
    at.switch_page("app_pages/admin.py").run()
    assert not at.exception, at.exception
    assert any("suspended" in w.value for w in at.warning)
    click(at.button, "Process 1 unprocessed file(s)")
    at.run()
    assert fake.rows("SELECT file_id, reason FROM ai_request WHERE file_id = 'f2'") == [
        {"file_id": "f2", "reason": "missing"}]
    click(at.button, "Run the task now")
    at.run()
    assert "EXECUTE TASK process_ai_task" in fake.executed


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


def test_history_shows_human_and_ai_events(fake):
    seed(fake)
    write.update_document("carol", fake.rows("SELECT * FROM document")[0], {"label": "Renamed"})
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    assert not at.exception, at.exception
    hist = at.dataframe[-1].value
    assert {"carol", "bob", "system:ai"} <= set(hist["actor"])
    assert isinstance(hist["event_ts"].iloc[0], (datetime,)) or hist["event_ts"].dtype.kind == "M"


def test_concurrent_tag_change_is_kept_not_overwritten(fake):
    """Bob attaches a tag while Alice has the page open; Alice saves her own
    tag change without touching Bob's: Bob's tag must survive."""
    seed(fake)
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    write.set_tags("bob", "d1", ["blue"], [])                 # concurrent change
    at.multiselect(key="doc:d1:tags").set_value([]).run()     # Alice removes "red"
    click(at.button, "Save tags")
    at.run()
    assert not at.exception, at.exception
    assert [r["tag_id"] for r in fake.rows("SELECT tag_id FROM document_tag")] == ["blue"]


def test_concurrent_metadata_edit_is_a_conflict(fake):
    seed(fake)
    at = app()
    at.query_params["doc"] = "d1"
    at.switch_page("app_pages/document.py").run()
    write.set_metadata("bob", "d1", "m1", "13", 1)            # concurrent change
    at.text_input(key="doc:d1:meta:m1").input("14")
    click(at.button, "Save metadata")
    at.run()
    assert not at.exception, at.exception
    assert any("changed by someone else" in e.value for e in at.error)
    assert fake.rows("SELECT value FROM document_metadata") == [{"value": "13"}]
    assert at.text_input(key="doc:d1:meta:m1").value == "13"  # draft reset to the latest
