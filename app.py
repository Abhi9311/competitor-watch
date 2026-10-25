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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

class PG:
    # Thin wrapper so the same code runs on Postgres (Neon) or SQLite. Rows behave like dicts.
    def __init__(self):
        import psycopg2, psycopg2.extras
        self._x = psycopg2.extras
        self.c = psycopg2.connect(DATABASE_URL)
    @staticmethod
    def conv(sql):
        s = sql.replace("?", "%s").replace("integer primary key autoincrement", "serial primary key")
        if s.lstrip().lower().startswith("insert or ignore"):
            s = re.sub(r"(?i)insert or ignore", "insert", s, count=1).rstrip() + " on conflict do nothing"
        return s
    def execute(self, sql, params=()):
        cur = self.c.cursor(cursor_factory=self._x.RealDictCursor)
        cur.execute(self.conv(sql), params)
        return cur
    def executescript(self, script):
        for st in [x.strip() for x in script.split(";") if x.strip()]: self.execute(st)
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        try:
            if et: self.c.rollback()
            else: self.c.commit()
        finally: self.c.close()
        return False

def db():
    if DATABASE_URL: return PG()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
with db() as _c:
    _c.executescript(SCHEMA)
    for _col in ("enriched integer default 0", "trend integer"):
        if DATABASE_URL: _c.execute(f"alter table posts add column if not exists {_col}")
        else:
            try: _c.execute(f"alter table posts add column {_col}")
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

INDEX_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Competitor Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--paper:#eef1f6;--ink:#141b34;--mute:#5d6683;--line:#d6dbe8;--card:#fff;--sig:#3d35f0;--new:#ffb81c;--ok:#12805c;--bad:#c4321f;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--paper:#0f1424;--ink:#e9ecf8;--mute:#9aa3c2;--line:#262e4d;--card:#171d35;--sig:#8b86ff;--ok:#38c796;--bad:#ff7b66}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:400 16px/1.55 'IBM Plex Sans',system-ui,sans-serif}
.w{max-width:960px;margin:0 auto;padding:28px 20px 80px}
header{display:flex;justify-content:space-between;align-items:center;font:700 18px 'Bricolage Grotesque'}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--new);margin-right:8px;box-shadow:0 0 0 4px color-mix(in srgb,var(--new) 30%,transparent)}
h1{font:800 clamp(38px,7vw,68px)/1.02 'Bricolage Grotesque';letter-spacing:-.03em;margin:64px 0 12px;max-width:14em}
.sub{color:var(--mute);max-width:34em;margin:0 0 28px}
.bar{display:flex;gap:8px;background:var(--card);border:2px solid var(--ink);border-radius:14px;padding:8px;box-shadow:6px 6px 0 var(--sig)}
.bar input{flex:1;min-width:0;border:0;background:#fff;color:#141b34;-webkit-text-fill-color:#141b34;color-scheme:only light;border-radius:8px;caret-color:var(--sig);opacity:1;-webkit-user-select:text;user-select:text;font:600 20px 'Bricolage Grotesque';padding:10px 12px;outline:0}.q{width:100%;margin:12px 0 4px;padding:11px 14px;border:1.5px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font:500 16px 'IBM Plex Sans'}
button{font:600 15px 'IBM Plex Sans';cursor:pointer;border-radius:10px;border:1.5px solid var(--ink);background:var(--card);color:var(--ink);padding:9px 14px}
button.go{background:var(--sig);color:#fff;border-color:var(--sig);padding:10px 20px;font-size:16px}
button:disabled{opacity:.5;cursor:wait}button:focus-visible,input:focus-visible,.chip:focus-visible{outline:3px solid var(--new);outline-offset:2px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 0}.chip{border:1px solid var(--line);background:var(--card);border-radius:99px;padding:5px 12px;font-size:14px;cursor:pointer;color:var(--ink)}
.steps{display:none;margin:36px 0 0;gap:0;border-left:3px solid var(--line)}.steps.on{display:block}
.st{padding:4px 0 4px 16px;color:var(--mute);position:relative}.st.cur{color:var(--ink);font-weight:600}.st.cur::before{content:"";position:absolute;left:-9px;top:11px;width:15px;height:15px;border-radius:50%;background:var(--new);animation:p 1s infinite alternate}.st.done{color:var(--ok)}.st.done::before{content:"✓";position:absolute;left:-12px;top:2px;background:var(--paper);color:var(--ok)}
@keyframes p{to{transform:scale(1.35)}}@media(prefers-reduced-motion:reduce){.st.cur::before{animation:none}}
#out{margin-top:40px}.head{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.head h2{font:800 30px 'Bricolage Grotesque';margin:0;letter-spacing:-.02em}
.tag{display:inline-block;border-radius:6px;padding:2px 9px;font-size:13px;font-weight:600;margin-left:10px;vertical-align:middle}
.t-ok{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}.t-bad{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}.t-new{background:var(--new);color:#141b34}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0;margin:22px 0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.facts div{padding:14px 16px;border-right:1px solid var(--line)}.facts div:last-child{border:0}.facts b{display:block;font:800 30px 'Bricolage Grotesque'}.facts span{color:var(--mute);font-size:14px}
.warn{background:color-mix(in srgb,var(--new) 22%,transparent);border-left:4px solid var(--new);padding:10px 14px;border-radius:6px;margin:10px 0;font-size:15px}
.err{background:color-mix(in srgb,var(--bad) 14%,transparent);border-left:4px solid var(--bad);padding:14px 16px;border-radius:6px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:26px 0 8px}.tabs button{border:0;background:none;border-bottom:3px solid transparent;border-radius:0;color:var(--mute)}.tabs button.on{color:var(--ink);border-color:var(--sig)}
.post{display:grid;grid-template-columns:1fr auto;gap:4px 20px;padding:16px 0;border-bottom:1px solid var(--line)}
.post a{color:var(--ink);font:700 18px/1.3 'Bricolage Grotesque';text-decoration:none}.post a:hover{color:var(--sig);text-decoration:underline}
.post p{grid-column:1;margin:2px 0;color:var(--mute);font-size:14px}.meta{font-size:13px;color:var(--mute)}
.kw{grid-row:1/3;grid-column:2;text-align:right;min-width:150px}.kw em{font-style:normal;font-weight:600;font-size:14px}
.vol{height:6px;background:var(--line);border-radius:9px;margin:6px 0 2px;overflow:hidden}.vol i{display:block;height:100%;background:var(--sig)}
.empty{padding:36px 0;color:var(--mute)}.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.pill{font-family:'Bricolage Grotesque';font-weight:700;border:1px solid var(--line);border-radius:6px;padding:2px 8px;margin:3px;display:inline-block;background:var(--card)}
@media(max-width:560px){.post{grid-template-columns:1fr}.kw{grid-row:auto;grid-column:1;text-align:left}.bar{flex-direction:column}}
</style></head><body><div class="w">
<header><span><span class="dot"></span>Competitor Watch</span><button id="test">Send test alert</button></header>
<h1>Know what your competitors published before your team does.</h1>
<p class="sub">Enter a domain. We read their sitemap, feeds and blog pages, remember every post, and email you only what's new, with the keyword each post targets.</p>
<div class="bar" id="bar"><input id="d" type="text" inputmode="url" placeholder="competitor.com" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Competitor domain"><button class="go" id="go">Start monitoring</button></div>
<p class="meta" style="margin:12px 0 0">Can't type in the box? <a href="#" id="pr" style="color:var(--sig);font-weight:600">Click here to enter a domain in a pop-up</a></p>
<div class="chips" id="chips"></div>
<div class="steps" id="steps"></div>
<div id="out"></div></div>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const S=["Discovering sources","Crawling listing pages","Extracting titles, dates and descriptions","Looking up keyword volume","Sending the alert"];
let st={tab:"new"};
const api=async(p,b)=>(await fetch(p,b&&{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})).json();
const ago=t=>t?new Date(t).toLocaleString([], {dateStyle:"medium",timeStyle:"short"}):"never";
async function chips(){const l=await api("/api/competitors");$("#chips").innerHTML=l.map(c=>`<button class="chip" data-d="${esc(c.domain)}">${esc(c.domain)} · ${c.posts} posts</button>`).join("")}
$("#chips").onclick=e=>{const d=e.target.dataset.d;if(d){$("#d").value=d;load(d)}};
async function scan(){const d=$("#d").value.trim();if(!d)return;
 $("#go").disabled=true;$("#out").innerHTML="";const el=$("#steps");el.classList.add("on");let i=0;
 const draw=()=>el.innerHTML=S.map((s,k)=>`<div class="st ${k<i?"done":k==i?"cur":""}">${s}</div>`).join("");draw();
 const t=setInterval(()=>{if(i<S.length-1){i++;draw()}},2500);
 let r;try{r=await api("/api/check",{domain:d})}catch(e){r={status:"error",message:"The server didn't respond. Try again."}}
 clearInterval(t);el.classList.remove("on");$("#go").disabled=false;
 if(r.domain){st.posts=await api("/api/posts?domain="+r.domain);st.runs=await api("/api/runs?domain="+r.domain)}
 st.r=r;st.tab="new";show();chips()}
async function load(d){st.posts=await api("/api/posts?domain="+d);st.runs=await api("/api/runs?domain="+d);
 st.r={status:"ok",domain:d,new:[],sources:null,found:st.posts.length,message:"Loaded from history.",loaded:true};st.tab="all";show()}
function post(p,isNew){const v=p.volume,t=p.trend,a=v==null?null:Math.abs(v),w=a?Math.min(100,Math.log10(a+1)/5*100):(t!=null?t:0);
 return `<div class="post"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title||p.url)}</a>
 <div class="kw">${p.keyword?`<em>${esc(p.keyword)}</em><div class="vol"><i style="width:${w}%"></i></div><span class="meta">${v==null?(t!=null?"Trends interest "+t+"/100":"volume unavailable"):(v<0?"~"+a.toLocaleString()+" searches/mo (estimate)":a.toLocaleString()+" searches/mo")}</span>`:""}</div>
 <p>${esc((p.description||"").slice(0,180))}</p><span class="meta">${p.published_at?ago(p.published_at):"date unknown"}${isNew?'<span class="tag t-new">NEW</span>':""}</span></div>`}
function show(){const r=st.r,o=$("#out");
 if(r.status!="ok"){o.innerHTML=`<div class="head"><h2>${esc(r.domain||"")}<span class="tag t-bad">${r.status=="blocked"?"Blocked by site":r.status=="no_blog"?"No blog found":"Couldn't scan"}</span></h2></div><div class="err" style="margin-top:16px">${esc(r.message)}</div>${(r.warnings||[]).map(w=>`<div class="warn">${esc(w)}</div>`).join("")}`;return}
 const n=r.new.length,pr=r.pricing,al=r.alert;
 const alertTxt=al?(al.sent.length?"Sent via "+al.sent.join(" + "):"Not sent"):"None needed";
 let tab=st.tab,body="";
 if(tab=="new")body=n?r.new.map(p=>post(p,1)).join(""):`<div class="empty">${r.baseline?`Baseline saved: ${r.indexed} posts are now remembered. Run again later and only new posts will show here.`:"Nothing new since the last check."}</div>`;
 if(tab=="all")body=`<input class="q" id="q" placeholder="Search posts or keywords…"><div id="plist">${st.posts.map(p=>post(p,0)).join("")||'<div class="empty">No posts stored yet.</div>'}</div>`;
 if(tab=="price")body=!pr?`<div class="empty">No pricing page found at /pricing or /plans.</div>`:pr.status=="changed"?`<p>Added</p>${pr.added.map(x=>`<span class="pill t-ok">${esc(x)}</span>`).join("")||"none"}<p>Removed</p>${pr.removed.map(x=>`<span class="pill t-bad">${esc(x)}</span>`).join("")||"none"}`:`<p class="meta">${pr.status=="baseline"?"Baseline saved. Changes will show here.":"No change since last check."}</p>${pr.prices.map(x=>`<span class="pill">${esc(x)}</span>`).join("")||'<div class="empty">No static prices found (page may load them with JavaScript).</div>'}`;
 if(tab=="runs")body=st.runs.map(x=>`<div class="post"><b>${ago(x.started)}<span class="tag ${x.status=="ok"?"t-ok":"t-bad"}">${x.status}</span></b><p>${esc(x.note)} · found ${x.found}, new ${x.new}</p></div>`).join("");
 o.innerHTML=`<div class="head"><h2>${esc(r.domain)}<span class="tag t-ok">Healthy</span>${n?`<span class="tag t-new">${n} new</span>`:""}</h2>
 <div class="row" style="margin:0"><button id="again">Run again</button><button id="sim" title="Demo only: deletes 3 saved posts so the next scan finds them again">Demo: pretend 3 are new</button></div></div>
 <div class="facts"><div><b>${r.found}</b><span>URLs found</span></div><div><b>${n}</b><span>new this run</span></div><div><b>${r.sources?r.sources.sitemap+"/"+r.sources.rss+"/"+r.sources.crawl:"–"}</b><span>sitemap / RSS / crawl</span></div><div><b style="font-size:20px;padding-top:7px">${alertTxt}</b><span>alert</span></div></div>
 ${(r.warnings||[]).map(w=>`<div class="warn">${esc(w)}</div>`).join("")}${al&&al.errors.length?al.errors.map(w=>`<div class="warn">${esc(w)}</div>`).join(""):""}
 <div class="tabs">${[["new","New posts"],["all","All posts"],["price","Pricing"],["runs","Run history"]].map(([k,l])=>`<button data-t="${k}" class="${tab==k?"on":""}">${l}</button>`).join("")}</div>${body}`;
 const q=$("#q");if(q)q.oninput=()=>{const v=q.value.toLowerCase();$("#plist").innerHTML=st.posts.filter(p=>((p.title||"")+(p.keyword||"")+(p.description||"")).toLowerCase().includes(v)).map(p=>post(p,0)).join("")||'<div class="empty">No matches.</div>'};
 o.querySelectorAll("[data-t]").forEach(b=>b.onclick=()=>{st.tab=b.dataset.t;show()});
 $("#again").onclick=scan;$("#sim").onclick=async()=>{await api("/api/demo/forget",{domain:r.domain,n:3});scan()}}
$("#go").onclick=scan;$("#d").onkeydown=e=>{if(e.key==="Enter")scan()};
$("#test").onclick=async e=>{e.target.textContent="Sending…";const r=await api("/api/test-alert",{});e.target.textContent=r.sent.length?"Sent. Check your inbox":r.errors[0].slice(0,40);setTimeout(()=>e.target.textContent="Send test alert",4000)};
$("#bar").onclick=()=>$("#d").focus();$("#d").focus();chips();$("#pr").onclick=e=>{e.preventDefault();const v=prompt("Enter a competitor domain, for example semrush.com");if(v){$("#d").value=v;scan()}};const qd=new URLSearchParams(location.search).get("d");if(qd){$("#d").value=qd;scan()}
</script></body></html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)