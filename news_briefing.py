#!/usr/bin/env python3
"""
news_briefing.py

Haalt RSS-feeds (en optioneel een Reddit-multireddit) op, groepeert ze per
categorie, capt elke categorie op een ingesteld aantal, en schrijft het
resultaat weg als Markdown + HTML. Geen AI-call, geen API key nodig — plak
output/latest.md zelf in een AI-chat als je een samenvatting wil, of lees
'm rechtstreeks via output/latest.html. Optioneel: verstuur via e-mail.

Gebruik:
    python3 news_briefing.py                # gebruikt config.yaml
    python3 news_briefing.py --config x.yaml
    python3 news_briefing.py --dry-run       # haalt feeds op, print titels, schrijft niks weg
"""

import argparse
import html
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import yaml

# ---------------------------------------------------------------------------
# Config & fetching
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_reddit_feed_url(reddit_cfg: dict) -> str:
    """Bouwt een multireddit RSS-url met sort=top&t=day, zodat Reddit zelf
    de 'top van vandaag' ranking doet (score-based) - geen eigen heuristiek
    nodig voor 'belangrijkheid' binnen deze categorie.
    """
    subs = "+".join(reddit_cfg["subreddits"])
    sort = reddit_cfg.get("sort", "top")
    time_filter = reddit_cfg.get("time_filter", "day")
    limit = reddit_cfg.get("fetch_limit", 30)
    return f"https://old.reddit.com/r/{subs}/{sort}/.rss?t={time_filter}&limit={limit}"


def fetch_articles(feeds: list, lookback_hours: int) -> dict:
    """Haalt alle feeds op en groepeert entries per category.
    Retourneert dict: {category: [ {title, link, summary, source}, ... ]}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    by_category: dict[str, list] = {}

    for feed_cfg in feeds:
        url = feed_cfg["url"]
        category = feed_cfg.get("category", "Overig")
        parsed = feedparser.parse(url)

        if parsed.bozo and not parsed.entries:
            print(f"  [!] kon feed niet lezen: {url} ({parsed.bozo_exception})", file=sys.stderr)
            continue

        feed_source_name = parsed.feed.get("title", url)
        is_reddit = "reddit.com" in url

        for entry in parsed.entries:
            published = _entry_datetime(entry)
            if published and published < cutoff:
                continue  # te oud, sla over

            link = entry.get("link", "")
            source_name = _reddit_source(link) if is_reddit else feed_source_name
            if is_reddit:
                # feed zelf via old.reddit.com ophalen (stabieler voor multireddit-RSS),
                # maar de link die je te zien krijgt naar de gewone site sturen
                link = link.replace("old.reddit.com", "www.reddit.com")

            summary = _clean_summary(entry.get("summary", ""))
            if _is_useless_summary(summary):
                summary = ""

            article = {
                "title": html.unescape(entry.get("title", "(geen titel)")),
                "link": link,
                "summary": summary,
                "source": source_name,
                "image": _extract_image(entry),
            }
            by_category.setdefault(category, []).append(article)

    return by_category


def _entry_datetime(entry):
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def _reddit_source(link: str) -> str:
    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", link)
    return f"r/{m.group(1)}" if m else "Reddit"


def _extract_image(entry) -> str | None:
    """Probeert een afbeelding te vinden voor dit item, met een paar
    fallbacks: media:thumbnail/content (veel nieuwsfeeds), enclosures, en
    als laatste de eerste <img src="..."> in de ruwe (HTML) summary - dat is
    hoe Reddit's RSS de thumbnail van een post insluit."""
    for field in ("media_thumbnail", "media_content"):
        media = entry.get(field)
        if media:
            url = media[0].get("url")
            if url:
                return url

    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]

    raw = entry.get("summary", "") or ""
    m = re.search(r'<img[^>]+src="([^"]+)"', raw)
    if m:
        return m.group(1)

    return None


def _clean_summary(raw: str, max_len: int = 400) -> str:
    """Maakt een RSS-summary leesbaar. Belangrijke volgorde: entities EERST
    ontleden (&nbsp; -> spatie, &amp; -> &, ...) voordat we op inhoud
    controleren - anders herkent _is_useless_summary bv. 'submitted by' niet
    omdat er dan nog steeds '&nbsp;submitted&nbsp;by' staat.
    """
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)  # spatie i.p.v. niks, anders plakken woorden aan elkaar
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\xa0]", " ", text)  # onzichtbare unicode-tekens
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def _is_useless_summary(text: str) -> bool:
    """Filtert placeholder-samenvattingen die geen echte inhoud geven:
    - Hacker News' RSS zet hier letterlijk enkel 'Comments' in.
    - Reddit's RSS zet hier voor link-posts enkel 'submitted by /u/x ...' in.
    - Alles onder de ~20 tekens is te kort om nuttig te zijn.
    """
    stripped = text.strip().lower()
    if not stripped:
        return True
    if stripped == "comments":
        return True
    if stripped.startswith("submitted by"):
        return True
    if len(stripped) < 20:
        return True
    return False


def cap_articles(by_category: dict, category_limits: dict, max_per_source: dict, default_limit: int = 10) -> dict:
    """Capt elke categorie op het ingestelde aantal. Voor categorieën in
    max_per_source wordt daarbij ook een limiet per bron (bv. per subreddit)
    gehandhaafd, zodat 1 hyperactieve sub (r/interestingasfuck bv.) niet de
    hele categorie inpikt terwijl rustigere bronnen nooit aan bod komen.
    De volgorde van de bronlijst blijft behouden (chronologisch of, voor
    Reddit, Reddit's eigen top/day-ranking) - we filteren enkel uit, we
    herschikken niet.
    """
    capped = {}
    for cat, articles in by_category.items():
        limit = category_limits.get(cat, default_limit)
        per_source_cap = max_per_source.get(cat)

        if not per_source_cap:
            capped[cat] = articles[:limit]
            continue

        selected = []
        counts: dict[str, int] = {}
        for a in articles:
            src = a["source"]
            if counts.get(src, 0) >= per_source_cap:
                continue
            selected.append(a)
            counts[src] = counts.get(src, 0) + 1
            if len(selected) >= limit:
                break
        capped[cat] = selected

    return capped


# ---------------------------------------------------------------------------
# Markdown output (voor als je 'm zelf in een AI-chat wil plakken)
# ---------------------------------------------------------------------------

def build_raw_markdown(by_category: dict) -> str:
    lines = [
        "> Plak dit bestand in een AI-chat met bv. de vraag: \"Vat dit samen "
        "per categorie in 3-6 bullets, meest belangrijke eerst, dupes combineren, "
        "bron erbij tussen haakjes.\"\n",
    ]

    for category, articles in by_category.items():
        if not articles:
            continue
        lines.append(f"## {category}\n")
        for a in articles:
            lines.append(f"### {a['title']}")
            lines.append(f"*{a['source']}* — [link]({a['link']})\n")
            if a["summary"]:
                lines.append(f"{a['summary']}\n")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output schrijven
# ---------------------------------------------------------------------------

def write_output(by_category: dict, output_dir: str) -> tuple[Path, Path]:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"# Nieuwsartikelen — {today}\n\n"

    markdown_full = header + build_raw_markdown(by_category)
    html_full = build_html(by_category, today)

    md_path = Path(output_dir) / f"briefing-{today}.md"
    md_path.write_text(markdown_full, encoding="utf-8")

    html_path = Path(output_dir) / f"briefing-{today}.html"
    html_path.write_text(html_full, encoding="utf-8")

    # handig: altijd ook een "latest" kopie zodat je 1 vaste pad/URL hebt
    (Path(output_dir) / "latest.md").write_text(markdown_full, encoding="utf-8")
    (Path(output_dir) / "latest.html").write_text(html_full, encoding="utf-8")

    return md_path, html_path


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

# vaste kleur + icoon per categorie - stabiel over dagen heen. Onbekende
# categorienamen (die je zelf toevoegt) vallen terug op de grijze/📰 default.
_CATEGORY_STYLE = {
    "België":       {"color": "#c8963e", "icon": "🇧🇪"},
    "Cybersecurity": {"color": "#1d6f6f", "icon": "🛡️"},
    "Tech":         {"color": "#a1483a", "icon": "💻"},
    "AI":           {"color": "#6b4c9a", "icon": "🤖"},
    "Gaming":       {"color": "#2f6b8f", "icon": "🎮"},
    "Wereld":       {"color": "#2f6b3a", "icon": "🌍"},
    "Reddit":       {"color": "#cc5023", "icon": "👽"},
}
_DEFAULT_STYLE = {"color": "#5a5a5a", "icon": "📰"}


def _style_for(category: str) -> dict:
    return _CATEGORY_STYLE.get(category, _DEFAULT_STYLE)


def _domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def build_html(by_category: dict, today: str) -> str:
    categories = [c for c, arts in by_category.items() if arts]
    total = sum(len(v) for v in by_category.values())

    nav_items = []
    sections = []

    for cat in categories:
        articles = by_category[cat]
        style = _style_for(cat)
        color = style["color"]
        icon = style["icon"]
        anchor = _slugify(cat)

        nav_items.append(
            f'<a href="#{anchor}" class="nav-pill" style="--accent:{color}">'
            f'{icon} {html.escape(cat)} <span class="count">{len(articles)}</span></a>'
        )

        cards = []
        for i, a in enumerate(articles, start=1):
            summary = f'<p class="summary">{html.escape(a["summary"])}</p>' if a["summary"] else ""
            domain = _domain(a["link"])
            favicon = (
                f'<img class="favicon" src="https://www.google.com/s2/favicons?domain={html.escape(domain)}&sz=32" alt="" loading="lazy">'
                if domain else ""
            )
            thumb = (
                f'<img class="thumb" src="{html.escape(a["image"])}" alt="" loading="lazy">'
                if a.get("image") else ""
            )
            rank = "" if thumb else f'<span class="rank">{i:02d}</span>'
            cards.append(f"""
        <article class="card">
          {thumb}
          {rank}
          <div class="card-body">
            <h3><a href="{html.escape(a['link'])}" target="_blank" rel="noopener">{html.escape(a['title'])}</a></h3>
            <div class="meta">{favicon}<span>{html.escape(a['source'])}</span></div>
            {summary}
          </div>
        </article>""")

        sections.append(f"""
      <section id="{anchor}" class="category" style="--accent:{color}">
        <h2><span class="cat-icon">{icon}</span> {html.escape(cat)} <span class="count">{len(articles)}</span></h2>
        <div class="cards">{''.join(cards)}</div>
      </section>""")

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ochtendbriefing — {today}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Lora:wght@500;600;700&family=Karla:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Karla', -apple-system, sans-serif;
    max-width: 920px;
    margin: 0 auto;
    padding: 40px 20px 80px;
    line-height: 1.55;
    color: #1c1c1c;
    background: #fafaf7;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #131417; color: #e8e6e1; }}
    .card {{ background: #1d1e22 !important; border-color: #2c2e33 !important; }}
    .meta span {{ color: #9a9a9a !important; }}
    .summary {{ color: #c7c5c0 !important; }}
    header {{ border-color: #33353a !important; }}
    .rank {{ color: #45474d !important; }}
  }}

  header {{
    border-bottom: 3px solid #1c1c1c;
    padding-bottom: 18px;
    margin-bottom: 8px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }}
  h1 {{
    font-family: 'Lora', Georgia, serif;
    font-size: 2rem;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .subtitle {{ font-size: 0.9rem; opacity: 0.6; margin: 0; }}

  nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 24px 0 36px; }}
  .nav-pill {{
    font-size: 0.85rem;
    text-decoration: none;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    border: 1.5px solid var(--accent);
    border-radius: 999px;
    padding: 6px 14px;
    transition: background 0.15s ease, transform 0.1s ease;
  }}
  .nav-pill:hover {{ background: color-mix(in srgb, var(--accent) 18%, transparent); transform: translateY(-1px); }}
  .nav-pill .count {{ opacity: 0.7; }}

  .category {{ margin-bottom: 44px; scroll-margin-top: 16px; }}
  .category h2 {{
    font-family: 'Lora', Georgia, serif;
    font-size: 1.35rem;
    font-weight: 600;
    color: var(--accent);
    border-bottom: 2px solid color-mix(in srgb, var(--accent) 35%, transparent);
    padding-bottom: 8px;
    margin-bottom: 16px;
    display: flex;
    align-items: baseline;
    gap: 8px;
  }}
  .cat-icon {{ font-size: 1.15rem; }}
  .category h2 .count {{ font-family: 'Karla', sans-serif; font-size: 0.8rem; font-weight: 400; opacity: 0.55; margin-left: auto; }}

  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 12px;
  }}
  .card {{
    background: #ffffff;
    border: 1px solid #e8e6e0;
    border-radius: 10px;
    padding: 16px 18px;
    display: flex;
    gap: 12px;
    transition: border-color 0.15s ease;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .thumb {{
    width: 84px;
    height: 84px;
    object-fit: cover;
    border-radius: 8px;
    flex-shrink: 0;
    background: #eee;
  }}
  .rank {{
    font-family: 'Lora', Georgia, serif;
    font-size: 0.8rem;
    font-weight: 600;
    color: #d8d5cd;
    line-height: 1.4;
    flex-shrink: 0;
  }}
  .card-body {{ min-width: 0; }}
  .card h3 {{ font-size: 1rem; font-weight: 500; margin: 0 0 6px; line-height: 1.35; }}
  .card h3 a {{ color: inherit; text-decoration: none; }}
  .card h3 a:hover {{ text-decoration: underline; }}
  .meta {{ display: flex; align-items: center; gap: 6px; font-size: 0.78rem; color: #888; margin-bottom: 8px; }}
  .favicon {{ width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }}
  .summary {{ font-size: 0.92rem; color: #444; margin: 0; }}

  footer {{ margin-top: 40px; font-size: 0.8rem; opacity: 0.5; text-align: center; }}
</style>
</head>
<body>
  <header>
    <h1>Ochtendbriefing</h1>
    <p class="subtitle">{today} · {total} artikelen · {len(categories)} categorieën</p>
  </header>

  <nav>{''.join(nav_items)}</nav>

  {''.join(sections)}

  <footer>Gegenereerd door news_briefing.py</footer>
</body>
</html>"""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "categorie"


# ---------------------------------------------------------------------------
# E-mail (optioneel)
# ---------------------------------------------------------------------------

def send_email(md_path: Path, html_path: Path, cfg: dict):
    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled"):
        return

    smtp_pass = os.environ.get(email_cfg["smtp_pass_env"], "")
    if not smtp_pass:
        print(f"  [!] env var {email_cfg['smtp_pass_env']} niet gezet, mail overgeslagen", file=sys.stderr)
        return

    msg = MIMEMultipart("alternative")
    today = datetime.now().strftime("%Y-%m-%d")
    msg["Subject"] = f"{email_cfg.get('subject_prefix', 'Briefing')} — {today}"
    msg["From"] = email_cfg["smtp_user"]
    msg["To"] = email_cfg["to"]

    msg.attach(MIMEText(md_path.read_text(encoding="utf-8"), "plain"))
    msg.attach(MIMEText(html_path.read_text(encoding="utf-8"), "html"))

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["smtp_user"], smtp_pass)
        server.send_message(msg)
    print("  -> e-mail verstuurd")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="alleen feeds ophalen en titels printen, niks wegschrijven")
    args = parser.parse_args()

    cfg = load_config(args.config)

    feeds = list(cfg.get("feeds", []))

    reddit_cfg = cfg.get("reddit", {})
    if reddit_cfg.get("enabled") and reddit_cfg.get("subreddits"):
        feeds.append({
            "url": build_reddit_feed_url(reddit_cfg),
            "category": reddit_cfg.get("category", "Reddit"),
        })

    print("Feeds ophalen...")
    by_category = fetch_articles(feeds, cfg.get("lookback_hours", 30))

    category_limits = cfg.get("category_limits", {})
    max_per_source = cfg.get("max_per_source", {})
    by_category = cap_articles(by_category, category_limits, max_per_source, cfg.get("default_category_limit", 10))

    total = sum(len(v) for v in by_category.values())
    print(f"  -> {total} artikelen gevonden over {len(by_category)} categorieën")

    if args.dry_run:
        for cat, arts in by_category.items():
            print(f"\n[{cat}] ({len(arts)})")
            for a in arts:
                print(f"  - {a['title']}  ({a['source']})")
        return

    if total == 0:
        print("Geen nieuwe artikelen binnen het lookback-venster, niks weggeschreven.")
        return

    md_path, html_path = write_output(by_category, cfg.get("output_dir", "output"))
    print(f"  -> geschreven naar {md_path} en {html_path}")
    print("  -> plak output/latest.md in een AI-chat voor een samenvatting, of lees 'm rechtstreeks.")

    send_email(md_path, html_path, cfg)
    print("Klaar.")


if __name__ == "__main__":
    main()
