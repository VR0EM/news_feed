# news-briefing

Persoonlijke ochtendbriefing: haalt elke dag RSS-feeds op uit meerdere bronnen,
groepeert ze per categorie, en zet de ruwe artikelen (titel, bron, link,
samenvatting) weg als Markdown + HTML in `output/latest.md` / `output/latest.html`.

**Geen API key, geen kosten.** Er zit geen AI-call in het script. Je opent
`output/latest.md` 's ochtends en plakt 'm zelf in Claude.ai (of een andere
AI-chat) met iets als *"vat dit samen per categorie, 3-6 bullets, belangrijkste
eerst"* — of je leest 'm gewoon rechtstreeks, de artikelen staan er al netjes
gegroepeerd en met samenvatting bij.

## Snel testen (zonder Docker)

```bash
pip install -r requirements.txt
python3 news_briefing.py --dry-run     # haalt alleen feeds op, print titels, schrijft niks weg
python3 news_briefing.py               # volledige run, schrijft output/latest.md + .html
```

Pas `config.yaml` aan: voeg/verwijder feeds, wijzig `category_limits` (hoeveel
artikelen per categorie), pas de `reddit.subreddits` lijst aan, of zet
`email.enabled: true` als je 'm liever in je mailbox krijgt dan als bestand.

**Categorieën:** België (5), Cybersecurity (10), Tech (15), AI (8), Gaming (10),
Wereld (8), Reddit (20, via een gecombineerde multireddit met Reddit's eigen
"top van de dag"-sortering). Voor de RSS-categorieën is de volgorde
chronologisch (nieuwste eerst, gepoold uit meerdere bronnen) — zonder AI-call
kunnen we "belangrijkheid" niet objectief inschatten. Reddit's top/day-sort
is wél een echte populariteitsranking (score-based).

## Draaien op Bletchley (Proxmox, aanbevolen)

**Optie A — losse LXC + cron (simpelst)**

1. Maak een lichte Debian LXC aan (1 vCPU, 512MB RAM volstaat ruim).
2. Clone/kopieer deze map erin, `pip install -r requirements.txt --break-system-packages`.
3. Crontab (`crontab -e`):
   ```
   30 6 * * * cd /pad/naar/news-briefing && python3 news_briefing.py >> /var/log/news-briefing.log 2>&1
   ```
4. Serveer `output/latest.html` via een simpele nginx container of file share
   zodat je 's ochtends gewoon één bookmark opent op je telefoon, of open
   `output/latest.md` en plak 'm in een AI-chat voor een samenvatting.

**Optie B — Docker (nette scheiding, herbruikbaar op andere hosts)**

```bash
cp .env.example .env        # alleen invullen als je email.enabled: true gebruikt
docker compose build
docker compose up -d
```

`docker-compose.yml` gebruikt [ofelia](https://github.com/mcuadros/ofelia) als
scheduler-sidecar en runt de briefing elke dag om 06:30. Pas het cron-schema
aan via het `ofelia.job-run.morning-briefing.schedule` label
(standaard-crontab syntax, seconden erbij).

Handmatig één keer draaien zonder op de scheduler te wachten:
```bash
docker compose run --rm news-briefing
```

## Feeds aanpassen

Elke feed in `config.yaml` heeft een `url` en `category`. Categorieën bepalen
de `##`-koppen in de uiteindelijke briefing — noem ze zoals je ze wil zien.
Wil je een bron toevoegen die geen RSS heeft? De meeste sites hebben er wel
één verstopt (vaak `/feed`, `/rss`, of te vinden via view-source op zoek naar
`application/rss+xml`). Reddit-subs werken altijd via `https://reddit.com/r/<sub>/.rss`.

## Later alsnog automatisch laten samenvatten?

Als je ooit toch de AI-stap wil automatiseren (bv. omdat je toch al Claude
Pro/Max hebt en er zo geen extra API-kosten bijkomen relevant zijn), is dat
een kleine toevoeging: `build_raw_markdown()` teruginruilen voor een call naar
de Anthropic API met het bestaande artikel-blok als prompt. Zeg het gerust
als je dat op een later moment wil laten inbouwen.

## Bekende beperkingen

- Geen deduplicatie tussen feeds op URL-niveau — als 3 bronnen hetzelfde
  nieuws oppikken, zie je het 3 keer in het bestand. Bij het handmatig
  samenvatten in een AI-chat valt dat vanzelf weg.
- `_clean_summary` is een lichte regex-strip, geen volwaardige HTML-parser.
  Voor de meeste RSS-feeds is dat voldoende.
- Als een feed tijdelijk down is, wordt die feed overgeslagen (met een
  waarschuwing in stderr) in plaats van dat het hele script faalt.
