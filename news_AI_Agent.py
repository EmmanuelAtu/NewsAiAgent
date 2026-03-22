import os
import json
import requests
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables from .env
load_dotenv()

NEWS_API_KEY = os.getenv("NEWS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEWS_API_BASE = "https://newsapi.org/v2/top-headlines"


client = OpenAI(api_key=OPENAI_API_KEY)

# Tracks seen article URLs to detect duplicates across runs/calls
seen_article_urls: set[str] = set()


# ---------------------------------------------
#  Tool 1 - Fetch news articles from NewsAPI
# ---------------------------------------------
def fetch_news_article(
    country: str = "ng",
    search_keyword: str = "technology",  # BUG FIX: renamed from 'keyword' to avoid shadowing built-in
    limit: int = 5,
) -> list[dict]:
    params = {
        "country": country,
        "q": search_keyword,
        "apiKey": NEWS_API_KEY,
    }

    # BUG FIX: Added error handling for network/API failures
    try:
        response = requests.get(NEWS_API_BASE, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to fetch news: {e}")
        return []

    articles = data.get("articles", [])[:limit]

    formatted = []
    for i, article in enumerate(articles):
        url = article.get("url", "")
        formatted.append({
            "id": str(i),
            "title": article.get("title", ""),
            "summary": article.get("description", ""),
            "url": url,
            "publishedAt": article.get("publishedAt", ""),
            # BUG FIX: Actual deduplication check against seen URLs
            "is_duplicate": url in seen_article_urls,
        })
        seen_article_urls.add(url)

    return formatted


# --------------------------------------------------------------
#  Tool schema for OpenAI function calling
# --------------------------------------------------------------
tools = [
    {
        "type": "function",
        "function": {
            "name": "fetch_news_article",
            "description": "Fetch the latest news articles from a news API.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "country": {
                        "type": "string",
                        "description": "Country code e.g. 'ng' for Nigeria",
                    },
                    "search_keyword": {  # BUG FIX: matches renamed function parameter
                        "type": "string",
                        "description": "Topic to search for e.g. 'technology'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of articles to return",
                    },
                },
                # BUG FIX: All parameters are required so the model doesn't skip them
                "required": [ "country", "search_keyword", "limit"],
            },
        },
    }
]


# --------------------------------------------------------------
#  Pydantic response models
# --------------------------------------------------------------
class NewsArticle(BaseModel):
    title: str = Field(description="Title of the news article")
    summary: str = Field(description="Short summary of the news article")
    url: str = Field(description="Link to the full article")
    is_duplicate: bool = Field(description="Whether this article was already seen before")


class NewsResponse(BaseModel):
    articles: list[NewsArticle] = Field(description="List of news articles fetched")
    message: str = Field(description="A brief assistant message summarizing what was found")


# --------------------------------------------------------------
#  Dispatcher: maps tool name → Python function
# --------------------------------------------------------------
def call_function(name: str, args: dict):
    if name == "fetch_news_article":
        return fetch_news_article(**args)
    raise ValueError(f"Unknown tool: {name}")


# --------------------------------------------------------------
#  Main agent loop
# --------------------------------------------------------------
def run_news_agent(user_query: str) -> NewsResponse | None:
    system_prompt = (
        "You are a helpful news assistant. Your task is to fetch the latest news articles "
        "and summarize them clearly. Avoid reporting duplicate articles."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]

    # Step 1: Ask model — it should decide to call fetch_news_article
    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=tools,
    )

    tool_calls = completion.choices[0].message.tool_calls

    if not tool_calls:
        print("[WARN] Model did not call any tool.")
        return None

    # Step 2: Execute each tool call
    messages.append(completion.choices[0].message)  # assistant message with tool_calls

    for tool_call in tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        print(f"[TOOL] Calling '{name}' with args: {args}")
        result = call_function(name, args)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result),
            }
        )

    # Step 3: Final structured parse — BUG FIX: do NOT pass tools here,
    # otherwise model may try to call a tool again instead of returning structured output
    completion_2 = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=messages,
        response_format=NewsResponse,  # No tools param here
    )

    parsed = completion_2.choices[0].message.parsed
    return parsed


# --------------------------------------------------------------
#  Entry point
# --------------------------------------------------------------
if __name__ == "__main__":
    result = run_news_agent(
        "Fetch the 5 latest news articles about technology from Nigeria."
    )

    if result:
        print(f"\nAssistant: {result.message}\n")
        for article in result.articles:
            duplicate_tag = " [DUPLICATE]" if article.is_duplicate else ""
            print(f"- {article.title}{duplicate_tag}")
            print(f"  {article.summary}")
            print(f"  {article.url}\n")