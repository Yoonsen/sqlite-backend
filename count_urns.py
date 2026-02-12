#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, Optional


def find_table(con: sqlite3.Connection, candidates: Iterable[str]) -> Optional[str]:
    cur = con.cursor()
    for name in candidates:
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        if row:
            return name
    return None


def count_urns(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    table = find_table(con, ["urns", "tokens", "unigrams", "ngrams"])
    if table is None:
        con.close()
        raise RuntimeError("No urns/tokens/unigrams/ngrams table found.")
    if table == "urns":
        row = con.execute("SELECT COUNT(*) FROM urns").fetchone()
    elif table == "tokens":
        row = con.execute("SELECT COUNT(DISTINCT book_id) FROM tokens").fetchone()
    else:
        row = con.execute(f"SELECT COUNT(DISTINCT book_id) FROM {table}").fetchone()
    con.close()
    return int(row[0]) if row else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Count book_id/urns in shard DBs.")
    parser.add_argument("dbs", nargs="+", help="Shard DB paths")
    args = parser.parse_args()
    for db in args.dbs:
        path = Path(db)
        if not path.exists():
            print(f"{db}\tMISSING")
            continue
        try:
            cnt = count_urns(db)
            print(f"{db}\t{cnt}")
        except Exception as exc:
            print(f"{db}\tERROR\t{exc}")


if __name__ == "__main__":
    main()
