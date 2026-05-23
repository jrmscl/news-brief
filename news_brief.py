#!/usr/bin/env python3
"""Parse a Feedly OPML, fetch each feed, keep recent items, print them grouped by category.

Stdlib only. Output is meant to be piped into Claude for summarization.

Usage:
    python3 news_brief.py OPML_FILE [--hours 24] [--only "Tech FR,Tech EN,Featured"] [--json]
"""
import argparse
import concurrent.futures
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

UA = "Mozilla/5.0 (compatible; news-brief/1.0; +https://claude.ai)"
NS = {"atom": "http://www.w3.org/2005/Atom"}


def read_opml(path):
    """Return {category: [(title, xml_url), ...]}."""
    tree = ET.parse(path)
    body = tree.getroot().find("body")
    cats = {}
    for cat in body.findall("outline"):
        name = cat.get("title") or cat.get("text") or "Sans catégorie"
        feeds = []
        for feed in cat.findall("outline"):
            url = feed.get("xmlUrl")
            if url:
                feeds.append((feed.get("title") or feed.get("text") or url, url))
        if feeds:
            cats[name] = feeds
    return cats


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt
    except ValueError:
        return None


def localname(tag):
    return tag.rsplit("}", 1)[-1]


def extract_items(xml_bytes):
    """Return list of dicts: {title, link, date, summary} from RSS or Atom."""
    items = []
    root = ET.fromstring(xml_bytes)
    # RSS: channel/item ; Atom: feed/entry
    rss_items = root.findall(".//item")
    if rss_items:
        for it in rss_items:
            get = lambda t: (it.findtext(t) or "").strip()
            items.append({
                "title": get("title"),
                "link": get("link"),
                "date": parse_date(get("pubDate") or get("{http://purl.org/dc/elements/1.1/}date")),
                "summary": get("description"),
            })
        return items
    for entry in root.findall(".//atom:entry", NS) or [e for e in root.iter() if localname(e.tag) == "entry"]:
        title = ""
        link = ""
        date = None
        summary = ""
        for child in entry:
            tag = localname(child.tag)
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "link":
                link = child.get("href") or link or (child.text or "")
            elif tag in ("updated", "published"):
                date = date or parse_date(child.text)
            elif tag in ("summary", "content"):
                summary = (child.text or "").strip()
        items.append({"title": title, "link": link, "date": date, "summary": summary})
    return items


def fetch(feed):
    title, url = feed
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        return title, extract_items(data), None
    except Exception as exc:  # noqa: BLE001 - report and continue
        return title, [], str(exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("opml")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--only", default="", help="comma-separated category names to include")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cats = read_opml(args.opml)
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        cats = {k: v for k, v in cats.items() if k in wanted}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    result = {}
    errors = []

    all_feeds = [(cat, feed) for cat, feeds in cats.items() for feed in feeds]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        future_map = {ex.submit(fetch, feed): (cat, feed) for cat, feed in all_feeds}
        for fut in concurrent.futures.as_completed(future_map):
            cat, feed = future_map[fut]
            title, items, err = fut.result()
            if err:
                errors.append(f"{cat} / {title}: {err}")
                continue
            recent = []
            for it in items:
                d = it["date"]
                if d is None:
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if d >= cutoff:
                    recent.append({"title": it["title"], "link": it["link"],
                                   "date": d.isoformat(), "source": title})
            if recent:
                result.setdefault(cat, []).extend(recent)

    for cat in result:
        result[cat].sort(key=lambda x: x["date"], reverse=True)

    if args.json:
        print(json.dumps({"items": result, "errors": errors}, ensure_ascii=False, indent=2))
        return

    total = sum(len(v) for v in result.values())
    print(f"# Articles des dernières {args.hours}h — {total} au total\n")
    for cat in sorted(result, key=lambda c: -len(result[c])):
        print(f"## {cat} ({len(result[cat])})")
        for it in result[cat]:
            print(f"- [{it['source']}] {it['title']}\n  {it['link']}")
        print()
    if errors:
        print(f"\n# {len(errors)} flux en erreur (ignorés)", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
