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


def query_database(database_id, page_size=50, filter_obj=None):
    """Returns the list of page objects from a database (single page of results, no pagination yet).
    Pass filter_obj to apply a Notion filter, e.g. a "contains" search on a text property."""
    url = f"{NOTION_BASE_URL}/databases/{database_id}/query"
    body = {"page_size": page_size}
    if filter_obj:
        body["filter"] = filter_obj
    resp = requests.post(url, headers=_headers(), json=body, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


def query_database_all(database_id, filter_obj=None, max_pages=10):
    """Follows pagination to collect every matching row, not just the first page.
    Used for historical/trend lookups where missing older records would be misleading."""
    all_results = []
    cursor = None
    for _ in range(max_pages):
        body = {"page_size": 100}
        if filter_obj:
            body["filter"] = filter_obj
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{NOTION_BASE_URL}/databases/{database_id}/query", headers=_headers(), json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        all_results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return all_results


def query_data_source(data_source_id, page_size=50, filter_obj=None):
    """
    For multi-source databases (Notion's newer structure, e.g. a database that
    bundles Service Request + Daily Job Log Master + Job Update Report into one
    view). These need the newer /v1/data_sources/{id}/query endpoint and a
    newer API version rather than the classic /v1/databases/{id}/query used
    for simple single-source databases elsewhere in this file.
    """
    headers = _headers()
    headers["Notion-Version"] = "2025-09-03"
    url = f"{NOTION_BASE_URL}/data_sources/{data_source_id}/query"
    body = {"page_size": page_size}
    if filter_obj:
        body["filter"] = filter_obj
    resp = requests.post(url, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


def query_data_source_all(data_source_id, filter_obj=None, max_pages=10):
    """Same as query_database_all, but for the newer data-sources endpoint."""
    headers = _headers()
    headers["Notion-Version"] = "2025-09-03"
    all_results = []
    cursor = None
    for _ in range(max_pages):
        body = {"page_size": 100}
        if filter_obj:
            body["filter"] = filter_obj
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{NOTION_BASE_URL}/data_sources/{data_source_id}/query", headers=headers, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        all_results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return all_results


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


def prop_status(props, name):
    """Notion's 'status' property type (distinct from 'select') — same shape, different key."""
    p = props.get(name)
    if not p or p["type"] != "status":
        return None
    st = p.get("status")
    return st.get("name") if st else None


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


def get_page_blocks(page_id, max_pages=10):
    """
    Reads a Notion page's content as blocks (not a database query) — used
    for pages like "Current Job Analyses" that are just a running list of
    headings and links, not structured rows.
    """
    all_blocks = []
    cursor = None
    for _ in range(max_pages):
        url = f"{NOTION_BASE_URL}/blocks/{page_id}/children"
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(url, headers=_headers(), params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        all_blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return all_blocks


def block_plain_text(block):
    """Extracts plain text + first link URL (if any) from a block's rich_text, whatever the block type."""
    block_type = block.get("type")
    type_data = block.get(block_type, {})
    rich_text = type_data.get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich_text)
    url = None
    for t in rich_text:
        link = (t.get("text") or {}).get("link")
        if link and link.get("url"):
            url = link["url"]
            break
    # Bookmark/embed blocks store the URL directly, not in rich_text
    if not url and "url" in type_data:
        url = type_data["url"]
    return text.strip(), url
