#!/usr/bin/env python3
import http.server, json, os, traceback, smtplib, time, datetime, threading, urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from groq import Groq
from tavily import TavilyClient

PORT = int(os.getenv('PORT', 8765))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'intel-dash.html')
SCHEDULE_FILE = os.path.expanduser('~/.inteldash_schedule.json')

GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY', '')
GMAIL_USER = os.getenv('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD', '')
SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK', '')


def send_email(to_address, subject, body):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError('GMAIL_USER / GMAIL_APP_PASSWORD env vars not set')
    msg = MIMEMultipart()
    msg['From'] = GMAIL_USER
    msg['To'] = to_address
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def send_slack(findings):
    if not SLACK_WEBHOOK:
        return
    high = [f for f in findings if f.get('impact_level') == 'high']
    if not findings:
        return
    lines = [f"*Intel Dash — {len(findings)} findings ({len(high)} high impact)*\n"]
    for f in (high or findings)[:6]:
        impact_emoji = '🔴' if f.get('impact_level') == 'high' else '🟡' if f.get('impact_level') == 'medium' else '🟢'
        lines.append(f"{impact_emoji} *{f.get('company','')}* [{f.get('change_type','').upper()}]\n{f.get('summary','')}\n_{f.get('date','')}_")
    payload = json.dumps({"text": "\n\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(SLACK_WEBHOOK, data=payload, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        print("  Slack notification sent")
    except Exception as e:
        print(f"  Slack failed: {e}")


KNOWN_DOMAINS = {
    'cleo consulting': 'cleoconsulting.com',
    'kpmg': 'kpmg.com', 'pwc': 'pwc.com', 'deloitte': 'deloitte.com',
    'accenture': 'accenture.com', 'mckinsey': 'mckinsey.com',
    'randstad': 'randstad.com', 'bain': 'bain.com', 'ey': 'ey.com',
}

def search_web(company: str, context: str = "") -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        all_results = []
        company_domain = KNOWN_DOMAINS.get(company.lower().strip())
        # Add qualifier to disambiguate company names from places/other entities
        qualifier = "company" if not context else context.split()[2] if len(context.split()) > 2 else "company"

        try:
            r1 = client.search(query=f'"{company}" {qualifier}', search_depth="advanced", topic="news", max_results=15, days=365, include_raw_content=False)
            all_results += r1.get("results", [])
        except Exception as e:
            print(f"  Tavily news search failed: {e}")

        try:
            r2 = client.search(query=f'"{company}" {qualifier} 2025 OR 2026', search_depth="advanced", max_results=10, days=365, include_raw_content=False)
            all_results += r2.get("results", [])
        except Exception as e:
            print(f"  Tavily web search failed: {e}")

        try:
            r3 = client.search(query=f'"{company}"', search_depth="advanced", max_results=5, days=365, include_domains=["linkedin.com"], include_raw_content=False)
            all_results += r3.get("results", [])
        except Exception:
            pass

        # Always search the company's own website directly
        if company_domain:
            try:
                r_site = client.search(query=f'{company} services news hiring', search_depth="advanced", max_results=10, days=730, include_domains=[company_domain], include_raw_content=False)
                all_results += r_site.get("results", [])
                print(f"  Direct site search for {company_domain}: {len(r_site.get('results',[]))} results")
            except Exception as e:
                print(f"  Site search failed: {e}")

        # If still fewer than 5 raw results, broaden further
        if len(all_results) < 5:
            try:
                r4 = client.search(query=f'{company} news updates services', search_depth="advanced", max_results=10, days=730, include_raw_content=False)
                all_results += r4.get("results", [])
            except Exception:
                pass
            try:
                r5 = client.search(query=f'{company}', search_depth="advanced", max_results=5, days=730, include_domains=["linkedin.com"], include_raw_content=False)
                all_results += r5.get("results", [])
            except Exception:
                pass

        # Name matching: require full company name OR all significant words present together
        def name_match(text, url=''):
            t = text.lower()
            cn = company.lower()
            if cn in t:
                return True
            # If result is from the company's own domain, always accept
            if company_domain and company_domain in url.lower():
                return True
            # All words ≥4 chars must appear
            words = [w for w in cn.split() if len(w) >= 4]
            return len(words) >= 2 and all(w in t for w in words)

        seen_urls = set()
        snippets = []
        for r in all_results:
            title = r.get('title', '')
            content = r.get('content', '')
            url = r.get('url', '')
            published = r.get('published_date', '')
            if url in seen_urls:
                continue
            seen_urls.add(url)
            combined = title + ' ' + content
            if not name_match(combined, url):
                continue
            date_tag = f" [published: {published[:10]}]" if published else ""
            snippets.append(f"- {title}{date_tag}: {content[:300]} (source: {url})")

        # If fewer than 4 matching snippets, do broader searches regardless of raw result count
        if len(snippets) < 4:
            print(f"  Only {len(snippets)} matching snippets — broadening search for {company}")
            try:
                r4b = client.search(query=f'{company} news updates services', search_depth="advanced", max_results=10, days=730, include_raw_content=False)
                for r in r4b.get("results", []):
                    url = r.get('url', '')
                    if url in seen_urls: continue
                    seen_urls.add(url)
                    title = r.get('title', '')
                    content = r.get('content', '')
                    combined = title + ' ' + content
                    if not name_match(combined, url): continue
                    published = r.get('published_date', '')
                    date_tag = f" [published: {published[:10]}]" if published else ""
                    snippets.append(f"- {title}{date_tag}: {content[:300]} (source: {url})")
            except Exception:
                pass
            try:
                r5b = client.search(query=f'{company}', search_depth="advanced", max_results=8, days=730, include_domains=["linkedin.com"], include_raw_content=False)
                for r in r5b.get("results", []):
                    url = r.get('url', '')
                    if url in seen_urls: continue
                    seen_urls.add(url)
                    title = r.get('title', '')
                    content = r.get('content', '')
                    combined = title + ' ' + content
                    if not name_match(combined, url): continue
                    published = r.get('published_date', '')
                    date_tag = f" [published: {published[:10]}]" if published else ""
                    snippets.append(f"- {title}{date_tag}: {content[:300]} (source: {url})")
            except Exception:
                pass

        if not snippets:
            print(f"  No results found for {company} — trying direct site search")
            try:
                first_word = company.split()[0].lower()
                r6 = client.search(query=f'site:{first_word}consulting.com OR "{company}"', search_depth="advanced", max_results=8, days=730, include_raw_content=False)
                for r in r6.get("results", []):
                    title = r.get('title', '')
                    content = r.get('content', '')
                    url = r.get('url', '')
                    published = r.get('published_date', '')
                    date_tag = f" [published: {published[:10]}]" if published else ""
                    snippets.append(f"- {title}{date_tag}: {content[:300]} (source: {url})")
            except Exception:
                pass

        snippets = snippets[:7]
        print(f"  Tavily found {len(snippets)} relevant results for {company}")
        return "\n".join(snippets)
    except Exception as e:
        print(f"  Tavily search failed: {e}")
        return ""


def call_groq(prompt: str) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="mixtral-8x7b-32768",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            err = str(e)
            print(f"  GROQ ERROR (attempt {attempt+1}): {type(e).__name__}: {err[:300]}")
            if '429' in err or 'rate' in err.lower():
                wait = 10 * (attempt + 1)
                print(f"  Rate limit, waiting {wait}s...")
                time.sleep(wait)
            elif 'token' in err.lower() or 'context' in err.lower() or 'length' in err.lower():
                raise RuntimeError(f"Prompt too long for Groq: {err[:200]}")
            else:
                raise
    raise RuntimeError("Still failing after retries")


def run_scheduled_research(cfg):
    """Run a full research pass and email/Slack the results."""
    competitors = [c.strip() for c in cfg.get('competitors', '').split(',') if c.strip()]
    my_company = cfg.get('myCompany', '')
    to_email = cfg.get('email', '')
    if not competitors:
        return
    print(f"  [Scheduler] Running research for: {', '.join(competitors)}")
    all_findings = []
    context = f"We are: {my_company}." if my_company else ""
    for company in competitors:
        try:
            web_results = search_web(company, my_company)
            if not web_results:
                continue
            prompt = (
                f'You are a competitive intelligence analyst. {context}\n'
                f'Extract 4-6 findings about "{company}" from the sources below.\n'
                f'Rules: only exact company "{company}", only facts from sources, specific events only.\n'
                f'Return ONLY a valid JSON array:\n'
                f'[{{"company":"{company}","change_type":"pricing|product|hiring|news","impact_level":"high|medium|low","summary":"2-3 sentences","date":"Mon YYYY (month and year only, e.g. Jan 2025)","url":"source url"}}]\n\n'
                f'Sources:\n{web_results}'
            )
            answer = call_groq(prompt)
            clean = answer.replace('```json', '').replace('```', '').strip()
            a, b = clean.find('['), clean.rfind(']') + 1
            if a >= 0:
                findings = json.loads(clean[a:b])
                all_findings.extend(findings)
        except Exception as e:
            print(f"  [Scheduler] Error for {company}: {e}")

    if not all_findings:
        print("  [Scheduler] No findings — skipping notifications")
        return

    send_slack(all_findings)

    if to_email:
        try:
            summary = '\n'.join(f"{i+1}. [{f['company']}] [{f.get('impact_level','').upper()}] {f.get('summary','')} ({f.get('date','')})" for i, f in enumerate(all_findings[:10]))
            prompt = f'Write a professional competitive intelligence digest email. First line: "Subject: ...". Then 3-5 bullet takeaways and one strategic recommendation.\n\nFindings:\n{summary}'
            digest = call_groq(prompt)
            lines = digest.strip().split('\n')
            subject = 'Intel Dash — Scheduled Digest'
            body_text = digest
            for i, line in enumerate(lines):
                if line.lower().startswith('subject:'):
                    subject = line.split(':', 1)[1].strip()
                    body_text = '\n'.join(lines[i+1:]).strip()
                    break
            send_email(to_email, subject, body_text)
            print(f"  [Scheduler] Digest emailed to {to_email}")
        except Exception as e:
            print(f"  [Scheduler] Email failed: {e}")


def scheduler_loop():
    last_fired = None
    while True:
        time.sleep(30)
        try:
            if not os.path.exists(SCHEDULE_FILE):
                continue
            with open(SCHEDULE_FILE) as f:
                cfg = json.load(f)
            if not cfg.get('enabled'):
                continue
            now = datetime.datetime.now().strftime('%H:%M')
            if now == cfg.get('time') and now != last_fired:
                last_fired = now
                threading.Thread(target=run_scheduled_research, args=(cfg,), daemon=True).start()
        except Exception as e:
            print(f"  [Scheduler] Error: {e}")


threading.Thread(target=scheduler_loop, daemon=True).start()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._cors()
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        try:
            with open(HTML_FILE, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        try:
            # --- Save schedule config ---
            if body.get("saveSchedule") is not None:
                cfg = body.get("saveSchedule")
                with open(SCHEDULE_FILE, 'w') as f:
                    json.dump(cfg, f)
                self._json({"ok": True})
                return

            # --- Get schedule config ---
            if body.get("getSchedule"):
                if os.path.exists(SCHEDULE_FILE):
                    with open(SCHEDULE_FILE) as f:
                        self._json(json.load(f))
                else:
                    self._json({})
                return

            # --- Send Slack notification ---
            if body.get("slackNotify"):
                findings = body.get("findings", [])
                send_slack(findings)
                self._json({"ok": True, "webhook": bool(SLACK_WEBHOOK)})
                return

            if not GROQ_API_KEY:
                self._json({"error": "GROQ_API_KEY env var not set — get a free key at console.groq.com"}, 400)
                return

            # --- Generate daily digest email ---
            if body.get("digestOnly"):
                to_email = body.get("toEmail", "")
                raw_text = body.get("rawText", "")
                prompt = (
                    'Write a professional competitive intelligence digest email. '
                    'First line must be "Subject: <subject here>". '
                    'Then write 3-5 key takeaways as bullet points, and end with one strategic recommendation. '
                    f'Findings:\n{raw_text}'
                )
                answer = call_groq(prompt)
                if to_email and answer:
                    lines = answer.strip().split('\n')
                    subject = 'Intel Dash — Daily Competitive Digest'
                    body_text = answer
                    for i, line in enumerate(lines):
                        if line.lower().startswith('subject:'):
                            subject = line.split(':', 1)[1].strip()
                            body_text = '\n'.join(lines[i + 1:]).strip()
                            break
                    try:
                        send_email(to_email, subject, body_text)
                        self._json({"answer": answer, "sent": True})
                        return
                    except Exception as e:
                        self._json({"answer": answer, "sent": False, "emailError": str(e)})
                        return
                self._json({"answer": answer, "sent": False})
                return

            # --- Generate battle card ---
            if body.get("battleCard"):
                company = body.get("company", "")
                my_company = body.get("myCompany", "")
                context = f" (We are: {my_company}.)" if my_company else ""
                prompt = (
                    f'Create a competitive battle card for {company}.{context} '
                    f'Format as plain text with sections:\n'
                    f'OVERVIEW: 2 sentences on market position.\n'
                    f'STRENGTHS: 3 bullet points.\n'
                    f'WEAKNESSES: 3 bullet points.\n'
                    f'HOW TO BEAT THEM: 3 tactical recommendations.\n'
                    f'KEY WATCH: One thing to monitor in the next 90 days.\n'
                    f'Be specific and actionable.'
                )
                answer = call_groq(prompt)
                self._json({"answer": answer})
                return

            # --- Research a single competitor ---
            company = body.get("company", "").strip()
            my_company = body.get("myCompany", "")
            if not company:
                self._json({"error": "Missing company"}, 400)
                return

            context = f"We are: {my_company}." if my_company else ""
            print(f"  Researching: {company}")

            web_results = search_web(company, my_company)
            if not web_results:
                print(f"  No web results found for {company}")
                self._json({"answer": f'No recent web results found for "{company}". This company may not have significant public news coverage.'})
                return

            print(f"  Got {len(web_results)} chars of web results for {company}")
            prompt = (
                f'You are a competitive intelligence analyst. {context}\n'
                f'Extract 4-6 findings about "{company}" AS A BUSINESS from the sources below.\n\n'
                f'Rules:\n'
                f'- "{company}" must be a BUSINESS/COMPANY — REJECT anything where "{company}" refers to a street name, location, church, event venue, nonprofit, or any non-business entity\n'
                f'- ONLY include findings where "{company}" the COMPANY is the primary subject — not a partner, client, or passing mention\n'
                f'- REJECT any result where "{company}" refers to a street, road, location, church, nonprofit, or physical place — only treat it as a business\n'
                f'- REJECT results that are about road construction, local events, churches, or anything unrelated to business competition\n'
                f'- {"Prioritize findings relevant to competing with this company given our context: " + my_company if my_company else ""}\n'
                f'- Include: hiring, partnerships, service offerings, LinkedIn updates, client wins, pricing, product launches, news\n'
                f'- Only use facts explicitly stated in sources — no invention\n'
                f'- If no valid business findings exist, return: []\n\n'
                f'Return ONLY a valid JSON array, no markdown, no explanation:\n'
                f'[{{"company":"{company}","change_type":"pricing|product|hiring|news","impact_level":"high|medium|low","summary":"3-5 sentences with specific numbers, product names, executive names, or deal sizes.","date":"Mon YYYY (month and year only, e.g. Jan 2025)","url":"exact source url"}}]\n\n'
                f'Sources:\n{web_results}'
            )

            answer = call_groq(prompt)
            print(f"  Got {len(answer)} chars for {company}")

            # Strip out any "no information" findings the LLM generated
            try:
                clean = answer.replace('```json','').replace('```','').strip()
                a, b = clean.find('['), clean.rfind(']') + 1
                if a >= 0:
                    findings = json.loads(clean[a:b])
                    no_info_phrases = ['no specific information', 'no relevant information', 'no information', 'not found in', 'cannot find', 'no details']
                    findings = [f for f in findings if not any(p in f.get('summary','').lower() for p in no_info_phrases)]
                    answer = json.dumps(findings)
            except Exception:
                pass

            self._json({"answer": answer})

        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")

    def _json(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} - {fmt % args}")


if not GROQ_API_KEY:
    print("  ⚠  GROQ_API_KEY not set. Run: export GROQ_API_KEY=your_key")
if not TAVILY_API_KEY:
    print("  ⚠  TAVILY_API_KEY not set — web search disabled. Get a free key at app.tavily.com")
if SLACK_WEBHOOK:
    print("  ✓  Slack notifications enabled")
print(f"\n  Intel Dash v2 → http://localhost:{PORT}")
print("  Powered by Groq + Tavily\n")
server = http.server.HTTPServer(("", PORT), Handler)
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\n  Server stopped.")
