#!/usr/bin/env python3
"""LeadPilot server. Needs only Python 3.8+ (no third-party packages)."""
import os, sys, json, time, re, hmac, hashlib, sqlite3, threading, secrets, html, csv, io
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    p = os.path.join(BASE, '.env')
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def env(k, d=''):
    return os.environ.get(k, d)


DATA_DIR = env('DATA_DIR', os.path.join(BASE, 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
STAGES = ['new', 'contacted', 'replied', 'number', 'called', 'meeting', 'closed', 'lost']
STAGE_LABEL = {'new': 'New', 'contacted': 'Contacted', 'replied': 'Replied', 'number': 'Number received',
               'called': 'Called', 'meeting': 'Meeting fixed', 'closed': 'Closed (won)', 'lost': 'Lost'}

# ------------------------------------------------------------------ database
DB = sqlite3.connect(os.path.join(DATA_DIR, 'leadpilot.db'), check_same_thread=False)
DB.row_factory = sqlite3.Row
LOCK = threading.RLock()
DB.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS leads(
 id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, place_id TEXT, name TEXT, category TEXT, city TEXT,
 address TEXT, phone TEXT, website TEXT, rating REAL, reviews INTEGER, maps_url TEXT, stage TEXT DEFAULT 'new',
 notes TEXT DEFAULT '', contact_number TEXT DEFAULT '', meeting_at TEXT DEFAULT '', next_followup TEXT DEFAULT '',
 bot_step INTEGER DEFAULT 0, opted_in INTEGER DEFAULT 0, optout INTEGER DEFAULT 0, source TEXT DEFAULT 'manual',
 created INTEGER, updated INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS idx_place ON leads(place_id) WHERE place_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, t INTEGER, type TEXT, text TEXT DEFAULT '', ref TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_ev ON events(lead_id, type);
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, biz_type TEXT, area TEXT, min_reviews INTEGER DEFAULT 100,
 min_rating REAL DEFAULT 4.0, no_website INTEGER DEFAULT 1, active INTEGER DEFAULT 1, last_run INTEGER,
 last_result TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sent_log(id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, lead_id INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
''')


def q(sql, args=()):
    with LOCK:
        return [dict(r) for r in DB.execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    with LOCK:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur.lastrowid


def kv_get(k, d=None):
    r = q1('SELECT v FROM kv WHERE k=?', (k,))
    return r['v'] if r else d


def kv_set(k, v):
    x('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (k, v))


def get_secret():
    s = env('SECRET')
    if s:
        return s.encode()
    p = os.path.join(DATA_DIR, 'secret.key')
    if not os.path.exists(p):
        with open(p, 'w') as f:
            f.write(secrets.token_hex(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    with open(p) as f:
        return f.read().strip().encode()


SECRET = get_secret()

# ------------------------------------------------------------------ helpers


def now_ms():
    return int(time.time() * 1000)


def tz():
    return timezone(timedelta(minutes=int(env('TZ_OFFSET_MIN', '330') or 330)))


def local_now():
    return datetime.now(tz())


def today_str():
    return local_now().strftime('%Y-%m-%d')


def in_days(n):
    return (local_now() + timedelta(days=n)).strftime('%Y-%m-%d')


def cc():
    return re.sub(r'\D', '', env('DEFAULT_CC', '91')) or '91'


def norm_phone(p):
    d = re.sub(r'\D', '', p or '').lstrip('0')
    return cc() + d if len(d) == 10 else d


def phone_key(p):
    d = re.sub(r'\D', '', p or '')
    return d[-10:] if len(d) >= 10 else d


def is_mobile(p):
    d = norm_phone(p)
    if cc() == '91':
        return bool(re.fullmatch(r'91[6-9]\d{9}', d))
    return len(d) >= 10


def extract_phone(text):
    for m in re.finditer(r'\+?\d[\d\s\-().]{8,16}\d', text or ''):
        d = re.sub(r'\D', '', m.group())
        if len(d) == 11 and d.startswith('0'):
            d = d[1:]
        if cc() == '91':
            if len(d) == 10 and d[0] in '6789':
                return d
            if len(d) == 12 and d.startswith('91') and d[2] in '6789':
                return d[2:]
        elif 8 <= len(d) <= 15:
            return d
    return None


def public_url():
    u = env('PUBLIC_URL')
    if not u and env('RAILWAY_PUBLIC_DOMAIN'):
        u = 'https://' + env('RAILWAY_PUBLIC_DOMAIN')
    return (u or 'http://localhost:' + env('PORT', '8080')).rstrip('/')


def trust_proxy():
    t = env('TRUST_PROXY')
    if t != '':
        return t == '1'
    return bool(env('RAILWAY_ENVIRONMENT') or env('RAILWAY_PUBLIC_DOMAIN'))


def lead_link(slug):
    return public_url() + '/d/' + slug


DEFAULT_TEMPLATES = {
    'first': "Hi {name} team 👋 I'm {my_name} from {company}. {name} has great reviews ({rating}★, {reviews}+ on Google) but no website of its own yet. We build 3D interactive websites. Here's our work and an idea for you: {link}\nWould you like a quick call to walk through it?",
    'followup': "Hi {name} team, just following up on my earlier message. A 3D website could bring you more customers from {city}. Here's the link again: {link}\nWhat's a good number and time for a quick call?",
    'ask': "Thanks for getting back to us! 🙌 What's the best phone number to reach you on? I'll do a short 5-minute call with the plan and pricing.",
    'reask': "Just checking, could you share a phone number and a good time to call? Even a rough time like \"after 5 pm\" works.",
    'thanks': "Perfect, thank you! {my_name} will call you shortly. 📞",
    'stop': "No problem, we won't message you again. Thanks and all the best!",
}


def get_templates():
    t = dict(DEFAULT_TEMPLATES)
    try:
        t.update(json.loads(kv_get('templates', '{}')))
    except ValueError:
        pass
    return t


def fill(tpl, lead):
    return (tpl.replace('{name}', lead.get('name') or '').replace('{category}', lead.get('category') or 'business')
            .replace('{city}', lead.get('city') or 'your city').replace('{rating}', str(lead.get('rating') or ''))
            .replace('{reviews}', str(lead.get('reviews') or '')).replace('{my_name}', env('MY_NAME', ''))
            .replace('{company}', env('COMPANY', '')).replace('{link}', lead_link(lead['slug']))
            .replace('{portfolio}', env('PORTFOLIO_URL', '')))


def ev(lead_id, typ, text='', ref=''):
    t = now_ms()
    x('INSERT INTO events(lead_id,t,type,text,ref) VALUES(?,?,?,?,?)', (lead_id, t, typ, text, ref))
    x('UPDATE leads SET updated=? WHERE id=?', (t, lead_id))


def notify(title, msg='', tags=''):
    topic = env('NTFY_TOPIC')
    if not topic:
        return

    def run():
        try:
            headers = {'Title': title.encode('ascii', 'ignore').decode() or 'LeadPilot'}
            if tags:
                headers['Tags'] = tags
            req = urllib.request.Request(env('NTFY_SERVER', 'https://ntfy.sh').rstrip('/') + '/' + urllib.parse.quote(topic),
                                         data=msg.encode('utf-8'), headers=headers, method='POST')
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print('notify failed:', e)
    threading.Thread(target=run, daemon=True).start()


def add_lead(name, category='', city='', address='', phone='', website='', rating=None, reviews=None, maps_url='',
             place_id=None, source='manual', stage='new', notes='', contact_number=''):
    place_id = place_id or None
    if place_id and q1('SELECT id FROM leads WHERE place_id=?', (place_id,)):
        return None
    slug = re.sub(r'[^a-z0-9]+', '-', (name or 'lead').lower()).strip('-')[:24] + '-' + secrets.token_hex(3)
    t = now_ms()
    lid = x('INSERT INTO leads(slug,place_id,name,category,city,address,phone,website,rating,reviews,maps_url,stage,notes,'
            'contact_number,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (slug, place_id, name, category, city, address, phone, website, rating, reviews, maps_url, stage, notes,
             contact_number, source, t, t))
    ev(lid, 'note', 'Lead added (' + source + ')')
    return lid


def find_lead(number):
    k = phone_key(number)
    if not k:
        return None
    for l in q('SELECT id, phone, contact_number FROM leads'):
        if phone_key(l['phone']) == k or phone_key(l['contact_number']) == k:
            return q1('SELECT * FROM leads WHERE id=?', (l['id'],))
    return None


def set_stage(lid, stage, note=''):
    l = q1('SELECT * FROM leads WHERE id=?', (lid,))
    if not l or stage not in STAGES or l['stage'] == stage:
        return
    nf = l['next_followup']
    if stage == 'contacted' and not nf:
        nf = in_days(3)
    if stage in ('closed', 'lost', 'meeting'):
        nf = ''
    x('UPDATE leads SET stage=?, next_followup=? WHERE id=?', (stage, nf, lid))
    ev(lid, 'stage', STAGE_LABEL[l['stage']] + ' → ' + STAGE_LABEL[stage] + (' (' + note + ')' if note else ''))


# ------------------------------------------------------------------ WhatsApp Cloud API


def wa_configured():
    return bool(env('WA_TOKEN') and env('WA_PHONE_ID'))


def wa_post(payload):
    if not wa_configured():
        raise RuntimeError('WhatsApp API is not configured (set WA_TOKEN and WA_PHONE_ID in .env)')
    url = 'https://graph.facebook.com/%s/%s/messages' % (env('WA_API_VERSION', 'v21.0'), env('WA_PHONE_ID'))
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + env('WA_TOKEN'), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError('WhatsApp API error: ' + e.read().decode('utf-8', 'ignore')[:300])


def wa_send_text(to, text):
    return wa_post({'messaging_product': 'whatsapp', 'to': norm_phone(to), 'type': 'text',
                    'text': {'preview_url': True, 'body': text}})


def wa_send_template(to, name, lang, params):
    comp = []
    if params:
        comp = [{'type': 'body', 'parameters': [{'type': 'text', 'text': str(p)} for p in params]}]
    return wa_post({'messaging_product': 'whatsapp', 'to': norm_phone(to), 'type': 'template',
                    'template': {'name': name, 'language': {'code': lang}, 'components': comp}})


def bot_reply(lead, key):
    text = fill(get_templates()[key], lead)
    try:
        wa_send_text(lead['phone'], text)
        ev(lead['id'], 'wa_out', text)
    except Exception as e:
        ev(lead['id'], 'wa_error', str(e))


def handle_inbound(m, contacts):
    frm = m.get('from', '')
    mid = m.get('id', '')
    typ = m.get('type', '')
    if typ == 'text':
        text = (m.get('text') or {}).get('body', '')
    elif typ == 'button':
        text = (m.get('button') or {}).get('text', '')
    elif typ == 'interactive':
        i = m.get('interactive') or {}
        text = (i.get('button_reply') or i.get('list_reply') or {}).get('title', '')
    else:
        text = '[' + typ + ']'
    if mid and q1("SELECT 1 FROM events WHERE type='wa_in' AND ref=?", (mid,)):
        return
    lead = find_lead(frm)
    if not lead:
        lid = add_lead(name=contacts.get(frm) or ('WhatsApp ' + frm[-4:]), phone=frm, source='inbound')
        lead = q1('SELECT * FROM leads WHERE id=?', (lid,))
    lid = lead['id']
    ev(lid, 'wa_in', text, mid)
    x('UPDATE leads SET opted_in=1 WHERE id=?', (lid,))
    if lead['optout']:
        return
    if re.search(r"\b(stop|unsubscribe|not interested|no thanks|don'?t message|do not message)\b", text.lower()):
        x('UPDATE leads SET optout=1, bot_step=9 WHERE id=?', (lid,))
        set_stage(lid, 'lost', 'opted out')
        bot_reply(lead, 'stop')
        return
    if lead['stage'] in ('new', 'contacted'):
        set_stage(lid, 'replied', 'they messaged')
    lead = q1('SELECT * FROM leads WHERE id=?', (lid,))
    step = lead['bot_step']
    phone = extract_phone(text)
    if phone and step in (0, 1, 2):
        x('UPDATE leads SET contact_number=?, bot_step=3 WHERE id=?', (phone, lid))
        if lead['stage'] in ('new', 'contacted', 'replied'):
            set_stage(lid, 'number', phone)
        bot_reply(lead, 'thanks')
        notify('HOT: number received', '%s gave number %s' % (lead['name'], phone), 'fire')
    elif step == 0:
        x('UPDATE leads SET bot_step=1 WHERE id=?', (lid,))
        bot_reply(lead, 'ask')
        notify('Reply from ' + lead['name'], text[:200], 'speech_balloon')
    elif step == 1:
        x('UPDATE leads SET bot_step=2 WHERE id=?', (lid,))
        bot_reply(lead, 'reask')
        notify('Reply from ' + lead['name'], text[:200], 'speech_balloon')
    else:
        if step == 2:
            x('UPDATE leads SET bot_step=9 WHERE id=?', (lid,))
        notify('Needs you: ' + lead['name'], text[:200], 'warning')


def handle_status(s):
    rid = s.get('recipient_id', '')
    st = s.get('status', '')
    ref = '%s:%s' % (s.get('id', ''), st)
    lead = find_lead(rid)
    if not lead or q1('SELECT 1 FROM events WHERE ref=? AND lead_id=?', (ref, lead['id'])):
        return
    txt = ''
    if st == 'failed':
        txt = json.dumps(s.get('errors', ''))[:200]
    ev(lead['id'], 'wa_' + st, txt, ref)


def handle_webhook(payload):
    for entry in payload.get('entry', []):
        for ch in entry.get('changes', []):
            v = ch.get('value', {})
            contacts = {c.get('wa_id'): (c.get('profile') or {}).get('name', '') for c in v.get('contacts', [])}
            for m in v.get('messages', []):
                handle_inbound(m, contacts)
            for s in v.get('statuses', []):
                handle_status(s)


def verify_sig(raw, header):
    secret = env('WA_APP_SECRET')
    if not secret:
        return True
    if not header or not header.startswith('sha256='):
        return False
    exp = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, header[7:])


# ------------------------------------------------------------------ automatic lead finder
JOB_RUNNING = threading.Lock()


def places_search(query, pages=3):
    key = env('GOOGLE_API_KEY')
    if not key:
        raise RuntimeError('GOOGLE_API_KEY is not set')
    mask = ('places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.websiteUri,'
            'places.rating,places.userRatingCount,places.googleMapsUri,places.businessStatus,nextPageToken')
    out, token = [], None
    for _ in range(pages):
        body = {'textQuery': query, 'pageSize': 20}
        if token:
            body['pageToken'] = token
        req = urllib.request.Request('https://places.googleapis.com/v1/places:searchText', data=json.dumps(body).encode(),
                                     method='POST', headers={'Content-Type': 'application/json', 'X-Goog-Api-Key': key,
                                                             'X-Goog-FieldMask': mask})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError('Google Places error: ' + e.read().decode('utf-8', 'ignore')[:300])
        out += j.get('places', [])
        token = j.get('nextPageToken')
        if not token:
            break
        time.sleep(1.2)
    return out


def run_job(job):
    try:
        places = places_search('%s in %s' % (job['biz_type'], job['area']))
    except Exception as e:
        x('UPDATE jobs SET last_run=?, last_result=? WHERE id=?', (now_ms(), 'Failed: ' + str(e)[:200], job['id']))
        return 0
    matched = added = nophone = 0
    for p in places:
        if p.get('businessStatus') == 'CLOSED_PERMANENTLY':
            continue
        if (p.get('userRatingCount') or 0) < job['min_reviews'] or (p.get('rating') or 0) < job['min_rating']:
            continue
        if job['no_website'] and p.get('websiteUri'):
            continue
        if not p.get('nationalPhoneNumber'):
            nophone += 1
            continue
        matched += 1
        lid = add_lead(name=(p.get('displayName') or {}).get('text', ''), category=job['biz_type'], city=job['area'],
                       address=p.get('formattedAddress', ''), phone=p.get('nationalPhoneNumber', ''),
                       website=p.get('websiteUri', ''), rating=p.get('rating'), reviews=p.get('userRatingCount'),
                       maps_url=p.get('googleMapsUri', ''), place_id=p.get('id'), source='auto-finder')
        if lid:
            added += 1
    res = '%d checked, %d matched, %d new%s' % (len(places), matched, added, (', %d skipped (no phone)' % nophone) if nophone else '')
    x('UPDATE jobs SET last_run=?, last_result=? WHERE id=?', (now_ms(), res, job['id']))
    return added


def run_all_jobs():
    if not JOB_RUNNING.acquire(blocking=False):
        return
    try:
        total = 0
        for job in q('SELECT * FROM jobs WHERE active=1'):
            total += run_job(job)
            time.sleep(2)
        if total:
            notify('LeadPilot: %d new leads' % total, 'The daily finder added %d new leads.' % total, 'mag')
    finally:
        JOB_RUNNING.release()


def scheduler():
    while True:
        try:
            ln = local_now()
            if ln.hour >= int(env('DAILY_HOUR', '9')) and kv_get('last_run') != ln.strftime('%Y-%m-%d'):
                kv_set('last_run', ln.strftime('%Y-%m-%d'))
                run_all_jobs()
        except Exception as e:
            print('scheduler error:', e)
        time.sleep(60)


# ------------------------------------------------------------------ auth
FAILS = {}


def make_token():
    exp = str(int(time.time()) + 7 * 86400)
    return exp + '.' + hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()


def check_token(tok):
    try:
        exp, sig = tok.split('.')
        return int(exp) > time.time() and hmac.compare_digest(sig, hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest())
    except Exception:
        return False


# ------------------------------------------------------------------ landing page
BOT_RE = re.compile(r'whatsapp|facebookexternalhit|facebot|bot\b|crawler|spider|preview|slurp|telegram|curl|python-requests|headless|bingpreview', re.I)

LANDING = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A website idea for @@NAME@@</title>
<meta property="og:title" content="A website idea for @@NAME@@"><meta property="og:description" content="3D interactive websites by @@COMPANY@@. See our work.">
<meta name="robots" content="noindex">
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;background:radial-gradient(circle at 20% 10%,#2b2f6b 0,#0b0d1f 55%);color:#eef0ff;font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.c{max-width:560px;width:100%}.k{letter-spacing:.14em;text-transform:uppercase;font-size:12px;color:#9aa3ff;margin:0 0 14px}
h1{font-size:clamp(30px,6vw,44px);line-height:1.1;margin:0 0 16px}p{color:#c9cdf2;margin:0 0 26px}
a.b{display:block;text-align:center;padding:15px 18px;border-radius:12px;text-decoration:none;font-weight:700;margin-bottom:12px}
a.p{background:#6366f1;color:#fff}a.w{background:#16a34a;color:#fff}
.f{margin-top:26px;font-size:12px;color:#8089c9}
</style></head><body><div class="c">
<p class="k">@@COMPANY@@</p>
<h1>A website idea for @@NAME@@</h1>
<p>@@LINE@@A 3D interactive website makes that trust visible the moment someone finds you online. See what we build, then message us if you'd like something similar for @@NAME@@.</p>
<a class="b p" href="/go/@@SLUG@@/p">See websites we've built →</a>
<a class="b w" href="/go/@@SLUG@@/w">Chat with us on WhatsApp</a>
<div class="f">Sent by @@MY@@. Not interested? Reply STOP on WhatsApp and we won't contact you again.</div>
</div>
<script>
(function(){var s='@@SLUG@@',t0=Date.now(),mx=0,sent=0;
function sc(){var h=document.documentElement;mx=Math.max(mx,Math.round((window.scrollY+innerHeight)/Math.max(h.scrollHeight,1)*100))}
addEventListener('scroll',sc);
function send(){var sec=Math.round((Date.now()-t0)/1000);if(sec<2||sent===sec)return;sent=sec;
var d=JSON.stringify({slug:s,seconds:sec,scroll:mx});
if(navigator.sendBeacon){navigator.sendBeacon('/track',new Blob([d],{type:'text/plain'}))}else{fetch('/track',{method:'POST',body:d,keepalive:true})}}
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='hidden')send()});addEventListener('pagehide',send);
setInterval(function(){if(!document.hidden)send()},20000);})();
</script></body></html>'''


def render_landing(lead):
    line = ''
    if lead['rating'] and lead['reviews']:
        line = 'Your customers rate you %s★ across %s+ Google reviews. ' % (lead['rating'], lead['reviews'])
    page = LANDING
    for k, v in (('@@NAME@@', lead['name']), ('@@COMPANY@@', env('COMPANY', 'Our studio')), ('@@MY@@', env('MY_NAME', env('COMPANY', ''))),
                 ('@@LINE@@', line), ('@@SLUG@@', lead['slug'])):
        page = page.replace(k, html.escape(str(v), quote=True))
    return page


def recent_event(lid, typ, ms):
    return q1('SELECT 1 FROM events WHERE lead_id=? AND type=? AND t>?', (lid, typ, now_ms() - ms))


# ------------------------------------------------------------------ API logic
def lead_summary(l):
    d = dict(l)
    d['link'] = lead_link(l['slug'])
    d['wa_number'] = norm_phone(l['phone'])
    d['mobile'] = is_mobile(l['phone'])
    return d


def state():
    leads = q('''SELECT l.*,
      (SELECT COUNT(*) FROM events e WHERE e.lead_id=l.id AND e.type='page_open') opens,
      (SELECT COUNT(*) FROM events e WHERE e.lead_id=l.id AND e.type IN ('click_portfolio','click_wa')) clicks,
      (SELECT COUNT(*) FROM events e WHERE e.lead_id=l.id AND e.type='wa_read') reads,
      (SELECT COUNT(*) FROM events e WHERE e.lead_id=l.id AND e.type='wa_in') inbound,
      (SELECT MAX(CAST(e.text AS INTEGER)) FROM events e WHERE e.lead_id=l.id AND e.type='time_on_page') secs
      FROM leads l ORDER BY l.updated DESC''')
    d0 = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    per_day = []
    for i in range(6, -1, -1):
        s = d0 - timedelta(days=i)
        e = s + timedelta(days=1)
        n = q1('SELECT COUNT(*) n FROM sent_log WHERE t>=? AND t<?', (int(s.timestamp() * 1000), int(e.timestamp() * 1000)))['n']
        per_day.append({'label': s.strftime('%a'), 'n': n})
    sent_today = per_day[-1]['n']
    return {
        'config': {'my_name': env('MY_NAME'), 'company': env('COMPANY'), 'public_url': public_url(),
                   'portfolio': env('PORTFOLIO_URL'), 'owner_wa': env('OWNER_WA_NUMBER'),
                   'daily_limit': int(env('DAILY_LIMIT', '50') or 50), 'daily_hour': int(env('DAILY_HOUR', '9') or 9),
                   'wa_api': wa_configured(), 'google': bool(env('GOOGLE_API_KEY')), 'ntfy': bool(env('NTFY_TOPIC')),
                   'template_name': env('WA_TEMPLATE_NAME'), 'webhook_url': public_url() + '/webhook',
                   'verify_token_set': bool(env('WA_VERIFY_TOKEN')), 'app_secret_set': bool(env('WA_APP_SECRET'))},
        'templates': get_templates(),
        'stages': [[k, STAGE_LABEL[k]] for k in STAGES],
        'sent_today': sent_today, 'per_day': per_day,
        'leads': [lead_summary(l) for l in leads],
        'jobs': q('SELECT * FROM jobs ORDER BY id'),
        'job_running': JOB_RUNNING.locked(),
    }


EDITABLE = ('name', 'category', 'city', 'address', 'phone', 'notes', 'contact_number', 'meeting_at', 'next_followup')


def api(method, path, body):
    """Returns (status, object)."""
    m = re.fullmatch(r'/api/lead/(\d+)(?:/(\w+))?', path)
    if path == '/api/state' and method == 'GET':
        return 200, state()
    if m:
        lid = int(m.group(1))
        sub = m.group(2)
        lead = q1('SELECT * FROM leads WHERE id=?', (lid,))
        if not lead:
            return 404, {'error': 'Lead not found'}
        if sub is None and method == 'GET':
            return 200, {'lead': lead_summary(lead), 'events': q('SELECT * FROM events WHERE lead_id=? ORDER BY t DESC, id DESC LIMIT 200', (lid,))}
        if sub is None and method == 'PATCH':
            for k in EDITABLE:
                if k in body:
                    x('UPDATE leads SET %s=? WHERE id=?' % k, (str(body[k]), lid))
            if 'opted_in' in body:
                x('UPDATE leads SET opted_in=? WHERE id=?', (1 if body['opted_in'] else 0, lid))
            if 'optout' in body:
                x('UPDATE leads SET optout=? WHERE id=?', (1 if body['optout'] else 0, lid))
            x('UPDATE leads SET updated=? WHERE id=?', (now_ms(), lid))
            return 200, {'ok': True}
        if sub is None and method == 'DELETE':
            x('DELETE FROM events WHERE lead_id=?', (lid,))
            x('DELETE FROM leads WHERE id=?', (lid,))
            return 200, {'ok': True}
        if sub == 'stage' and method == 'POST':
            st = body.get('stage')
            if st not in STAGES:
                return 400, {'error': 'Bad stage'}
            if st == 'number' and body.get('value'):
                x('UPDATE leads SET contact_number=? WHERE id=?', (str(body['value']), lid))
            if st == 'meeting' and body.get('value'):
                x('UPDATE leads SET meeting_at=? WHERE id=?', (str(body['value']), lid))
            set_stage(lid, st, str(body.get('value') or body.get('note') or ''))
            return 200, {'ok': True}
        if sub == 'sent' and method == 'POST':
            kind = body.get('kind', 'first')
            x('INSERT INTO sent_log(t,lead_id) VALUES(?,?)', (now_ms(), lid))
            ev(lid, 'sent', 'WhatsApp opened: ' + kind)
            if lead['stage'] == 'new':
                set_stage(lid, 'contacted')
            elif kind == 'followup':
                x('UPDATE leads SET next_followup=? WHERE id=?', (in_days(4), lid))
            return 200, {'ok': True}
        if sub == 'send' and method == 'POST':
            if lead['optout']:
                return 400, {'error': 'This lead opted out'}
            text = str(body.get('text', '')).strip()
            if not text:
                return 400, {'error': 'Empty message'}
            try:
                wa_send_text(lead['phone'], text)
            except Exception as e:
                return 400, {'error': str(e)}
            ev(lid, 'wa_out', text)
            x('INSERT INTO sent_log(t,lead_id) VALUES(?,?)', (now_ms(), lid))
            if lead['stage'] == 'new':
                set_stage(lid, 'contacted')
            return 200, {'ok': True}
        if sub == 'template' and method == 'POST':
            if lead['optout']:
                return 400, {'error': 'This lead opted out'}
            if not lead['opted_in']:
                return 400, {'error': 'Only leads who opted in (messaged you first, or ticked "opted in") can receive template messages.'}
            name = env('WA_TEMPLATE_NAME')
            if not name:
                return 400, {'error': 'WA_TEMPLATE_NAME is not set in .env'}
            try:
                wa_send_template(lead['phone'], name, env('WA_TEMPLATE_LANG', 'en'), [lead['name'], lead_link(lead['slug'])])
            except Exception as e:
                return 400, {'error': str(e)}
            ev(lid, 'wa_out', 'Template message: ' + name)
            x('INSERT INTO sent_log(t,lead_id) VALUES(?,?)', (now_ms(), lid))
            if lead['stage'] == 'new':
                set_stage(lid, 'contacted')
            return 200, {'ok': True}
        if sub == 'bot' and method == 'POST':
            x('UPDATE leads SET bot_step=? WHERE id=?', (9 if body.get('pause') else 0, lid))
            ev(lid, 'note', 'Bot paused, you took over' if body.get('pause') else 'Bot resumed')
            return 200, {'ok': True}
    if path == '/api/lead' and method == 'POST':
        name = str(body.get('name', '')).strip()
        if not name:
            return 400, {'error': 'Name required'}
        lid = add_lead(name=name, category=body.get('category', ''), city=body.get('city', ''), phone=body.get('phone', ''))
        return 200, {'id': lid}
    if path == '/api/templates' and method == 'POST':
        t = get_templates()
        for k in DEFAULT_TEMPLATES:
            if k in body:
                t[k] = str(body[k])
        kv_set('templates', json.dumps(t))
        return 200, {'ok': True}
    if path == '/api/jobs' and method == 'POST':
        bt, area = str(body.get('biz_type', '')).strip(), str(body.get('area', '')).strip()
        if not bt or not area:
            return 400, {'error': 'Business type and area are required'}
        jid = x('INSERT INTO jobs(biz_type,area,min_reviews,min_rating,no_website) VALUES(?,?,?,?,?)',
                (bt, area, int(body.get('min_reviews') or 100), float(body.get('min_rating') or 4.0), 1 if body.get('no_website', True) else 0))
        return 200, {'id': jid}
    if path == '/api/jobs/run' and method == 'POST':
        if not env('GOOGLE_API_KEY'):
            return 400, {'error': 'GOOGLE_API_KEY is not set in .env'}
        if JOB_RUNNING.locked():
            return 200, {'ok': True, 'note': 'Already running'}
        threading.Thread(target=run_all_jobs, daemon=True).start()
        return 200, {'ok': True}
    jm = re.fullmatch(r'/api/jobs/(\d+)', path)
    if jm:
        jid = int(jm.group(1))
        if method == 'DELETE':
            x('DELETE FROM jobs WHERE id=?', (jid,))
            return 200, {'ok': True}
        if method == 'PATCH':
            if 'active' in body:
                x('UPDATE jobs SET active=? WHERE id=?', (1 if body['active'] else 0, jid))
            return 200, {'ok': True}
    if path == '/api/import' and method == 'POST':
        added = skipped = 0
        stage_by_label = {v.lower(): k for k, v in STAGE_LABEL.items()}
        for r in body.get('leads', []):
            name = str(r.get('name', '')).strip()
            phone = str(r.get('phone', '')).strip()
            if not name:
                continue
            if phone and find_lead(phone):
                skipped += 1
                continue
            st = str(r.get('stage', 'new')).strip()
            st = st if st in STAGES else stage_by_label.get(st.lower(), 'new')

            def num(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            rev = num(r.get('reviews'))
            lid = add_lead(name=name, category=str(r.get('category', '')), city=str(r.get('city', '')), address=str(r.get('address', '')),
                           phone=phone, website=str(r.get('website', '')), rating=num(r.get('rating')), reviews=int(rev) if rev is not None else None,
                           maps_url=str(r.get('mapsUrl', r.get('maps_url', ''))), place_id=r.get('placeId') or r.get('place_id'),
                           source='import', stage=st, notes=str(r.get('notes', '')),
                           contact_number=str(r.get('contactNumber', r.get('contact_number', ''))))
            if lid:
                added += 1
            else:
                skipped += 1
        return 200, {'added': added, 'skipped': skipped}
    return 404, {'error': 'Not found'}


def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    cols = ['name', 'category', 'city', 'address', 'phone', 'website', 'rating', 'reviews', 'stage', 'contact_number',
            'meeting_at', 'next_followup', 'notes', 'maps_url', 'link']
    w.writerow(cols)
    for l in q('SELECT * FROM leads ORDER BY id'):
        l['link'] = lead_link(l['slug'])
        w.writerow([STAGE_LABEL.get(l[c], l[c]) if c == 'stage' else l.get(c, '') for c in cols])
    return out.getvalue()


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version = 'LeadPilot'

    def log_message(self, fmt, *a):
        pass

    def ip(self):
        if trust_proxy():
            return (self.headers.get('X-Forwarded-For') or '').split(',')[0].strip() or self.client_address[0]
        return self.client_address[0]

    def send(self, code, body, ctype='application/json', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if ctype.startswith('text') or 'json' in ctype else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def authed(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'lp' and check_token(v):
                return True
        return False

    def raw_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 8 * 1024 * 1024:
            raise ValueError('Body too large')
        return self.rfile.read(n) if n else b''

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            # ---- public routes
            if method == 'GET' and path == '/health':
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/d/([\w-]+)', path)
            if m and method == 'GET':
                lead = q1('SELECT * FROM leads WHERE slug=?', (m.group(1),))
                if not lead:
                    return self.send(404, 'Not found', 'text/plain')
                ua = self.headers.get('User-Agent', '')
                if BOT_RE.search(ua):
                    if not recent_event(lead['id'], 'preview_fetch', 6 * 3600 * 1000):
                        ev(lead['id'], 'preview_fetch', 'Link preview or bot (not counted as a human open)')
                else:
                    ev(lead['id'], 'page_open', ua[:120])
                    if not recent_event(lead['id'], 'notified_open', 3600 * 1000) and lead['stage'] in ('new', 'contacted'):
                        ev(lead['id'], 'notified_open', '')
                        notify('Link opened: ' + lead['name'], '%s opened your link.' % lead['name'], 'eyes')
                return self.send(200, render_landing(lead), 'text/html', {'Cache-Control': 'no-store'})
            m = re.fullmatch(r'/go/([\w-]+)/([pw])', path)
            if m and method == 'GET':
                lead = q1('SELECT * FROM leads WHERE slug=?', (m.group(1),))
                if not lead:
                    return self.send(404, 'Not found', 'text/plain')
                if m.group(2) == 'p':
                    target = env('PORTFOLIO_URL')
                    if not target:
                        return self.send(404, 'Portfolio link not configured', 'text/plain')
                    typ, label = 'click_portfolio', 'Clicked "See websites we built"'
                else:
                    owner = re.sub(r'\D', '', env('OWNER_WA_NUMBER'))
                    if not owner:
                        return self.send(404, 'WhatsApp number not configured', 'text/plain')
                    text = "Hi! I'm from %s. I saw your website idea and I'd like to know more." % lead['name']
                    target = 'https://wa.me/%s?text=%s' % (owner, urllib.parse.quote(text))
                    typ, label = 'click_wa', 'Clicked "Chat on WhatsApp"'
                if not BOT_RE.search(self.headers.get('User-Agent', '')):
                    ev(lead['id'], typ, label)
                    if not recent_event(lead['id'], 'notified_' + typ, 3600 * 1000):
                        ev(lead['id'], 'notified_' + typ, '')
                        notify('Warm lead: ' + lead['name'], label, 'fire' if typ == 'click_wa' else 'eyes')
                return self.redirect(target)
            if path == '/track' and method == 'POST':
                try:
                    d = json.loads(self.raw_body().decode('utf-8', 'ignore') or '{}')
                    lead = q1('SELECT id FROM leads WHERE slug=?', (str(d.get('slug', '')),))
                    sec = max(0, min(int(d.get('seconds', 0)), 3600))
                    if lead and sec:
                        prev = q1("SELECT id, CAST(text AS INTEGER) s FROM events WHERE lead_id=? AND type='time_on_page' ORDER BY id DESC LIMIT 1", (lead['id'],))
                        if not prev or sec > prev['s']:
                            ev(lead['id'], 'time_on_page', str(sec))
                except Exception:
                    pass
                return self.send(204, b'', 'text/plain')
            if path == '/webhook':
                if method == 'GET':
                    qs = urllib.parse.parse_qs(u.query)
                    tok = env('WA_VERIFY_TOKEN')
                    if tok and qs.get('hub.mode', [''])[0] == 'subscribe' and hmac.compare_digest(qs.get('hub.verify_token', [''])[0], tok):
                        return self.send(200, qs.get('hub.challenge', [''])[0], 'text/plain')
                    return self.send(403, 'Forbidden', 'text/plain')
                if method == 'POST':
                    raw = self.raw_body()
                    if not verify_sig(raw, self.headers.get('X-Hub-Signature-256')):
                        return self.send(403, 'Bad signature', 'text/plain')
                    try:
                        handle_webhook(json.loads(raw.decode('utf-8') or '{}'))
                    except Exception as e:
                        print('webhook error:', repr(e))
                    return self.send(200, 'ok', 'text/plain')
            # ---- login
            if path == '/api/login' and method == 'POST':
                ip = self.ip()
                n, t0 = FAILS.get(ip, (0, time.time()))
                if time.time() - t0 > 600:
                    n, t0 = 0, time.time()
                if n >= 8:
                    return self.send(429, {'error': 'Too many attempts. Try again in a few minutes.'})
                d = json.loads(self.raw_body() or b'{}')
                if hmac.compare_digest(str(d.get('password', '')).encode(), env('ADMIN_PASSWORD').encode()):
                    FAILS.pop(ip, None)
                    secure = '; Secure' if public_url().startswith('https') else ''
                    return self.send(200, {'ok': True}, headers={'Set-Cookie': 'lp=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800%s' % (make_token(), secure)})
                FAILS[ip] = (n + 1, t0)
                time.sleep(1)
                return self.send(401, {'error': 'Wrong password'})
            if path == '/api/logout' and method == 'POST':
                return self.send(200, {'ok': True}, headers={'Set-Cookie': 'lp=; HttpOnly; Path=/; Max-Age=0'})
            # ---- dashboard
            if method == 'GET' and path in ('/', '/index.html'):
                with open(os.path.join(BASE, 'dashboard.html'), 'rb') as f:
                    return self.send(200, f.read(), 'text/html', {'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY'})
            # ---- protected API
            if path.startswith('/api/'):
                if not self.authed():
                    return self.send(401, {'error': 'Not logged in'})
                if method != 'GET' and self.headers.get('X-LP') != '1':
                    return self.send(403, {'error': 'Missing header'})
                if path == '/api/export.csv' and method == 'GET':
                    return self.send(200, export_csv(), 'text/csv', {'Content-Disposition': 'attachment; filename="leads-%s.csv"' % today_str()})
                raw = self.raw_body() if method in ('POST', 'PATCH', 'DELETE') else b''
                body = json.loads(raw.decode('utf-8')) if raw else {}
                code, obj = api(method, path, body)
                return self.send(code, obj)
            return self.send(404, 'Not found', 'text/plain')
        except Exception as e:
            print('error', method, path, repr(e))
            return self.send(500, {'error': 'Server error: ' + str(e)[:200]})

    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def do_PATCH(self): self.route('PATCH')
    def do_DELETE(self): self.route('DELETE')


def main():
    pw = env('ADMIN_PASSWORD')
    if len(pw) < 8:
        sys.exit('Set ADMIN_PASSWORD (at least 8 characters) in .env before starting. See .env.example.')
    threading.Thread(target=scheduler, daemon=True).start()
    if env('RAILWAY_ENVIRONMENT') and not os.environ.get('DATA_DIR'):
        print('WARNING: DATA_DIR is not set. Add a Railway volume mounted at /data and set DATA_DIR=/data, '
              'otherwise your data is lost on every redeploy.')
    # Hosting platforms (Railway etc.) set PORT and need the server to listen on all interfaces.
    host = env('HOST') or ('0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')
    port = int(env('PORT', '8080'))
    srv = ThreadingHTTPServer((host, port), H)
    print('LeadPilot running on http://%s:%d  (public URL: %s)' % (host, port, public_url()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
