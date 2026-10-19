#!/usr/bin/env python3
"""rapidboon datasets: rapidboon.dataset.json, and the one SQLite file built from its data files.

A dataset endpoint is a folder with rapidboon.dataset.json and its data:

    {"rapidboon_dataset": 1, "id": "city-weather", "name": "City weather", "version": "1.0.0",
     "description": "Daily readings", "price_per_query_usd": 0.001,
     "tables": [{"name": "readings", "source": "data/readings.csv", "indexes": [["city"]],
                 "columns": [{"name": "city", "type": "text"}, {"name": "temp_c", "type": "real"}]}],
     "policy": {"columns": {"readings": ["city", "temp_c"]}, "max_rows": 1000,
                "aggregate_only": false, "min_group_size": 10, "accounts": []},
     "release": "data-v1", "prepare": "python export.py"}

Column types are text, integer and real. A source is CSV (with a header row), JSON (an array of objects),
JSON Lines (.jsonl, .ndjson) or Parquet (needs pyarrow); columns are matched by name and extra ones are left
out. `prepare`, if set, is a command the build runs in the folder first, to write the sources from data kept
elsewhere (an export from Firestore, Postgres, an API...); in CI it runs on every publish. A source still missing
is downloaded from the repo's GitHub Release `release` with the gh CLI (in CI, GH_TOKEN is the job's token). The policy says what consumers may see: the shared columns of each
shared table, rows per answer, aggregate-only answers over groups of at least min_group_size rows, and an
optional allow-list of accounts. The gateway enforces it on every query and checks these rules again.

The build writes .rapidboon/dataset.sqlite: STRICT tables, their indexes, and _rapidboon_dataset holding the
metadata without the sources (nor release and prepare), plus content_sha256 (of the metadata and the source bytes, so the same data
rebuilt by another SQLite version publishes as "unchanged").

    python rbdataset.py build DIR    build; prints the steps as JSON, exit 1 if one failed (2: not built here)

The SDK's tools/rbdataset.py and rapidboon-cli's rapidboon/dataset.py are the same file. Standard library
only (pyarrow only for Parquet).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

FILE = "rapidboon.dataset.json"
OUT = Path(".rapidboon") / "dataset.sqlite"
META_TABLE = "_rapidboon_dataset"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL"}
ROW_CAP = 10_000
BATCH = 10_000


def check(meta) -> None:
    """ValueError naming the first problem; the gateway checks the same rules when it is published."""
    if not isinstance(meta, dict) or meta.get("rapidboon_dataset") != 1:
        raise ValueError("rapidboon_dataset must be 1")
    for key, pattern in (("id", ID_RE), ("version", VERSION_RE)):
        if not isinstance(meta.get(key), str) or not pattern.fullmatch(meta[key]):
            raise ValueError(f"{key}: {meta.get(key)!r} is not valid (an id is lowercase letters, digits and "
                             "dashes; a version looks like 1.2.3)")
    if not isinstance(meta.get("name"), str) or not 0 < len(meta["name"]) <= 80:
        raise ValueError("name must be 1-80 characters")
    if not isinstance(meta.get("description", ""), str) or len(meta.get("description", "")) > 500:
        raise ValueError("description must be at most 500 characters")
    price = meta.get("price_per_query_usd", 0)
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or not 0 <= price <= 10:
        raise ValueError("price_per_query_usd must be a number between 0 and 10 (USD)")
    if not isinstance(meta.get("release", ""), str):
        raise ValueError("release must be the tag of a GitHub Release")
    if not isinstance(meta.get("prepare", ""), str) or len(meta.get("prepare", "")) > 500:
        raise ValueError("prepare must be a command of at most 500 characters")
    tables = meta.get("tables")
    if not isinstance(tables, list) or not 0 < len(tables) <= 32:
        raise ValueError("tables must list 1-32 tables")
    declared = {}
    for t in tables:
        name = t.get("name") if isinstance(t, dict) else None
        if not isinstance(name, str) or not TABLE_RE.fullmatch(name) or name in declared:
            raise ValueError(f"table {name!r} needs a unique lowercase name (letters, digits, _)")
        source = t.get("source")
        if not isinstance(source, str) or not source or re.match(r"[a-zA-Z]:|[\\/]", source) or ".." in re.split(r"[\\/]", source):
            raise ValueError(f"{name}: source must be a data file's path inside the endpoint folder")
        columns = t.get("columns")
        if not isinstance(columns, list) or not 0 < len(columns) <= 128:
            raise ValueError(f"{name}: columns must list 1-128 columns")
        cols = set()
        for c in columns:
            cname = c.get("name") if isinstance(c, dict) else None
            if not isinstance(cname, str) or not COLUMN_RE.fullmatch(cname) or cname in cols:
                raise ValueError(f"{name}: column {cname!r} needs a unique lowercase name")
            if c.get("type") not in TYPES:
                raise ValueError(f"{name}.{cname}: type must be one of {', '.join(TYPES)}")
            cols.add(cname)
        indexes = t.get("indexes", [])
        if not isinstance(indexes, list) or any(not isinstance(i, list) or not i or any(x not in cols for x in i)
                                                for i in indexes):
            raise ValueError(f"{name}: indexes must be lists of its column names")
        declared[name] = cols
    policy = meta.get("policy", {})
    shared = policy.get("columns", {}) if isinstance(policy, dict) else None
    if not isinstance(shared, dict):
        raise ValueError("policy.columns must map each shared table to its shared columns")
    for name, cols in shared.items():
        if name not in declared:
            raise ValueError(f"policy.columns: there is no table {name}")
        if not isinstance(cols, list) or not cols or any(c not in declared[name] for c in cols) or len(set(cols)) != len(cols):
            raise ValueError(f"policy.columns.{name} must list some of its columns, each once")
    max_rows, group = policy.get("max_rows", 1000), policy.get("min_group_size", 10)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 0 < max_rows <= ROW_CAP:
        raise ValueError(f"policy.max_rows must be an integer between 1 and {ROW_CAP}")
    if not isinstance(policy.get("aggregate_only", False), bool):
        raise ValueError("policy.aggregate_only must be true or false")
    if isinstance(group, bool) or not isinstance(group, int) or not 2 <= group <= 100_000:
        raise ValueError("policy.min_group_size must be an integer between 2 and 100000")
    accounts = policy.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > 100 or any(not isinstance(a, str) or not ID_RE.fullmatch(a)
                                                                    for a in accounts):
        raise ValueError("policy.accounts must list at most 100 account ids")


def validate(meta) -> list[str]:
    try:
        check(meta)
        return []
    except ValueError as e:
        return [str(e)]


def load(root: str | Path) -> dict:
    return json.loads((Path(root) / FILE).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

class NotBuiltHere(Exception):
    pass


def convert(value, ctype: str):
    """A source value as the column's type; ValueError if it isn't one."""
    if value is None or value == "":
        return None
    if ctype == "text":
        if isinstance(value, str):
            return value
        return json.dumps(value) if isinstance(value, (bool, list, dict)) else str(value)
    if isinstance(value, (bool, list, dict)):
        raise ValueError
    if ctype == "integer":
        if isinstance(value, str):
            return int(value.strip())
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        return int(value)
    number = float(value.strip()) if isinstance(value, str) else float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def records(path: Path, names: list[str]):
    """Each row of a source as a dict (or a tuple in `names` order for Parquet), streamed."""
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t" if suffix == ".tsv" else ",")
            missing = [n for n in names if n not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"no column {', '.join(missing)} in its header")
            yield from reader
    elif suffix in (".jsonl", ".ndjson"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("a .json source is an array of objects (use .jsonl for big files)")
        yield from data
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise NotBuiltHere("Parquet sources need pyarrow: python -m pip install pyarrow") from None
        for batch in pq.ParquetFile(path).iter_batches(columns=names, batch_size=BATCH):
            columns = batch.to_pydict()
            yield from (dict(zip(names, row)) for row in zip(*(columns[n] for n in names)))
    else:
        raise ValueError("a source is .csv, .tsv, .json, .jsonl, .ndjson or .parquet")


def prepare(root: Path, meta: dict) -> list[dict]:
    """Run the dataset's prepare command in its folder: it writes the sources (an export from a database, say)."""
    if not meta.get("prepare"):
        return []
    r = subprocess.run(meta["prepare"], shell=True, cwd=root, capture_output=True, text=True, errors="replace")
    return [{"step": f"prepare: {meta['prepare']}", "status": "ok" if r.returncode == 0 else "failed",
             "detail": (r.stdout + r.stderr).strip()[-800:]}]


def fetch(root: Path, meta: dict) -> list[dict]:
    """Download the sources missing from the folder from the repo's GitHub Release `release` (gh CLI)."""
    missing = [t["source"] for t in meta["tables"] if not (root / t["source"]).is_file()]
    if not missing:
        return []
    if not meta.get("release"):
        return [{"step": "sources", "status": "failed", "detail": f"missing {', '.join(missing)} (and no release "
                 "to download them from: commit them, use Git LFS, or name a GitHub Release in \"release\")"}]
    gh = shutil.which("gh")
    if not gh:
        return [{"step": f"download from release {meta['release']}", "status": "not built here",
                 "detail": f"needs gh (https://cli.github.com) to download {', '.join(missing)}"}]
    results = []
    for source in missing:
        dest = (root / source).parent
        dest.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([gh, "release", "download", meta["release"], "--pattern", Path(source).name, "--dir", str(dest),
                            "--clobber"], cwd=root, capture_output=True, text=True)
        ok = r.returncode == 0 and (root / source).is_file()
        results.append({"step": f"download {source} from release {meta['release']}", "status": "ok" if ok else "failed",
                        "detail": "" if ok else (r.stderr or r.stdout).strip()[-800:] or "the release has no such asset"})
    return results


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------

def load_table(con: sqlite3.Connection, root: Path, t: dict, digest) -> str:
    """Create table t, fill it from its source and index it; returns what to report."""
    path, names = root / t["source"], [c["name"] for c in t["columns"]]
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"source {t['source']} is outside the endpoint folder")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    con.execute(f'CREATE TABLE "{t["name"]}" ('
                + ", ".join(f'"{c["name"]}" {TYPES[c["type"]]}' for c in t["columns"]) + ") STRICT")
    insert = f'INSERT INTO "{t["name"]}" VALUES ({", ".join("?" * len(names))})'
    rows, count, extra = [], 0, []
    for count, record in enumerate(records(path, names), 1):
        if not isinstance(record, dict):
            raise ValueError(f"row {count} is not an object")
        if count == 1:
            extra = sorted(record.keys() - set(names))
        values = []
        for c in t["columns"]:
            try:
                values.append(convert(record.get(c["name"]), c["type"]))
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f"row {count}: {c['name']} = {record.get(c['name'])!r} is not {c['type']}") from None
        rows.append(values)
        if len(rows) == BATCH:
            con.executemany(insert, rows)
            rows = []
    con.executemany(insert, rows)
    for i, columns in enumerate(t.get("indexes", [])):
        con.execute(f'CREATE INDEX "{t["name"]}_{i}" ON "{t["name"]}" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in columns)})')
    return f"{count} rows" + (f"; left out its other columns: {', '.join(extra)}" if extra else "")


def build(root: str | Path) -> list[dict]:
    """Build .rapidboon/dataset.sqlite from the sources: [{step, status, detail}] like `rapidboon build`."""
    root = Path(root).resolve()
    try:
        meta = load(root)
        check(meta)
    except (OSError, ValueError) as e:
        return [{"step": f"check {FILE}", "status": "failed", "detail": str(e)}]
    results = [{"step": f"check {FILE}", "status": "ok", "detail": ""}]
    for step in (prepare, fetch):
        results += step(root, meta)
        if any(r["status"] != "ok" for r in results):
            return results
    out = root / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / ".gitignore").write_text("*\n")  # build output: never committed, wherever the folder is
    temp = out.with_suffix(".tmp")
    temp.unlink(missing_ok=True)
    digest = hashlib.sha256(json.dumps(meta, sort_keys=True).encode())
    con = sqlite3.connect(temp)
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        for t in meta["tables"]:
            step = f"{t['name']} from {t['source']}"
            try:
                results.append({"step": step, "status": "ok", "detail": load_table(con, root, t, digest)})
            except NotBuiltHere as e:
                results.append({"step": step, "status": "not built here", "detail": str(e)})
                return results
            except Exception as e:  # a bad value, an unreadable file, or pyarrow's own errors
                detail = str(e) if isinstance(e, (ValueError, OSError)) else f"{type(e).__name__}: {e}"
                results.append({"step": step, "status": "failed", "detail": detail})
                return results
        stored = {k: v for k, v in meta.items() if k not in ("release", "prepare")}
        stored["tables"] = [{k: v for k, v in t.items() if k != "source"} for t in meta["tables"]]
        stored["content_sha256"] = digest.hexdigest()
        con.execute(f"CREATE TABLE {META_TABLE} (json TEXT NOT NULL)")
        con.execute(f"INSERT INTO {META_TABLE} VALUES (?)", (json.dumps(stored),))
        con.commit()
    finally:
        con.close()
        if results[-1]["status"] != "ok":
            temp.unlink(missing_ok=True)
    try:
        os.replace(temp, out)
    except OSError as e:
        temp.unlink(missing_ok=True)
        return results + [{"step": "write", "status": "failed", "detail": f"{e} (is {OUT.as_posix()} open elsewhere?)"}]
    results.append({"step": "write", "status": "ok",
                    "detail": f"{OUT.as_posix()}, {out.stat().st_size / 1e6:.1f} MB (publish limit: 500 MB by default)"})
    return results


def main(argv: list[str] | None = None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "build":
        sys.exit("usage: python rbdataset.py build DIR")
    results = build(args[1])
    print(json.dumps(results, indent=2))
    statuses = {r["status"] for r in results}
    sys.exit(1 if "failed" in statuses else 2 if "not built here" in statuses else 0)


if __name__ == "__main__":
    main()
