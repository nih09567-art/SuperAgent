import requests
import pytest

from src.tools.crawler import (
    CrawlUnavailableError,
    Crawler,
    DirectHttpClient,
    UnsafeCrawlUrl,
)


_HTML = """
<html><head><title>AI Agent News</title></head>
<body><article><h1>AI Agent News</h1><p>Verified industry update.</p></article></body></html>
"""


class _Direct:
    def __init__(self, result=_HTML, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def crawl(self, _url):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _Jina:
    def __init__(self, result=_HTML, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def crawl(self, _url, return_format="html"):
        self.calls += 1
        assert return_format == "html"
        if self.error:
            raise self.error
        return self.result


def test_auto_transport_uses_direct_fetch_without_calling_jina() -> None:
    direct = _Direct()
    jina = _Jina()

    article = Crawler(
        direct_client=direct,
        jina_client=jina,
        transport="auto",
    ).crawl("https://example.com/news")

    assert "Verified industry update" in article.to_markdown()
    assert direct.calls == 1
    assert jina.calls == 0


def test_auto_transport_falls_back_to_jina_after_direct_failure() -> None:
    direct = _Direct(error=requests.ConnectTimeout())
    jina = _Jina()

    article = Crawler(
        direct_client=direct,
        jina_client=jina,
        transport="auto",
    ).crawl("https://example.com/news")

    assert "Verified industry update" in article.to_markdown()
    assert direct.calls == 1
    assert jina.calls == 1


def test_auto_transport_reports_bounded_failure_summary() -> None:
    crawler = Crawler(
        direct_client=_Direct(error=requests.ConnectTimeout("secret-url")),
        jina_client=_Jina(error=requests.ConnectTimeout("secret-url")),
        transport="auto",
    )

    with pytest.raises(CrawlUnavailableError) as exc_info:
        crawler.crawl("https://example.com/news?token=secret")

    assert str(exc_info.value) == (
        "all crawl transports failed "
        "(direct:ConnectTimeout, jina:ConnectTimeout)"
    )
    assert "secret" not in str(exc_info.value)


def test_direct_transport_blocks_private_network_targets_before_request() -> None:
    class _NoRequestSession:
        def get(self, *_args, **_kwargs):
            raise AssertionError("private URL reached the HTTP client")

    client = DirectHttpClient(session=_NoRequestSession())

    with pytest.raises(UnsafeCrawlUrl, match="non-public"):
        client.crawl("http://127.0.0.1/internal")
