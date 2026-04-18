"""Disk cache for model generations keyed by (model_name, prompt_hash, temperature, seed, max_tokens).

This is the most critical module. Every generation must go through the cache so
experiments are resumable and rerunnable without GPU time.
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional


_CACHE_DIR = Path("results/cache")
_DB_PATH = _CACHE_DIR / "generations.db"
_lock = threading.Lock()


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS generations (
            cache_key TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            temperature REAL NOT NULL,
            seed INTEGER NOT NULL,
            max_tokens INTEGER NOT NULL,
            output TEXT NOT NULL,
            token_log_probs TEXT,
            n_tokens INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    conn.commit()
    return conn


_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _ensure_db(_DB_PATH)
    return _conn


def _make_key(
    model_name: str,
    prompt: str,
    temperature: float,
    seed: int,
    max_tokens: int,
) -> tuple[str, str]:
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    raw = f"{model_name}|{prompt_hash}|{temperature:.4f}|{seed}|{max_tokens}"
    cache_key = hashlib.sha256(raw.encode()).hexdigest()
    return cache_key, prompt_hash


def get(
    model_name: str,
    prompt: str,
    temperature: float,
    seed: int,
    max_tokens: int,
) -> Optional[dict]:
    """Return cached generation dict or None if not cached."""
    cache_key, _ = _make_key(model_name, prompt, temperature, seed, max_tokens)
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT output, token_log_probs, n_tokens FROM generations WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    token_log_probs = json.loads(row[1]) if row[1] else None
    return {"output": row[0], "token_log_probs": token_log_probs, "n_tokens": row[2]}


def put(
    model_name: str,
    prompt: str,
    temperature: float,
    seed: int,
    max_tokens: int,
    output: str,
    n_tokens: int,
    token_log_probs: Optional[list[float]] = None,
) -> None:
    """Store a generation in the cache."""
    cache_key, prompt_hash = _make_key(model_name, prompt, temperature, seed, max_tokens)
    token_log_probs_json = json.dumps(token_log_probs) if token_log_probs else None
    with _lock:
        conn = _get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO generations
               (cache_key, model_name, prompt_hash, temperature, seed, max_tokens,
                output, token_log_probs, n_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cache_key,
                model_name,
                prompt_hash,
                temperature,
                seed,
                max_tokens,
                output,
                token_log_probs_json,
                n_tokens,
            ),
        )
        conn.commit()


def cache_size() -> int:
    """Return the number of cached generations."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM generations").fetchone()
    return row[0] if row else 0


def set_cache_dir(path: str | Path) -> None:
    """Override the cache directory (useful for Colab where /content has fast storage)."""
    global _DB_PATH, _conn
    _DB_PATH = Path(path) / "generations.db"
    _conn = None  # force reconnect on next access
