import os
#!/usr/bin/env python3
"""
Embedding service for the memory system (ONNX Runtime version).

HTTP server that provides:
- POST /embed — embed one or more texts
- POST /similarity — compute cosine similarity between texts
- POST /search — find most similar memories from a list
- POST /batch-embed — embed many texts efficiently (base64 output)
- GET /health — health check

Uses BGE-small-en-v1.5 via ONNX Runtime (local, 384 dimensions, ~300MB RAM).
Runs on 127.0.0.1:8896 (host-only, not exposed).
"""

import json
import sys
import time
import numpy as np
import onnxruntime as ort
from http.server import HTTPServer, BaseHTTPRequestHandler
from transformers import AutoTokenizer

MODEL_DIR = os.environ.get("MODEL_DIR", "/app/models/bge-small-en-v1.5")
ONNX_PATH = MODEL_DIR + "/onnx/model.onnx"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8896

tokenizer = None
session = None


def get_model():
    global tokenizer, session
    if session is None:
        print(f"Loading {MODEL_NAME} (ONNX)...", flush=True)
        start = time.time()
        tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 2
        session = ort.InferenceSession(ONNX_PATH, opts, providers=["CPUExecutionProvider"])
        print(f"Model loaded in {time.time()-start:.1f}s (ONNX Runtime)", flush=True)
    return tokenizer, session


def embed_texts(texts):
    """Embed a list of texts, returns numpy array of normalized embeddings."""
    tok, sess = get_model()
    encoded = tok(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
    inputs = {
        "input_ids": encoded["input_ids"].astype(np.int64),
        "attention_mask": encoded["attention_mask"].astype(np.int64),
    }
    # Add token_type_ids if the model expects it
    input_names = [i.name for i in sess.get_inputs()]
    if "token_type_ids" in input_names:
        inputs["token_type_ids"] = encoded.get("token_type_ids", np.zeros_like(encoded["input_ids"])).astype(np.int64)

    outputs = sess.run(None, inputs)
    # outputs[0] is last_hidden_state: (batch, seq_len, hidden_size)
    # Mean pooling with attention mask
    token_embeddings = outputs[0]
    mask = encoded["attention_mask"][..., np.newaxis].astype(np.float32)
    summed = np.sum(token_embeddings * mask, axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    embeddings = summed / counts
    # L2 normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.clip(norms, 1e-9, None)
    embeddings = embeddings / norms
    return embeddings


def cosine_similarity(a, b):
    """Cosine similarity between two vectors (assumes normalized)."""
    return float(np.dot(a, b))


class EmbeddingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/health":
            self._send_json({
                "status": "ok",
                "model": MODEL_NAME,
                "runtime": "onnxruntime",
                "dimensions": 384,
                "model_loaded": session is not None,
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            data = self._read_json()

            if self.path == "/embed":
                texts = data.get("texts", [])
                if isinstance(texts, str):
                    texts = [texts]
                if not texts:
                    return self._send_json({"error": "missing texts"}, 400)
                embeddings = embed_texts(texts)
                self._send_json({
                    "embeddings": embeddings.tolist(),
                    "dimensions": 384,
                    "count": len(texts),
                })

            elif self.path == "/similarity":
                text_a = data.get("a", "")
                text_b = data.get("b", "")
                if not text_a or not text_b:
                    return self._send_json({"error": "missing a or b"}, 400)
                embs = embed_texts([text_a, text_b])
                sim = cosine_similarity(embs[0], embs[1])
                self._send_json({"similarity": sim})

            elif self.path == "/search":
                query = data.get("query", "")
                candidates = data.get("candidates", [])
                top_k = data.get("top_k", 10)
                if not query or not candidates:
                    return self._send_json({"error": "missing query or candidates"}, 400)

                texts = [c["text"] for c in candidates]
                all_texts = [query] + texts
                embs = embed_texts(all_texts)
                query_emb = embs[0]
                candidate_embs = embs[1:]

                scores = []
                for i, c_emb in enumerate(candidate_embs):
                    sim = cosine_similarity(query_emb, c_emb)
                    scores.append({"id": candidates[i]["id"], "similarity": sim})

                scores.sort(key=lambda x: x["similarity"], reverse=True)
                self._send_json({"results": scores[:top_k]})

            elif self.path == "/batch-embed":
                texts = data.get("texts", [])
                ids = data.get("ids", [])
                if not texts:
                    return self._send_json({"error": "missing texts"}, 400)
                import base64
                embeddings = embed_texts(texts)
                results = []
                for i, emb in enumerate(embeddings):
                    blob = emb.astype(np.float32).tobytes()
                    results.append({
                        "id": ids[i] if i < len(ids) else i,
                        "embedding_b64": base64.b64encode(blob).decode(),
                    })
                self._send_json({"results": results, "count": len(results)})

            else:
                self._send_json({"error": "not found"}, 404)

        except Exception as e:
            self._send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    get_model()
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), EmbeddingHandler)
    print(f"Embedding service running on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"Model: {MODEL_NAME} (384 dimensions, ONNX Runtime)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
        server.server_close()
