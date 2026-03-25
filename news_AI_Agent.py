import os
import json
import time
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

WORLD_NEWS_API_KEY = os.getenv("WORLD_NEWS_API_KEY")          # World News API key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

WORLD_NEWS_API_BASE = "https://api.worldnewsapi.com/search-news"
BAILEYS_SERVER = "http://localhost:3000"

client = OpenAI(api_key=OPENAI_API_KEY)

# Tracks seen article URLs globally to avoid duplicates
seen_article_urls: set[str] = set()

app = Flask(__name__)


# ---------------------------------------------
#  Tool 1-Fetch news articles from World News API
# ---------------------------------------------
def fetch_news_article(
    country: str = "ng",
    search_keyword: str = "technology",
    limit: int = 5,
) -> list[dict]:
    params = {
        "source-country": country,
        "text": search_keyword,
        "number": limit,
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

    articles = data.get("news", [])

    formatted = []
    for i, article in enumerate(articles):
        url = article.get("url", "")
        formatted.append({
            "id": str(i),
            "title": article.get("title", ""),
            "summary": article.get("text", "")[:300],
            "url": url,
            "publishedAt": article.get("publish_date", ""),
            "is_duplicate": url in seen_article_urls,
        })
        seen_article_urls.add(url)

    return formatted


# ---------------------------------------------
#  Tool 2 -Send one article as a WhatsApp message
# ---------------------------------------------
def send_to_whatsapp(jid: str, message: str) -> dict:
    try:
        response = requests.post(
            f"{BAILEYS_SERVER}/send",
            json={"jid": jid, "message": message},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f" Failed to send: {e}")
        return {"success": False, "error": str(e)}


def format_article_message(article) -> str:
    return (
        f"📰 *{article.title}*\n\n"
        f"{article.summary}...\n\n"
        f"🔗 {article.url}"
    )


# --------------------------------------------------------------
#  Tool schema for OpenAI function calling(Used to define and register tools with the model)
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
                    "search_keyword": {
                        "type": "string",
                        "description": "Topic to search for e.g. 'technology'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of articles to return",
                    },
                },
                "required": ["country", "search_keyword", "limit"],
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
#  Dispatcher
# --------------------------------------------------------------
def call_function(name: str, args: dict):
    if name == "fetch_news_article":
        return fetch_news_article(**args)
    raise ValueError(f"Unknown tool: {name}")


# --------------------------------------------------------------
#  Main agent — runs when user triggers the bot
# --------------------------------------------------------------
def run_news_agent(user_query: str) -> NewsResponse | None:
    system_prompt = (
        "You are a helpful news assistant. Fetch the latest news articles "
        "based on the user's request and summarize them clearly. "
        "Always fetch from Nigeria (country='ng'). Avoid duplicates."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]

    completion = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=tools,
    )

    tool_calls = completion.choices[0].message.tool_calls
    if not tool_calls:
        print("[WARN] Model did not call any tool.")
        return None

    messages.append(completion.choices[0].message)

    for tool_call in tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)
        print(f"[TOOL] Calling '{name}' with args: {args}")
        result = call_function(name, args)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result),
        })

    completion_2 = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=messages,
        response_format=NewsResponse,
    )

    return completion_2.choices[0].message.parsed


# --------------------------------------------------------------
#  Flask webhook — Baileys calls this when user sends a trigger
# --------------------------------------------------------------
@app.route("/fetch-news", methods=["POST"])
def fetch_news_webhook():
    data = request.get_json()
    WHATSAPP_JID = data.get("jid")
    query = data.get("query", "latest technology news from Nigeria")

    if not WHATSAPP_JID:
        return jsonify({"error": "jid is required"}), 400

    print(f"\n[BOT] Triggered by {WHATSAPP_JID} with query: '{query}'")

    result = run_news_agent(query)

    if not result or not result.articles:
        send_to_whatsapp(WHATSAPP_JID, " Sorry, I couldn't find any news right now. Try again later.")
        return jsonify({"status": "no articles"}), 200

    sent = 0
    skipped = 0

    for article in result.articles:
        if article.is_duplicate:
            skipped += 1
            continue
        message = format_article_message(article)
        send_to_whatsapp(WHATSAPP_JID, message)
        sent += 1
        time.sleep(2)  # Avoid WhatsApp rate limiting

    # Send a final summary message
    send_to_whatsapp(WHATSAPP_JID, f"✅ Done! Sent {sent} article(s). Type *get news* anytime for fresh updates.")

    print(f"[BOT] Done. Sent: {sent} | Skipped duplicates: {skipped}")
    return jsonify({"status": "ok", "sent": sent, "skipped": skipped}), 200


# --------------------------------------------------------------
#  Entry point
# --------------------------------------------------------------
if __name__ == "__main__":
    print("[AGENT] 🚀 Python news agent running on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000)