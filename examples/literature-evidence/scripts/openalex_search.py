#!/usr/bin/env python3
"""Fetch and normalize one bounded OpenAlex Works result page."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCHEMA_VERSION = "literature-retrieval-v1"
ENDPOINT = "https://api.openalex.org/works"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class RetrievalError(ValueError):
    """A safe, user-facing retrieval validation error."""


def normalize_response(
    payload: Any,
    *,
    query: str,
    max_records: int,
    retrieved_at: str,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate an OpenAlex response and produce a bounded manifest."""
    if not isinstance(payload, dict):
        raise RetrievalError("OpenAlex response must be a JSON object")
    meta = payload.get("meta")
    results = payload.get("results")
    if not isinstance(meta, dict) or not isinstance(results, list):
        raise RetrievalError("OpenAlex response is missing object meta or list results")
    source_count = meta.get("count")
    if not isinstance(source_count, int) or source_count < 0:
        raise RetrievalError("OpenAlex meta.count must be a non-negative integer")

    records = [_normalize_record(item) for item in results[:max_records]]
    warnings: list[str] = []
    truncated = source_count > len(records)
    if truncated:
        warnings.append(
            f"bounded retrieval returned {len(records)} of {source_count} matching works"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "openalex",
        "endpoint": ENDPOINT,
        "query": query,
        "filters": filters or {},
        "retrieved_at": retrieved_at,
        "source_count": source_count,
        "returned_count": len(records),
        "truncated": truncated,
        "warnings": warnings,
        "records": records,
    }


def _normalize_record(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RetrievalError("OpenAlex result entries must be JSON objects")
    work_id = item.get("id")
    title = item.get("display_name") or item.get("title")
    if not isinstance(work_id, str) or not work_id.strip():
        raise RetrievalError("OpenAlex result is missing a stable work id")
    if not isinstance(title, str) or not title.strip():
        raise RetrievalError(f"OpenAlex result {work_id} is missing a title")
    primary = item.get("primary_location")
    landing_page = (
        primary.get("landing_page_url", "") if isinstance(primary, dict) else ""
    )
    doi = item.get("doi")
    return {
        "id": work_id.strip(),
        "title": title.strip(),
        "publication_year": _optional_int(item.get("publication_year")),
        "type": item.get("type") if isinstance(item.get("type"), str) else "",
        "doi": doi if isinstance(doi, str) else "",
        "landing_page": landing_page if isinstance(landing_page, str) else "",
        "cited_by_count": _optional_int(item.get("cited_by_count")) or 0,
    }


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def fetch_response(
    *,
    query: str,
    max_records: int,
    api_key: str | None,
    timeout: float,
    filters: dict[str, str],
) -> Any:
    params = {"search": query, "per-page": str(max_records)}
    if filters:
        params["filter"] = ",".join(
            f"{name}:{value}" for name, value in filters.items()
        )
    if api_key:
        params["api_key"] = api_key
    request = Request(
        f"{ENDPOINT}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "aigineering-example/0.5"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise RetrievalError(f"OpenAlex returned HTTP {response.status}")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                raise RetrievalError("OpenAlex response exceeds the 8 MiB limit")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RetrievalError(f"OpenAlex returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError) as exc:
        raise RetrievalError("OpenAlex request failed or timed out") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise RetrievalError("OpenAlex response exceeds the 8 MiB limit")
    return json.loads(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-records", type=int, default=25)
    parser.add_argument("--from-year", type=int)
    parser.add_argument("--to-year", type=int)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--api-key-env", default="OPENALEX_API_KEY")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--retrieved-at",
        help="ISO-8601 timestamp; required for byte-identical fixture replay",
    )
    parser.add_argument(
        "--action",
        action="store_true",
        help="emit one /exec action for HarnessCandidateAdapter",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        query = args.query.strip()
        if not query:
            raise RetrievalError("query must not be empty")
        if not 1 <= args.max_records <= 100:
            raise RetrievalError("max-records must be between 1 and 100")
        if args.timeout <= 0 or args.timeout > 60:
            raise RetrievalError(
                "timeout must be greater than 0 and at most 60 seconds"
            )
        if args.from_year and args.to_year and args.from_year > args.to_year:
            raise RetrievalError("from-year must not be later than to-year")
        for year in (args.from_year, args.to_year):
            if year is not None and not 1 <= year <= 9999:
                raise RetrievalError("year filters must be between 1 and 9999")
        filters = {}
        if args.from_year:
            filters["from_publication_date"] = f"{args.from_year:04d}-01-01"
        if args.to_year:
            filters["to_publication_date"] = f"{args.to_year:04d}-12-31"
        if args.fixture:
            payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        else:
            payload = fetch_response(
                query=query,
                max_records=args.max_records,
                api_key=os.environ.get(args.api_key_env),
                timeout=args.timeout,
                filters=filters,
            )
        retrieved_at = args.retrieved_at or datetime.now(timezone.utc).isoformat()
        manifest = normalize_response(
            payload,
            query=query,
            max_records=args.max_records,
            retrieved_at=retrieved_at,
            filters=filters,
        )
        document = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        if args.action:
            document = "/exec " + json.dumps(
                {"outputs": {"retrieval_manifest": document}},
                sort_keys=True,
                ensure_ascii=False,
            )
        print(document)
        return 0
    except (OSError, json.JSONDecodeError, RetrievalError) as exc:
        print(f"retrieval failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
