import logging
import os

import requests

logger = logging.getLogger(__name__)


class JinaClient:
    def crawl(self, url: str, return_format: str = "html") -> str:
        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
        }
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        else:
            logger.warning(
                "Jina API key is not set. Provide your own key to access a higher rate limit. See https://jina.ai/reader for more information."
            )
        data = {"url": url}
        connect_timeout = max(
            0.1, float(os.getenv("JINA_CONNECT_TIMEOUT_SECONDS", "5"))
        )
        read_timeout = max(
            0.1, float(os.getenv("JINA_READ_TIMEOUT_SECONDS", "20"))
        )
        response = requests.post(
            "https://r.jina.ai/",
            headers=headers,
            json=data,
            timeout=(connect_timeout, read_timeout),
        )
        response.raise_for_status()
        return response.text
