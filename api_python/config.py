from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class AppConfig:
    postings_dbs: List[str]
    sidecar_dbs: Optional[List[str]]
    words_db: Optional[str]
    imagination_db: Optional[str]
    annotation_registry_db: Optional[str]
    annotation_base_dir: Optional[str]
    ext_path: str
    default_schema: str


def load_config() -> AppConfig:
    cfg_path = os.environ.get("POSTINGS_CONFIG", "").strip()
    if not cfg_path:
        raise RuntimeError("POSTINGS_CONFIG is not set.")
    path = Path(cfg_path)
    data = json.loads(path.read_text())
    postings_dbs = data.get("postings_dbs") or []
    if not postings_dbs:
        raise RuntimeError("postings_dbs is required in config.")
    sidecar_dbs = data.get("sidecar_dbs") or None
    words_db = data.get("words_db") or None
    imagination_db = data.get("imagination_db") or os.environ.get("POSTINGS_IMAGINATION_DB")
    annotation_registry_db = data.get("annotation_registry_db") or None
    annotation_base_dir = data.get("annotation_base_dir") or None
    for path in postings_dbs:
        if not Path(path).exists():
            raise RuntimeError(f"postings_db not found: {path}")
    if sidecar_dbs is not None:
        if len(sidecar_dbs) != len(postings_dbs):
            raise RuntimeError("sidecar_dbs must have same length as postings_dbs.")
        for path in sidecar_dbs:
            if not Path(path).exists():
                raise RuntimeError(f"sidecar_db not found: {path}")
    if words_db and not Path(words_db).exists():
        raise RuntimeError(f"words_db not found: {words_db}")
    if imagination_db and not Path(imagination_db).exists():
        # Optional: Print warning or handle more strictly? Let's keep it as is.
        pass
    if annotation_registry_db and not Path(annotation_registry_db).exists():
        raise RuntimeError(f"annotation_registry_db not found: {annotation_registry_db}")
    if annotation_base_dir and not Path(annotation_base_dir).exists():
        raise RuntimeError(f"annotation_base_dir not found: {annotation_base_dir}")
    ext_path = data.get("ext_path") or ""
    default_schema = data.get("default_schema") or "unigrams"
    return AppConfig(
        postings_dbs=postings_dbs,
        sidecar_dbs=sidecar_dbs,
        words_db=words_db,
        imagination_db=imagination_db,
        annotation_registry_db=annotation_registry_db,
        annotation_base_dir=annotation_base_dir,
        ext_path=ext_path,
        default_schema=default_schema,
    )
