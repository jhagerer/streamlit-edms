"""The shared Snowflake session must never mix one viewer's statements into
another viewer's open transaction."""

import threading
import time

from core import db


def test_statements_wait_for_an_open_transaction(fake):
    order = []

    def other_viewer():
        db.query("SELECT 1")
        order.append("other")

    with db.transaction() as tx:
        t = threading.Thread(target=other_viewer)
        t.start()
        time.sleep(0.1)
        order.append("tx")                 # still inside the transaction
        tx.run("SELECT 1")
    t.join(2)
    assert order == ["tx", "other"]


def test_rollback_on_error(fake):
    try:
        with db.transaction() as tx:
            tx.run("INSERT INTO ai_job_log (event_id, event_ts, request_id, file_id, document_id, "
                   "step) VALUES ('x', '2026-01-01', 'r', 'f', 'd', 'queued')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert fake.count("ai_job_log") == 0


def test_duplicate_key_detection():
    assert db.is_duplicate_key(RuntimeError("Duplicate key value violates unique constraint"))
    assert db.is_duplicate_key(RuntimeError("UNIQUE constraint failed: change_log.event_id"))
    assert not db.is_duplicate_key(RuntimeError("Object does not exist"))
