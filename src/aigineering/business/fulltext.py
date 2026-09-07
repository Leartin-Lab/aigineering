"""Pure parsing helpers for full text locations in scholarly metadata."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_FORMATS = {"pdf", "xml", "html"}
_SECRET_QUERY = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "auth",
    "signature",
    "credential",
)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme.lower() not in {"http", "https"} or not host:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host_lower = host.rstrip(".").lower()
        if host_lower == "localhost" or host_lower.endswith(".localhost"):
            return None
        try:
            address = ip_address(host_lower)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
            or address.is_multicast
        ):
            return None

        # Metadata occasionally contains archive URLs with an API key in the
        # query.  Keep harmless query parameters, but never publish secrets.
        query = urlencode(
            [
                (key, val)
                for key, val in parse_qsl(parts.query, keep_blank_values=True)
                if not any(
                    marker in key.lower().replace("-", "_") for marker in _SECRET_QUERY
                )
            ]
        )
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, query, ""))
    except (TypeError, ValueError):
        return None


def _format(value: object, url: str, field: str | None = None) -> str:
    explicit = str(value).lower() if isinstance(value, str) else ""
    if explicit in _FORMATS:
        return explicit
    if field:
        field_lower = field.lower()
        if "pdf" in field_lower:
            return "pdf"
        if "xml" in field_lower or "tei" in field_lower:
            return "xml"
        if "html" in field_lower or "landing" in field_lower:
            return "html"
    suffix = urlsplit(url).path.lower().rsplit("/", 1)[-1]
    if suffix.endswith(".pdf"):
        return "pdf"
    if suffix.endswith((".xml", ".tei")):
        return "xml"
    return "html"


def _work_ref(provider: str, record: Mapping) -> object:
    if provider == "unpaywall":
        return record.get("doi") or record.get("id")
    ids = record.get("ids")
    if isinstance(ids, Mapping):
        return record.get("doi") or ids.get("doi") or record.get("id") or ids.get("id")
    return record.get("doi") or record.get("id")


def fulltext_locations(provider: str, record: Mapping) -> tuple[dict, ...]:
    """Extract safe, non-authorizing candidates from provider metadata.

    URL filtering removes obvious unsafe metadata and credentials; it is not a
    substitute for a network fetcher's full SSRF protection.
    """
    if not isinstance(provider, str) or not isinstance(record, Mapping):
        return ()
    provider_name = provider.lower().strip()
    if provider_name not in {"unpaywall", "openalex"}:
        return ()
    reference_value = _work_ref(provider_name, record)
    reference = reference_value if isinstance(reference_value, str) else None
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(
        value: object,
        *,
        fmt: object = None,
        version: object = None,
        license_value: object = None,
        field: str | None = None,
        access: object = "open",
    ) -> None:
        url = _safe_url(value)
        if url is None or url in seen:
            return
        seen.add(url)
        candidates.append(
            {
                "url": url,
                "format": _format(fmt, url, field),
                "version": version if isinstance(version, str) else None,
                "license": license_value if isinstance(license_value, str) else None,
                "provider": provider_name,
                "access": "open" if access == "open" else "unknown",
                "original_work": reference,
            }
        )

    def location(location: object) -> None:
        if not isinstance(location, Mapping):
            return
        license_value = location.get("license")
        version = location.get("version")
        if provider_name == "unpaywall":
            fields = (("url_for_pdf", "pdf"), ("url_for_landing_page", "html"))
        else:
            fields = (
                ("pdf_url", "pdf"),
                ("xml_url", "xml"),
                ("landing_page_url", "html"),
            )
        for field, fmt in fields:
            add(
                location.get(field),
                fmt=fmt,
                version=version,
                license_value=license_value,
                field=field,
                access=(
                    "open"
                    if provider_name == "unpaywall"
                    or location.get("is_oa") is True
                    or location.get("availability") == "open"
                    else "unknown"
                ),
            )

    if provider_name in {"unpaywall", "openalex"}:
        best = record.get("best_oa_location")
        location(best)
        locations = record.get(
            "oa_locations" if provider_name == "unpaywall" else "locations"
        )
        if isinstance(locations, (list, tuple)):
            for item in locations:
                location(item)
        if provider_name == "openalex":
            content = record.get("content_urls")
            if isinstance(content, Mapping):
                for field, value in content.items():
                    if isinstance(value, str):
                        add(
                            value,
                            field=str(field),
                            version=None,
                            license_value=None,
                            access="unknown",
                        )
    return tuple(candidates)
