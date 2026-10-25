import html as htmllib, os, re, json, gzip, ssl, hashlib, sqlite3, smtplib, asyncio, time, csv
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin, urlencode, parse_qsl
from urllib.robotparser import RobotFileParser
import httpx
from dateutil import parser as dparse
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from fastapi import FastAPI
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

DB = os.getenv("DB_PATH", "monitor.db")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 CompetitorWatch/1.0"
BLOG = re.compile(r"/(blog|resources?|articles?|insights?|news|learn|stories|guides?|library|posts?)(/|$)", re.I)
SKIP = re.compile(r"/(category|categories|tag|tags|author|authors|page|topics?|feed)(/|$)|\.(jpe?g|png|gif|pdf|xml|svg|webp)$", re.I)
STOP = set("a an the and or of to in on for with your you is are how what why when best top guide ultimate complete vs new our we it this that from by at be can 2023 2024 2025 2026".split())

SCHEMA = """
create table if not exists competitors(id integer primary key autoincrement, domain text unique, baselined integer default 0, last_checked text);
create table if not exists posts(id integer primary key autoincrement, competitor_id integer, url text, title text, published_at text, description text,
  keyword text, volume integer, first_seen text, ok integer default 1, unique(competitor_id,url));
create table if not exists pricing(id integer primary key autoincrement, competitor_id integer, hash text, prices text, captured text);
create table if not exists runs(id integer primary key autoincrement, competitor_id integer, started text, status text, found integer, new integer, note text);
"""
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
with db() as _c:
    _c.executescript(SCHEMA)
    try: _c.execute("alter table posts add column enriched integer default 0")
    except sqlite3.OperationalError: pass
    try: _c.execute("alter table posts add column trend integer")
    except sqlite3.OperationalError: pass

def norm(u):
    p = urlparse(u.strip()); h = p.netloc.lower().removeprefix("www.")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))])
    return f"https://{h}{p.path.rstrip('/') or '/'}" + (f"?{q}" if q else "")

def iso(s):
    try: return dparse.parse(s).isoformat()
    except Exception: return None

def clean(s):
    s = htmllib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", " ", s or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()

def slug_title(u):
    return re.sub(r"[-_]+", " ", urlparse(u).path.rstrip("/").split("/")[-1]).strip().capitalize() or u

def price_tokens(html):
    t = HTMLParser(html)
    for n in t.css("script,style,nav,footer,header,noscript"): n.decompose()
    txt = t.body.text(separator=" ", strip=True) if t.body else ""
    return sorted(set(re.findall(r"[$€£₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?/\s?(?:mo|month|yr|year|user|seat))?", txt)))

def render_js(url):
    try: from playwright.sync_api import sync_playwright
    except ImportError: return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page(user_agent=UA)
            pg.goto(url, wait_until="networkidle", timeout=25000); h = pg.content(); b.close(); return h
    except Exception: return None

LISTING_WORDS = {"guides","guide","articles","article","webinars","events","resources","blog","news","insights","stories","library","posts","learn","topics","categories"}
def is_listing(title, url):
    # True for index/listing pages (e.g. "Articles, Page 1", "Zapier guides", "Webinars and Events") that are not real posts
    seg = urlparse(url).path.strip("/").split("/")[-1].lower()
    t = re.split(r"\s[|–—-]\s", title or "")[0].strip().lower()
    words = re.findall(r"[a-z0-9']+", t)
    if re.search(r"\bpage\s*\d+\b", t) or re.fullmatch(r"\d+", seg): return True
    if seg in LISTING_WORDS: return True
    return bool(words) and len([w for w in words if w not in STOP and w not in LISTING_WORDS and w != "zapier"]) == 0 or (len(words) <= 3 and any(w in LISTING_WORDS for w in words))

def keyword(title, url, brand):
    t = re.split(r"\s[|–—-]\s|:", title)[0] if title else url.rstrip("/").split("/")[-1].replace("-", " ")
    w = [x for x in re.findall(r"[a-z0-9']+", t.lower()) if x not in STOP and x != brand]
    return " ".join(w[:3])

def parse(html):
    if not html or not html.strip(): return dict(title=None, date=None, desc=None, article=False)
    t = HTMLParser(html)
    def meta(*names):
        for n in names:
            e = t.css_first(f'meta[property="{n}"], meta[name="{n}"]')
            if e and e.attributes.get("content"): return e.attributes["content"].strip()
    h1, ti = t.css_first("h1"), t.css_first("title")
    title = meta("og:title") or (h1.text(strip=True) if h1 else None) or (ti.text(strip=True) if ti else None)
    date, ld = meta("article:published_time", "datePublished", "date"), False
    for n in t.css('script[type="application/ld+json"]'):
        txt = n.text()
        if re.search(r'"(Blog|News)?Posting"|"(Blog|News|Tech)?Article"', txt): ld = True
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', txt)
        if m and not date: date = m.group(1)
    if not date:
        tm = t.css_first("time[datetime]"); date = tm.attributes["datetime"] if tm else None
    return dict(title=title, date=iso(date) if date else None, desc=meta("description", "og:description"),
                article=ld or meta("og:type") == "article" or bool(date))

class Site:
    def __init__(s, domain):
        s.domain, s.base, s.blocked, s.err = domain, f"https://{domain}", False, ""
        s.c = httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"},
                                follow_redirects=True, timeout=15)
        s.rp, s.sem = RobotFileParser(), asyncio.Semaphore(6)

    async def get(s, url, robots=True):
        if robots and not s.rp.can_fetch("*", url): return None
        async with s.sem:
            await asyncio.sleep(0.1)
            for _ in range(2):
                try: r = await s.c.get(url)
                except httpx.HTTPError as e: s.err = str(e) or type(e).__name__; return None
                if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:3000]:
                    s.blocked = True; await asyncio.sleep(1); continue
                return r if r.status_code == 200 else None
        return None

    def same(s, u):
        h = urlparse(u).netloc.lower().removeprefix("www.")
        return h == s.domain or h.endswith("." + s.domain)

    async def sitemaps(s, robots_txt):
        q = re.findall(r"(?im)^sitemap:\s*(\S+)", robots_txt) or [s.base + "/sitemap.xml", s.base + "/sitemap_index.xml"]
        out, seen = {}, set()
        while q and len(seen) < 25:
            q.sort(key=lambda x: not re.search(r"post|blog|article|news|insight|resource", x, re.I))
            u = q.pop(0)
            if u in seen: continue
            seen.add(u); r = await s.get(u, False)
            if not r: continue
            txt = gzip.decompress(r.content).decode("utf8", "ignore") if u.endswith(".gz") else r.text
            for blk in re.findall(r"<(url|sitemap)>(.*?)</\1>", txt, re.S):
                loc = re.search(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", blk[1], re.S)
                if not loc: continue
                loc = loc.group(1).strip()
                if blk[0] == "sitemap": q.append(loc)
                elif BLOG.search(urlparse(loc).path) and not SKIP.search(urlparse(loc).path) and urlparse(loc).path.strip("/").count("/") >= 1:
                    lm = re.search(r"<lastmod>(.*?)</lastmod>", blk[1])
                    out[norm(loc)] = {"lastmod": iso(lm.group(1)) if lm else None}
        return out

    async def feeds(s, home):
        c = [urljoin(s.base, x) for x in re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', home)]
        c += [s.base + p for p in ("/feed", "/rss.xml", "/blog/feed", "/blog/rss.xml", "/feed.xml", "/blog/feed.xml", "/atom.xml")]
        for u in dict.fromkeys(c):
            r = await s.get(u, False)
            if not r or "<" not in r.text[:300]: continue
            out = {}
            for it in re.findall(r"<(?:item|entry)[ >].*?</(?:item|entry)>", r.text, re.S):
                tag = lambda n: (re.search(rf"<{n}[^>]*>(.*?)</{n}>", it, re.S | re.I) or [None, ""])[1]
                l = re.search(r'<link[^>]*href="([^"]+)"', it) or re.search(r"<link>(.*?)</link>", it, re.S)
                if not l: continue
                url = clean(l.group(1))
                out[norm(url)] = {"title": clean(tag("title")), "date": iso(clean(tag("pubDate") or tag("dc:date") or tag("published") or tag("updated"))),
                                  "desc": clean(tag("description") or tag("summary"))[:300], "trusted": True}
            if out: return out
        return {}

    async def crawl(s, home):
        if not home: return {}
        hp, lists, out = HTMLParser(home), {s.base + "/blog", s.base + "/resources"}, {}
        def links(h):
            for a in h.css("a[href]"):
                u = urljoin(s.base, a.attributes["href"]).split("#")[0]
                if s.same(u): yield u, a.text(strip=True)
        for u, _ in links(hp):
            p = urlparse(u).path
            if BLOG.search(p) and not SKIP.search(p) and p.strip("/").count("/") == 0: lists.add(norm(u))
        for L in list(lists)[:4]:
            for pg in (L, L + "/page/2", L + "?page=2"):
                r = await s.get(pg)
                if not r: continue
                lp = urlparse(L).path
                for u, txt in links(HTMLParser(r.text)):
                    p = urlparse(u).path
                    if p.startswith(lp + "/") and len(p.strip("/").split("/")) > len(lp.strip("/").split("/")) and not SKIP.search(p):
                        out.setdefault(norm(u), {"title": txt if len(txt) > 12 else None})
        return out

    async def pricing(s):
        for p in ("/pricing", "/plans", "/pricing-plans"):
            r = await s.get(s.base + p)
            if r: break
        else: return None
        prices = price_tokens(r.text)
        if not prices:
            h = await asyncio.to_thread(render_js, str(r.url))
            if h: prices = price_tokens(h)
        return prices

async def _dfs(kws, login, pw):
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live", auth=(login, pw),
                             json=[{"keywords": kws, "location_code": 2840, "language_code": "en"}])
        if r.status_code in (401, 403):
            try: m = r.json().get("status_message")
            except Exception: m = r.reason_phrase
            return {}, f"DataForSEO credentials were rejected ({m}). The alert was sent without volume data."
        j = r.json(); t = (j.get("tasks") or [None])[0]
        if not t: return {}, f"DataForSEO: {j.get('status_message')}. The alert was sent without volume data."
        if t.get("status_code") != 20000: return {}, f"DataForSEO: {t.get('status_message')}. The alert was sent without volume data."
        res = t.get("result") or []
        return {str(x["keyword"]).lower(): x.get("search_volume") for x in res}, None
    except Exception as e:
        return {}, f"DataForSEO error ({type(e).__name__}). The alert was sent without volume data."

async def _ke(kws, key):
    out = {}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            for i in range(0, len(kws), 100):
                r = await c.post("https://api.keywordseverywhere.com/v1/get_keyword_data",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                                 data={"dataSource": "gkp", "country": "us", "currency": "usd", "kw[]": kws[i:i + 100]})
                if r.status_code in (401, 403): return out, "Keywords Everywhere key was rejected. The alert was sent without volume data."
                if r.status_code == 402: return out, "Keywords Everywhere credits are used up. The alert was sent without volume data."
                for x in r.json().get("data", []): out[str(x["keyword"]).lower()] = x.get("vol", x.get("search_volume"))
        return out, None
    except Exception as e:
        return out, f"Keywords Everywhere error ({type(e).__name__}). The alert was sent without volume data."

async def volumes(kws):
    tidy = lambda x: re.sub(r"[\s\u200b\u200c\u200d\ufeff]", "", x or "")
    login, pw, ke = tidy(os.getenv("DATAFORSEO_LOGIN")), tidy(os.getenv("DATAFORSEO_PASSWORD")), tidy(os.getenv("KEYWORDS_EVERYWHERE_API_KEY"))
    if login and pw:
        v, w = await _dfs(kws, login, pw)
        if not w or not ke: return v, w
        return await _ke(kws, ke)
    if ke: return await _ke(kws, ke)
    return {}, "Keyword API not configured. Volumes are unavailable."

def csv_volumes():
    # Real volumes you looked up yourself (e.g. Google Keyword Planner). File: volumes.csv next to app.py, columns: keyword,volume
    p = Path(__file__).resolve().parent / "volumes.csv"; out = {}
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            try: out[r["keyword"].strip().lower()] = int(str(r["volume"]).replace(",", "").strip())
            except Exception: pass
    return out

def _trends(kws):
    # FREE: real Google Trends popularity (0-100, last 12 months). Relative, NOT monthly searches.
    try: from pytrends.request import TrendReq
    except ImportError: return {}
    out, py = {}, TrendReq(hl="en-US", tz=0, timeout=(5, 15))
    for i in range(0, len(kws), 5):
        b = kws[i:i + 5]
        try:
            py.build_payload(b, timeframe="today 12-m")
            df = py.interest_over_time()
            for k in b:
                if k in df and int(round(df[k].mean())) > 0: out[k] = int(round(df[k].mean()))
        except Exception: break  # usually a Google rate limit; keep what we have
        time.sleep(1.5)
    return out

def send_alert(subject, html, text):
    sent, errs = [], []
    u, pw, to = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD"), os.getenv("ALERT_TO")
    if u and pw and to:
        try:
            m = MIMEMultipart("alternative"); m["Subject"], m["From"], m["To"] = subject, u, to; m.attach(MIMEText(html, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as sm:
                sm.login(re.sub(r"\s", "", u), re.sub(r"\s", "", pw)); sm.sendmail(u, [x.strip() for x in to.split(",")], m.as_string())
            sent.append("email")
        except Exception as e: errs.append(f"Gmail failed: {e}")
    if os.getenv("SLACK_WEBHOOK_URL"):
        try: httpx.post(os.getenv("SLACK_WEBHOOK_URL"), json={"text": f"*{subject}*\n{text}"}, timeout=10); sent.append("slack")
        except Exception as e: errs.append(f"Slack failed: {e}")
    if not sent and not errs: errs.append("No alert channel configured (set Gmail or Slack env vars).")
    return {"sent": sent, "errors": errs}

def estimate_volume(kw):
    # Rough demo estimate (NOT real Google data). Stored as a negative number so the UI and emails can label it "(est.)".
    n = max(1, len(kw.split())); h = int(hashlib.md5(kw.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    lo, hi = {1: (8000, 90000), 2: (1000, 14000), 3: (100, 3500)}.get(n, (50, 1200))
    return int(round(lo * (hi / lo) ** h / 10) * 10)

def fmt_vol(v): return "n/a" if v is None else (f"~{-v:,} (est.)" if v < 0 else f"{v:,}")

def vol_label(p):
    v = p.get("volume")
    if v is not None: return fmt_vol(v)
    t = p.get("trend")
    return f"Trends interest {t}/100" if t is not None else "n/a"

def render(domain, new, pr, baseline, total):
    vol = vol_label
    rows = "".join(f"<tr><td style='padding:6px 10px'><a href='{p['url']}'>{p['title'] or p['url']}</a></td><td style='padding:6px 10px'>{p['keyword'] or ''}</td><td style='padding:6px 10px'>{vol(p)}</td></tr>" for p in new)
    lines = "\n".join(f"• {p['title'] or p['url']} ({p['url']}) | keyword: {p['keyword'] or '-'} | volume: {vol(p)}" for p in new)
    ph = pt = ""
    if pr and pr.get("status") == "changed":
        ph = f"<h3>Pricing changed</h3><p>Added: {', '.join(pr['added']) or 'none'}<br>Removed: {', '.join(pr['removed']) or 'none'}</p>"
        pt = f"\nPricing changed. Added: {', '.join(pr['added']) or 'none'}. Removed: {', '.join(pr['removed']) or 'none'}."
    if baseline:
        subj = f"Now tracking {domain}: {total} posts indexed"
    else:
        subj = f"{domain} published {len(new)} new post{'s' if len(new) != 1 else ''}" if new else f"{domain} changed its pricing"
    note = f"<p>Baseline saved: {total} posts are now remembered. You will only be alerted about posts published after this.</p>" if baseline else ""
    body = f"<div style='font-family:Arial,sans-serif'><h2>{subj}</h2>{note}" + (f"<table border='0' cellspacing='0'><tr><th align=left>Post</th><th align=left>Keyword</th><th align=left>Volume</th></tr>{rows}</table>" if new else "") + ph + "</div>"
    return subj, body, (lines + pt) or "Baseline saved. Only new posts will trigger alerts."

async def check(domain):
    holder = []
    try: return await _check(domain, holder)
    finally:
        for h in holder: await h.c.aclose()

async def _check(domain, holder):
    domain = re.sub(r"^https?://|[/?#].*$", "", domain.strip().lower()).removeprefix("www.")
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain): return {"status": "error", "message": "That doesn't look like a domain. Try something like example.com."}
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("insert or ignore into competitors(domain) values(?)", (domain,))
        comp = c.execute("select * from competitors where domain=?", (domain,)).fetchone()
    s, warns = Site(domain), []
    holder.append(s)
    rob = await s.get(s.base + "/robots.txt", False)
    s.rp.parse(rob.text.splitlines() if rob else [])
    home = await s.get(s.base); hh = home.text if home else ""
    sm, fd, cr, prices = await asyncio.gather(s.sitemaps(rob.text if rob else ""), s.feeds(hh), s.crawl(hh), s.pricing())
    cands = {}
    for d in (sm, fd, cr):
        for u, v in d.items(): cands.setdefault(u, {}).update({k: x for k, x in v.items() if x})
    sources = {"sitemap": len(sm), "rss": len(fd), "crawl": len(cr)}
    def finish(status, msg, **kw):
        with db() as c:
            c.execute("insert into runs(competitor_id,started,status,found,new,note) values(?,?,?,?,?,?)", (comp["id"], now, status, len(cands), kw.get("n", 0), msg))
            c.execute("update competitors set last_checked=? where id=?", (now, comp["id"]))
        return {"status": status, "domain": domain, "message": msg, "sources": sources, "found": len(cands), "new": [], "warnings": warns, **{k: v for k, v in kw.items() if k != "n"}}
    if not cands:
        if s.blocked: return finish("blocked", f"{domain} is blocking automated requests (403/429/challenge page). We retried politely and found nothing in the sitemap or feeds.")
        if s.err and not home: return finish("error", f"Couldn't reach {domain}: {s.err}. Check the domain and try again.")
        return finish("no_blog", "No blog detected. We checked robots.txt sitemaps, RSS/Atom feeds and /blog, /resources listing pages.")
    with db() as c: existing = {r["url"] for r in c.execute("select url from posts where competitor_id=?", (comp["id"],))}
    baseline = not comp["baselined"]
    fresh = sorted([u for u in cands if u not in existing], key=lambda u: cands[u].get("date") or cands[u].get("lastmod") or "", reverse=True)
    async def detail(u):
        d = dict(cands[u]); ok = True
        r = await s.get(u)
        if r:
            p = parse(r.text); ok = p["article"] or bool(p["date"])
            d.update({"title": p["title"] or d.get("title"), "date": p["date"] or d.get("date") or d.get("lastmod"), "desc": p["desc"] or d.get("desc")})
        else: d["date"] = d.get("date") or d.get("lastmod")
        d["title"] = d.get("title") or slug_title(u)
        if ok and is_listing(d["title"], u): ok = False
        return u, d, ok
    cap = 100 if baseline else 40
    done = await asyncio.gather(*[detail(u) for u in fresh[:cap]])
    rest = [(u, {**cands[u], "date": cands[u].get("lastmod"), "title": cands[u].get("title") or slug_title(u)}, True) for u in fresh[cap:]]
    brand = domain.split(".")[0]
    posts = [dict(url=u, title=d.get("title"), published_at=d.get("date"), description=d.get("desc"), ok=int(ok), enriched=e,
                  keyword=keyword(d.get("title"), u, brand) if ok else None, volume=None, trend=None) for grp, e in ((done, 1), (rest, 0)) for u, d, ok in grp]
    good = [p for p in posts if p["ok"]]
    good.sort(key=lambda p: p["published_at"] or "", reverse=True)
    target = good[:20] if baseline else good
    kws = sorted({p["keyword"] for p in target if p["keyword"]})
    if kws:
        vols, w = await volumes(kws)
        if w: warns.append(w)
        elif not any(vols.get(k) for k in kws): warns.append("The keyword service returned no volume for these keywords. Google has no data for some rare phrases, so they show n/a.")
        for p in target: p["volume"] = vols.get(p["keyword"])
        cv = csv_volumes()
        if cv:
            hit = 0
            for p in target:
                if p["volume"] is None and p["keyword"] in cv: p["volume"] = cv[p["keyword"]]; hit += 1
            if hit: warns.append(f"{hit} volume(s) come from volumes.csv (looked up manually, e.g. Google Keyword Planner).")
        # FREE fallback: Google Trends interest for keywords that have no real volume
        need = sorted({p["keyword"] for p in target if p["volume"] is None and p["keyword"]})
        if need:
            tr = await asyncio.to_thread(_trends, need)
            for p in target:
                if p["volume"] is None: p["trend"] = tr.get(p["keyword"])
            if tr: warns.append("Where real volume is unavailable, 'Trends interest' is Google Trends popularity (0-100), not monthly searches.")
        if os.getenv("ESTIMATE_VOLUME") == "1":
            miss = [p for p in target if p["volume"] is None and p["trend"] is None and p["keyword"]]
            for p in miss: p["volume"] = -estimate_volume(p["keyword"])
            if miss: warns.append("Volumes marked (est.) are rough demo estimates, not real Google data.")
    with db() as c:
        for p in posts:
            c.execute("insert or ignore into posts(competitor_id,url,title,published_at,description,keyword,volume,trend,first_seen,ok,enriched) values(?,?,?,?,?,?,?,?,?,?,?)",
                      (comp["id"], p["url"], p["title"], p["published_at"], p["description"], p["keyword"], p["volume"], p["trend"], now, p["ok"], p["enriched"]))
        pr = None
        if prices is not None:
            h = hashlib.sha1(json.dumps(prices).encode()).hexdigest()
            prev = c.execute("select * from pricing where competitor_id=? order by id desc limit 1", (comp["id"],)).fetchone()
            if not prev: pr = {"status": "baseline", "prices": prices}
            elif prev["hash"] != h:
                old = json.loads(prev["prices"])
                pr = {"status": "changed", "prices": prices, "added": [x for x in prices if x not in old], "removed": [x for x in old if x not in prices]}
            else: pr = {"status": "unchanged", "prices": prices}
            if not prev or prev["hash"] != h: c.execute("insert into pricing(competitor_id,hash,prices,captured) values(?,?,?,?)", (comp["id"], h, json.dumps(prices), now))
        c.execute("update competitors set baselined=1 where id=?", (comp["id"],))
    with db() as c:
        todo = [dict(r) for r in c.execute("select id,url from posts where competitor_id=? and ok=1 and enriched=0 order by coalesce(published_at,first_seen) desc limit 40", (comp["id"],))]
    if todo:
        got = await asyncio.gather(*[s.get(t["url"]) for t in todo])
        with db() as c:
            for t, r in zip(todo, got):
                if not r: c.execute("update posts set enriched=2 where id=?", (t["id"],)); continue
                p = parse(r.text)
                c.execute("update posts set title=coalesce(?,title), published_at=coalesce(?,published_at), description=coalesce(?,description), keyword=coalesce(?,keyword), enriched=1 where id=?",
                          (p["title"], p["date"], p["desc"], keyword(p["title"], t["url"], brand) if p["title"] else None, t["id"]))
    alert = None
    if baseline or good or (pr and pr["status"] == "changed"):
        subj, html, text = render(domain, [] if baseline else good, pr, baseline, len(good))
        alert = await asyncio.to_thread(send_alert, subj, html, text)
    res = finish("ok", "Baseline created." if baseline else f"{len(good)} new post(s) since last check.", n=0 if baseline else len(good),
                 baseline=baseline, pricing=pr, alert=alert)
    res["new"] = [] if baseline else good
    res["indexed"] = len(good)
    return res

app = FastAPI()
class Req(BaseModel):
    domain: str
    n: int = 3

@app.post("/api/check")
async def api_check(r: Req):
    try: return await check(r.domain)
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"status": "error", "domain": r.domain, "message": f"The scan hit an unexpected error ({type(e).__name__}: {e}). Try again or try another domain.", "warnings": []}

@app.get("/api/competitors")
def competitors():
    with db() as c:
        return [dict(r) for r in c.execute("""select domain,last_checked,(select count(*) from posts where competitor_id=c.id and ok=1) posts,
          (select status from runs where competitor_id=c.id order by id desc limit 1) status from competitors c order by last_checked desc""")]

@app.get("/api/posts")
def posts(domain: str):
    with db() as c:
        return [dict(r) for r in c.execute("""select p.url,p.title,p.published_at,p.description,p.keyword,p.volume,p.trend,p.first_seen from posts p join competitors c on c.id=p.competitor_id
          where c.domain=? and p.ok=1 order by coalesce(p.published_at,p.first_seen) desc limit 200""", (domain,))]

@app.get("/api/runs")
def runs(domain: str):
    with db() as c:
        return [dict(r) for r in c.execute("select r.started,r.status,r.found,r.new,r.note from runs r join competitors c on c.id=r.competitor_id where c.domain=? order by r.id desc limit 15", (domain,))]

@app.post("/api/demo/forget")
def forget(r: Req):
    with db() as c:
        c.execute("delete from posts where id in (select p.id from posts p join competitors c on c.id=p.competitor_id where c.domain=? and p.ok=1 order by (p.published_at is null), p.published_at desc limit ?)", (r.domain, r.n))
    return {"ok": True}

@app.post("/api/test-alert")
async def test_alert(): return await asyncio.to_thread(send_alert, "Competitor Watch test alert", "<p>Alerts are working.</p>", "Alerts are working.")

@app.get("/api/diag")
async def diag():
    v, w = await volumes(["email marketing"])
    return {"dataforseo_login_set": bool(os.getenv("DATAFORSEO_LOGIN")), "dataforseo_password_set": bool(os.getenv("DATAFORSEO_PASSWORD")),
            "keywords_everywhere_key_set": bool(os.getenv("KEYWORDS_EVERYWHERE_API_KEY")),
            "volume_for_email_marketing": v.get("email marketing"), "warning": w}

HERE = Path(__file__).resolve().parent

@app.get("/")
def index():
    for p in (HERE / "static" / "index.html", HERE / "index.html"):
        if p.exists(): return FileResponse(p)
    return HTMLResponse("index.html not found. Put it in a folder named 'static' next to app.py.", status_code=404)
