"""Advisory retrieval over locally authored incident hypotheses."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

_CONCEPTS = {
    term: category
    for category, terms in {
        "memory": "memoria memory ram rss",
        "disk": "disco disk armazenamento storage volume",
        "latency": "lentidao lento demora atraso latencia latency slow delayed",
        "stall": "travamento travamentos stalled stalls thrashing",
        "contention": "gargalo gargalos contencao concorrencia contention bottleneck",
        "indexing": "indexacao indexar indexador indexing indexer",
        "research": "pesquisa pesquisas research busca buscas",
        "failure": "erro erros falha falhas bug bugs fault failure failures",
        "cost": "custo custos cost costs preco prices",
        "duplicate": "duplicado duplicada duplicados repetido repetida repetidas identical duplicate duplicates",
        "cache": "cache caches caching cached",
        "provider": "provedor provedores provider providers",
        "gateway": "gateway gateways",
        "cpu": "cpu processador processor",
        "iops": "io iops leituraescrita",
        "limit": "limite limites limit limits quota cota",
    }.items()
    for term in terms.split()
}
_STOP = frozenset("a as ao os o de da do das dos para por em um uma e ou com and an the of in on for or to with".split())
_BUILTINS = frozenset(f"sentence_transformers.models.{part}" for part in ("Transformer", "Pooling", "Normalize"))
_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MAX_ROWS = 100


def _features(text: str) -> dict[str, float]:
    ascii_friendly = "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                             if not unicodedata.combining(char))
    terms = re.findall(r"[^\W_]+", ascii_friendly.replace("i/o", " io "))
    features = {}
    for term in set(terms) - _STOP:
        if len(term) < 2:
            continue
        concept = _CONCEPTS.get(term, term)
        features[f"word:{concept}"] = 1.0
        padded = f"^{concept}$"
        for start in range(len(padded) - 2):
            features[f"gram:{padded[start:start + 3]}"] = 0.3
    return features


def _lexical_scores(query: str, texts: list[str]) -> list[float]:
    base = _features(query)
    norm = math.sqrt(sum(weight * weight for weight in base.values()))
    scores = []
    for text in texts:
        features = _features(text)
        dot = sum(value * features.get(key, 0) for key, value in base.items())
        other_norm = math.sqrt(sum(weight * weight for weight in features.values()))
        scores.append(dot / (norm * other_norm) if norm and other_norm else 0.0)
    return scores


def _record(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    code, note, day, severity = (item.get(key) for key in ("code", "key_text", "last_day", "severity"))
    last_seen, occurrences = item.get("last_seen"), item.get("occurrences")
    if (not isinstance(code, str) or not _CODE.fullmatch(code)
            or not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000
            or not isinstance(day, str) or not _DATE.fullmatch(day)
            or severity not in ("info", "warning", "critical", "error")
            or type(last_seen) not in (int, float) or not 0 <= last_seen <= 10**12
            or type(occurrences) is not int or not 0 < occurrences <= 10**12):
        return None
    return {"code": code, "key_text": " ".join(note.split()), "last_day": day,
            "severity": severity, "last_seen": last_seen, "occurrences": occurrences}


def _local_file(directory: Path, name: str, size_limit: int) -> Path:
    file = (directory / name).resolve(strict=True)
    if not file.is_relative_to(directory) or not file.is_file() or not 0 < file.stat().st_size <= size_limit:
        raise ValueError("Invalid model file")
    return file


def _local_model(path: Path) -> Path:
    if not path.is_absolute() or path.anchor.startswith(("//", "\\\\")):
        raise ValueError("Model must have an absolute local path")
    model = path.resolve(strict=True)
    if not model.is_dir() or model.anchor.startswith(("//", "\\\\")):
        raise ValueError("Model must be a local directory")
    _local_file(model, "config.json", 65_536)
    _local_file(model, "model.safetensors", 1024**3)
    manifest = _local_file(model, "modules.json", 65_536)
    with manifest.open("rb") as stream:
        payload = stream.read(65_537)
    if len(payload) > 65_536:
        raise ValueError("Model manifest exceeds its size bound")
    try:
        modules = json.loads(payload)
    except RecursionError:
        raise ValueError("Unsupported local model layout") from None
    if not isinstance(modules, list) or not 2 <= len(modules) <= 3:
        raise ValueError("Unsupported local model layout")
    types = []
    for module in modules:
        if not isinstance(module, dict) or not isinstance(module.get("type"), str) or module["type"] not in _BUILTINS:
            raise ValueError("Unsupported model module")
        kind = module["type"].rsplit(".", 1)[1]
        relative = module.get("path")
        if not isinstance(relative, str) or (relative and not re.fullmatch(r"[0-9]+_[A-Za-z0-9_]+", relative)):
            raise ValueError("Unsupported model module location")
        if kind == "Transformer" and relative:
            raise ValueError("Transformer weights belong in the model root")
        if relative:
            submodule = (model / relative).resolve(strict=True)
            if not submodule.is_dir() or not submodule.is_relative_to(model):
                raise ValueError("Model module must remain inside the local directory")
        types.append(kind)
    if types not in (["Transformer", "Pooling"], ["Transformer", "Pooling", "Normalize"]):
        raise ValueError("Unsupported model pipeline")
    return model


def _neural_scores(path: Path, query: str, texts: list[str]) -> tuple[list[float] | None, str | None]:
    try:
        local_model = _local_model(path)
    except (ValueError, OSError, RuntimeError, RecursionError):
        return None, "invalid_local_model"
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None, "sentence_transformers_unavailable"
    except Exception:
        return None, "neural_backend_failed"
    try:
        engine = SentenceTransformer(str(local_model), device="cpu", local_files_only=True, trust_remote_code=False,
                                     token=False, model_kwargs={"use_safetensors": True})
        engine.max_seq_length = min(engine.max_seq_length or 192, 192)
        output = engine.encode([query, *texts], batch_size=8, show_progress_bar=False, normalize_embeddings=True)
        vectors = [list(map(float, row)) for row in output]
        if len(vectors) != len(texts) + 1 or not 1 <= len(vectors[0]) <= 8192:
            raise ValueError("Unexpected embedding dimensions")
        base = vectors[0]
        scores = []
        for vector in vectors[1:]:
            if len(vector) != len(base) or not all(math.isfinite(value) for value in vector):
                raise ValueError("Invalid neural embedding")
            score = sum(a * b for a, b in zip(base, vector))
            if not math.isfinite(score):
                raise ValueError("Invalid neural score")
            scores.append(score)
        return scores, None
    except Exception:
        # Dependency/loader exceptions can contain operator paths or host details.
        return None, "neural_backend_failed"


def match_incidents(query: str, incidents: list[dict], *, model_path: Path | None = None, limit: int = 5) -> dict:
    """Rank up to 100 supplied hypotheses. Scores are similarity, not fault probabilities."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 400:
        raise ValueError("Incident query must contain between 1 and 400 characters")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Incident match limit must be between 1 and 20")
    if not isinstance(incidents, list) or model_path is not None and not isinstance(model_path, Path):
        raise ValueError("Invalid incident history or local model path")
    clean = query.strip()
    records = [row for item in incidents[:_MAX_ROWS] if (row := _record(item)) is not None]
    texts = [f"{row['code']} {row['key_text']}" for row in records]
    backend, fallback = "lexical", None
    scores = None
    if model_path is not None and records:
        scores, fallback = _neural_scores(model_path, clean, texts)
        if scores is not None:
            backend = "sentence_transformer"
    if scores is None:
        scores = _lexical_scores(clean, texts)
    cutoff = 0.35 if backend == "sentence_transformer" else 0.15
    ranked = [{**row, "score": round(min(score, 1.0), 4)} for row, score in zip(records, scores) if score >= cutoff]
    ranked.sort(key=lambda row: (-row["score"], -row["last_seen"], row["code"]))
    return {"backend": backend, "fallback_reason": fallback, "results": ranked[:limit]}
