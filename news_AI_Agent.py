import os
import time
import json
import requests
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables from .env
load_dotenv()

WORLD_NEWS_API_KEY = os.getenv("WORLD_NEWS_API_KEY")
WORLD_NEWS_API_BASE = "https://api.worldnewsapi.com/search-news"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_RECIPIENT=os.getenv("WHATSAPP_RECIPIENT")   


client = OpenAI(api_key=OPENAI_API_KEY)

BAILEYS_SERVER = "http://localhost:3000" # MY local Node.js server

# Tracks seen article URLs to detect duplicates across runs/calls
seen_article_urls: set[str] = set()


# ---------------------------------------------
#  Tool 1 - Fetch news articles from NewsAPI
# ---------------------------------------------
WORLD_NEWS_API_KEY = os.getenv("WORLD_NEWS_API_KEY")
WORLD_NEWS_API_BASE = "https://api.worldnewsapi.com/search-news"

def fetch_news_article(
    country: str = "ng",
    search_keyword: str = "technology",
    limit: int = 5,
) -> list[dict]:
    params = {
        "source-country": country,   # World News API uses "source-country" not "country"
        "text": search_keyword,       # uses "text" not "q"
        "number": limit,              # uses "number" not "pageSize"
        "api-key": WORLD_NEWS_API_KEY,
        "language": "en",
    }

    try:
        response = requests.get(WORLD_NEWS_API_BASE, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to fetch news: {e}")
        return []

    articles = data.get("news", [])  # World News API returns "news" not "articles"

    formatted = []
    for i, article in enumerate(articles):
        url = article.get("url", "")
        formatted.append({
            "id": str(i),
            "title": article.get("title", ""),
            "summary": article.get("text", "")[:300],  # returns full text, truncate it
            "url": url,
            "publishedAt": article.get("publish_date", ""),
            "is_duplicate": url in seen_article_urls,
        })
        seen_article_urls.add(url)

    return formatted
# ----------------------------------------------------------------------
#  Tool 2 - Tool to send a WhatsApp message via Baileys (Node.js server)
# ----------------------------------------------------------------------
def send_to_whatsapp(phone: str, message: str) -> dict:
    """
    Sends a single message to a WhatsApp number via the local Baileys server.
    phone: number with country code, no + or spaces e.g. "2348012345678"
    message: the text to send
    """
    try:
        response = requests.post(
            f"{BAILEYS_SERVER}/send",
            json={"phone": phone, "message": message},
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        print(f" Sent to {phone}")
        return result
    except requests.exceptions.RequestException as e:
        print(f" Failed to send to {phone}: {e}")
        return {"success": False, "error": str(e)}
 
 
def check_baileys_server() -> bool:
    """Check if the local Baileys WhatsApp server is running."""
    try:
        response = requests.get(f"{BAILEYS_SERVER}/health", timeout=5)
        data = response.json()
        if not data.get("whatsapp_connected"):
            print("Server is up but WhatsApp is not connected yet. Scan the QR code in the terminal.")
            return False
        return True
    except requests.exceptions.ConnectionError:
        print("Baileys server is not running. Start it with: node server.js")
        return False
 
 
def format_article_message(article) -> str:
    """Format a single NewsArticle into a clean WhatsApp message."""
    return (
        f"📰 *{article.title}*\n\n"
        f"{article.summary}\n\n"
        f"🔗 {article.url}"
    )


# --------------------------------------------------------------
#  Tool schema for OpenAI function calling
# --------------------------------------------------------------
#Basically,this defines the tools that the AI agent can call, including their names, descriptions, and expected parameters. The agent will use this information to decide which tool to call based on the user's query and the context
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
    # 1. Check WhatsApp server is running before doing anything
    if not check_baileys_server():
        print("\n[STOP] Please start the Baileys server first:\n  cd whatsapp-server && node server.js\n")
        exit(1)
 
    # 2. Fetch and process news articles
    result = run_news_agent(
        "Fetch the 5 latest news articles about technology from Nigeria."
    )
 
    if not result:
        print("[STOP] No articles returned.")
        exit(1)
 
    print(f"\nAssistant: {result.message}\n")
 
    # 3. Send each non-duplicate article to WhatsApp one at a time
    sent_count = 0
    skipped_count = 0
 
    for article in result.articles:
        if article.is_duplicate:
            print(f"[SKIP] Duplicate: {article.title}")
            skipped_count += 1
            continue
 
        message = format_article_message(article)
        print(f"[SEND] {article.title}")
        send_to_whatsapp(WHATSAPP_RECIPIENT, message)
 
        sent_count += 1
        time.sleep(2)  # Wait 2s between messages to avoid WhatsApp rate limiting
 
    print(f"\n✅ Done! Sent: {sent_count} | Skipped (duplicates): {skipped_count}")
 