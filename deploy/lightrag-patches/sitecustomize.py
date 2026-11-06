"""Fixes for the pinned LightRAG 1.5.4 image, loaded by Python at startup via PYTHONPATH.

Keyed-lock deadlock: LightRAG truncates entity names to 512 bytes after it drops
self-loop relations, so two long Thai names that share their first ~170 characters
become one name and their relation becomes X->X. The relation merge then takes the
keyed lock with keys [X, X]; the per-key lock is not reentrant, so the second
acquire waits on the first forever and the pipeline stays busy, refusing every
new document with 409. Deduplicating the keys lets the merge skip the self-loop
as it already does for src == tgt.

Python only warns about an exception raised here, so a missing target exits instead:
a LightRAG upgrade cannot drop the fix silently.
"""

try:
    from lightrag.kg import shared_storage

    _context = shared_storage._KeyedLockContext
    _original_init = _context.__init__
except (ImportError, AttributeError) as exc:
    raise SystemExit(f"deploy/lightrag-patches: keyed-lock patch target missing: {exc}") from exc


def _init_with_unique_keys(self, parent, namespace, keys, enable_logging):
    _original_init(self, parent, namespace, list(dict.fromkeys(keys)), enable_logging)


_context.__init__ = _init_with_unique_keys
