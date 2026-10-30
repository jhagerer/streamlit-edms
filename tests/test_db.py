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
            tx.run("INSERT INTO sync_state (table_name, seq) VALUES ('x', 1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert fake.rows("SELECT * FROM sync_state WHERE table_name = 'x'") == []


def test_duplicate_key_detection():
    assert db.is_duplicate_key(RuntimeError("Duplicate key value violates unique constraint"))
    assert db.is_duplicate_key(RuntimeError("UNIQUE constraint failed: change_log.event_id"))
    assert not db.is_duplicate_key(RuntimeError("Object does not exist"))
