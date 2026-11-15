"""Local BGE embeddings and a reusable cache for approved construction passages."""
import argparse
from contextlib import closing
from functools import lru_cache
import hashlib
from http.client import HTTPException as DownloadError
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from app.ingest_construction import ROOT


REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
ORIGIN = f"https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/{REVISION}"
# Local filename, published filename, SHA-256.
FILES = (
    ("tokenizer.json", "tokenizer.json", "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    ("model.onnx", "onnx/model_quantized.onnx", "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
)
MODEL_DIR = ROOT / "data" / "models" / "bge-small-zh-v1.5-int8"
CACHE_PATH = ROOT / "data" / "construction_vectors.sqlite3"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
CACHE_FORMAT = (REVISION + ":cls-l2-512-v1\0").encode()


class RetrievalUnavailable(RuntimeError):
    """Actionable error for the CLI and the workbench."""


def matches(path: Path, checksum: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == checksum


def prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, remote, checksum in FILES:
        destination = directory / name
        if matches(destination, checksum):
            continue
        request = Request(f"{ORIGIN}/{remote}?download=true", headers={"User-Agent": "SecureAgent/1"})
        with TemporaryDirectory(dir=directory) as temporary:
            candidate = Path(temporary) / name
            with urlopen(request, timeout=60) as response, candidate.open("wb") as output:
                shutil.copyfileobj(response, output)
            if not matches(candidate, checksum):
                raise RetrievalUnavailable(f"{name} 校验失败，请重新运行模型准备命令。")
            candidate.replace(destination)


class Encoder:
    def __init__(self, directory: Path):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except (ImportError, OSError) as exc:
            raise RetrievalUnavailable("请在当前虚拟环境安装 requirements-semantic.txt 中的 CPU 依赖。") from exc
        if not all(matches(directory / name, checksum) for name, _, checksum in FILES):
            raise RetrievalUnavailable("请先运行 python -m app.semantic_retrieval prepare，准备完整的中文嵌入模型。")
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        options.inter_op_num_threads = 1
        self.np = np
        try:
            self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            self.model = ort.InferenceSession(str(directory / "model.onnx"), sess_options=options,
                                              providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise RetrievalUnavailable("中文嵌入模型加载失败，请重新运行模型准备命令检查 CPU 环境。") from exc

    def encode(self, texts: list[str]):
        try:
            tokens = self.tokenizer.encode_batch(texts)
            arrays = {
                "input_ids": self.np.asarray([t.ids for t in tokens], dtype=self.np.int64),
                "attention_mask": self.np.asarray([t.attention_mask for t in tokens], dtype=self.np.int64),
                "token_type_ids": self.np.asarray([t.type_ids for t in tokens], dtype=self.np.int64),
            }
            hidden, = self.model.run(["last_hidden_state"], arrays)
        except Exception as exc:
            raise RetrievalUnavailable("本地模型编码失败，请检查 CPU 运行环境。") from exc
        cls = hidden[:, 0, :]
        lengths = self.np.linalg.norm(cls, axis=1, keepdims=True)
        if not self.np.all(self.np.isfinite(lengths) & (lengths > 0)):
            raise RetrievalUnavailable("模型生成的向量无效。")
        return cls / lengths


@lru_cache(maxsize=1)
def get_encoder() -> Encoder:
    return Encoder(MODEL_DIR)


def cached_passages(db, encoder, project: str, passages: list[str]):
    """Resolve only this request's eligible texts, even if older vectors remain on disk."""
    db.execute("""CREATE TABLE IF NOT EXISTS vectors (
        project TEXT NOT NULL, digest TEXT NOT NULL, value BLOB NOT NULL,
        PRIMARY KEY(project,digest))""")
    keys = [hashlib.sha256(CACHE_FORMAT + text.encode()).hexdigest() for text in passages]
    unique = dict(zip(keys, passages))
    vectors = {}
    distinct_keys = list(unique)
    for offset in range(0, len(distinct_keys), 200):
        batch = distinct_keys[offset:offset + 200]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(f"SELECT digest,value FROM vectors WHERE project=? AND digest IN ({marks})",
                          (project, *batch))
        for key, blob in rows:
            if len(blob) == 512 * 4:
                vectors[key] = encoder.np.frombuffer(blob, dtype="<f4")

    missing = [key for key in distinct_keys if key not in vectors]
    for offset in range(0, len(missing), 16):
        batch = missing[offset:offset + 16]
        encoded = encoder.encode([unique[key] for key in batch])
        with db:
            for key, vector in zip(batch, encoded, strict=True):
                vectors[key] = vector
                db.execute("INSERT INTO vectors VALUES (?,?,?) ON CONFLICT(project,digest) "
                           "DO UPDATE SET value=excluded.value",
                           (project, key, vector.astype("<f4", copy=False).tobytes()))
    return encoder.np.stack([vectors[key] for key in keys])


def dense_order(project: str, question: str, passages: list[str], limit: int) -> list[int]:
    encoder = get_encoder()
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(CACHE_PATH, timeout=15)) as db:
            matrix = cached_passages(db, encoder, project, passages)
        query = encoder.encode([QUERY_PREFIX + question])[0]
        scores = matrix @ query
    except (OSError, sqlite3.Error) as exc:
        raise RetrievalUnavailable("无法使用本地向量缓存，请检查 data 目录是否可写。") from exc
    return encoder.np.argsort(-scores, kind="stable")[:limit].tolist()


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 CPU 版中文 BGE 嵌入模型")
    parser.add_argument("action", choices=["prepare"])
    parser.parse_args()
    try:
        prepare(MODEL_DIR)
        get_encoder().encode(["模型自检"])
    except (OSError, DownloadError, RetrievalUnavailable) as exc:
        parser.exit(1, f"模型准备失败：{exc}\n")
    print("中文嵌入模型已就绪，CPU 自检通过。")


if __name__ == "__main__":
    main()
