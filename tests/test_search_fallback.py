from src.tools.search import PublicWebSearchTool, ResilientSearchTool


_DUCK_HTML = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">
      AI Agent News
    </a>
  </h2>
  <a class="result__snippet">A verified industry update.</a>
</div>
"""


class _Tool:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def invoke(self, _input):
        self.calls += 1
        return self.result

    async def ainvoke(self, _input):
        self.calls += 1
        return self.result


def test_public_search_parser_extracts_canonical_duckduckgo_url() -> None:
    results = PublicWebSearchTool()._parse_duckduckgo(_DUCK_HTML)

    assert results == [
        {
            "title": "AI Agent News",
            "url": "https://example.com/news",
            "content": "A verified industry update.",
            "score": None,
        }
    ]


def test_resilient_search_falls_back_after_tavily_401() -> None:
    primary = _Tool("Error 401: Unauthorized")
    fallback = _Tool(
        [{"title": "Fallback", "url": "https://example.com", "content": "ok"}]
    )
    tool = ResilientSearchTool(primary=primary, fallback=fallback)

    result = tool.invoke({"query": "AI Agent"})

    assert result[0]["title"] == "Fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_resilient_search_keeps_successful_tavily_result() -> None:
    primary = _Tool(
        [{"title": "Primary", "url": "https://example.com", "content": "ok"}]
    )
    fallback = _Tool([])
    tool = ResilientSearchTool(primary=primary, fallback=fallback)

    result = tool.invoke({"query": "AI Agent"})

    assert result[0]["title"] == "Primary"
    assert primary.calls == 1
    assert fallback.calls == 0
