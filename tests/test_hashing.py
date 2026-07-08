from __future__ import annotations

from radar.hashing import hash_author


def test_hash_is_deterministic():
    assert hash_author("alice", "pepper1") == hash_author("alice", "pepper1")


def test_hash_is_pepper_sensitive():
    assert hash_author("alice", "pepper1") != hash_author("alice", "pepper2")


def test_hash_is_author_sensitive():
    assert hash_author("alice", "pepper1") != hash_author("bob", "pepper1")


def test_hash_output_is_fixed_length_hex():
    digest = hash_author("alice", "pepper1")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex
