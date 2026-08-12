import logging
import os
import sys

from .article import Article
from .direct_http_client import DirectHttpClient
from .jina_client import JinaClient
from .readability_extractor import ReadabilityExtractor

logger = logging.getLogger(__name__)


class CrawlUnavailableError(RuntimeError):
    """Raised after every configured crawl transport fails."""


class Crawler:
    def __init__(
        self,
        *,
        direct_client: DirectHttpClient | None = None,
        jina_client: JinaClient | None = None,
        transport: str | None = None,
    ) -> None:
        selected = str(
            transport or os.getenv("CRAWLER_TRANSPORT", "auto")
        ).strip().lower()
        if selected not in {"auto", "direct", "jina"}:
            raise ValueError("CRAWLER_TRANSPORT must be auto, direct, or jina")
        self.transport = selected
        self.direct_client = direct_client or DirectHttpClient()
        self.jina_client = jina_client or JinaClient()

    def crawl(self, url: str) -> Article:
        transports = (
            [("direct", self.direct_client)]
            if self.transport == "direct"
            else [("jina", self.jina_client)]
            if self.transport == "jina"
            else [("direct", self.direct_client), ("jina", self.jina_client)]
        )
        failures: list[str] = []
        html = ""
        for name, client in transports:
            try:
                html = (
                    client.crawl(url)
                    if name == "direct"
                    else client.crawl(url, return_format="html")
                )
                if html.strip():
                    break
                failures.append(f"{name}:EmptyResponse")
            except Exception as exc:
                failures.append(f"{name}:{type(exc).__name__}")
                logger.warning("Crawl transport %s failed: %s", name, type(exc).__name__)
        if not html.strip():
            raise CrawlUnavailableError(
                "all crawl transports failed (" + ", ".join(failures) + ")"
            )
        extractor = ReadabilityExtractor()
        article = extractor.extract_article(html)
        article.url = url
        return article


if __name__ == "__main__":
    if len(sys.argv) == 2:
        url = sys.argv[1]
    else:
        url = "https://fintel.io/zh-hant/s/br/nvdc34"
    crawler = Crawler()
    article = crawler.crawl(url)
    print(article.to_markdown())
