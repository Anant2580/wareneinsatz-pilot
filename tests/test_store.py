"""Unit tests for the content-addressed document store."""

import hashlib

from wareneinsatz.ingest.store import DocumentStore


def test_files_are_stored_under_their_hash(tmp_path):
    store = DocumentStore(tmp_path)
    content = b"<invoice/>"
    sha256 = hashlib.sha256(content).hexdigest()

    relative = store.save(content, sha256, ".xml")

    assert relative == f"{sha256[:2]}/{sha256}.xml"
    assert (tmp_path / relative).read_bytes() == content
    assert store.open(relative) == content


def test_saving_twice_is_harmless_and_leaves_no_temp_files(tmp_path):
    store = DocumentStore(tmp_path)
    content = b"%PDF-1.7 ..."
    sha256 = hashlib.sha256(content).hexdigest()

    first = store.save(content, sha256, ".pdf")
    second = store.save(content, sha256, ".pdf")

    assert first == second
    assert [p.name for p in (tmp_path / sha256[:2]).iterdir()] == [f"{sha256}.pdf"]
