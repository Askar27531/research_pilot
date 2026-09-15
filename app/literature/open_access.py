"""Full-text candidate discovery and ordering.

Publisher PDF endpoints routinely answer automated requests with 403 — sometimes
bot protection, sometimes an entitlement wall — while a repository or preprint
copy of the *same* paper usually downloads without complaint. OpenAlex already
knows every hosting location, so acquisition ranks them all (repository first,
publisher last) instead of trusting the single ``best_oa_location``, which is
frequently the publisher page that blocked us to begin with.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.urls import is_https_shaped_url
from app.schemas import FullTextAvailability, OpenAccessLocation, PaperMetadata

#: Publishers whose PDF endpoints most often answer a bot with 403. Ranking only
#: reorders these — a publisher copy is still tried last, never dropped outright,
#: because plenty of publishers (MDPI, PLOS, Frontiers…) serve PDFs openly.
PUBLISHER_HOSTS: tuple[str, ...] = (
    "sciencedirect.com",
    "elsevier.com",
    "onlinelibrary.wiley.com",
    "wiley.com",
    "link.springer.com",
    "springer.com",
    "tandfonline.com",
    "sagepub.com",
    "academic.oup.com",
    "ieeexplore.ieee.org",
    "dl.acm.org",
    "iopscience.iop.org",
    "pubs.acs.org",
    "pubs.rsc.org",
    "cell.com",
    "science.org",
    "jamanetwork.com",
    "nejm.org",
    "bmj.com",
    "thelancet.com",
)

#: Metadata indexes and aggregators. OpenAlex frequently types these as
#: "repository", but their pages only link out to the real full text, so a PDF
#: request there can never succeed. They rank below even the publishers.
INDEX_HOSTS: tuple[str, ...] = (
    "doaj.org",
    "pubmed.ncbi.nlm.nih.gov",
    "semanticscholar.org",
    "scilit.net",
    "ouci.dntb.gov.ua",
    "openalex.org",
    "crossref.org",
    "scite.ai",
    "connectedpapers.com",
    "researchgate.net",
)


def host_of(url: str) -> str:
    """Lowercased hostname, or "" when the URL cannot be parsed."""
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def is_publisher_host(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    return any(host == item or host.endswith(f".{item}") for item in PUBLISHER_HOSTS)


def is_index_host(url: str) -> bool:
    """True for metadata indexes that never serve full text themselves."""
    host = host_of(url)
    if not host:
        return False
    return any(host == item or host.endswith(f".{item}") for item in INDEX_HOSTS)


def is_doi_resolver_url(url: str) -> bool:
    """True for ``doi.org`` links, which resolve to an unknown third party.

    A DOI URL carries no promise of open access: it redirects to whichever
    publisher owns the record, which is how a paper ends up failing on the
    redirect limit or on a 403. Treating it as a known hosting location would
    let the picker label such a paper "可直接获取" — so it is classified as a
    deferred lookup rather than a usable copy.
    """
    host = host_of(url)
    return host in {"doi.org", "dx.doi.org"}


def _is_index(location: OpenAccessLocation) -> bool:
    return is_index_host(location.url)


def _is_repository(location: OpenAccessLocation) -> bool:
    return (location.source_type or "").casefold() == "repository" or (
        location.host_type or ""
    ).casefold() == "repository"


def _is_publisher(location: OpenAccessLocation) -> bool:
    return (location.source_type or "").casefold() == "journal" or is_publisher_host(
        location.url
    )


def _rank_key(location: OpenAccessLocation) -> tuple[int, int, int, int, str]:
    """Sort key, best first: repositories, unknown hosts, publishers, indexes.

    Within a tier: open copies first, then direct PDFs, then accepted/submitted
    versions — those are the ones that live in repositories rather than on the
    publisher's own site. Indexes are tested first because OpenAlex types them as
    repositories even though they never host the full text themselves.
    """
    if _is_index(location):
        tier = 3
    elif _is_repository(location):
        tier = 0
    elif _is_publisher(location):
        tier = 2
    else:
        tier = 1
    version = (location.version or "").casefold()
    return (
        tier,
        0 if location.is_oa else 1,
        0 if location.pdf_url else 1,
        0 if version in {"acceptedversion", "submittedversion"} else 1,
        location.url,
    )


def arxiv_pdf_url(metadata: PaperMetadata) -> str | None:
    """Canonical public PDF URL for an arXiv-sourced paper, if any."""
    arxiv_id = metadata.arxiv_id or (
        metadata.stable_id[len("arxiv:") :]
        if metadata.stable_id.startswith("arxiv:")
        else None
    )
    if not arxiv_id:
        return None
    identifier = arxiv_id.strip().rstrip("/").rsplit("/", 1)[-1]
    return f"https://arxiv.org/pdf/{identifier}"


def open_access_candidates(metadata: PaperMetadata) -> list[str]:
    """Every URL worth trying for the full text, most likely to succeed first.

    Publisher locations that are not open access are skipped outright: a paywalled
    publisher page cannot serve a PDF without entitlement, so attempting it only
    spends a request to collect a guaranteed 403.
    """
    usable = [
        location
        for location in metadata.oa_locations
        if location.url
        and not location.stale
        and not (_is_publisher(location) and not location.is_oa)
    ]
    stale = {location.url for location in metadata.oa_locations if location.stale}
    known = {location.url for location in usable}
    legacy = str(metadata.open_access_url) if metadata.open_access_url else None
    if legacy and legacy not in known and legacy not in stale:
        # Rows stored before oa_locations existed carry only this single URL.
        usable.append(OpenAccessLocation(url=legacy, is_oa=True))

    ordered: list[str] = []
    for location in sorted(usable, key=_rank_key):
        if location.url not in ordered:
            ordered.append(location.url)

    # A synthesized arXiv PDF is a preprint-repository copy, so it belongs ahead
    # of any publisher URL — otherwise a blocking publisher link shadows it.
    arxiv = arxiv_pdf_url(metadata)
    if arxiv and arxiv not in ordered:
        position = next(
            (index for index, url in enumerate(ordered) if is_publisher_host(url)),
            len(ordered),
        )
        ordered.insert(position, arxiv)
    return ordered


#: Host label for a location we cannot match to a known publisher or index.
_UNKNOWN_HOST_LABEL = "未知来源站点"


def _host_label(url: str) -> str:
    """Readable, compact host label for a candidate URL."""
    return host_of(url) or _UNKNOWN_HOST_LABEL


def _tier_of(url: str) -> int:
    """Acquisition tier for a bare candidate URL, mirroring ``_rank_key``.

    ``open_access_candidates`` returns plain URL strings, while ``_rank_key``
    operates on ``OpenAccessLocation`` objects, so the tier — the part that
    decides "will this 403?" — is reproduced here against the same host lists
    (index first, because OpenAlex types index pages as repositories).
    """
    if is_index_host(url):
        return 3
    if is_publisher_host(url):
        return 2
    return 1


def predict_full_text(metadata: PaperMetadata) -> FullTextAvailability:
    """Guess, from metadata alone, whether the PDF can be auto-downloaded.

    Deliberately reuses :func:`open_access_candidates` instead of re-deriving
    the rules, so this label can never drift from what acquisition will
    actually attempt. The tier of the first candidate decides the verdict: a
    repository or preprint copy downloads over plain HTTP, while a publisher
    endpoint is the one that answers 403 and ends in a manual upload.

    Never a promise: a repository link can still 404 and a publisher link can
    still happen to be open, so the wording stays predictive throughout.
    """
    # Presence in metadata is not the same as being fetchable: OpenAlex records
    # ``http://`` links and bare repository handles that the downloader refuses
    # outright. Counting those as usable would label a paper "可直接获取" and
    # then fail every attempt, which is precisely the promise this must not make.
    shaped = [url for url in open_access_candidates(metadata) if is_https_shaped_url(url)]
    # ``doi.org`` resolves to whoever owns the record — usually the publisher
    # that blocks us. It is a lookup, not a location, so it only becomes the
    # verdict when nothing better was recorded.
    usable = [url for url in shaped if not is_doi_resolver_url(url)]
    if not usable:
        if metadata.doi:
            if shaped:
                return FullTextAvailability(
                    level="uncertain",
                    label="可能需手动上传",
                    detail=(
                        "元数据只记录了 DOI 解析链接（doi.org），需跳转到实际来源，"
                        "能否下载取决于该来源（常为出版商页面）；"
                        "开始分析时会用 DOI 补查一次公开副本。"
                    ),
                    host="doi.org",
                    candidates=len(shaped),
                )
            return FullTextAvailability(
                level="uncertain",
                label="可能需手动上传",
                detail=(
                    "元数据未收录可直接下载的开放获取链接；已记录 DOI，"
                    "开始分析时会用 DOI 补查一次公开全文。"
                ),
                host="doi.org",
            )
        return FullTextAvailability(
            level="manual",
            label="需手动上传",
            detail="元数据既无开放获取链接也无 DOI，无法自动定位公开 PDF。",
        )

    # ``open_access_candidates`` already returns its URLs best-first, so the head
    # of the list is exactly what the downloader would attempt first.
    best = usable[0]
    host = _host_label(best)
    tier = _tier_of(best)
    if tier < 2:
        return FullTextAvailability(
            level="direct",
            label="可直接获取",
            detail=f"首选来源为开放获取副本（{host}），通常可直接下载。",
            host=host,
            candidates=len(usable),
        )
    if tier == 3:
        # Every recorded location is an index page (OpenAlex types some as
        # repositories), so no recorded URL serves the PDF itself.
        return FullTextAvailability(
            level="uncertain",
            label="可能需手动上传",
            detail=(
                f"已记录来源均为文献索引页（{host}），不能直接下载 PDF；"
                "开始分析时会再补查一次开放获取副本。"
            ),
            host=host,
            candidates=len(usable),
        )
    return FullTextAvailability(
        level="uncertain",
        label="可能被拦截",
        detail=(
            f"仅有出版商站点链接（{host}），自动下载常被 403 拦截；"
            "失败后会提示手动上传。"
        ),
        host=host,
        candidates=len(usable),
    )
