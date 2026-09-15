"""Pre-selection full-text availability labels.

These labels appear on the paper picker *before* anything is downloaded, so the
tests pin the two properties that make them trustworthy:

1. The tier matches what acquisition would actually try first — the same
   ``open_access_candidates`` ordering, not a second copy of the rules.
2. A publisher-only record is never labelled "可直接获取": that is exactly the
   case that ends in a 403 and a manual upload.
"""

from app.literature.open_access import open_access_candidates, predict_full_text
from app.schemas import OpenAccessLocation, PaperAuthor, PaperMetadata


def _paper(**overrides) -> PaperMetadata:
    values = {
        "stable_id": "doi:10.1/x",
        "source_id": "10.1/x",
        "title": "A paper",
        "authors": [PaperAuthor(name="A. Author")],
        "year": 2024,
        "doi": "10.1/x",
    }
    values.update(overrides)
    return PaperMetadata(**values)


def _location(url: str, **overrides) -> OpenAccessLocation:
    values = {"url": url, "is_oa": True}
    values.update(overrides)
    return OpenAccessLocation(**values)


def test_repository_copy_is_direct():
    paper = _paper(
        oa_locations=[
            _location("https://link.springer.com/article/10.1/x"),
            _location(
                "https://repo.example.ac.jp/files/x.pdf",
                source_type="repository",
                pdf_url="https://repo.example.ac.jp/files/x.pdf",
            ),
        ]
    )

    hint = predict_full_text(paper)

    assert hint.level == "direct"
    assert hint.label == "可直接获取"
    assert hint.host == "repo.example.ac.jp"
    assert hint.candidates == 2


def test_publisher_only_record_is_flagged_as_uncertain():
    paper = _paper(
        oa_locations=[_location("https://onlinelibrary.wiley.com/doi/pdfdirect/10.1/x")]
    )

    hint = predict_full_text(paper)

    assert hint.level == "uncertain"
    assert hint.label == "可能被拦截"
    assert "403" in hint.detail
    assert hint.host == "onlinelibrary.wiley.com"


def test_index_only_record_points_at_manual_upload():
    paper = _paper(
        oa_locations=[_location("https://doaj.org/article/abc", source_type="repository")]
    )

    hint = predict_full_text(paper)

    assert hint.level == "uncertain"
    assert hint.label == "可能需手动上传"
    assert hint.host == "doaj.org"


def test_doi_without_locations_is_deferred_not_manual():
    hint = predict_full_text(_paper())

    assert hint.level == "uncertain"
    assert hint.label == "可能需手动上传"
    assert hint.host == "doi.org"
    assert "DOI" in hint.detail


def test_no_locations_and_no_doi_is_manual():
    hint = predict_full_text(_paper(doi=None))

    assert hint.level == "manual"
    assert hint.label == "需手动上传"
    assert hint.candidates == 0


def test_stale_locations_never_count_as_available():
    paper = _paper(
        oa_locations=[
            _location("https://repo.example.ac.jp/gone.pdf", stale=True),
        ]
    )

    assert open_access_candidates(paper) == []
    assert predict_full_text(paper).level == "uncertain"


def test_paywalled_publisher_location_is_skipped_like_acquisition_does():
    paper = _paper(
        oa_locations=[
            _location("https://www.sciencedirect.com/science/article/pii/S1", is_oa=False),
            _location("https://repo.example.org/x.pdf", source_type="repository"),
        ]
    )

    hint = predict_full_text(paper)

    assert hint.level == "direct"
    assert hint.host == "repo.example.org"
    # Only the repository copy survives, exactly as the downloader would see it.
    assert len(open_access_candidates(paper)) == 1


def test_arxiv_id_alone_yields_a_synthesized_repository_copy():
    paper = _paper(doi=None, arxiv_id="2401.00001", oa_locations=[])

    hint = predict_full_text(paper)

    assert hint.level == "direct"
    assert hint.host == "arxiv.org"


def test_non_https_location_is_never_labelled_directly_available():
    """Acquisition rejects non-HTTPS URLs, so the label must not promise one.

    Real OpenAlex records carry ``http://`` repository links; predicting from
    the URL's mere presence would promise a download the downloader refuses.
    """
    paper = _paper(
        oa_locations=[
            _location(
                "http://dspace.example.org/bitstream/1/paper.pdf",
                source_type="repository",
            )
        ]
    )

    hint = predict_full_text(paper)

    assert hint.level == "uncertain"
    assert hint.label == "可能需手动上传"
    assert hint.candidates == 0
    # Acquisition would still try it and fail, but the label must not promise it.
    assert open_access_candidates(paper) == [
        "http://dspace.example.org/bitstream/1/paper.pdf"
    ]


def test_https_shape_check_matches_the_downloader_gate():
    from app.core.urls import is_https_shaped_url

    assert is_https_shaped_url("https://repo.example.org/a.pdf")
    assert not is_https_shaped_url("http://repo.example.org/a.pdf")
    assert not is_https_shaped_url("hdl.handle.net/10397/91590")
    assert not is_https_shaped_url("https://user:pw@repo.example.org/a.pdf")


def test_doi_resolver_link_is_not_a_direct_download():
    """A doi.org URL redirects to an unknown publisher, so it cannot be promised."""
    paper = _paper(oa_locations=[_location("https://doi.org/10.5194/acp-13-245-2013")])

    hint = predict_full_text(paper)

    assert hint.level == "uncertain"
    assert hint.label == "可能需手动上传"
    assert hint.host == "doi.org"
    assert "DOI" in hint.detail


def test_doi_resolver_link_does_not_mask_a_real_repository_copy():
    paper = _paper(
        oa_locations=[
            _location("https://doi.org/10.1016/j.jai.2024.02.003"),
            _location("https://doaj.org/article/917234f0", source_type="repository"),
        ]
    )

    hint = predict_full_text(paper)

    # doaj is an index, so the verdict is the index branch — but it must never
    # come back as "direct" off the DOI link ahead of it.
    assert hint.level == "uncertain"
    assert hint.label == "可能需手动上传"
    assert hint.host == "doaj.org"
    assert hint.candidates == 1


def test_tier_matches_what_acquisition_would_try_first():
    """The label must describe the first URL the downloader would attempt."""
    paper = _paper(
        oa_locations=[
            _location("https://www.tandfonline.com/doi/pdf/10.1/x"),
            _location("https://europepmc.org/articles/PMC1", source_type="repository"),
        ]
    )

    # Same ordering primitive acquisition uses, so the two cannot drift apart.
    first_attempted = open_access_candidates(paper)[0]

    hint = predict_full_text(paper)

    assert hint.level == "direct"
    assert hint.host == "europepmc.org"
    assert first_attempted.startswith("https://europepmc.org")
