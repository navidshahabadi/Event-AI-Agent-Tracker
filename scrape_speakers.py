#!/usr/bin/env python3
"""
Phase 2: Visit external event sites and scrape speakers.
Reads sap_events_raw.json, processes events that have external_url but no speakers.
Runs standalone — no SAP login needed.
"""
import asyncio, json
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

OUTPUT = Path.home() / "sap-event-tracker" / "sap_events_raw.json"

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def _extract_speakers(page):
    """Extract speakers using multiple strategies: CSS classes, schema.org, and pattern matching."""
    # Strategy 1: known speaker-related CSS classes
    css_selectors = [
        '[class*="speaker" i]', '[class*="panelist" i]', '[class*="presenter" i]',
        '[class*="faculty" i]', '[class*="keynote" i]', '[class*="expert" i]',
        '[class*="team-member" i]', '[class*="bio" i]', '[class*="person" i]',
        '[itemtype*="Person"]', 'figure:has(figcaption)',
    ]
    speakers = []
    for sel in css_selectors:
        try:
            els = await page.query_selector_all(sel)
            if len(els) < 2: continue
            for el in els:
                try:
                    raw = (await el.inner_text()).strip().replace("\n", " ")
                    if not (8 < len(raw) < 600): continue
                    ne = await el.query_selector("h2,h3,h4,strong,b,[class*='name' i],[class*='title' i]:first-child")
                    te = await el.query_selector("[class*='title' i],[class*='role' i],[class*='position' i],[class*='job' i]")
                    ce = await el.query_selector("[class*='company' i],[class*='org' i],[class*='employer' i]")
                    name    = (await ne.inner_text()).strip() if ne else ""
                    title   = (await te.inner_text()).strip() if te else ""
                    company = (await ce.inner_text()).strip() if ce else ""
                    if not name: continue
                    speakers.append({"name": name, "title": title, "company": company, "raw": raw[:300]})
                except: pass
            if speakers: break
        except: continue

    # Strategy 2: schema.org JSON-LD embedded in the page
    if not speakers:
        try:
            ld = await page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                        try {
                            const d = JSON.parse(s.textContent);
                            const items = Array.isArray(d) ? d : [d];
                            items.forEach(item => {
                                const people = item['@type'] === 'Person' ? [item]
                                    : (item.performer || item.speaker || item.organizer || []);
                                (Array.isArray(people) ? people : [people]).forEach(p => {
                                    if (p && p.name) results.push({
                                        name: p.name,
                                        title: p.jobTitle || '',
                                        company: (p.worksFor && p.worksFor.name) || '',
                                        raw: p.name
                                    });
                                });
                            });
                        } catch(e) {}
                    });
                    return results;
                }""")
            if ld: speakers = ld
        except: pass

    # Dedupe by name
    seen, uniq = set(), []
    for s in speakers:
        k = s["name"].lower()
        if k not in seen: seen.add(k); uniq.append(s)
    return uniq

async def scrape_site(ctx, ev):
    for attempt in range(2):  # retry once on crash
        ext = await ctx.new_page()
        try:
            # Dismiss any dialogs (security prompts, popups) automatically
            ext.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
            log(f"  Visiting: {ev['external_url']}")
            await ext.goto(ev["external_url"], timeout=30000, wait_until="domcontentloaded")
            await ext.wait_for_timeout(2000)

        # Scroll homepage first to capture any speakers shown there
        h = await ext.evaluate("document.body.scrollHeight")
        for y in range(0, h + 300, 250):
            await ext.evaluate(f"window.scrollTo(0,{y})")
            await ext.wait_for_timeout(30)

        # Try extracting from homepage first before navigating away
        homepage_speakers = await _extract_speakers(ext)
        if homepage_speakers:
            log(f"    {len(homepage_speakers)} speakers on homepage")
            return homepage_speakers

        # Navigate to a speakers/agenda sub-page
        for kw in ["speakers", "speaker", "agenda", "panelists", "faculty", "sessions"]:
            found = False
            for a in await ext.query_selector_all(f"nav a, header a, a[href*='{kw}']"):
                try:
                    href = (await a.get_attribute("href") or "").strip()
                    txt  = (await a.inner_text()).strip().lower()
                    if (kw in txt or kw in href.lower()) and href and "javascript" not in href:
                        if not href.startswith("http"):
                            base = "/".join(ev["external_url"].split("/")[:3])
                            href = base + "/" + href.lstrip("/")
                        if href != ext.url:
                            await ext.goto(href, timeout=20000, wait_until="domcontentloaded")
                            await ext.wait_for_timeout(1500)
                            log(f"    → navigated to {kw} page")
                            found = True; break
                except: pass
            if found: break

        # Scroll the sub-page
        h = await ext.evaluate("document.body.scrollHeight")
        for y in range(0, h + 300, 250):
            await ext.evaluate(f"window.scrollTo(0,{y})")
            await ext.wait_for_timeout(40)

        uniq = await _extract_speakers(ext)

        log(f"    {len(uniq)} speakers found")
        return uniq

    except Exception as e:
        log(f"    error (attempt {attempt+1}): {e}")
        try: await ext.close()
        except: pass
        if attempt == 0:
            log(f"    retrying…")
            continue
        return []
    finally:
        try: await ext.close()
        except: pass
    break
  return []

async def main():
    stubs = json.loads(OUTPUT.read_text())
    to_process = [e for e in stubs if e.get("external_url")]
    to_scrape = [e for e in to_process if not e.get("speakers")]  # only skip events already with speakers
    log(f"{len(to_process)} events with URLs — {len(to_scrape)} to process")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        ctx = await browser.new_context()

        for ev in to_scrape:
            log(f"\n[{stubs.index(ev)+1}/{len(stubs)}] {ev['name']}")
            ev["speakers"] = await scrape_site(ctx, ev)
            OUTPUT.write_text(json.dumps(stubs, indent=2))

        await browser.close()

    total = sum(len(e.get("speakers", [])) for e in stubs)
    log(f"\nDone — {total} speakers across {len([e for e in stubs if e.get('speakers')])} events")
    log(f"Saved → {OUTPUT}")

asyncio.run(main())
