from __future__ import annotations

import hashlib


def hash_author(author: str, pepper: str) -> str:
    """One-way hash of an author handle, salted with a local pepper.

    Not reversible; two different peppers produce unrelated hashes for the
    same author, so the pepper itself must stay out of version control.
    """
    return hashlib.sha256(f"{pepper}:{author}".encode("utf-8")).hexdigest()
