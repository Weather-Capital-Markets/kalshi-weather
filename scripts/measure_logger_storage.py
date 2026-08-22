"""Read-only logger storage analysis for VPS or laptop.

Walks data/raw JSONL.gz captures, reports bytes by category, gzip ratios,
and consecutive-payload redundancy. Does not modify capture or storage format.

Usage:
  python scripts/measure_logger_storage.py --data-dir data/raw
  python scripts/measure_logger_storage.py --data-dir data/raw --redundancy-sample 500
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ingestion.writer import read_jsonl_gz


CATEGORIES = (
    "orderbook",
    "markets",
    "trades",
    "pm_orderbook",
    "pm_markets",
)


@dataclass
class CategoryStats:
    files: int = 0
    records: int = 0
    bytes_on_disk: int = 0
    uncompressed_bytes: int = 0


@dataclass
class RedundancyStats:
    pairs: int = 0
    payload_identical: int = 0
    envelope_identical: int = 0


def _record_line_bytes(record: dict[str, Any]) -> int:
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    return len(line.encode("utf-8"))


def _payload_bytes(record: dict[str, Any]) -> bytes:
    payload = record.get("payload")
    if payload is None:
        return b""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()


def scan_data_dir(data_dir: Path) -> dict[str, CategoryStats]:
    stats: dict[str, CategoryStats] = {cat: CategoryStats() for cat in CATEGORIES}
    other_bytes = 0
    other_files = 0

    if not data_dir.is_dir():
        return stats

    for path in sorted(data_dir.rglob("*.jsonl.gz")):
        rel_parts = path.relative_to(data_dir).parts
        if len(rel_parts) < 3:
            other_files += 1
            other_bytes += path.stat().st_size
            continue
        category = rel_parts[1]
        if category not in stats:
            other_files += 1
            other_bytes += path.stat().st_size
            continue
        cat = stats[category]
        cat.files += 1
        on_disk = path.stat().st_size
        cat.bytes_on_disk += on_disk
        records = read_jsonl_gz(path)
        cat.records += len(records)
        for record in records:
            cat.uncompressed_bytes += _record_line_bytes(record)

    if other_files:
        stats["_other"] = CategoryStats(files=other_files, bytes_on_disk=other_bytes)
    return stats


def redundancy_for_category(
    data_dir: Path,
    category: str,
    *,
    max_pairs: int,
) -> RedundancyStats:
    result = RedundancyStats()
    if not data_dir.is_dir():
        return result

    for path in sorted(data_dir.rglob(f"*/{category}/*.jsonl.gz")):
        records = read_jsonl_gz(path)
        prev_payload: bytes | None = None
        prev_line: bytes | None = None
        for record in records:
            payload_b = _payload_bytes(record)
            line_b = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
            if prev_payload is not None:
                result.pairs += 1
                if payload_b == prev_payload:
                    result.payload_identical += 1
                if line_b == prev_line:
                    result.envelope_identical += 1
                if result.pairs >= max_pairs:
                    return result
            prev_payload = payload_b
            prev_line = line_b
    return result


def batch_gzip_ratio_sample(data_dir: Path, category: str, sample_lines: int = 200) -> float | None:
    """Compare summed per-file on-disk bytes vs one gzip blob for N lines."""
    if not data_dir.is_dir():
        return None
    lines: list[str] = []
    per_member_sum = 0
    for path in sorted(data_dir.rglob(f"*/{category}/*.jsonl.gz")):
        records = read_jsonl_gz(path)
        if not records:
            continue
        per_member_sum += path.stat().st_size
        for record in records:
            lines.append(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
            if len(lines) >= sample_lines:
                break
        if len(lines) >= sample_lines:
            break
    if not lines:
        return None
    import io

    raw = "".join(lines).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(raw)
    batch = len(buf.getvalue())
    return per_member_sum / batch if batch else None


def format_mb(nbytes: int) -> str:
    return f"{nbytes / 1e6:.2f}"


def print_report(
    data_dir: Path,
    stats: dict[str, CategoryStats],
    redundancy: dict[str, RedundancyStats],
    batch_ratios: dict[str, float | None],
) -> None:
    total_disk = sum(s.bytes_on_disk for s in stats.values())
    total_records = sum(s.records for s in stats.values())

    print(f"data_dir={data_dir.resolve()}")
    print(f"total_on_disk_mb={format_mb(total_disk)} records={total_records}")
    print()
    print("category | files | records | disk_mb | uncompressed_mb | gzip_ratio | bytes/record")
    print("-" * 78)

    ordered_cats = list(CATEGORIES) + sorted(k for k in stats if k not in CATEGORIES)
    for cat in ordered_cats:
        s = stats.get(cat)
        if s is None or s.files == 0:
            continue
        ratio = s.uncompressed_bytes / s.bytes_on_disk if s.bytes_on_disk else 0
        bpr = s.bytes_on_disk / s.records if s.records else 0
        share = s.bytes_on_disk / total_disk * 100 if total_disk else 0
        print(
            f"{cat:12} | {s.files:5} | {s.records:7} | "
            f"{format_mb(s.bytes_on_disk):>7} ({share:4.1f}%) | "
            f"{format_mb(s.uncompressed_bytes):>7} | {ratio:5.2f} | {bpr:6.1f}"
        )

    print()
    print("redundancy (consecutive records within same file):")
    for cat, r in redundancy.items():
        if r.pairs == 0:
            continue
        p_rate = r.payload_identical / r.pairs
        e_rate = r.envelope_identical / r.pairs
        print(
            f"  {cat}: payload_identical={p_rate:.1%} envelope_identical={e_rate:.1%} "
            f"(pairs={r.pairs})"
        )

    print()
    print("batch_vs_per_member_gzip_ratio (higher = per-line gzip overhead):")
    for cat, ratio in batch_ratios.items():
        if ratio is not None:
            print(f"  {cat}: {ratio:.1f}x")

    if total_disk == 0:
        print()
        print(
            "NOTE: no data on disk. Run on VPS with accured logger data, or see "
            "knowledge/audit-logger-storage.md for code-derived estimates."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure logger raw storage (read-only)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/raw"),
        help="Root raw capture directory (default: data/raw)",
    )
    parser.add_argument(
        "--redundancy-sample",
        type=int,
        default=2000,
        help="Max consecutive pairs per category for redundancy stats",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir
    stats = scan_data_dir(data_dir)

    redundancy: dict[str, RedundancyStats] = {}
    batch_ratios: dict[str, float | None] = {}
    for cat in ("orderbook", "pm_orderbook"):
        redundancy[cat] = redundancy_for_category(
            data_dir, cat, max_pairs=args.redundancy_sample
        )
        batch_ratios[cat] = batch_gzip_ratio_sample(data_dir, cat)

    print_report(data_dir, stats, redundancy, batch_ratios)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
