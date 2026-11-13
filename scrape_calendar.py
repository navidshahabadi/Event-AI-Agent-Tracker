#!/usr/bin/env python3
"""
SAP Event Tracker Scraper
Steps:
  1. Opens pre-filtered SAP calendar (3rd party, Americas, in-person)
  2. Waits for you to log in
  3. Goes through every event, finds the external event website
  4. Visits that site, finds speakers/agenda, extracts names/titles/companies
  5. Saves everything to sap_events_raw.json for the tracker
"""
import asyncio, json, csv, re
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

SAP_URL = (
    "https://calendar.events.enter.sap/webapp/index.html"
    "#/list?tab=allEvents"
    "&geo_coverage=g2_CAN%2Cg3_US_NORTHEAST"
    "&event_format=IN_PERSON"
    "&event_host=THIRD_PARTY"
)
OUTPUT      = Path.home() / "sap-event-tracker" / "sap_events_raw.json"
CONTACT_CSV = Path.home() / "sap-event-tracker" / "event_contacts.csv"

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Speaker extraction ─────────────────────────────────────────────────────────
async def extract_speakers(page):
    speakers = []

    # Try schema.org JSON-LD first (most reliable)
    try:
        ld = await page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                    try {
                        const d = JSON.parse(s.textContent);
                        const items = Array.isArray(d) ? d : [d];
                        items.forEach(item => {
                            const people = item['@type'] === 'Person' ? [item]
                                : (item.performer || item.speaker || item.organizer || []);
                            (Array.isArray(people) ? people : [people]).forEach(p => {
                                if (p && p.name) out.push({
                                    name: p.name,
                                    title: p.jobTitle || '',
                                    company: (p.worksFor && p.worksFor.name) || '',
                                    raw: p.name
                                });
                            });
                        });
                    } catch(e) {}
                });
                return out;
            }""")
        if ld and len(ld) >= 2:
            speakers = ld
    except: pass

    # CSS class heuristics
    if not speakers:
        for sel in [
            '[class*="speaker" i]', '[class*="panelist" i]', '[class*="presenter" i]',
            '[class*="faculty" i]', '[class*="keynote" i]', '[class*="expert" i]',
            '[class*="person" i]', '[class*="team-member" i]', '[class*="bio" i]',
            '[itemtype*="Person"]', 'figure:has(figcaption)',
        ]:
            try:
                els = await page.query_selector_all(sel)
                if len(els) < 2: continue
                batch = []
                for el in els:
                    try:
                        raw = (await el.inner_text()).strip().replace("\n", " ")
                        if not (8 < len(raw) < 600): continue
                        ne = await el.query_selector("h2,h3,h4,strong,b,[class*='name' i]")
                        te = await el.query_selector("[class*='title' i],[class*='role' i],[class*='position' i],[class*='job' i]")
                        ce = await el.query_selector("[class*='company' i],[class*='org' i],[class*='employer' i]")
                        name    = (await ne.inner_text()).strip() if ne else ""
                        title   = (await te.inner_text()).strip() if te else ""
                        company = (await ce.inner_text()).strip() if ce else ""
                        if not name: continue
                        batch.append({"name": name, "title": title, "company": company, "raw": raw[:300]})
                    except: pass
                if batch:
                    speakers = batch
                    break
            except: continue

    # Dedupe by name
    seen, uniq = set(), []
    for s in speakers:
        k = s["name"].lower()
        if k not in seen: seen.add(k); uniq.append(s)
    return uniq

# ── Visit external site and scrape speakers ────────────────────────────────────
async def scrape_external(ctx, url):
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Scroll homepage to load lazy content
        h = await page.evaluate("document.body.scrollHeight")
        for y in range(0, h + 300, 300):
            await page.evaluate(f"window.scrollTo(0,{y})")
            await page.wait_for_timeout(30)

        # Try homepage speakers first
        speakers = await extract_speakers(page)
        if speakers:
            log(f"      {len(speakers)} speakers on homepage")
            return speakers

        # Look for speakers/agenda nav link
        for kw in ["speakers", "speaker", "agenda", "panelists", "faculty"]:
            links = await page.query_selector_all(f"nav a, header a, a[href*='{kw}']")
            for a in links:
                try:
                    href = (await a.get_attribute("href") or "").strip()
                    txt  = (await a.inner_text()).strip().lower()
                    if (kw in txt or kw in href.lower()) and href and "javascript" not in href:
                        if not href.startswith("http"):
                            base = "/".join(url.split("/")[:3])
                            href = base + "/" + href.lstrip("/")
                        if href != page.url:
                            await page.goto(href, timeout=20000, wait_until="domcontentloaded")
                            await page.wait_for_timeout(1500)
                            # Scroll the sub-page
                            h = await page.evaluate("document.body.scrollHeight")
                            for y in range(0, h + 300, 300):
                                await page.evaluate(f"window.scrollTo(0,{y})")
                                await page.wait_for_timeout(30)
                            log(f"      → {kw} page: {page.url[:60]}")
                            speakers = await extract_speakers(page)
                            if speakers:
                                log(f"      {len(speakers)} speakers found")
                                return speakers
                except: pass

        log(f"      0 speakers found")
        return []
    except Exception as e:
        log(f"      error: {e}")
        return []
    finally:
        try: await page.close()
        except: pass

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    # Load any previously saved data so we can resume
    saved = {}
    if OUTPUT.exists():
        try:
            for e in json.loads(OUTPUT.read_text()):
                saved[e["name"]] = e
        except: pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=60, args=["--start-maximized"])
        ctx  = await browser.new_context(viewport=None)
        page = await ctx.new_page()

        # ── Step 1: Open pre-filtered calendar ──────────────────────────────
        log("Opening SAP calendar (filters already in URL)…")
        await page.goto(SAP_URL)

        print("\n" + "="*55)
        print("  Log in to SAP — script starts automatically.")
        print("="*55 + "\n")

        # Wait until event rows appear (meaning logged in and page loaded)
        await page.wait_for_selector("tr.evonsiCalEventColumnListItem", timeout=300000)
        await page.wait_for_timeout(1000)
        log("Calendar loaded ✓")

        # Load all rows (click "More" until gone)
        while True:
            try:
                btn = page.locator(".sapMGrowingListTrigger").first
                if await btn.is_visible(timeout=1000):
                    await btn.click(); await page.wait_for_timeout(800)
                else: break
            except: break

        rows = await page.query_selector_all("tr.evonsiCalEventColumnListItem")
        log(f"{len(rows)} events found")

        # ── Step 2: Collect stubs from list ─────────────────────────────────
        stubs = []
        for row in rows:
            name = date = ""
            try:
                el = await row.query_selector(".evonsiCalEventColumnListItem-titleCell-eventName")
                name = (await el.inner_text()).strip()
            except: pass
            try:
                d = await (await row.query_selector(".evonsiMDateTableCell-day")).inner_text()
                m = await (await row.query_selector(".evonsiMDateTableCell-month")).inner_text()
                date = f"{d.strip()} {m.strip()}"
            except: pass
            prev = saved.get(name, {})
            stubs.append({
                "name": name, "date": date,
                "external_url":  prev.get("external_url"),
                "contact_name":  prev.get("contact_name", ""),
                "contact_email": prev.get("contact_email", ""),
                "speakers":      prev.get("speakers", [])
            })

        OUTPUT.write_text(json.dumps(stubs, indent=2))

        # ── Step 3: Open each event, get external URL + contact ──────────────
        log("Opening each event…")
        for i, stub in enumerate(stubs):
            if stub.get("external_url") is not None:
                log(f"  [{i+1}/{len(stubs)}] {stub['name'][:55]} — skipping (have URL)")
                continue

            log(f"  [{i+1}/{len(stubs)}] {stub['name'][:55]}")
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)

                rows = await page.query_selector_all("tr.evonsiCalEventColumnListItem")
                if i >= len(rows):
                    log(f"    row not found — skipping"); continue

                url_before = page.url
                try: await rows[i].click()
                except: await page.evaluate("(el) => el.click()", rows[i])
                await page.wait_for_timeout(3000)

                if page.url == url_before:
                    await page.evaluate("(el) => el.click()", rows[i])
                    await page.wait_for_timeout(3000)
                if page.url == url_before:
                    log(f"    couldn't open — skipping"); continue

                # Scroll detail page fully
                await page.evaluate("""
                    async () => { const h = document.body.scrollHeight;
                        for (let y=0; y<=h; y+=300) {
                            window.scrollTo(0,y);
                            await new Promise(r=>setTimeout(r,40)); }}""")
                await page.wait_for_timeout(500)

                # Find external URL (look for label "External Event Website")
                links = await page.evaluate("""
                    () => {
                        const seen = new Set(), out = [];
                        document.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href;
                            if (!href.startsWith('http')) return;
                            if (['sap.com','enter.sap','sharepoint','mailto'].some(x=>href.includes(x))) return;
                            if (seen.has(href)) return; seen.add(href);
                            let ctx = a.innerText.trim();
                            let el = a.parentElement;
                            for (let j=0;j<5;j++) {
                                if (!el) break;
                                ctx = (el.innerText||'').trim().slice(0,300);
                                el = el.parentElement;
                            }
                            out.push({href, text: a.innerText.trim(), ctx});
                        });
                        return out;
                    }""")

                for lnk in links:
                    if "external event" in (lnk["ctx"]+lnk["text"]).lower():
                        stub["external_url"] = lnk["href"]; break
                if not stub["external_url"] and links:
                    stub["external_url"] = links[0]["href"]

                # Grab "Main Event Contact"
                contact = await page.evaluate("""
                    () => {
                        let name='', email='';
                        document.querySelectorAll('*').forEach(el => {
                            if ((el.innerText||'').trim().toLowerCase()==='main event contact') {
                                const sib = el.nextElementSibling;
                                if (sib) {
                                    name = sib.innerText.trim();
                                    const link = sib.querySelector('a[href^="mailto:"]');
                                    if (link) email = link.href.replace('mailto:','').trim();
                                }
                            }
                        });
                        return {name, email};
                    }""")
                stub["contact_name"]  = contact.get("name", "")
                stub["contact_email"] = contact.get("email", "")

                log(f"    URL: {stub['external_url'] or 'none'}")
                log(f"    Contact: {stub['contact_name']} {stub['contact_email']}")

            except Exception as e:
                log(f"    error: {e}")

            OUTPUT.write_text(json.dumps(stubs, indent=2))

            # Go back to list
            try:
                await page.go_back()
                await page.wait_for_function(
                    "() => document.querySelectorAll('tr.evonsiCalEventColumnListItem').length > 0",
                    timeout=8000)
                while True:
                    try:
                        btn = page.locator(".sapMGrowingListTrigger").first
                        if await btn.is_visible(timeout=800): await btn.click(); await page.wait_for_timeout(700)
                        else: break
                    except: break
            except:
                await page.goto(SAP_URL); await page.wait_for_timeout(3000)

        # ── Step 4: Visit each external site, scrape speakers ───────────────
        to_scrape = [e for e in stubs if e.get("external_url") and not e.get("speakers")]
        log(f"\nScraping speakers from {len(to_scrape)} external sites…")

        for ev in to_scrape:
            log(f"  {ev['name'][:55]}")
            ev["speakers"] = await scrape_external(ctx, ev["external_url"])
            OUTPUT.write_text(json.dumps(stubs, indent=2))

        # ── Write contact CSV ────────────────────────────────────────────────
        with open(CONTACT_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Event", "Date", "Contact Name", "Contact Email", "External URL"])
            for e in stubs:
                w.writerow([e["name"], e["date"],
                            e.get("contact_name",""), e.get("contact_email",""),
                            e.get("external_url","")])

        total_sp = sum(len(e.get("speakers",[])) for e in stubs)
        log(f"\n{'='*55}")
        log(f"Done — {len(stubs)} events, {total_sp} speakers")
        log(f"Tracker data → {OUTPUT}")
        log(f"Contact list → {CONTACT_CSV}")
        log(f"Open tracker: file:///Users/I532804/sap-event-tracker/tracker.html")
        log(f"{'='*55}")

        await page.wait_for_timeout(5000)
        await browser.close()

asyncio.run(main())
