"""
Minimal Notion REST API client — no SDK needed, just requests.
Requires a Notion internal integration token (NOTION_TOKEN) that has been
shared with each database you want to query, from inside Notion:
  Database → ... menu → Connections → add your integration.
"""

import os
import requests

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _headers():
    token = _require_env("NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def query_database(database_id, page_size=50):
    """Returns the list of page objects from a database (single page of results, no pagination yet)."""
    url = f"{NOTION_BASE_URL}/databases/{database_id}/query"
    resp = requests.post(url, headers=_headers(), json={"page_size": page_size}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


# ---- Property extraction helpers ----
# Notion page properties are deeply nested by type; these pull out plain values.

def prop_text(props, name):
    p = props.get(name)
    if not p:
        return None
    if p["type"] == "title":
        parts = p.get("title", [])
    elif p["type"] == "rich_text":
        parts = p.get("rich_text", [])
    else:
        return None
    return "".join(t.get("plain_text", "") for t in parts).strip() or None


def prop_number(props, name):
    p = props.get(name)
    if not p or p["type"] != "number":
        return None
    return p.get("number")


def prop_select(props, name):
    p = props.get(name)
    if not p or p["type"] != "select":
        return None
    sel = p.get("select")
    return sel.get("name") if sel else None


def prop_checkbox(props, name):
    p = props.get(name)
    if not p or p["type"] != "checkbox":
        return None
    return p.get("checkbox", False)


def prop_date(props, name):
    p = props.get(name)
    if not p or p["type"] != "date":
        return None
    d = p.get("date")
    return d.get("start") if d else None
