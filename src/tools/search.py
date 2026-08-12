import asyncio
import logging
import datetime
import functools
import json
import os
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

import requests
from bs4 import BeautifulSoup
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr
from .decorators import create_logged_tool

TAVILY_MAX_RESULTS = 5
logger = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = (
    "your_",
    "replace_",
    "replace-me",
    "placeholder",
    "changeme",
)


def _has_valid_tavily_key() -> bool:
    value = os.getenv("TAVILY_API_KEY", "").strip()
    if not value:
        return False
    normalized = value.lower()
    return not any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def is_search_available() -> bool:
    """Whether pre-planning web search can be used safely."""
    return _has_valid_tavily_key() or _public_fallback_enabled()


def get_search_status() -> dict:
    configured = is_search_available()
    return {
        "configured": configured,
        "provider": (
            "tavily+public-fallback"
            if _has_valid_tavily_key() and _public_fallback_enabled()
            else "tavily"
            if _has_valid_tavily_key()
            else "public-fallback"
        ),
        "reason": (
            None
            if configured
            else "TAVILY_API_KEY is unavailable and public fallback is disabled"
        ),
    }


def _public_fallback_enabled() -> bool:
    return os.getenv("SEARCH_PUBLIC_FALLBACK_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _timeout(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _result_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            url = unquote(target)
    return url


class PublicWebSearchTool(BaseTool):
    """Keyless public HTML search used when Tavily is unavailable."""

    name: str = "tavily_tool"
    description: str = (
        "Search the public web. Tavily is preferred when available; a bounded "
        "public HTML search fallback is used when its credential or service fails."
    )
    max_results: int = TAVILY_MAX_RESULTS

    def _parse_duckduckgo(self, source: str) -> list[dict]:
        soup = BeautifulSoup(source, "html.parser")
        results = []
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            if link is None:
                continue
            url = _result_url(link.get("href", ""))
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            snippet = item.select_one(".result__snippet")
            results.append(
                {
                    "title": link.get_text(" ", strip=True),
                    "url": url,
                    "content": (
                        snippet.get_text(" ", strip=True) if snippet else ""
                    ),
                    "score": None,
                }
            )
            if len(results) >= self.max_results:
                break
        return results

    def _parse_bing(self, source: str) -> list[dict]:
        soup = BeautifulSoup(source, "html.parser")
        results = []
        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a")
            if link is None:
                continue
            url = str(link.get("href") or "").strip()
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            snippet = item.select_one(".b_caption p")
            results.append(
                {
                    "title": link.get_text(" ", strip=True),
                    "url": url,
                    "content": (
                        snippet.get_text(" ", strip=True) if snippet else ""
                    ),
                    "score": None,
                }
            )
            if len(results) >= self.max_results:
                break
        return results

    def _run(self, query: str, **kwargs):
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SuperAgentResearch/1.0)"
        }
        timeout = (
            _timeout("SEARCH_FALLBACK_CONNECT_TIMEOUT_SECONDS", 2.0),
            _timeout("SEARCH_FALLBACK_READ_TIMEOUT_SECONDS", 5.0),
        )
        providers = (
            (
                "duckduckgo",
                "https://html.duckduckgo.com/html/?q=" + quote_plus(query),
                self._parse_duckduckgo,
            ),
            (
                "bing",
                "https://www.bing.com/search?q=" + quote_plus(query),
                self._parse_bing,
            ),
        )
        for provider, url, parser in providers:
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                results = parser(response.text)
                if results:
                    logger.info("Public search fallback used provider %s", provider)
                    return results
            except requests.RequestException as exc:
                logger.warning(
                    "Public search provider %s failed: %s",
                    provider,
                    type(exc).__name__,
                )
        return []

    async def _arun(self, query: str, **kwargs):
        return await asyncio.to_thread(self._run, query, **kwargs)


class ResilientSearchTool(BaseTool):
    """Use Tavily first and fail over when it rejects or cannot serve a query."""

    name: str = "tavily_tool"
    description: str = PublicWebSearchTool.model_fields["description"].default
    _primary: BaseTool = PrivateAttr()
    _fallback: BaseTool = PrivateAttr()

    def __init__(self, *, primary: BaseTool, fallback: BaseTool, **kwargs):
        super().__init__(**kwargs)
        self._primary = primary
        self._fallback = fallback

    @staticmethod
    def _failed(value) -> bool:
        if value in (None, [], {}):
            return True
        rendered = json.dumps(value, ensure_ascii=False, default=str).lower()
        return any(
            marker in rendered
            for marker in (
                "401",
                "unauthorized",
                "invalid api key",
                "authentication failed",
            )
        )

    def _run(self, query: str, **kwargs):
        try:
            result = self._primary.invoke({"query": query})
            if not self._failed(result):
                return result
            logger.warning("Tavily search was rejected; using public fallback")
        except Exception as exc:
            logger.warning(
                "Tavily search failed with %s; using public fallback",
                type(exc).__name__,
            )
        return self._fallback.invoke({"query": query})

    async def _arun(self, query: str, **kwargs):
        try:
            result = await self._primary.ainvoke({"query": query})
            if not self._failed(result):
                return result
            logger.warning("Tavily search was rejected; using public fallback")
        except Exception as exc:
            logger.warning(
                "Tavily search failed with %s; using public fallback",
                type(exc).__name__,
            )
        return await self._fallback.ainvoke({"query": query})


class UnavailableSearchTool(BaseTool):
    """A schema-compatible fallback that keeps Agent/Tool registration available."""

    name: str = "tavily_tool"
    description: str = (
        "Web search is currently unavailable because TAVILY_API_KEY is not configured. "
        "This fallback returns no results instead of failing application startup."
    )

    def _run(self, query: str, **kwargs):
        logger.warning("Skipped Tavily search because TAVILY_API_KEY is not configured")
        return []

    async def _arun(self, query: str, **kwargs):
        return self._run(query, **kwargs)


# Templates for English and Chinese
FORMAT_TEMPLATE = {
    "en": "Now is {CURRENT_TIME}, {query}",
    "zh": "当前时间是: {CURRENT_TIME}, {query}",
}

def contains_chinese(text: str) -> bool:
    """Checks if the string contains at least one Chinese character (U+4E00-U+9FFF)."""
    if not text: 
        return False
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def inject_current_time(tool_cls: type[BaseTool]) -> type[BaseTool]:
    """
    Class decorator to inject the current time into the input dictionary of a LangChain tool's invoke/ainvoke methods.
    Selects different formatting templates based on the query language (Chinese/English).
    Logs a warning or debug message if the input is not a dictionary or lacks the 'query' key.
    """
    original_invoke = getattr(tool_cls, 'invoke', None)
    original_ainvoke = getattr(tool_cls, 'ainvoke', None)

    if original_invoke:
        @functools.wraps(original_invoke)
        def invoke(self, input: dict | str, config=None, **kwargs):
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:00")
            processed_input = input

            if isinstance(input, dict):
                original_query = input.get('query')
                if original_query:
                    processed_input = input.copy()
                    processed_input['current_time'] = current_time

                    # Determine language and select template
                    lang = 'zh' if contains_chinese(original_query) else 'en'
                    template = FORMAT_TEMPLATE.get(lang, FORMAT_TEMPLATE['en'])
                    processed_input['query'] = template.format(
                        CURRENT_TIME=current_time, query=original_query
                    )
                    logger.debug(f"Injected time using '{lang}' template, now processed_input={processed_input} into invoke input") # Updated log message
                else:
                    logger.debug(f"Input dictionary {input} lacks 'query' key or value is empty, skipping time injection.")
            else:
                logger.warning(
                    f"Input type {type(input)} for invoke is not a dictionary. "
                    f"Cannot inject 'current_time'."
                )
            return original_invoke(self, processed_input, config=config, **kwargs)
        setattr(tool_cls, 'invoke', invoke)

    if original_ainvoke:
        @functools.wraps(original_ainvoke)
        async def ainvoke(self, input: dict | str, config=None, **kwargs):
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:00")
            processed_input = input 

            if isinstance(input, dict):
                original_query = input.get('query')
                if original_query:
                    processed_input = input.copy()
                    processed_input['current_time'] = current_time

                    # Determine language and select template
                    lang = 'zh' if contains_chinese(original_query) else 'en'
                    template = FORMAT_TEMPLATE.get(lang, FORMAT_TEMPLATE['en'])

                    processed_input['query'] = template.format(
                        CURRENT_TIME=current_time, query=original_query
                    )
                    logger.debug(f"Injected time using '{lang}' template, now processed_input={processed_input} into ainvoke input") # Updated log message
                else:
                    logger.debug(f"Input dictionary {input} lacks 'query' key or value is empty, skipping time injection.")
            else:
                logger.warning(
                    f"Input type {type(input)} for ainvoke is not a dictionary. "
                    f"Cannot inject 'current_time'."
                )
            return await original_ainvoke(self, processed_input, config=config, **kwargs)
        setattr(tool_cls, 'ainvoke', ainvoke)

    return tool_cls

public_search_tool = PublicWebSearchTool(
    name="tavily_tool",
    max_results=TAVILY_MAX_RESULTS,
)

if _has_valid_tavily_key():
    TimeInjectedTavily = inject_current_time(TavilySearchResults)
    LoggedTimeInjectedTavily = create_logged_tool(TimeInjectedTavily)
    primary_tavily_tool = LoggedTimeInjectedTavily(
        name="tavily_tool",
        max_results=TAVILY_MAX_RESULTS,
    )
    tavily_tool = (
        ResilientSearchTool(
            primary=primary_tavily_tool,
            fallback=public_search_tool,
        )
        if _public_fallback_enabled()
        else primary_tavily_tool
    )
elif _public_fallback_enabled():
    tavily_tool = public_search_tool
else:
    tavily_tool = UnavailableSearchTool()
