"""Hybrid keyword + vector (TF-IDF) search over the NL Q&A database."""

import pickle
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .database import get_conn, get_items_for_index

_vectorizer: Optional[TfidfVectorizer] = None
_matrix = None      # sparse matrix
_ids: list[int] = []
_rec_keys: list[str] = []

CACHE_PATH = Path(__file__).parent / "tfidf_cache.pkl"


def build_index() -> None:
    """Build (or rebuild) the TF-IDF matrix from all items in the DB."""
    global _vectorizer, _matrix, _ids, _rec_keys

    rows = get_items_for_index()
    if not rows:
        return

    _ids = [r["id"] for r in rows]
    _rec_keys = [r["rec_key"] for r in rows]
    texts = [f"{r['question']} {r['answer']}" for r in rows]

    _vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        max_features=80_000,
        sublinear_tf=True,
        min_df=1,
    )
    _matrix = _vectorizer.fit_transform(texts)

    with open(CACHE_PATH, "wb") as f:
        pickle.dump({
            "vectorizer": _vectorizer,
            "matrix": _matrix,
            "ids": _ids,
            "rec_keys": _rec_keys,
            "count": len(rows),
        }, f)


def _ensure_index() -> bool:
    global _vectorizer, _matrix, _ids, _rec_keys
    if _vectorizer is not None:
        return True
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "rb") as f:
                cached = pickle.load(f)
            _vectorizer = cached["vectorizer"]
            _matrix = cached["matrix"]
            _ids = cached["ids"]
            _rec_keys = cached.get("rec_keys", [])
            return True
        except Exception:
            pass
    return False


def _fetch_rows_by_ids(ids: list[int]) -> dict:
    """Return {id: row_dict} for the given IDs."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, rec_key, question, answer, subject, answer_date, answer_lib "
            f"FROM qa_items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def vector_search(query: str, top_k: int = 10) -> list[dict]:
    if not _ensure_index():
        return []
    q_vec = _vectorizer.transform([query])
    scores = cosine_similarity(q_vec, _matrix).flatten()
    top_idx = np.argsort(scores)[::-1][:top_k * 2]
    top_idx = [i for i in top_idx if scores[i] >= 0.01][:top_k]
    id_list = [_ids[i] for i in top_idx]
    rows = _fetch_rows_by_ids(id_list)
    results = []
    for i, idx in enumerate(top_idx):
        row = rows.get(_ids[idx])
        if row:
            results.append({**row, "score": float(scores[idx]), "search_type": "vector"})
    return results


def keyword_search(query: str, top_k: int = 10) -> list[dict]:
    # Escape FTS5 special chars
    safe_q = query.replace('"', '""')
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT q.id, q.rec_key, q.question, q.answer, q.subject,
                   q.answer_date, q.answer_lib,
                   bm25(qa_fts) AS score
            FROM qa_fts
            JOIN qa_items q ON qa_fts.rowid = q.id
            WHERE qa_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (safe_q, top_k)).fetchall()
    return [{**dict(r), "score": abs(float(r["score"])), "search_type": "keyword"}
            for r in rows]


def hybrid_search(query: str, top_k: int = 8) -> list[dict]:
    kw = {r["rec_key"]: r for r in keyword_search(query, top_k * 2)}
    vec = {r["rec_key"]: r for r in vector_search(query, top_k * 2)}

    all_keys = set(kw) | set(vec)
    if not all_keys:
        return []

    kw_max = max((r["score"] for r in kw.values()), default=1) or 1
    vec_max = max((r["score"] for r in vec.values()), default=1) or 1

    merged = []
    for k in all_keys:
        base = kw.get(k) or vec.get(k)
        kw_s = kw[k]["score"] / kw_max if k in kw else 0.0
        vec_s = vec[k]["score"] / vec_max if k in vec else 0.0
        combined = 0.45 * kw_s + 0.55 * vec_s
        merged.append({**base, "combined_score": combined,
                       "search_type": "hybrid" if (k in kw and k in vec) else base["search_type"]})

    merged.sort(key=lambda x: x["combined_score"], reverse=True)
    return merged[:top_k]


def invalidate_cache() -> None:
    global _vectorizer, _matrix, _ids, _rec_keys
    _vectorizer = _matrix = None
    _ids = []
    _rec_keys = []
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
