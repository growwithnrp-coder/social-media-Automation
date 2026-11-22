# LeadPilot server

Finds leads every day, sends you a tracked link per lead, records opens/clicks/replies, runs a WhatsApp bot that asks for the phone number, and alerts you when a lead is hot. Needs only Python 3.8+ (no installs).

## How the flow works

1. **Daily finder** pulls businesses from Google Maps (rating, 100+ reviews, no website, has phone) into your leads.
2. **You send the first message** from the dashboard with one click (opens WhatsApp with the text ready). It contains the lead's personal link.
3. The link opens a landing page with **"See websites we've built"** (goes to your portfolio) and **"Chat with us on WhatsApp"**. Every open, click and time-on-page is recorded.
4. When they **message your WhatsApp Business number**, the bot replies, asks for their phone number, saves it, moves the lead to "Number received" and **pings your phone**.
5. You call them, then fix the meeting. Mark stages in the dashboard.
6. If someone replies STOP, they are marked opted out and never messaged again.

## 1. Run it on your computer first (5 minutes)

```
cp .env.example .env        # then edit .env (ADMIN_PASSWORD at minimum)
python3 server.py
```
Open http://localhost:8080 and log in. The dashboard works; tracking links only work for people who can reach your server, so deploy before sending real messages.

## 2a. Easiest: Railway

Push this folder to a private GitHub repo (the `.gitignore` keeps your secrets out), then in Railway: New Project → Deploy from GitHub repo. Add a **Volume** mounted at `/data` and set `DATA_DIR=/data` (otherwise your data is lost on every redeploy). Add your settings from `.env.example` in the service's **Variables** tab (not a `.env` file), generate a domain under Settings → Networking, and open it. `PORT`, HTTPS and the public domain are picked up automatically.

## 2b. Or any VPS (Put it online, needed for tracking and WhatsApp)

Any small Linux VPS (₹400-800/month) works. Point a domain/subdomain to it, then:

**Caddy (gives free HTTPS automatically)** `/etc/caddy/Caddyfile`:
```
leads.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}
```
Set `PUBLIC_URL=https://leads.yourdomain.com` and `TRUST_PROXY=1` in `.env`.

**Keep it running** `/etc/systemd/system/leadpilot.service`:
```
[Unit]
Description=LeadPilot
After=network.target

[Service]
WorkingDirectory=/opt/leadpilot
ExecStart=/usr/bin/python3 /opt/leadpilot/server.py
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
Then `systemctl enable --now leadpilot`. Back up the `data/` folder regularly (it holds everything).

## 3. Google lead finder

Google Cloud Console → create a project → enable **Places API (New)** → create an API key → put it in `GOOGLE_API_KEY`. Restrict the key to that API. Each search costs a small amount per request after Google's free monthly credit, so start with a few searches in **Auto-finder**.

## 4. WhatsApp Cloud API (Meta)

1. developers.facebook.com → create an app (type Business) → add the **WhatsApp** product.
2. Add a phone number. It must be a number **not currently used in the WhatsApp app** (get a new SIM or use the Meta test number while developing).
3. Copy the **Phone number ID** into `WA_PHONE_ID`. Create a permanent token (Business settings → System users → generate token with `whatsapp_business_messaging`) into `WA_TOKEN`.
4. WhatsApp → Configuration → Webhook: Callback URL `https://leads.yourdomain.com/webhook`, Verify token = your `WA_VERIFY_TOKEN`. Subscribe to **messages**. Put your app secret in `WA_APP_SECRET`.
5. Put the same number in `OWNER_WA_NUMBER` (digits only) so the landing page's WhatsApp button opens a chat with it.

Rules to stay within: businesses may only receive Meta **template** messages if they opted in. Cold first messages go out through the dashboard's one-click WhatsApp (sent by you, like a normal chat). Once a lead messages you, the bot can reply freely for 24 hours.

Optional template (create in Meta → Message templates, category Marketing, two variables) for opted-in leads:
`Hi {{1}}, thanks for your interest in a 3D website. Here's our work and your personal link: {{2}}`
Put its name in `WA_TEMPLATE_NAME`.

## 5. Phone alerts (optional)

Install the free **ntfy** app, subscribe to a hard-to-guess topic name, set `NTFY_TOPIC` to it. You get a push for opens, clicks, replies and numbers.

## Notes

- Link previews (WhatsApp fetching the link) are detected and not counted as opens.
- The dashboard login is one password. Use a long one, and keep the server on HTTPS.
- Import your v1 browser backup (`LeadPilot.html` → Settings → Backup) from Leads → Import.
