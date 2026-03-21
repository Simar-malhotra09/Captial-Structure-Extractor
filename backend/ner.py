"""
Extract all company/entity names from a debt note HTML file.
Usage: python3 ner.py path/to/debt_note.html
"""

import sys
import os
import re
import json
import httpx


def html_to_text(html: str) -> str:
    """Strip HTML to plain text. No bs4 needed."""
    # Remove ix tags but keep their text content
    text = re.sub(r'<ix:[^>]*>([^<]*)</ix:[^>]*>', r'\1', html)
    # Remove all other tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Clean whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def extract_entities(text: str, api_key: str) -> list[str]:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "temperature": 0,
            "messages": [{"role": "user", "content": f"""This is a debt disclosure from an SEC 10-K filing. List only the companies that this document is ABOUT — the parent company, any subsidiaries, and any entities that issue debt or have obligations described here.

Do NOT include: banks, trustees, administrative agents, collateral agents, counterparties, or lenders.

Be thorough — scan the entire text for every issuer/borrower/guarantor entity. Return ONLY a JSON array of strings.

{text[:20000]}"""}],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    
    try:
        return json.loads(raw)
    except:
        fb = raw.find("[")
        lb = raw.rfind("]")
        if fb != -1 and lb > fb:
            return json.loads(raw[fb:lb+1])
        return []


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ner.py <debt_note.html>", file=sys.stderr)
        sys.exit(1)
    
    filepath = sys.argv[1]
    with open(filepath) as f:
        html = f.read()
    
    text = html_to_text(html)
    print(f"Extracted {len(text)} chars of text", file=sys.stderr)
    print(f"First 200 chars: {text[:200]}", file=sys.stderr)
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY set", file=sys.stderr)
        sys.exit(1)
    
    entities = extract_entities(text, api_key)
    print(json.dumps(entities, indent=2))


if __name__ == "__main__":
    main()
