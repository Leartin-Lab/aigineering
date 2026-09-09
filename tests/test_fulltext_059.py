from aigineering.business.fulltext import fulltext_locations


def test_unpaywall_locations_and_best_are_deduplicated_and_prioritized():
    result = fulltext_locations(
        "unpaywall",
        {
            "doi": "10.1234/example",
            "best_oa_location": {
                "url_for_pdf": "https://publisher.example/a.pdf",
                "version": "publishedVersion",
                "license": "cc-by",
            },
            "oa_locations": [
                {"url_for_pdf": "https://publisher.example/a.pdf"},
                {
                    "url_for_landing_page": "https://repo.example/item/1",
                    "version": "acceptedVersion",
                },
            ],
        },
    )
    assert [item["url"] for item in result] == [
        "https://publisher.example/a.pdf",
        "https://repo.example/item/1",
    ]
    assert (
        result[0]["format"] == "pdf" and result[0]["original_work"] == "10.1234/example"
    )


def test_openalex_content_urls_and_archive_query_secrets_are_removed():
    result = fulltext_locations(
        "openalex",
        {
            "id": "https://openalex.org/W1",
            "content_urls": {
                "pdf": "https://archive.org/download/x/x.pdf?api_key=do-not-leak&download=1",
                "tei": "https://repo.example/x.xml",
            },
            "locations": [
                {"landing_page_url": "https://repo.example/x", "license": "cc-by"}
            ],
        },
    )
    assert result[0]["url"] == "https://repo.example/x"
    assert result[0]["access"] == "unknown"
    assert "do-not-leak" not in repr(result)
    assert any(
        item["format"] == "pdf" and item["url"].endswith("?download=1")
        for item in result
    )
    assert all(item["original_work"] == "https://openalex.org/W1" for item in result)


def test_empty_malformed_unsupported_and_unsafe_urls_return_no_candidates():
    assert fulltext_locations("unpaywall", {}) == ()
    assert fulltext_locations("other", {"doi": "x"}) == ()
    result = fulltext_locations(
        "unpaywall",
        {
            "doi": "x",
            "oa_locations": [
                {"url_for_pdf": "ftp://example.org/a.pdf"},
                {"url_for_pdf": "https://user:pass@example.org/a.pdf"},
                {"url_for_pdf": "https://127.0.0.1/a.pdf"},
                {"url_for_pdf": "https://localhost/a.pdf"},
                {"url_for_pdf": "https://example.org/a.pdf?token=secret"},
            ],
        },
    )
    assert len(result) == 1
    assert result[0]["url"] == "https://example.org/a.pdf"
