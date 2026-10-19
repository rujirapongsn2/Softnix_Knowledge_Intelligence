"""Re-embed LightRAG's NanoVectorDB files with a new embedding model, without re-running LLM extraction.

LightRAG keeps vectors only in vdb_chunks/entities/relationships.json, and every
record stores the exact ``content`` LightRAG embedded, so the graph, KV stores and
LLM cache stay valid when only the vectors are rebuilt.  Run it with LightRAG
stopped, against a copy of rag_storage, then copy the three files back.

    python rebuild_lightrag_vectors.py IN_DIR OUT_DIR --base-url http://host.docker.internal:8090/v1 \
        --model embeddinggemma-2 --dimension 768 [--api-key KEY]
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

NAMESPACES = ("chunks", "entities", "relationships")
BATCH = 16


def embed(texts, args):
    body = json.dumps({"model": args.model, "input": texts, "encoding_format": "float"}).encode()
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {args.api_key}"} if args.api_key else {})}
    request = urllib.request.Request(f"{args.base_url.rstrip('/')}/embeddings", data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response:
        data = sorted(json.load(response)["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in data]


def embed_one_fitting(text, args, shortened):
    # LightRAG's openai binding truncates over-long input the same way before embedding.
    while True:
        try:
            return embed([text], args)[0]
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 413, 500) or len(text) < 200:
                raise
            text = text[: len(text) * 3 // 4]
            shortened.append(len(text))


def embed_batch(texts, args, shortened):
    try:
        return embed(texts, args)
    except urllib.error.HTTPError:
        return [embed_one_fitting(text, args, shortened) for text in texts]


def rebuild(namespace, args):
    path = os.path.join(args.in_dir, f"vdb_{namespace}.json")
    storage = json.load(open(path))
    records = storage["data"]
    contents = [record["content"] for record in records]
    batches = [contents[i:i + BATCH] for i in range(0, len(contents), BATCH)]
    shortened: list[int] = []
    with ThreadPoolExecutor(4) as pool:
        vectors = np.array([v for part in pool.map(lambda b: embed_batch(b, args, shortened), batches) for v in part], dtype=np.float32)
    if vectors.shape != (len(records), args.dimension):
        sys.exit(f"{namespace}: expected {(len(records), args.dimension)} vectors, got {vectors.shape}")
    for record, vector in zip(records, vectors, strict=True):
        record.pop("__vector__", None)
        record["vector"] = base64.b64encode(zlib.compress(vector.astype(np.float16).tobytes())).decode()
    matrix = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    out = {"embedding_dim": args.dimension, "data": records, "matrix": base64.b64encode(matrix.astype(np.float32).tobytes()).decode()}
    target = os.path.join(args.out_dir, f"vdb_{namespace}.json")
    with open(target + ".tmp", "w") as handle:
        json.dump(out, handle, ensure_ascii=False)
    os.replace(target + ".tmp", target)
    print(f"{namespace}: {len(records)} records re-embedded at {args.dimension}d, {len(shortened)} truncation step(s)", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("in_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    for namespace in NAMESPACES:
        rebuild(namespace, args)


if __name__ == "__main__":
    main()
