"""Copy LightRAG's extraction and summary cache from the JSON file into PGKVStorage.

Run inside the lightrag image with the new storage env, while the LightRAG server is stopped:
    python copy_llm_cache.py /app/data/rag_storage/kv_store_llm_response_cache.json

Re-ingesting a document whose chunks are unchanged then answers its entity extraction from this
cache instead of the LLM. Query caches are not copied. Copying again overwrites the same keys.
"""
import asyncio
import json
import os
import sys

from lightrag.kg.postgres_impl import PGKVStorage
from lightrag.kg.shared_storage import initialize_share_data
from lightrag.namespace import NameSpace

BATCH = 500


async def main(path):
    with open(path) as handle:
        cache = {key: value for key, value in json.load(handle).items()
                 if key.startswith(("default:extract:", "default:summary:"))}
    initialize_share_data()
    storage = PGKVStorage(
        namespace=NameSpace.KV_STORE_LLM_RESPONSE_CACHE, workspace=os.environ.get("POSTGRES_WORKSPACE", ""),
        global_config={"working_dir": "/tmp/lightrag-copy", "embedding_batch_num": 10}, embedding_func=None)
    await storage.initialize()
    keys = list(cache)
    for start in range(0, len(keys), BATCH):
        await storage.upsert({key: cache[key] for key in keys[start:start + BATCH]})
    await storage.index_done_callback()
    stored = await storage.get_by_ids(keys)
    missing = sum(1 for value in stored if value is None)
    await storage.finalize()
    print(json.dumps({"copied": len(keys), "missing_after_copy": missing}))
    if missing:
        sys.exit(1)


asyncio.run(main(sys.argv[1]))
