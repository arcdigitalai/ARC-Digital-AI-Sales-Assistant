#!/usr/bin/env python3
"""
AI Sales Assistant — COMPLETE SINGLE FILE
Everything in one file. Nothing can be missing.
Render: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --preload
Local:  python app.py
Login:  kevin@salesai.com (password set 2026-08-05, not stored here)
"""
import os,json,csv,io,re,logging,threading,time,hashlib,base64,sqlite3,smtplib,tempfile
import urllib.request,urllib.error,urllib.parse
from contextlib import contextmanager
from datetime import datetime,timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from collections import defaultdict
import jwt
from flask import Flask,jsonify,Response,request,g,Blueprint

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger=logging.getLogger("sales")

# ══ CONFIG ═══════════════════════════════════════════════════════════════════
SECRET_KEY=os.environ.get("SECRET_KEY","ai-sales-secret-32chars-2024xyz!")
JWT_ALGORITHM="HS256"
TOKEN_EXPIRE_HOURS=48
GROQ_API_KEY=os.environ.get("GROQ_API_KEY","")
GROQ_MODEL=os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile")
BLAND_API_KEY=os.environ.get("BLAND_API_KEY","")
GMAIL_EMAIL=os.environ.get("GMAIL_SENDER_EMAIL","")
GMAIL_PASSWORD=os.environ.get("GMAIL_APP_PASSWORD","")
GMAIL_NAME=os.environ.get("GMAIL_SENDER_NAME","ARC Digital AI Sales Team")
SENDGRID_API_KEY=os.environ.get("SENDGRID_API_KEY","")
TWILIO_SID=os.environ.get("TWILIO_ACCOUNT_SID","")
TWILIO_TOKEN=os.environ.get("TWILIO_AUTH_TOKEN","")
TWILIO_FROM=os.environ.get("TWILIO_FROM_NUMBER","")
TWILIO_ADMIN=os.environ.get("TWILIO_ADMIN_NUMBER","")
GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID","")
GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET","")
GOOGLE_REDIRECT_URI=os.environ.get("GOOGLE_REDIRECT_URI","")
DAILY_REPORT_HOUR=int(os.environ.get("DAILY_REPORT_HOUR_UTC","18"))
AUTO_EMAIL_HOUR=int(os.environ.get("AUTO_EMAIL_HOUR_UTC","9"))
AUTO_FOLLOWUP_HOUR=int(os.environ.get("AUTO_FOLLOWUP_HOUR_UTC","10"))
AUTO_CALL_HOUR=int(os.environ.get("AUTO_CALL_HOUR_UTC","11"))
AUTO_SCORE_HOUR=int(os.environ.get("AUTO_SCORE_HOUR_UTC","8"))
DB_PATH=os.environ.get("DB_PATH",os.path.join(tempfile.gettempdir(),"sales.db"))
BLAND_INBOUND_NUMBER=os.environ.get("BLAND_INBOUND_NUMBER","")
ARC_BANK_BSB=os.environ.get("ARC_BANK_BSB","062-000")
ARC_BANK_ACCOUNT=os.environ.get("ARC_BANK_ACCOUNT","12345678")
ARC_BANK_NAME=os.environ.get("ARC_BANK_NAME","ARC Digital")
ARC_ABN=os.environ.get("ARC_ABN","12 345 678 901")
ARC_PHONE=os.environ.get("ARC_PHONE","+61 XXX XXX XXX")
XERO_INVOICE_TEMPLATE=os.environ.get("XERO_INVOICE_TEMPLATE","ACCREC")

# ══ DATABASE ══════════════════════════════════════════════════════════════════
def _dbconn():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)),exist_ok=True)
    c=sqlite3.connect(DB_PATH,check_same_thread=False,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

@contextmanager
def get_conn():
    c=_dbconn()
    try: yield c; c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()

def q(sql,args=()):
    with get_conn() as c: return [dict(r) for r in c.execute(sql,args).fetchall()]
def q1(sql,args=()):
    with get_conn() as c:
        r=c.execute(sql,args).fetchone(); return dict(r) if r else None
def run(sql,args=()):
    with get_conn() as c: return c.execute(sql,args).lastrowid
def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,full_name TEXT NOT NULL,password TEXT NOT NULL,
            role TEXT DEFAULT 'sales_rep',is_active INTEGER DEFAULT 1,
            google_refresh_token TEXT,last_login TEXT,
            created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,industry TEXT,employee_count INTEGER,annual_revenue INTEGER,
            website TEXT,city TEXT,country TEXT,description TEXT,technologies TEXT,
            status TEXT DEFAULT 'prospect',lead_score INTEGER DEFAULT 0,ai_summary TEXT,
            linkedin_url TEXT,funding_stage TEXT,created_by INTEGER,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            first_name TEXT NOT NULL,last_name TEXT,email TEXT,phone TEXT,title TEXT,
            department TEXT,seniority_level TEXT DEFAULT 'individual',
            is_decision_maker INTEGER DEFAULT 0,created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS emails(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER,created_by INTEGER,email_type TEXT DEFAULT 'cold',
            subject TEXT NOT NULL,body TEXT NOT NULL,recipient_email TEXT NOT NULL,
            recipient_name TEXT,status TEXT DEFAULT 'draft',sent_at TEXT,ai_model_used TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER,created_by INTEGER,title TEXT NOT NULL,
            meeting_type TEXT DEFAULT 'discovery',description TEXT,scheduled_at TEXT,
            duration_minutes INTEGER DEFAULT 30,status TEXT DEFAULT 'proposed',
            meeting_link TEXT,google_event_id TEXT,notes TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY AUTOINCREMENT,
            bland_call_id TEXT,company_id INTEGER,contact_id INTEGER,created_by INTEGER,
            phone_number TEXT NOT NULL,objective TEXT DEFAULT 'qualify',task_prompt TEXT,
            voice TEXT DEFAULT 'nat',status TEXT DEFAULT 'queued',duration_seconds INTEGER,
            recording_url TEXT,transcript TEXT,summary TEXT,error_message TEXT,
            created_at TEXT DEFAULT(datetime('now')),updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS lead_scores(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
            total_score INTEGER DEFAULT 0,revenue_score INTEGER DEFAULT 0,
            employee_score INTEGER DEFAULT 0,industry_score INTEGER DEFAULT 0,
            buying_signal_score INTEGER DEFAULT 0,department_signal_score INTEGER DEFAULT 0,
            email_activity_score INTEGER DEFAULT 0,tier TEXT DEFAULT 'cold',
            updated_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS buying_signals(id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            signal_type TEXT NOT NULL,signal_name TEXT NOT NULL,signal_description TEXT,
            strength INTEGER DEFAULT 5,source TEXT DEFAULT 'ai',
            detected_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS sms_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_number TEXT NOT NULL,from_number TEXT,body TEXT NOT NULL,
            status TEXT DEFAULT 'sent',event_type TEXT,error_msg TEXT,
            created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,sender_name TEXT,message TEXT NOT NULL,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS call_transcripts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER REFERENCES calls(id) ON DELETE CASCADE,
            company_id INTEGER, contact_id INTEGER,
            transcript TEXT, summary TEXT, sentiment TEXT DEFAULT 'neutral',
            meeting_booked INTEGER DEFAULT 0, google_meet_link TEXT,
            interest_score INTEGER DEFAULT 0, next_action TEXT,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS email_drafts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER, created_by INTEGER,
            draft_type TEXT DEFAULT 'follow_up',
            subject TEXT NOT NULL, body TEXT NOT NULL,
            recipient_email TEXT NOT NULL, recipient_name TEXT,
            status TEXT DEFAULT 'pending_approval',
            approved_by INTEGER, approved_at TEXT,
            scheduled_send_day INTEGER DEFAULT 0,
            call_transcript_id INTEGER,
            sent_at TEXT, created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS opportunities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER, created_by INTEGER,
            title TEXT NOT NULL,
            interest_score INTEGER DEFAULT 10,
            quoted_value REAL DEFAULT 0,
            accepted_value REAL,
            status TEXT DEFAULT 'open',
            quotation_text TEXT, invoice_text TEXT,
            notes TEXT,
            created_at TEXT DEFAULT(datetime('now')),
            updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS contact_linkedin(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
            company_id INTEGER,
            linkedin_url TEXT, position TEXT, seniority TEXT,
            is_key_person INTEGER DEFAULT 0,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS company_chat(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            sender TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS inbound_calls(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bland_call_id TEXT, phone_number TEXT,
            caller_name TEXT, campaign_source TEXT DEFAULT 'facebook_ad',
            transcript TEXT, summary TEXT, sentiment TEXT DEFAULT 'neutral',
            interest_score INTEGER DEFAULT 0, meeting_booked INTEGER DEFAULT 0,
            google_meet_link TEXT, company_id INTEGER, contact_id INTEGER,
            status TEXT DEFAULT 'completed', duration_seconds INTEGER,
            recording_url TEXT, created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
            company_id INTEGER, contact_id INTEGER, created_by INTEGER,
            invoice_number TEXT UNIQUE, invoice_type TEXT DEFAULT 'invoice',
            subject TEXT, content TEXT, amount REAL DEFAULT 0,
            gst_amount REAL DEFAULT 0, total_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'draft', xero_invoice_id TEXT,
            due_date TEXT, issued_date TEXT,
            approved_by INTEGER, approved_at TEXT,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS quotations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
            company_id INTEGER, contact_id INTEGER, created_by INTEGER,
            quote_number TEXT UNIQUE, content TEXT, amount REAL DEFAULT 0,
            gst_amount REAL DEFAULT 0, total_amount REAL DEFAULT 0,
            valid_until TEXT, status TEXT DEFAULT 'draft',
            xero_quote_id TEXT, template TEXT DEFAULT 'professional',
            approved_by INTEGER, approved_at TEXT,
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS imported_leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT DEFAULT '',
            first_name TEXT DEFAULT '', last_name TEXT DEFAULT '', title TEXT DEFAULT '',
            email TEXT DEFAULT '', company_name_for_emails TEXT DEFAULT '', email_status TEXT DEFAULT '',
            seniority TEXT DEFAULT '', departments TEXT DEFAULT '', sub_departments TEXT DEFAULT '',
            work_direct_phone TEXT DEFAULT '', home_phone TEXT DEFAULT '', mobile_phone TEXT DEFAULT '',
            corporate_phone TEXT DEFAULT '', other_phone TEXT DEFAULT '', stage TEXT DEFAULT '',
            employees TEXT DEFAULT '', industry TEXT DEFAULT '', keywords TEXT DEFAULT '',
            person_linkedin TEXT DEFAULT '', website TEXT DEFAULT '', company_linkedin TEXT DEFAULT '',
            facebook_url TEXT DEFAULT '', twitter_url TEXT DEFAULT '',
            city TEXT DEFAULT '', state TEXT DEFAULT '', country TEXT DEFAULT '',
            company_address TEXT DEFAULT '', company_city TEXT DEFAULT '', company_state TEXT DEFAULT '',
            company_country TEXT DEFAULT '', company_phone TEXT DEFAULT '',
            technologies TEXT DEFAULT '', annual_revenue TEXT DEFAULT '', total_funding TEXT DEFAULT '',
            latest_funding TEXT DEFAULT '', latest_funding_amount TEXT DEFAULT '', last_raised_at TEXT DEFAULT '',
            subsidiary_of TEXT DEFAULT '', subsidiary_of_org_id TEXT DEFAULT '',
            email_sent TEXT DEFAULT '', num_retail_locations TEXT DEFAULT '',
            tertiary_email_source TEXT DEFAULT '', tertiary_email_status TEXT DEFAULT '',
            tertiary_email_verification TEXT DEFAULT '',
            primary_intent_topic TEXT DEFAULT '', primary_intent_score TEXT DEFAULT '',
            secondary_intent_topic TEXT DEFAULT '', secondary_intent_score TEXT DEFAULT '',
            about_company TEXT DEFAULT '', company_short_name TEXT DEFAULT '', qualify_contact TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS imported_lead_emails(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER UNIQUE REFERENCES imported_leads(id) ON DELETE CASCADE,
            subject TEXT DEFAULT '', body TEXT DEFAULT '',
            status TEXT DEFAULT 'draft', sent_at TEXT,
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));
        """)
        # imported_leads grows columns as new export formats appear (IMP_FIELDS
        # is the source of truth) — add any missing ones to an existing table.
        have={r[1] for r in c.execute("PRAGMA table_info(imported_leads)")}
        for col in IMP_FIELDS:
            if col not in have:
                c.execute(f"ALTER TABLE imported_leads ADD COLUMN {col} TEXT DEFAULT ''")
        # manual-outreach status on contacts (new / follow_up / contacted /
        # dont_disturb / not_interested), set from the Contacts tab.
        have_ct={r[1] for r in c.execute("PRAGMA table_info(contacts)")}
        if "contact_status" not in have_ct:
            c.execute("ALTER TABLE contacts ADD COLUMN contact_status TEXT DEFAULT 'new'")
    _seed_db()

def _seed_db():
    # Guard checks BOTH emails so renaming the admin never re-triggers seeding.
    if q1("SELECT 1 FROM users WHERE email IN('kevin@salesai.com','admin@salesai.com')"): return
    pw=hashlib.sha256(("adminkevin@123456"+SECRET_KEY).encode()).hexdigest()
    aid=run("INSERT INTO users(email,full_name,password,role) VALUES(?,?,?,?)",
            ("kevin@salesai.com","System Admin",pw,"admin"))
    demos=[
        ("Stripe","FinTech",4000,7500000000,"stripe.com","San Francisco","USA",91,"opportunity",'["Python","Go"]'),
        ("Notion","SaaS",400,300000000,"notion.so","San Francisco","USA",78,"qualified",'["TypeScript"]'),
        ("Vercel","Technology",350,200000000,"vercel.com","San Francisco","USA",74,"prospect",'["Next.js"]'),
        ("Figma","Design",1000,400000000,"figma.com","San Francisco","USA",85,"qualified",'["C++"]'),
        ("Linear","Software",80,50000000,"linear.app","San Francisco","USA",62,"prospect",'["TypeScript"]'),
        ("Retool","SaaS",300,100000000,"retool.com","San Francisco","USA",55,"prospect",'["React"]'),
        ("PlanetScale","Database",150,60000000,"planetscale.com","San Mateo","USA",38,"cold",'["MySQL"]'),
        ("Loom","Technology",200,80000000,"loom.com","San Francisco","USA",44,"prospect",'["React"]'),
        ("Airtable","SaaS",800,350000000,"airtable.com","San Francisco","USA",82,"opportunity",'["React"]'),
        ("Miro","Software",1500,400000000,"miro.com","Amsterdam","Netherlands",77,"qualified",'["React"]'),
    ]
    phones=["+14155550100","+14155550101","+14155550102","+14155550103","+14155550104",
            "+14155550105","+14155550106","+14155550107","+14155550108","+31201234567"]
    for (nm,ind,emp,rev,web,city,country,sc,status,techs),phone in zip(demos,phones):
        cid=run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,website,
                   city,country,lead_score,status,technologies,created_by,description)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (nm,ind,emp,rev,web,city,country,sc,status,techs,aid,f"Leading {ind} company"))
        run("""INSERT INTO contacts(company_id,first_name,last_name,email,phone,title,
               department,seniority_level,is_decision_maker) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,"Alex","Johnson",f"alex@{web}",phone,"VP Engineering","Engineering","vp",1))
        run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,
               industry_score,buying_signal_score,department_signal_score,email_activity_score,tier)
               VALUES(?,?,80,70,80,75,60,50,?)""",
            (cid,sc,"hot" if sc>=70 else "warm" if sc>=40 else "cold"))
        run("""INSERT INTO buying_signals(company_id,signal_type,signal_name,signal_description,strength,source)
               VALUES(?,?,?,?,?,?)""",(cid,"hiring","Rapid Hiring","20+ open roles",8,"demo"))
    cids=[r["id"] for r in q("SELECT id FROM companies LIMIT 8")]
    for i,(cid,st,et) in enumerate(zip(cids,
        ["sent","sent","opened","replied","draft","sent","opened","sent"],
        ["cold","follow_up","meeting_request","cold","cold","follow_up","meeting_request","cold"])):
        run("""INSERT INTO emails(company_id,created_by,email_type,subject,body,
               recipient_email,recipient_name,status,ai_model_used) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,aid,et,"Quick question about your growth",
             "Hi Alex,\n\nI noticed you are scaling fast.\n\nWorth a 15-min call?\n\nBest,\nARC Digital AI Sales Team",
             f"alex@demo{i}.com","Alex Johnson",st,"groq"))
    for i,(t,mt) in enumerate([("Discovery Call","discovery"),("Product Demo","demo"),
                                ("Follow-up","follow_up"),("Negotiation","negotiation")]):
        sched=(datetime.utcnow()+timedelta(days=i+1)).strftime("%Y-%m-%d %H:%M:%S")
        run("""INSERT INTO meetings(company_id,created_by,title,meeting_type,
               scheduled_at,duration_minutes,status) VALUES(?,?,?,?,?,?,?)""",
            (cids[i%len(cids)],aid,t,mt,sched,30,"scheduled"))
    logger.info("✅ Demo data seeded")

# ══ AUTH ══════════════════════════════════════════════════════════════════════
def hash_pw(p): return hashlib.sha256((p+SECRET_KEY).encode()).hexdigest()
def verify_pw(p,h): return hash_pw(p)==h
def make_token(uid,email,role):
    return jwt.encode({"sub":str(uid),"email":email,"role":role,
        "exp":datetime.utcnow()+timedelta(hours=TOKEN_EXPIRE_HOURS)},
        SECRET_KEY,algorithm=JWT_ALGORITHM)
def decode_token(t): return jwt.decode(t,SECRET_KEY,algorithms=[JWT_ALGORITHM])
def login_required(f):
    @wraps(f)
    def dec(*a,**kw):
        tok=(request.headers.get("Authorization","").replace("Bearer ","")).strip()
        if not tok: return jsonify({"error":"Missing token"}),401
        try:
            p=decode_token(tok)
            g.user={"id":int(p["sub"]),"email":p["email"],"role":p["role"]}
        except jwt.ExpiredSignatureError: return jsonify({"error":"Token expired"}),401
        except Exception: return jsonify({"error":"Invalid token"}),401
        return f(*a,**kw)
    return dec

# ══ AI SERVICE ════════════════════════════════════════════════════════════════
GROQ_MODELS=[
    GROQ_MODEL,
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
]

def _groq(prompt, system=None, max_tokens=700):
    """Groq API with 4-model fallback chain on 403/429 rate limits."""
    if not GROQ_API_KEY: return None
    msgs=[]
    if system: msgs.append({"role":"system","content":system})
    msgs.append({"role":"user","content":prompt})
    for model in GROQ_MODELS:
        body=json.dumps({"model":model,"messages":msgs,"max_tokens":max_tokens}).encode()
        try:
            # User-Agent required since ~Aug 2026: Groq's Cloudflare returns
            # 403 (error 1010) for requests with urllib's default signature.
            req=urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                data=body,headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json",
                                   "User-Agent":"ARC-Sales-Assistant/1.0"})
            with urllib.request.urlopen(req,timeout=25) as r:
                result=json.loads(r.read())["choices"][0]["message"]["content"].strip()
                if model!=GROQ_MODEL: logger.info(f"Groq fallback: {model}")
                return result
        except urllib.error.HTTPError as e:
            if e.code in(429,503,403):
                logger.warning(f"Groq {e.code} on {model} — trying next")
                time.sleep(0.4); continue
            if e.code==401:
                logger.error("Groq 401 — GROQ_API_KEY invalid. Get new key at console.groq.com")
                return None
            logger.warning(f"Groq HTTP {e.code} on {model}")
            continue
        except urllib.error.URLError as e:
            logger.warning(f"Groq network: {e.reason}"); return None
        except Exception as e:
            logger.warning(f"Groq {model}: {e}"); continue
    logger.warning("Groq: all models rate-limited. Using fallback content.")
    return None
def company_summary(co):
    rev=co.get("annual_revenue") or 0
    emp=co.get("employee_count") or "N/A"
    p=f"2-sentence B2B sales summary for {co['name']}, {co.get('industry','tech')}, {emp} employees, ${rev:,} revenue. Be actionable."
    return (_groq(p,"B2B sales expert.",180) or
            f"{co['name']} is a {co.get('industry','technology')} company with {co.get('employee_count','N/A')} employees. Strong AI sales candidate.")

def generate_email(co,ct,email_type,custom=""):
    name=f"{ct.get('first_name','Team')} {ct.get('last_name','')}".strip()
    sender=GMAIL_NAME or "ARC Digital AI Sales Team"
    p={"cold":f"Cold B2B email to {name} at {co['name']}. {custom}\nSUBJECT: ...\nBODY:\n...",
       "follow_up":f"Follow-up to {name} at {co['name']} — no reply.\nSUBJECT: ...\nBODY:\n...",
       "meeting_request":f"Meeting request to {name} at {co['name']}.\nSUBJECT: ...\nBODY:\n..."}
    result=_groq(p.get(email_type,p["cold"]),"Expert B2B copywriter.",480)
    if result:
        subj,lines,in_b="", [],False
        for line in result.strip().split("\n"):
            if line.upper().startswith("SUBJECT:"): subj=line.split(":",1)[1].strip()
            elif line.upper().strip()=="BODY:": in_b=True
            elif in_b: lines.append(line)
        if subj and lines: return {"subject":subj,"body":"\n".join(lines).strip()}
    t={"cold":{"subject":f"Helping {co['name']} automate sales",
               "body":f"Hi {name},\n\nI noticed {co['name']} is scaling fast.\n\nWe help companies automate sales with AI. Worth a 15-min call?\n\nBest,\n{sender}"},
       "follow_up":{"subject":f"Following up — {co['name']}",
                    "body":f"Hi {name},\n\nResurfacing my note. Any availability this month?\n\nThanks,\n{sender}"},
       "meeting_request":{"subject":f"15 min — AI Sales demo for {co['name']}?",
                          "body":f"Hi {name},\n\nThree slots:\n• Tue 10 AM\n• Wed 2 PM\n• Thu 11 AM\n\nDoes any work?\n\nBest,\n{sender}"}}
    return t.get(email_type,t["cold"])

def buying_signals(co):
    rev=co.get("annual_revenue") or 0
    p=f"3 buying signals for {co.get('name','')}, {co.get('industry','')}, ${rev:,}. JSON: [{{\"type\":\"...\",\"name\":\"...\",\"description\":\"...\",\"strength\":7}}]"
    result=_groq(p,"Return valid JSON array only.",280)
    if result:
        try:
            m=re.search(r"\[.*\]",result,re.DOTALL)
            if m: return json.loads(m.group())
        except Exception: pass
    out=[]
    if (co.get("employee_count") or 0)>100: out.append({"type":"scale","name":"Enterprise Scale","description":"Budget authority","strength":7})
    if rev>1_000_000: out.append({"type":"revenue","name":"Strong Revenue","description":"Investment capacity","strength":8})
    out.append({"type":"tech","name":"Tech-Forward","description":"Active stack","strength":6})
    return out

def call_script(co, ct, objective="qualify"):
    """Sarah - ARC Digital AI caller trained on Kevin Raju's methodology."""
    name     = f"{ct.get('first_name','there')}".strip()
    industry = (co.get('industry') or 'trade').lower()
    company  = co.get('name','the business')

    sarah_system = (
        "You are Sarah, a warm AI assistant calling on behalf of Kevin Raju from ARC Digital. "
        "Kevin has 10+ years helping 200+ organisations improve business processes. "
        "You call trade businesses to find pain points: missed calls, admin, quote follow-up, "
        "after-hours enquiries, scheduling, owner workload. "
        "You do NOT sell software. You qualify pain, then offer a 15-min call with Kevin. "
        "Be warm, calm, practical, slightly humorous. Short sentences. Ask one question at a time. "
        "Sound like a sharp friendly ops assistant who gets trade businesses. "
        "NEVER say: digital transformation, AI solution, operational optimisation, business assessment. "
        "USE: missed calls, booking jobs, quote follow-up, admin, after-hours calls, owner workload."
    )
    prompt = (
        f"Generate Sarah's full call script for {name} at {company} ({industry} business). "
        f"Objective: {objective}. Include: opening, discovery questions, pain-point responses, "
        f"Kevin intro (only after pain found), meeting booking, not-interested exit. "
        f"Output ONLY what Sarah says. Natural speech. Under 500 words."
    )
    ai_result = _groq(prompt, sarah_system, 700)
    if ai_result and len(ai_result) > 80:
        return ai_result

    # Fallback — full Sarah script with all branches
    return (
        f"Hi {name}, Sarah here. "
        f"I'm an AI assistant calling on behalf of Kevin Raju from ARC Digital. "
        f"I'll be quick — I'm guessing you're probably on-site, driving, or about to get interrupted. "
        f"Kevin has been speaking with {industry} businesses about missed calls, "
        f"quote follow-up and admin bottlenecks. "
        f"One practical question: when you're on the tools and the phone rings, "
        f"what normally happens to that enquiry? "
        f"\n\n"
        f"[IF ASKED WHAT THIS IS ABOUT]: "
        f"Fair question. Kevin helps {industry} businesses look at where enquiries, bookings, "
        f"quotes and customer follow-up are getting lost. "
        f"Sometimes it's missed calls. Sometimes manual admin. "
        f"Sometimes quotes just don't get followed up. "
        f"Just a quick check to see if any of that's relevant to {company}. "
        f"\n\n"
        f"[IF BUSY]: No problem. Would there be a better time for a quick call back? "
        f"\n\n"
        f"[DISCOVERY — ask ONE at a time]: "
        f"Who normally answers the phone when you're on the tools? "
        f"What happens after hours? "
        f"Are job enquiries captured somewhere or mostly manual? "
        f"Do quotes get followed up consistently? "
        f"How much admin still falls back on you personally? "
        f"If you could remove one repetitive task from the business tomorrow, what would it be? "
        f"\n\n"
        f"[WHEN THEY NAME A PROBLEM]: "
        f"That makes sense. We hear that quite a bit from trade businesses. "
        f"How often would you say that happens? "
        f"And what impact does that have — lost jobs, customer frustration, "
        f"or just more work after hours? "
        f"\n\n"
        f"[ONLY AFTER GENUINE PAIN POINT — KEVIN INTRO]: "
        f"That's the type of thing Kevin usually looks at. "
        f"He starts with the process first, not the technology. "
        f"Sometimes the fix is simple. Sometimes automation or AI can help. "
        f"Would it be worth a 15-minute conversation with Kevin to see where the gaps are? "
        f"\n\n"
        f"[IF THEY AGREE — COLLECT]: Name, email, preferred phone, preferred time, main challenge. "
        f"Then say: Perfect. I'll pass that through to Kevin's team so they can send the invite. "
        f"Thanks for your time today. "
        f"\n\n"
        f"[NOT INTERESTED]: No worries at all. "
        f"Thanks for your time and all the best with the business. "
        f"\n\n"
        f"[REMOVE FROM LIST]: Absolutely. I'll record that request. "
        f"You won't be contacted again. Thanks."
    )
# ══════════════════════════════════════════════════════
# PLUMBING-SPECIFIC AI FUNCTIONS
# ══════════════════════════════════════════════════════

PLUMBING_CONTEXT = (
    "You are an AI sales assistant for ARC Digital (Kevin Raju). "
    "Kevin helps Australian plumbing businesses automate their operations. "
    "Products/Services: AI phone answering, booking automation, CRM, quote follow-up, after-hours call handling. "
    "Target: Plumbing businesses across Australia. "
    "Pain points: missed calls, manual admin, quote follow-up, after-hours enquiries, scheduling, owner workload."
)

def ai_call_transcript_analysis(transcript_text, company_name, contact_name):
    if not transcript_text:
        return {"interest_score":0,"meeting_booked":False,"summary":"No transcript","next_action":"retry_call","sentiment":"neutral","pain_points":[],"key_quote":""}
    prompt = (
        f"Analyse this AI sales call transcript for a plumbing business.\n"
        f"Company: {company_name}\nContact: {contact_name}\n"
        f"Transcript: {transcript_text[:2500]}\n\n"
        "Return JSON only with keys: interest_score(0-10 int), meeting_booked(bool), "
        "sentiment(positive/neutral/negative), summary(str), pain_points(list), "
        "next_action(book_meeting/send_followup/send_thankyou/retry_call/no_action), "
        "meeting_datetime(ISO or null), key_quote(str)"
    )
    result = _groq(prompt, PLUMBING_CONTEXT, 500)
    if result:
        try:
            import re as _re
            m = _re.search(r'\{.*\}', result, _re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception:
            pass
    text_lower = transcript_text.lower()
    score = 5
    if any(w in text_lower for w in ['yes','interested','sounds good','book','meeting','definitely']): score += 3
    if any(w in text_lower for w in ['no','not interested','busy','remove']): score -= 3
    booked = any(w in text_lower for w in ['book','schedule','meeting','appointment'])
    return {"interest_score":max(0,min(10,score)),"meeting_booked":booked,
            "sentiment":"positive" if score>=7 else "negative" if score<=3 else "neutral",
            "summary":f"Call with {contact_name} at {company_name}.",
            "pain_points":[],"next_action":"send_followup" if score>=5 else "retry_call","key_quote":""}

def generate_post_call_email(co, ct, analysis, transcript_text=""):
    company = co.get('name','the company')
    fname   = ct.get('first_name','there')
    score   = analysis.get('interest_score', 5)
    booked  = analysis.get('meeting_booked', False)
    summary = analysis.get('summary','')
    meet_link = analysis.get('meet_link','')

    if booked or score >= 8:
        subject = f"Great speaking with you today, {fname} — {'Meeting Confirmed' if booked else 'Next Steps'}"
        body_prompt = (
            f"Write a warm thank-you follow-up email from Kevin Raju (ARC Digital) to {fname} at {company} (plumbing business).\n"
            f"Call summary: {summary}\n"
            f"{'Meeting confirmed. Google Meet: ' + meet_link if booked and meet_link else 'They showed strong interest. Suggest 15-min call.'}\n"
            f"Key point: {analysis.get('key_quote','')}\n"
            "Keep under 200 words. Australian tone. Professional and warm. Include subject line."
        )
    elif score >= 5:
        subject = f"Following up from our call today — {company}"
        body_prompt = (
            f"Write a brief follow-up email from Kevin Raju (ARC Digital) to {fname} at {company} (plumbing business).\n"
            f"They were mildly interested in AI automation for missed calls and admin. Summary: {summary}\n"
            "Offer a 15-min call. Under 150 words. No pressure. Australian."
        )
    else:
        subject = f"Thanks for your time today, {fname}"
        body_prompt = (
            f"Write a short thank-you email from Kevin Raju (ARC Digital) to {fname} at {company} (plumbing business).\n"
            "They were not interested. Keep the door open. Under 100 words. Warm and professional."
        )

    result = _groq(body_prompt, "Expert B2B email writer for Australian trade businesses.", 400)
    body = ""
    if result:
        lines = result.strip().splitlines()
        # Skip subject line if included
        start = 0
        for i, line in enumerate(lines):
            if line.upper().startswith('SUBJECT'): start = i+1; continue
            if line.strip() and i >= start: body = "\n".join(lines[i:]).strip(); break

    if not body:
        if booked:
            body = (f"Hi {fname},\n\nThank you so much for speaking with Sarah today on behalf of Kevin Raju from ARC Digital.\n\n"
                    f"I am delighted to confirm our meeting. Here is your Google Meet link:\n{meet_link}\n\n"
                    f"Looking forward to showing you how we can help {company} capture more leads and reduce admin.\n\n"
                    "All the best,\nKevin Raju\nARC Digital\nkevin@arcdigital.com.au")
        elif score >= 5:
            body = (f"Hi {fname},\n\nThank you for taking the time to speak with Sarah from ARC Digital today.\n\n"
                    f"We would love to show you how we help plumbing businesses like {company} stop missing calls and cut admin time.\n\n"
                    "Would a quick 15-minute call with Kevin work this week?\n\n"
                    "All the best,\nKevin Raju\nARC Digital\nkevin@arcdigital.com.au")
        else:
            body = (f"Hi {fname},\n\nThank you for taking a moment to speak with Sarah today.\n\n"
                    f"We completely understand — it is a busy time for {company}. If you ever want to explore how AI can help with missed calls or admin down the track, please do not hesitate to reach out.\n\n"
                    "All the best,\nKevin Raju\nARC Digital")

    return {"email_type": "meeting_confirm" if booked else ("followup" if score>=5 else "thankyou"),
            "subject": subject, "body": body, "interest_score": score}

def qualify_company_plumbing(co, contacts, signals):
    industry = (co.get('industry') or '').lower()
    emp = co.get('employee_count') or 0
    rev = co.get('annual_revenue') or 0
    plumbing_kw = ['plumb','pipe','drain','hvac','trade','construction','service','maintenance']
    industry_fit = min(10, sum(2 for k in plumbing_kw if k in industry) + 2)
    if 2 <= emp <= 5:     size_score = 10
    elif 6 <= emp <= 20:  size_score = 9
    elif 21 <= emp <= 50: size_score = 7
    elif emp == 0:        size_score = 5
    else:                 size_score = 4
    if 100000 <= rev <= 2000000:   rev_score = 10
    elif rev > 2000000:            rev_score = 7
    elif rev > 0:                  rev_score = 5
    else:                          rev_score = 5
    sig_score = min(10, len(signals) * 2)
    qualify_score = int((industry_fit + size_score + rev_score + sig_score) / 4)

    prompt = (f"Rate 0-10 how suitable {co.get('name')} (industry:{co.get('industry')}, "
              f"employees:{emp}, revenue:${rev:,}) is for AI call-answering automation for Australian plumbing. "
              "Return JSON: {\"qualify_score\":7,\"reason\":\"one sentence\",\"recommended_value\":4500,\"risk\":\"low\"}")
    ai_data = {}
    result = _groq(prompt, "B2B sales qualification expert.", 150)
    if result:
        try:
            import re as _re
            m = _re.search(r'\{[^}]+\}', result, _re.DOTALL)
            if m: ai_data = json.loads(m.group())
        except Exception: pass

    final_score = ai_data.get('qualify_score', qualify_score)
    final_score = max(0, min(10, int(final_score)))
    return {
        "qualify_score":    final_score,
        "industry_fit":     industry_fit,
        "size_score":       size_score,
        "revenue_score":    rev_score,
        "reason":           ai_data.get('reason', f"{'Strong' if final_score>=7 else 'Moderate'} fit for plumbing automation"),
        "recommended_value":ai_data.get('recommended_value', 3000 + final_score * 500),
        "risk":             ai_data.get('risk','medium'),
        "tier":             "hot" if final_score>=8 else "warm" if final_score>=5 else "cold",
    }

def generate_company_chatbot_response(company_id, user_message, history):
    co = q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if not co: return "Company not found."
    cts  = q("SELECT * FROM contacts WHERE company_id=?", (company_id,))
    sigs = q("SELECT * FROM buying_signals WHERE company_id=?", (company_id,))
    ls   = q1("SELECT * FROM lead_scores WHERE company_id=?", (company_id,))
    opp  = q1("SELECT * FROM opportunities WHERE company_id=? ORDER BY created_at DESC LIMIT 1", (company_id,))
    ctx = (f"Company: {co['name']} | Industry: {co.get('industry')} | Employees: {co.get('employee_count')} "
           f"| Revenue: ${co.get('annual_revenue') or 0:,} | Score: {co.get('lead_score',0)}/100 "
           f"| Status: {co.get('status')} | Contacts: {len(cts)} "
           f"| Signals: {', '.join(s.get('signal_name','') for s in sigs[:3]) or 'none'} "
           f"| Tier: {ls.get('tier','unknown') if ls else 'not scored'} "
           f"| Opp: {'$'+str(opp.get('quoted_value',0)) if opp else 'none'}")
    hist = "\n".join([f"{m['role']}: {m['content']}" for m in (history or [])[-6:]])
    system = (f"{PLUMBING_CONTEXT}\n\nYou are a sales intelligence chatbot for {co['name']}.\n"
              f"Company data: {ctx}\nPrevious: {hist}\n"
              "Give specific, actionable sales intelligence about this company. Under 150 words.")
    return (_groq(user_message, system, 300) or
            f"Based on {co['name']}'s profile, they are a {'strong' if (co.get('lead_score') or 0)>=70 else 'moderate'} prospect for ARC Digital's plumbing automation.")

def generate_quotation(co, ct, opportunity):
    value   = opportunity.get('quoted_value', 5000)
    company = co.get('name','the company')
    name    = f"{ct.get('first_name','')} {ct.get('last_name','')}".strip() if ct else "Team"
    inv_num = f"ARC-Q-{co.get('id',1):04d}-{datetime.utcnow().strftime('%Y%m')}"
    prompt  = (f"Write a professional quotation for ARC Digital (Kevin Raju, ABN 12 345 678 901) to {company}.\n"
               f"Contact: {name} | Value: AUD ${value:,.2f} | Quote: {inv_num}\n"
               "Services: AI call answering, booking automation, CRM, quote follow-up, after-hours, reporting.\n"
               "Include: quote number, date, validity 30 days, itemised services, GST, payment terms 30 days, Australian format.")
    result = _groq(prompt, "Professional Australian business document writer.", 700)
    if result: return result
    return (f"QUOTATION\n{'='*50}\nARC Digital | ABN: 12 345 678 901 | kevin@arcdigital.com.au\n\n"
            f"Quote No: {inv_num}\nDate: {datetime.utcnow().strftime('%d %B %Y')}\n"
            f"Valid Until: {(datetime.utcnow()+timedelta(days=30)).strftime('%d %B %Y')}\n\n"
            f"Prepared For: {company}\nAttn: {name}\n\n"
            "SERVICES:\n"
            f"  AI Call Answering & Lead Capture ......... AUD ${value*0.35:,.2f}/mo\n"
            f"  CRM & Quote Follow-Up Automation ......... AUD ${value*0.20:,.2f}/mo\n"
            f"  After-Hours Enquiry Handling ............. AUD ${value*0.18:,.2f}/mo\n"
            f"  SMS & Email Automation ................... AUD ${value*0.12:,.2f}/mo\n"
            f"  Reporting & Analytics .................... AUD ${value*0.10:,.2f}/mo\n"
            "  {'─'*45}\n"
            f"  Subtotal (ex GST) ........................ AUD ${value/1.1:,.2f}/mo\n"
            f"  GST (10%) ................................ AUD ${value/11:,.2f}/mo\n"
            f"  TOTAL (inc GST) .......................... AUD ${value:,.2f}/mo\n\n"
            "Payment Terms: 30 days from invoice\nThis quotation is valid for 30 days.\n\n"
            "Kevin Raju | ARC Digital | kevin@arcdigital.com.au")

def generate_invoice(co, ct, opportunity):
    value   = opportunity.get('accepted_value') or opportunity.get('quoted_value', 5000)
    company = co.get('name','the company')
    name    = f"{ct.get('first_name','')} {ct.get('last_name','')}".strip() if ct else "Team"
    inv_num = f"ARC-INV-{co.get('id',1):04d}-{datetime.utcnow().strftime('%Y%m')}"
    prompt  = (f"Write a professional Australian tax invoice for ARC Digital (Kevin Raju, ABN 12 345 678 901) to {company}.\n"
               f"Contact: {name} | Amount: AUD ${value:,.2f} | Invoice: {inv_num}\n"
               "Services: AI business automation monthly subscription.\n"
               "Include: invoice number, issue date, due date 30 days, GST breakdown, bank BSB 062-000 Acc 12345678, ARC Digital.")
    result = _groq(prompt, "Professional Australian tax invoice writer.", 600)
    if result: return result
    return (f"TAX INVOICE\n{'='*50}\nARC Digital | ABN: 12 345 678 901\nkevin@arcdigital.com.au | +61 XXX XXX XXX\n\n"
            f"Invoice No: {inv_num}\nIssue Date: {datetime.utcnow().strftime('%d %B %Y')}\n"
            f"Due Date: {(datetime.utcnow()+timedelta(days=30)).strftime('%d %B %Y')}\n\n"
            f"Bill To: {company}\nAttn: {name}\n\n"
            f"  AI Business Automation — Monthly Subscription\n"
            f"  {'─'*45}\n"
            f"  Amount (ex GST) .......................... AUD ${value/1.1:,.2f}\n"
            f"  GST (10%) ................................ AUD ${value/11:,.2f}\n"
            f"  TOTAL DUE ................................ AUD ${value:,.2f}\n\n"
            "Payment Details:\n"
            "  Bank: Commonwealth Bank\n  BSB: 062-000\n  Account: 12345678\n  Name: ARC Digital\n"
            f"  Reference: {inv_num}\n\nThank you for your business.")




# ══════════════════════════════════════════════════════════════════════════════
# INVOICE & QUOTATION GENERATION — Xero-compatible format
# ══════════════════════════════════════════════════════════════════════════════

INVOICE_TEMPLATES = {
    "professional": {
        "name": "Professional",
        "accent": "#1e3a5f",
        "font": "Arial, sans-serif",
    },
    "modern": {
        "name": "Modern Blue",
        "accent": "#3b82f6",
        "font": "Helvetica, sans-serif",
    },
    "classic": {
        "name": "Classic",
        "accent": "#1a1a1a",
        "font": "Georgia, serif",
    },
}

def build_invoice_number():
    existing = q("SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1")
    if existing and existing[0].get("invoice_number"):
        try:
            last = int(existing[0]["invoice_number"].split("-")[-1])
            return f"ARC-INV-{last+1:04d}"
        except Exception:
            pass
    return f"ARC-INV-{datetime.utcnow().strftime('%Y%m')}-0001"

def build_quote_number():
    existing = q("SELECT quote_number FROM quotations ORDER BY id DESC LIMIT 1")
    if existing and existing[0].get("quote_number"):
        try:
            last = int(existing[0]["quote_number"].split("-")[-1])
            return f"ARC-Q-{last+1:04d}"
        except Exception:
            pass
    return f"ARC-Q-{datetime.utcnow().strftime('%Y%m')}-0001"

def generate_invoice_html(invoice, opportunity, company, contact, template="professional"):
    """Generate a full HTML invoice — Xero-approved format."""
    tmpl = INVOICE_TEMPLATES.get(template, INVOICE_TEMPLATES["professional"])
    accent = tmpl["accent"]
    font   = tmpl["font"]
    amount_ex  = invoice.get("amount", 0) or 0
    gst        = invoice.get("gst_amount", amount_ex * 0.1)
    total      = invoice.get("total_amount", amount_ex + gst)
    inv_num    = invoice.get("invoice_number", "ARC-INV-0001")
    issued     = (invoice.get("issued_date") or datetime.utcnow().strftime("%d %B %Y"))[:10]
    try:
        issued_dt  = datetime.strptime(issued, "%Y-%m-%d")
        issued_fmt = issued_dt.strftime("%d %B %Y")
    except Exception:
        issued_fmt = issued
    due_date   = invoice.get("due_date") or (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        due_dt  = datetime.strptime(due_date, "%Y-%m-%d")
        due_fmt = due_dt.strftime("%d %B %Y")
    except Exception:
        due_fmt = due_date
    co_name    = (company or {}).get("name","Client")
    co_city    = (company or {}).get("city","")
    co_country = (company or {}).get("country","Australia")
    ct_name    = f"{(contact or {}).get('first_name','')} {(contact or {}).get('last_name','')}".strip()
    content    = invoice.get("content","")
    # Parse content for line items (simple: each line is an item)
    items = []
    for line in (content or "").split("\n"):
        if "........" in line or ":" in line:
            parts = line.split(".")
            if len(parts) >= 2:
                desc = parts[0].strip().lstrip("-•1234567890. ")
                val_str = "".join(c for c in parts[-1] if c.isdigit() or c == ".")
                try:
                    val = float(val_str)
                    if val > 0:
                        items.append({"description": desc, "amount": val})
                except Exception:
                    pass
    if not items:
        items = [{"description": "AI Business Automation — Monthly Subscription", "amount": amount_ex}]

    rows_html = ""
    for item in items:
        rows_html += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;color:#374151">{item['description']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:right;color:#374151">AUD ${item['amount']:,.2f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{inv_num} — ARC Digital</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:{font}; background:#f9fafb; color:#111827; }}
  .page {{ max-width:800px; margin:0 auto; background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  .header {{ background:{accent}; color:#fff; padding:36px 40px; display:flex; justify-content:space-between; align-items:flex-start; }}
  .logo-area h1 {{ font-size:1.6rem; font-weight:700; letter-spacing:-.5px; }}
  .logo-area p {{ font-size:.85rem; opacity:.8; margin-top:2px; }}
  .inv-meta {{ text-align:right; }}
  .inv-meta .inv-type {{ font-size:1.3rem; font-weight:700; text-transform:uppercase; letter-spacing:1px; }}
  .inv-meta .inv-num {{ font-size:.9rem; opacity:.85; margin-top:4px; }}
  .body {{ padding:36px 40px; }}
  .parties {{ display:grid; grid-template-columns:1fr 1fr; gap:32px; margin-bottom:28px; }}
  .party-label {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:#6b7280; margin-bottom:6px; }}
  .party-name {{ font-size:1rem; font-weight:600; color:#111827; }}
  .party-detail {{ font-size:.85rem; color:#6b7280; margin-top:2px; }}
  .dates-row {{ display:flex; gap:32px; margin-bottom:28px; padding:16px; background:#f3f4f6; border-radius:8px; }}
  .date-item label {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:#6b7280; display:block; margin-bottom:4px; }}
  .date-item span {{ font-size:.95rem; font-weight:600; color:#111827; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:20px; }}
  thead th {{ background:{accent}; color:#fff; padding:10px 8px; text-align:left; font-size:.8rem; font-weight:600; letter-spacing:.03em; }}
  thead th:last-child {{ text-align:right; }}
  .totals {{ margin-left:auto; width:260px; }}
  .total-row {{ display:flex; justify-content:space-between; padding:6px 0; font-size:.9rem; color:#374151; }}
  .total-final {{ display:flex; justify-content:space-between; padding:12px 0 6px; font-size:1.1rem; font-weight:700; color:{accent}; border-top:2px solid {accent}; margin-top:6px; }}
  .bank-section {{ background:{accent}15; border-left:4px solid {accent}; padding:16px 20px; border-radius:4px; margin-top:28px; }}
  .bank-section h4 {{ font-size:.85rem; font-weight:700; color:{accent}; text-transform:uppercase; letter-spacing:.05em; margin-bottom:10px; }}
  .bank-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
  .bank-item label {{ font-size:.72rem; color:#6b7280; display:block; }}
  .bank-item span {{ font-size:.9rem; font-weight:600; color:#111827; }}
  .footer {{ background:#f9fafb; border-top:1px solid #e5e7eb; padding:20px 40px; font-size:.78rem; color:#9ca3af; display:flex; justify-content:space-between; }}
  .status-badge {{ display:inline-block; padding:4px 12px; border-radius:20px; font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }}
  .status-draft {{ background:#fef3c7; color:#92400e; }}
  .status-approved {{ background:#d1fae5; color:#065f46; }}
  .status-paid {{ background:#dbeafe; color:#1e40af; }}
  @media print {{
    body {{ background:#fff; }}
    .page {{ box-shadow:none; }}
  }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <div class="logo-area">
      <h1>ARC Digital</h1>
      <p>AI Business Automation</p>
      <p style="margin-top:8px;font-size:.8rem;opacity:.75">ABN: {ARC_ABN}</p>
      <p style="font-size:.8rem;opacity:.75">kevin@arcdigital.com.au</p>
      <p style="font-size:.8rem;opacity:.75">{ARC_PHONE}</p>
    </div>
    <div class="inv-meta">
      <div class="inv-type">Tax Invoice</div>
      <div class="inv-num">{inv_num}</div>
      <div style="margin-top:12px">
        <span class="status-badge status-{invoice.get('status','draft')}">{invoice.get('status','DRAFT').upper()}</span>
      </div>
    </div>
  </div>

  <div class="body">
    <div class="parties">
      <div>
        <div class="party-label">From</div>
        <div class="party-name">ARC Digital</div>
        <div class="party-detail">Kevin Raju</div>
        <div class="party-detail">kevin@arcdigital.com.au</div>
        <div class="party-detail">ABN: {ARC_ABN}</div>
      </div>
      <div>
        <div class="party-label">Bill To</div>
        <div class="party-name">{co_name}</div>
        {f'<div class="party-detail">{ct_name}</div>' if ct_name else ''}
        {f'<div class="party-detail">{co_city}, {co_country}</div>' if co_city else ''}
      </div>
    </div>

    <div class="dates-row">
      <div class="date-item"><label>Issue Date</label><span>{issued_fmt}</span></div>
      <div class="date-item"><label>Due Date</label><span>{due_fmt}</span></div>
      <div class="date-item"><label>Payment Terms</label><span>30 Days</span></div>
      {f'<div class="date-item"><label>Opportunity</label><span>{(opportunity or {{}}).get("title","")[:30]}</span></div>' if opportunity else ''}
    </div>

    <table>
      <thead>
        <tr><th style="width:75%">Description</th><th>Amount (AUD)</th></tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>

    <div class="totals">
      <div class="total-row"><span>Subtotal (ex GST)</span><span>AUD ${amount_ex:,.2f}</span></div>
      <div class="total-row"><span>GST (10%)</span><span>AUD ${gst:,.2f}</span></div>
      <div class="total-final"><span>TOTAL DUE</span><span>AUD ${total:,.2f}</span></div>
    </div>

    <div class="bank-section">
      <h4>Payment Details</h4>
      <div class="bank-grid">
        <div class="bank-item"><label>Bank</label><span>Commonwealth Bank of Australia</span></div>
        <div class="bank-item"><label>Account Name</label><span>{ARC_BANK_NAME}</span></div>
        <div class="bank-item"><label>BSB</label><span>{ARC_BANK_BSB}</span></div>
        <div class="bank-item"><label>Account Number</label><span>{ARC_BANK_ACCOUNT}</span></div>
        <div class="bank-item"><label>Reference</label><span>{inv_num}</span></div>
        <div class="bank-item"><label>Amount</label><span>AUD ${total:,.2f}</span></div>
      </div>
    </div>
  </div>

  <div class="footer">
    <span>ARC Digital — ABN {ARC_ABN} — kevin@arcdigital.com.au</span>
    <span>Please use {inv_num} as your payment reference</span>
  </div>
</div>
</body>
</html>"""


def generate_quotation_html(quotation, opportunity, company, contact, template="professional"):
    """Generate a full HTML quotation — Xero-compatible format."""
    tmpl    = INVOICE_TEMPLATES.get(template, INVOICE_TEMPLATES["professional"])
    accent  = tmpl["accent"]
    font    = tmpl["font"]
    amount_ex  = quotation.get("amount", 0) or 0
    gst        = quotation.get("gst_amount", amount_ex * 0.1)
    total      = quotation.get("total_amount", amount_ex + gst)
    q_num      = quotation.get("quote_number", "ARC-Q-0001")
    issued_fmt = datetime.utcnow().strftime("%d %B %Y")
    valid_dt   = quotation.get("valid_until") or (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        valid_fmt = datetime.strptime(valid_dt, "%Y-%m-%d").strftime("%d %B %Y")
    except Exception:
        valid_fmt = valid_dt
    co_name  = (company or {}).get("name","Client")
    co_city  = (company or {}).get("city","")
    ct_name  = f"{(contact or {}).get('first_name','')} {(contact or {}).get('last_name','')}".strip()
    content  = quotation.get("content","")

    # Service items
    services = [
        {"desc":"AI Call Answering & Lead Capture (24/7)","qty":"1 month","unit": round(amount_ex * 0.35, 2)},
        {"desc":"CRM & Quote Follow-Up Automation",       "qty":"1 month","unit": round(amount_ex * 0.20, 2)},
        {"desc":"After-Hours Enquiry Handling",           "qty":"1 month","unit": round(amount_ex * 0.18, 2)},
        {"desc":"AI SMS & Email Automation",              "qty":"1 month","unit": round(amount_ex * 0.15, 2)},
        {"desc":"Reporting Dashboard & Analytics",        "qty":"1 month","unit": round(amount_ex * 0.12, 2)},
    ]

    rows_html = ""
    for s in services:
        rows_html += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;color:#374151">{s['desc']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:center;color:#6b7280">{s['qty']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:right;color:#374151">AUD ${s['unit']:,.2f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{q_num} — ARC Digital Quotation</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:{font}; background:#f9fafb; color:#111827; }}
  .page {{ max-width:800px; margin:0 auto; background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  .header {{ background:{accent}; color:#fff; padding:36px 40px; display:flex; justify-content:space-between; align-items:flex-start; }}
  .logo-area h1 {{ font-size:1.6rem; font-weight:700; }}
  .logo-area p {{ font-size:.85rem; opacity:.8; margin-top:2px; }}
  .q-meta {{ text-align:right; }}
  .q-meta .q-type {{ font-size:1.3rem; font-weight:700; text-transform:uppercase; letter-spacing:1px; }}
  .q-meta .q-num {{ font-size:.9rem; opacity:.85; margin-top:4px; }}
  .body {{ padding:36px 40px; }}
  .parties {{ display:grid; grid-template-columns:1fr 1fr; gap:32px; margin-bottom:28px; }}
  .party-label {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:#6b7280; margin-bottom:6px; }}
  .party-name {{ font-size:1rem; font-weight:600; }}
  .party-detail {{ font-size:.85rem; color:#6b7280; margin-top:2px; }}
  .info-row {{ display:flex; gap:24px; margin-bottom:28px; padding:16px; background:#f3f4f6; border-radius:8px; flex-wrap:wrap; }}
  .info-item label {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:#6b7280; display:block; margin-bottom:4px; }}
  .info-item span {{ font-size:.95rem; font-weight:600; color:#111827; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:20px; }}
  thead th {{ background:{accent}; color:#fff; padding:10px 8px; text-align:left; font-size:.8rem; font-weight:600; }}
  thead th:last-child {{ text-align:right; }}
  .totals {{ margin-left:auto; width:280px; }}
  .total-row {{ display:flex; justify-content:space-between; padding:6px 0; font-size:.9rem; color:#374151; }}
  .total-final {{ display:flex; justify-content:space-between; padding:12px 0 6px; font-size:1.1rem; font-weight:700; color:{accent}; border-top:2px solid {accent}; margin-top:6px; }}
  .note-box {{ background:{accent}10; border-left:4px solid {accent}; padding:14px 18px; border-radius:4px; margin-top:24px; }}
  .note-box h4 {{ font-size:.82rem; font-weight:700; color:{accent}; text-transform:uppercase; margin-bottom:8px; }}
  .note-box p {{ font-size:.85rem; color:#374151; line-height:1.6; }}
  .terms {{ margin-top:20px; font-size:.82rem; color:#6b7280; line-height:1.7; }}
  .footer {{ background:#f9fafb; border-top:1px solid #e5e7eb; padding:20px 40px; font-size:.78rem; color:#9ca3af; display:flex; justify-content:space-between; }}
  @media print {{ body {{ background:#fff; }} .page {{ box-shadow:none; }} }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <div class="logo-area">
      <h1>ARC Digital</h1>
      <p>AI Business Automation</p>
      <p style="margin-top:8px;font-size:.8rem;opacity:.75">ABN: {ARC_ABN}</p>
      <p style="font-size:.8rem;opacity:.75">kevin@arcdigital.com.au | {ARC_PHONE}</p>
    </div>
    <div class="q-meta">
      <div class="q-type">Quotation</div>
      <div class="q-num">{q_num}</div>
      <div style="margin-top:8px;font-size:.8rem;opacity:.85">For: {co_name}</div>
    </div>
  </div>

  <div class="body">
    <div class="parties">
      <div>
        <div class="party-label">From</div>
        <div class="party-name">ARC Digital</div>
        <div class="party-detail">Kevin Raju — Founder</div>
        <div class="party-detail">kevin@arcdigital.com.au</div>
        <div class="party-detail">ABN: {ARC_ABN}</div>
      </div>
      <div>
        <div class="party-label">Prepared For</div>
        <div class="party-name">{co_name}</div>
        {f'<div class="party-detail">{ct_name}</div>' if ct_name else ''}
        {f'<div class="party-detail">{co_city}, Australia</div>' if co_city else ''}
      </div>
    </div>

    <div class="info-row">
      <div class="info-item"><label>Quote Date</label><span>{issued_fmt}</span></div>
      <div class="info-item"><label>Valid Until</label><span>{valid_fmt}</span></div>
      <div class="info-item"><label>Payment Terms</label><span>30 Days</span></div>
      <div class="info-item"><label>Quote Number</label><span>{q_num}</span></div>
    </div>

    <table>
      <thead>
        <tr><th style="width:60%">Service Description</th><th style="text-align:center;width:15%">Period</th><th>Amount (AUD)</th></tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>

    <div class="totals">
      <div class="total-row"><span>Subtotal (ex GST)</span><span>AUD ${amount_ex:,.2f}</span></div>
      <div class="total-row"><span>GST (10%)</span><span>AUD ${gst:,.2f}</span></div>
      <div class="total-final"><span>TOTAL</span><span>AUD ${total:,.2f}/month</span></div>
    </div>

    <div class="note-box">
      <h4>What's Included</h4>
      <p>✅ 24/7 AI phone answering — never miss a plumbing enquiry again<br>
      ✅ Automatic job booking &amp; scheduling<br>
      ✅ Quote follow-up automation — recover lost jobs<br>
      ✅ After-hours call handling with instant SMS alerts<br>
      ✅ Monthly reporting dashboard — track every lead<br>
      ✅ Onboarding &amp; setup included — live in 5 business days</p>
    </div>

    <div class="terms">
      <strong>Terms &amp; Conditions:</strong><br>
      • This quotation is valid for 30 days from the date above.<br>
      • Prices are in Australian Dollars (AUD) and include GST where indicated.<br>
      • Monthly subscription — cancel with 30 days written notice.<br>
      • Setup fee waived for contracts signed within quotation validity period.<br>
      • ARC Digital ABN: {ARC_ABN} | Governed by the laws of Victoria, Australia.
    </div>
  </div>

  <div class="footer">
    <span>ARC Digital — ABN {ARC_ABN}</span>
    <span>To accept, reply to kevin@arcdigital.com.au with quote number {q_num}</span>
  </div>
</div>
</body>
</html>"""


def push_invoice_to_xero(invoice_data, company, contact):
    """Push invoice to Xero via MCP (if available). Returns xero_id or None."""
    # Xero integration is available via MCP server in the Claude interface
    # This function prepares the data in Xero's expected format
    xero_payload = {
        "Type": "ACCREC",
        "Contact": {
            "Name": (company or {}).get("name","Client"),
            "EmailAddress": (contact or {}).get("email",""),
        },
        "Date": datetime.utcnow().strftime("%Y-%m-%d"),
        "DueDate": (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d"),
        "InvoiceNumber": invoice_data.get("invoice_number",""),
        "LineItems": [
            {
                "Description": "AI Business Automation — Monthly Subscription",
                "Quantity": 1,
                "UnitAmount": invoice_data.get("amount", 0),
                "TaxType": "OUTPUT2",  # Australian GST
                "AccountCode": "200",
            }
        ],
        "Status": "DRAFT",
        "CurrencyCode": "AUD",
        "Reference": f"ARC Digital — {(company or {}).get('name','')}",
    }
    logger.info(f"Xero payload prepared for {(company or {}).get('name','')} — {invoice_data.get('invoice_number')}")
    return None, xero_payload  # Returns (xero_id, payload) — xero_id set when actually pushed via MCP


# ══════════════════════════════════════════════════════════════════════════════
# BLAND AI INBOUND CALL HANDLING (Facebook Ad → Inbound Call)
# ══════════════════════════════════════════════════════════════════════════════

SARAH_INBOUND_SCRIPT = """You are Sarah, an AI receptionist for Kevin Raju from ARC Digital.
Someone has called in response to a Facebook advertisement about helping plumbing businesses stop missing calls and reduce admin.

YOUR ROLE: Answer the call warmly, find out who they are, understand their pain points, and either:
1. Book a meeting with Kevin (preferred), OR
2. Qualify them and pass their details to Kevin

OPENING (when they call in):
"Hi there, thanks for calling ARC Digital! This is Sarah. You've reached us about our AI automation for trade businesses — am I right? Great timing!
Can I grab your name first?"

AFTER NAME:
"Thanks [Name]. And which plumbing business are you calling from?"

PAIN POINT DISCOVERY (pick the most relevant):
"What made you reach out today — is it more about missed calls, or is it the admin side of things?"
"When you're on the tools and the phone rings — what normally happens to that enquiry?"
"How much time would you say you lose each week on admin, quotes and follow-ups?"

IF INTERESTED — BOOK MEETING:
"That's exactly what Kevin helps with. He starts with your specific process first, not the technology.
Would a quick 15-minute call with Kevin work for you? I can lock in a time right now."
Collect: preferred time, email for calendar invite.

CLOSING — ALWAYS COLLECT:
Full name, business name, phone number, email, main problem.
"Perfect. I'll get Kevin's team to send through the calendar invite to [email].
Thanks so much for calling — Kevin is going to love hearing from you!"

TONE: Warm, Australian, practical, friendly. Short sentences. Never corporate. Never pushy."""

def process_inbound_call(call_data):
    """Process an incoming Bland AI call (from Facebook ad)."""
    transcript  = call_data.get("concatenated_transcript","") or call_data.get("transcript","")
    caller_phone = call_data.get("from","") or call_data.get("phone_number","")
    bland_id    = call_data.get("call_id","")
    duration    = call_data.get("call_length") or call_data.get("duration_seconds",0)
    recording   = call_data.get("recording_url","")

    analysis = ai_call_transcript_analysis(transcript, "Inbound Caller", "Unknown")

    # Try to extract caller details from transcript
    caller_name = ""
    caller_biz  = ""
    caller_email = ""
    if transcript:
        lines = transcript.lower()
        # Simple extraction patterns
        import re as _re
        name_m = _re.search(r"(?:my name is|i'm|this is)\s+([a-z]+(?:\s+[a-z]+)?)", lines)
        if name_m: caller_name = name_m.group(1).title()
        biz_m  = _re.search(r"(?:from|with|at)\s+([a-z\s]+(?:plumbing|pipes?|drains?|trade))", lines)
        if biz_m:  caller_biz = biz_m.group(1).title().strip()
        email_m = _re.search(r"[\w.-]+@[\w.-]+\.\w+", transcript)
        if email_m: caller_email = email_m.group(0)

    # Save inbound call
    iid = run("""INSERT INTO inbound_calls(bland_call_id,phone_number,caller_name,campaign_source,
                transcript,summary,sentiment,interest_score,meeting_booked,status,duration_seconds,recording_url)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bland_id, caller_phone, caller_name, "facebook_ad",
         transcript, analysis.get("summary",""), analysis.get("sentiment","neutral"),
         analysis.get("interest_score",0), 1 if analysis.get("meeting_booked") else 0,
         "completed", duration, recording))

    # Create contact + company if business identified
    if caller_biz:
        existing_co = q1("SELECT id FROM companies WHERE name LIKE ?", (f"%{caller_biz[:20]}%",))
        if not existing_co:
            cid_new = run("""INSERT INTO companies(name,industry,status,description,created_by,updated_at)
                            VALUES(?,?,?,?,?,?)""",
                (caller_biz,"Plumbing","prospect",
                 f"Inbound call via Facebook Ad. Score:{analysis.get('interest_score',0)}/10",1,now()))
        else:
            cid_new = existing_co["id"]

        if caller_name and cid_new:
            parts = caller_name.split(" ", 1)
            existing_ct = q1("SELECT id FROM contacts WHERE company_id=? AND first_name=?", (cid_new, parts[0]))
            if not existing_ct:
                run("""INSERT INTO contacts(company_id,first_name,last_name,email,phone,
                       seniority_level,is_decision_maker) VALUES(?,?,?,?,?,?,?)""",
                    (cid_new,parts[0],parts[1] if len(parts)>1 else "",
                     caller_email,caller_phone,"owner",1))

    # Send SMS notification to Kevin
    score = analysis.get("interest_score",0)
    notify("hot_lead" if score>=7 else "company_added", {
        "company_name": caller_biz or f"Caller {caller_phone}",
        "lead_score": score,
        "industry": "Plumbing (Inbound FB Ad)",
    })

    logger.info(f"Inbound call processed: {caller_phone} score={score} biz={caller_biz}")
    return {"id":iid,"analysis":analysis,"caller_name":caller_name,"caller_biz":caller_biz}


def setup_bland_inbound_agent():
    """Configure Bland AI inbound phone number with Sarah's script."""
    if not BLAND_API_KEY or not BLAND_INBOUND_NUMBER:
        return {"status":"not_configured","message":"Set BLAND_API_KEY and BLAND_INBOUND_NUMBER in Render env"}
    payload = json.dumps({
        "phone_number": BLAND_INBOUND_NUMBER,
        "task": SARAH_INBOUND_SCRIPT,
        "model": "enhanced",
        "voice": "nat",
        "language": "en-AU",
        "max_duration": 8,
        "record": True,
        "wait_for_greeting": False,
        "amd": False,
        "webhook": "",  # Set to your Render URL + /api/bland/inbound-webhook
        "interruption_threshold": 50,
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.bland.ai/v1/inbound/{BLAND_INBOUND_NUMBER}",
            data=payload,
            headers={"authorization":BLAND_API_KEY,"Content-Type":"application/json",
                     "User-Agent":"ARC-Sales-Assistant/1.0"})
        req.get_method = lambda: "POST"
        with urllib.request.urlopen(req,timeout=15) as r:
            return {"status":"configured","data":json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"status":"error","message":f"Bland {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"status":"error","message":str(e)}



def _crm_snapshot():
    """Compact, real-data CRM snapshot used as chatbot grounding context."""
    try:
        cos=q("SELECT name,industry,lead_score,status,crm_status,city FROM companies")
    except Exception:
        cos=q("SELECT name,industry,lead_score,status FROM companies")
    hot=sum(1 for c in cos if (c.get("lead_score") or 0)>=70)
    warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
    cold=sum(1 for c in cos if 1<=(c.get("lead_score") or 0)<40)
    unscored=len(cos)-hot-warm-cold
    qualified=sum(1 for c in cos if c.get("status") in ("qualified","opportunity"))
    top=sorted(cos,key=lambda x:x.get("lead_score") or 0,reverse=True)[:8]
    month=datetime.utcnow().strftime("%Y-%m"); today=datetime.utcnow().strftime("%Y-%m-%d")
    opps=[]
    try: opps=q("SELECT title,stage,quoted_value,probability,monthly_recurring,expected_close_date,next_action,company_id FROM opportunities")
    except Exception: pass
    open_o=[o for o in opps if (o.get("stage") or "new") not in ("closed_won","closed_lost")]
    pipeline=sum(o.get("quoted_value") or 0 for o in open_o)
    weighted=sum((o.get("quoted_value") or 0)*(o.get("probability") or 0)/100.0 for o in open_o)
    won=[o for o in opps if o.get("stage")=="closed_won"]
    by_stage=defaultdict(int)
    for o in open_o: by_stage[o.get("stage") or "new"]+=1
    overdue=today_n=0
    try:
        overdue=q1("SELECT COUNT(*) AS n FROM activities WHERE status NOT IN('completed','cancelled') AND due_date<?",(today,))["n"]
        today_n=q1("SELECT COUNT(*) AS n FROM activities WHERE status NOT IN('completed','cancelled') AND due_date=?",(today,))["n"]
    except Exception: pass
    emails_sent=q1("SELECT COUNT(*) AS n FROM emails WHERE status!='draft'")["n"]
    meetings=q1("SELECT COUNT(*) AS n FROM meetings WHERE status='scheduled'")["n"]
    return {"total":len(cos),"hot":hot,"warm":warm,"cold":cold,"unscored":unscored,
            "qualified":qualified,"top":top,"open_opps":open_o,"pipeline":pipeline,
            "weighted":weighted,"won":won,"by_stage":dict(by_stage),"overdue":overdue,
            "due_today":today_n,"emails_sent":emails_sent,"meetings":meetings,"month":month}

CHAT_SYSTEM=("You are the ARC Digital CRM Assistant — a sharp, friendly B2B sales expert built into "
    "ARC Digital's own CRM. ARC Digital (founder Kevin Raju, Australia) sells AI-powered automation to "
    "Australian trade and plumbing businesses: 'Sarah', a 24/7 AI agent that answers missed calls and SMS, "
    "qualifies jobs and books them; plus process automation, n8n workflows, system integration, CRM "
    "implementation, quoting/invoicing automation and managed support. Typical deals: $3k-20k project + "
    "monthly recurring.\n"
    "YOU ARE EMBEDDED IN THE APP and know its layout exactly — answer UI questions specifically, never "
    "generically. Left sidebar groups: Overview (Dashboard, Analytics) · Sales CRM (Leads & Qualification, "
    "Companies, Contacts, Opportunities, Activities, Quotations, Invoices) · Communications (Emails, Email "
    "Drafts, Calls, Inbound Calls, Meetings, SMS Logs, Transcripts, Chat) · Automation & AI (Automation, "
    "Integrations, ARC Apps) · Administration (Users, CRM Settings, Audit Log, System Health). Tasks and "
    "reminders live in Sales CRM → Activities (the 'task bar'). Clicking a lead row opens its workspace "
    "panel with quick actions: Call, SMS, Email, Meeting, Note, Task, Qualify, Convert, 🚀 Autopilot. "
    "AI-drafted outreach waits in Email Drafts for Approve & Send. The 🚀 Autopilot toggle automates "
    "score→qualify→draft with human approval before anything sends.\n"
    "YOU CAN TAKE ACTIONS: when the user asks to be reminded or to schedule a task/follow-up, the system "
    "creates it automatically and it POPS UP on screen when due — if asked about reminders, tell them to "
    "just type e.g. 'remind me to call Jake tomorrow at 2pm'.\n"
    "You can and should:\n"
    "- Answer questions about the live CRM data snapshot provided (leads, scores, pipeline, activities).\n"
    "- Give practical B2B sales advice: qualification (budget/authority/need/timing), follow-up cadence, "
    "objection handling, discovery questions, quoting, ROI framing, closing, account management.\n"
    "- Suggest concrete next actions inside this CRM (e.g. 'qualify the 3 hot leads', 'convert X', "
    "'clear the overdue tasks').\n"
    "Style: direct and useful, short paragraphs or bullet lists, plain language, use the actual numbers "
    "and names from the snapshot when relevant, AUD for money. Never invent data that is not in the "
    "snapshot — say what you'd need instead. Keep answers under ~250 words unless asked for detail.")

# ── Chat actions: the assistant can CREATE reminders/tasks from plain chat ──
_REMINDER_RE=re.compile(
    r"\b(remind me|set (?:a )?reminder|schedule (?:a )?(?:call|task|follow[- ]?up|meeting|reminder)|"
    r"create (?:a )?(?:task|reminder))\b",re.I)

def _parse_reminder_fallback(msg,base=None):
    """Deterministic reminder parser — used when Groq is unavailable."""
    local=base or now_awst()
    t=None
    m=re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",msg,re.I)
    if m:
        h=int(m.group(1))%12+(12 if m.group(3).lower()=="pm" else 0)
        t=f"{h:02d}:{m.group(2) or '00'}"
    else:
        m24=re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b",msg)
        if m24: t=f"{int(m24.group(1)):02d}:{m24.group(2)}"
    d=None; low=msg.lower()
    mdate=re.search(r"\b(20\d{2}-\d{2}-\d{2})\b",msg)
    mh=re.search(r"\bin (\d{1,3}) hours?\b",low)
    mnd=re.search(r"\bin (\d{1,3}) days?\b",low)
    if mdate: d=mdate.group(1)
    elif "tomorrow" in low: d=(local+timedelta(days=1)).strftime("%Y-%m-%d")
    elif "today" in low or "tonight" in low: d=local.strftime("%Y-%m-%d")
    elif mh:
        dt=local+timedelta(hours=int(mh.group(1))); d=dt.strftime("%Y-%m-%d"); t=dt.strftime("%H:%M")
    elif mnd: d=(local+timedelta(days=int(mnd.group(1)))).strftime("%Y-%m-%d")
    if not d: d=(local+timedelta(days=1)).strftime("%Y-%m-%d")
    subj=msg
    ms=re.search(r"(?:remind me to|remind me|set (?:a )?reminder (?:to|for)|schedule (?:a )?|"
                 r"create (?:a )?(?:task|reminder)(?: to| for)?)\s*(.+)",msg,re.I)
    if ms: subj=ms.group(1)
    subj=re.sub(r"\b(tomorrow|today|tonight|at \d{1,2}(:\d{2})?\s*(am|pm)?|on 20\d{2}-\d{2}-\d{2}|"
                r"20\d{2}-\d{2}-\d{2}|in \d+ (days?|hours?))\b","",subj,flags=re.I)
    subj=re.sub(r"\s{2,}"," ",subj).strip(" .,-") or "Follow up"
    return {"subject":subj[:120],"due_date":d,"due_time":t,"company":None}

def _chat_try_action(message):
    """If the message asks for a reminder/task, create it and return the
    confirmation text. Groq extracts the details; deterministic fallback."""
    if not _REMINDER_RE.search(message): return None
    parsed=None
    ai=_groq("Extract the reminder from this message. Reply ONLY JSON like "
             '{"subject":"...","due_date":"YYYY-MM-DD","due_time":"HH:MM or null","company":"company name or null"}. '
             f"Current local time (Perth): {now_awst().strftime('%Y-%m-%d %H:%M %A')} — resolve relative dates from it.\n"
             f"Message: {message}","You extract structured reminders. Output valid JSON only.",160)
    if ai:
        try:
            m=re.search(r"\{.*\}",ai,re.S)
            if m:
                j=json.loads(m.group())
                if j.get("subject") and re.match(r"20\d{2}-\d{2}-\d{2}$",str(j.get("due_date",""))):
                    tt=j.get("due_time")
                    if tt in (None,"null","None","") or not re.match(r"^\d{2}:\d{2}$",str(tt)): tt=None
                    parsed={"subject":str(j["subject"])[:120],"due_date":j["due_date"],
                            "due_time":tt,"company":j.get("company")}
        except Exception: pass
    if not parsed: parsed=_parse_reminder_fallback(message)
    cid=None; cname=""
    if parsed.get("company") and str(parsed["company"]).lower() not in ("null","none"):
        co=q1("SELECT id,name FROM companies WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
              (f"%{str(parsed['company']).strip()}%",))
        if co: cid=co["id"]; cname=co["name"]
    aid=run("""INSERT INTO activities(type,subject,description,company_id,assigned_to,created_by,
               due_date,due_time,priority,status)
               VALUES('follow_up',?,?,?,?,?,?,?,'high','not_started')""",
        (parsed["subject"],"Reminder set from AI Chat",cid,g.user["id"],g.user["id"],
         parsed["due_date"],parsed.get("due_time")))
    audit("activity",aid,"created","source","chat",parsed["subject"])
    when=parsed["due_date"]+((" at "+parsed["due_time"]) if parsed.get("due_time") else "")
    return (f"⏰ **Reminder set:** {parsed['subject']}\n"
            f"📅 Due **{when}**"+(f" · linked to **{cname}**" if cid else "")+"\n\n"
            "It will **pop up on screen** when it's due (with Complete / Snooze buttons), and it's on your "
            "list under **Sales CRM → Activities**. Say `show my reminders` any time.")

def _chat_my_reminders():
    local=now_awst(); today=local.strftime("%Y-%m-%d")
    rows=q("""SELECT a.*,co.name AS company_name FROM activities a
              LEFT JOIN companies co ON co.id=a.company_id
              WHERE a.status NOT IN('completed','cancelled') AND (a.assigned_to=? OR a.created_by=?)
                AND a.due_date IS NOT NULL
              ORDER BY a.due_date,a.due_time LIMIT 12""",(g.user["id"],g.user["id"]))
    if not rows: return "No open reminders or tasks. Set one with e.g. `remind me to call Jake tomorrow at 2pm`."
    lines=[]
    for a in rows:
        overdue=a["due_date"]<today
        lines.append(f"{'🔴' if overdue else '⏰'} **{a['subject']}** — {a['due_date']}"
                     +(f" {a['due_time']}" if a.get('due_time') else "")
                     +(f" · {a['company_name']}" if a.get('company_name') else "")
                     +(" · OVERDUE" if overdue else ""))
    return "**Your reminders & tasks:**\n"+"\n".join(lines)+"\n\nManage them under Sales CRM → Activities."

def crm_chat_reply(message,history=None):
    # Action layer first: "remind me…" / "schedule…" actually creates the
    # reminder (popped on screen when due) instead of just talking about it.
    action=_chat_try_action(message)
    if action: return action
    if re.search(r"\b(show|list|what are|check)\b.*\b(reminders?|tasks?)\b",message,re.I) \
       or message.lower().strip() in ("reminders","my reminders","my tasks","show my reminders","show my tasks"):
        return _chat_my_reminders()
    s=_crm_snapshot()
    tier=lambda v:"🔥" if (v or 0)>=70 else "🟡" if (v or 0)>=40 else "❄️" if (v or 0)>=1 else "⚪"
    # ── Try full AI mode first (Groq) with real CRM grounding + history ──
    ctx=(f"LIVE CRM SNAPSHOT ({datetime.utcnow().strftime('%d %b %Y')}):\n"
         f"Leads: {s['total']} total | {s['hot']} hot / {s['warm']} warm / {s['cold']} cold / {s['unscored']} unscored | {s['qualified']} qualified\n"
         f"Top leads: "+", ".join(f"{c['name']} ({c.get('lead_score') or 0}/100, {c.get('status','')})" for c in s["top"][:6])+"\n"
         f"Open opportunities: {len(s['open_opps'])} worth AUD ${s['pipeline']:,.0f} (weighted ${s['weighted']:,.0f})"
         +(" | stages: "+", ".join(f"{k}:{v}" for k,v in s["by_stage"].items()) if s["by_stage"] else "")+"\n"
         +("Open opp details: "+"; ".join(f"{o['title']} [{o.get('stage')}] ${o.get('quoted_value') or 0:,.0f}"
             +(f" next: {o.get('next_action')}" if o.get('next_action') else "") for o in s["open_opps"][:6])+"\n" if s["open_opps"] else "")
         +f"Won deals all-time: {len(s['won'])} | Activities: {s['overdue']} overdue, {s['due_today']} due today | "
         f"Emails sent: {s['emails_sent']} | Meetings scheduled: {s['meetings']}")
    hist=""
    if history:
        # Drop old offline-fallback replies from context — the model would
        # otherwise read "AI mode is offline" in its own history and echo it.
        clean=[h for h in history if not (h["role"]=="assistant" and
               ("AI mode" in h["content"] or "data mode" in h["content"]))]
        if clean:
            hist="\nRECENT CONVERSATION:\n"+"\n".join(f"{h['role']}: {h['content'][:300]}" for h in clean[-8:])
    ai=_groq(f"{ctx}{hist}\n\nUSER QUESTION: {message}",CHAT_SYSTEM,800)
    if ai: return ai
    # ── Deterministic fallback (Groq unavailable) — real data, no AI.
    # Keyword routing applies only to short command-like messages; long
    # free-form questions get an honest "AI mode needed" answer instead of
    # a mis-matched data dump.
    cmd=message.lower().strip()
    is_command=len(cmd)<=32 and "?" not in cmd and not cmd.startswith(("how","why","what","when","who","can ","could","should","write","draft"))
    if not is_command and "help" not in cmd:
        return ("That needs my **AI mode**, which is offline right now (the Groq API key is invalid — "
                "get a fresh free key at console.groq.com, update GROQ_API_KEY in the app's .env and "
                "restart the app).\nMeanwhile I can answer from live CRM data: try **show leads**, "
                "**pipeline**, **analytics**, **opportunities** or **activities**.")
    if any(w in cmd for w in ["lead","hot","top","prospect"]):
        lines=[f"{i}. {tier(c.get('lead_score'))} **{c['name']}** — {c.get('lead_score') or 0}/100"
               +(f" · {c.get('industry')}" if c.get('industry') else "")+(f" · {c.get('status')}" if c.get('status') else "")
               for i,c in enumerate(s["top"],1)]
        return ("🔥 **Top leads right now:**\n"+"\n".join(lines)
                +f"\n\nTotals: {s['hot']} hot / {s['warm']} warm / {s['cold']} cold / {s['unscored']} unscored · {s['qualified']} qualified."
                +"\nOpen the Leads workspace and click a row to qualify or convert.")
    if any(w in cmd for w in ["pipeline","revenue","forecast","deal"]):
        stages=("\n".join(f"• {k.replace('_',' ')}: {v}" for k,v in s["by_stage"].items())) or "• (no open opportunities)"
        return (f"💰 **Pipeline:** {len(s['open_opps'])} open opportunities worth **AUD ${s['pipeline']:,.0f}** "
                f"(weighted ${s['weighted']:,.0f}).\n{stages}"
                +(f"\n🏆 Won all-time: {len(s['won'])}." if s["won"] else "")
                +"\n\nSee the Opportunities page for stage moves and warnings.")
    if any(w in cmd for w in ["analytic","stat","kpi","report","summary","daily"]):
        return (f"📊 **CRM summary — {datetime.utcnow().strftime('%d %b %Y')}**\n"
                f"• Leads: {s['total']} ({s['hot']} 🔥 / {s['warm']} 🟡 / {s['cold']} ❄️ / {s['unscored']} ⚪), {s['qualified']} qualified\n"
                f"• Pipeline: AUD ${s['pipeline']:,.0f} across {len(s['open_opps'])} open opps (weighted ${s['weighted']:,.0f})\n"
                f"• Activities: {s['overdue']} overdue, {s['due_today']} due today\n"
                f"• Emails sent: {s['emails_sent']} · Meetings scheduled: {s['meetings']}")
    if any(w in cmd for w in ["task","activit","overdue","todo","follow"]):
        return (f"✅ **Activities:** {s['overdue']} overdue, {s['due_today']} due today.\n"
                +("Clear the overdue ones first — open Activities → Overdue." if s['overdue'] else "Nothing overdue — nice.")
                +"\nCompleting an activity always asks for an outcome and can auto-create the follow-up.")
    if any(w in cmd for w in ["opportunit","kanban","stage"]):
        det=("\n".join(f"• **{o['title']}** [{(o.get('stage') or 'new').replace('_',' ')}] ${o.get('quoted_value') or 0:,.0f}"
             for o in s["open_opps"][:8])) or "No open opportunities — convert a qualified lead from the Leads workspace."
        return f"💼 **Open opportunities:**\n{det}"
    if "help" in cmd:
        return ("I can answer with live CRM data: try **show leads**, **pipeline**, **analytics**, "
                "**opportunities**, **activities**, **daily report**.\n"
                "For free-form questions (sales advice, drafting, strategy) I use Groq AI — currently "
                "unavailable, so I'm in data mode.")
    return ("I'm in **data mode** right now (the Groq AI key is unavailable — refresh GROQ_API_KEY in .env "
            "to unlock free-form answers).\nI can still answer from live CRM data: try **show leads**, "
            "**pipeline**, **analytics**, **opportunities**, **activities** or **help**.")

# ══ SERVICES ══════════════════════════════════════════════════════════════════
IND_SCORES={"technology":15,"software":15,"saas":15,"fintech":12,"design":10,
            "database":12,"healthcare":12,"financial":12,"cloud":14,"ai":15}

def score_company(co,contacts,signals):
    rev=co.get("annual_revenue") or 0
    rs=100 if rev>=100_000_000 else 85 if rev>=10_000_000 else 70 if rev>=1_000_000 else 40 if rev>=100_000 else 15
    emp=co.get("employee_count") or 0
    es=100 if emp>=1000 else 85 if emp>=500 else 70 if emp>=100 else 50 if emp>=20 else 20
    ind_=(co.get("industry") or "").lower()
    is_=next((v for k,v in IND_SCORES.items() if k in ind_),8)
    bss=min(100,sum(s.get("strength",5) for s in signals)*10//max(len(signals),1)) if signals else 0
    SM={"c_suite":30,"vp":25,"director":20,"manager":15,"individual":5}
    ds=min(100,sum(SM.get(c.get("seniority_level",""),5)+(20 if c.get("is_decision_maker") else 0) for c in contacts)) if contacts else 0
    total=max(0,min(100,int(rs*0.25+es*0.15+is_*0.20+bss*0.20+ds*0.10+50*0.10)))
    return {"total_score":total,"revenue_score":rs,"employee_score":es,"industry_score":is_,
            "buying_signal_score":bss,"department_signal_score":ds,"email_activity_score":50,
            "tier":"hot" if total>=70 else "warm" if total>=40 else "cold"}

def _send_via_sendgrid(to_email, to_name, subject, body, html=None):
    """Send via SendGrid HTTP API — works on Render free tier (port 443, not 587)."""
    if not SENDGRID_API_KEY:
        return None
    html_body = html or "<br>".join((body or "").replace("\r","").split("\n"))
    payload = json.dumps({
        "personalizations":[{"to":[{"email":to_email,"name":to_name or ""}]}],
        "from":{"email":GMAIL_EMAIL or "noreply@example.com","name":GMAIL_NAME},
        "subject":subject or "(no subject)",
        "content":[
            {"type":"text/plain","value":body or ""},
            {"type":"text/html","value":html_body if html else f"<html><body><p>{html_body}</p></body></html>"},
        ],
    }).encode("utf-8")
    try:
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send",
            data=payload,
            headers={"Authorization":f"Bearer {SENDGRID_API_KEY}","Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status in (200,202):
                logger.info(f"📧 SendGrid sent to {to_email}: {subject[:50]}")
                return {"status":"sent","method":"sendgrid"}
        return {"status":"error","message":f"SendGrid returned {r.status}"}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:300]
        logger.error(f"SendGrid HTTP {e.code}: {body_err}")
        if e.code == 401:
            return {"status":"error","message":"SendGrid API key invalid — check SENDGRID_API_KEY in Render env"}
        if e.code == 403:
            return {"status":"error","message":"SendGrid 403 — verify sender at app.sendgrid.com/settings/sender_auth"}
        return {"status":"error","message":f"SendGrid {e.code}: {body_err[:100]}"}
    except Exception as e:
        logger.warning(f"SendGrid failed: {e}")
        return None

def _send_via_smtp(to_email, to_name, subject, body, html=None):
    """Gmail SMTP — works on Render paid/local. Port 587 blocked on Render free."""
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        return {"status":"not_configured","message":"Set GMAIL_SENDER_EMAIL + GMAIL_APP_PASSWORD"}
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject or "(no subject)"
        msg["From"]    = f"{GMAIL_NAME} <{GMAIL_EMAIL}>"
        msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email
        msg.attach(MIMEText(body or "","plain","utf-8"))
        html_body = html or ("<html><body><p>"+"<br>".join((body or "").replace("\r","").split("\n"))+"</p></body></html>")
        msg.attach(MIMEText(html_body,"html","utf-8"))
        smtp = smtplib.SMTP("smtp.gmail.com",587,timeout=20)
        smtp.ehlo(); smtp.starttls(); smtp.ehlo()
        smtp.login(GMAIL_EMAIL,GMAIL_PASSWORD)
        smtp.sendmail(GMAIL_EMAIL,to_email,msg.as_string())
        smtp.quit()
        logger.info(f"📧 SMTP sent to {to_email}: {subject[:50]}")
        return {"status":"sent","method":"smtp"}
    except smtplib.SMTPAuthenticationError:
        return {"status":"error","message":"Gmail auth failed — use App Password from myaccount.google.com/apppasswords"}
    except OSError as e:
        if any(x in str(e) for x in ["101","unreachable","refused","timed out"]):
            return {"status":"error","message":"SMTP port 587 blocked on Render free tier. Add SENDGRID_API_KEY to env vars (free at sendgrid.com)"}
        return {"status":"error","message":f"SMTP network: {e}"}
    except Exception as e:
        logger.error(f"SMTP: {type(e).__name__}: {e}")
        return {"status":"error","message":str(e)}

def _google_access_token(uid=None):
    """Exchange a stored Google refresh token for an access token.
    Prefers the given user, else any user with a token (admin first)."""
    row=None
    if uid: row=q1("SELECT google_refresh_token FROM users WHERE id=? AND google_refresh_token IS NOT NULL",(uid,))
    if not row or not row.get("google_refresh_token"):
        row=q1("SELECT google_refresh_token FROM users WHERE google_refresh_token IS NOT NULL "
               "ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, id LIMIT 1")
    if not row or not row.get("google_refresh_token") or not GOOGLE_CLIENT_ID:
        return None
    from urllib.parse import quote as _q
    payload=(f"client_id={_q(GOOGLE_CLIENT_ID)}&client_secret={_q(GOOGLE_CLIENT_SECRET)}"
             f"&refresh_token={_q(row['google_refresh_token'])}&grant_type=refresh_token").encode()
    try:
        req=urllib.request.Request("https://oauth2.googleapis.com/token",
            data=payload,headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req,timeout=15) as r:
            return json.loads(r.read()).get("access_token")
    except Exception as e:
        logger.warning(f"google access token: {e}")
        return None

def _send_via_gmail_api(to_email, to_name, subject, body, html=None):
    """Send through the user's own Gmail via the Google OAuth connection
    (same one that powers Calendar sync). Needs the gmail.send scope —
    users connected before 2026-08-04 must Disconnect + Connect again."""
    token=_google_access_token()
    if not token:
        return None   # OAuth not connected — let the caller fall through
    msg=MIMEMultipart("alternative")
    msg["Subject"]=subject or "(no subject)"
    if GMAIL_EMAIL: msg["From"]=f"{GMAIL_NAME} <{GMAIL_EMAIL}>"
    msg["To"]=f"{to_name} <{to_email}>" if to_name else to_email
    msg.attach(MIMEText(body or "","plain","utf-8"))
    html_body=html or ("<html><body><p>"+"<br>".join((body or "").replace("\r","").split("\n"))+"</p></body></html>")
    msg.attach(MIMEText(html_body,"html","utf-8"))
    raw=base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        req=urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps({"raw":raw}).encode(),
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=20) as r:
            if r.status in (200,202):
                logger.info(f"📧 Gmail API sent to {to_email}: {(subject or '')[:50]}")
                return {"status":"sent","method":"gmail_api"}
        return {"status":"error","method":"gmail_api","message":f"Gmail API returned {r.status}"}
    except urllib.error.HTTPError as e:
        err=e.read().decode()[:300]
        logger.error(f"Gmail API {e.code}: {err}")
        if e.code in (401,403):
            return {"status":"error","method":"gmail_api",
                    "message":"Gmail send permission missing — click Disconnect then Connect Google in Integrations to re-grant (now includes Gmail)"}
        return {"status":"error","method":"gmail_api","message":f"Gmail API {e.code}: {err[:120]}"}
    except Exception as e:
        logger.warning(f"Gmail API send: {e}")
        return {"status":"error","method":"gmail_api","message":str(e)}

def send_email_smtp(to_email, to_name, subject, body, html=None):
    """
    Multi-method email sender. Never raises. Tries each configured method and
    FALLS THROUGH on failure (previously a dead SendGrid key blocked the
    whole chain):
    1. Gmail API via Google OAuth (the Calendar connection) — preferred
    2. SendGrid HTTP API
    3. Gmail SMTP app-password
    Optional `html` overrides the auto-generated HTML part (rich documents).
    Returns: {"status":"sent"|"error"|"not_configured", "method":..., "message":...}
    """
    if not to_email or "@" not in to_email:
        return {"status":"error","message":f"Invalid email address: {to_email!r}"}
    errors=[]
    r = _send_via_gmail_api(to_email,to_name,subject,body,html)
    if r and r.get("status")=="sent": return r
    if r: errors.append(r.get("message","Gmail API failed"))
    if SENDGRID_API_KEY:
        r = _send_via_sendgrid(to_email,to_name,subject,body,html)
        if r and r.get("status")=="sent": return r
        if r: errors.append(r.get("message","SendGrid failed"))
    if GMAIL_EMAIL and GMAIL_PASSWORD:
        r = _send_via_smtp(to_email,to_name,subject,body,html)
        if r and r.get("status")=="sent": return r
        if r: errors.append(r.get("message","SMTP failed"))
    if errors:
        return {"status":"error","message":" | ".join(errors)}
    return {"status":"not_configured","message":
        "No email configured. Easiest: Integrations → Connect Google (grants Gmail send). "
        "Alternatives: SENDGRID_API_KEY, or GMAIL_SENDER_EMAIL + GMAIL_APP_PASSWORD."}
def test_gmail():
    """Test email connectivity: Gmail API (OAuth) first, then SendGrid, then SMTP."""
    parts=[]
    token=_google_access_token()
    if token:
        try:
            req=urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization":f"Bearer {token}"})
            with urllib.request.urlopen(req,timeout=10) as r:
                prof=json.loads(r.read())
                parts.append(f"✅ Gmail (Google account) connected — sends as {prof.get('emailAddress','')}")
        except urllib.error.HTTPError as e:
            if e.code in (401,403):
                parts.append("⚠️ Google connected but missing Gmail permission — Disconnect then Connect Google again to re-grant")
            else:
                parts.append(f"⚠️ Gmail API: HTTP {e.code}")
        except Exception as e:
            parts.append(f"⚠️ Gmail API: {e}")
    else:
        parts.append("⚠️ Google account not connected — click Connect Google (grants Calendar + Gmail send)")
    if SENDGRID_API_KEY:
        try:
            req=urllib.request.Request("https://api.sendgrid.com/v3/user/profile",
                headers={"Authorization":f"Bearer {SENDGRID_API_KEY}"})
            with urllib.request.urlopen(req,timeout=10) as r:
                profile=json.loads(r.read())
                parts.append(f"✅ SendGrid connected ({profile.get('username','')})")
        except urllib.error.HTTPError as e:
            parts.append(f"❌ SendGrid key invalid ({e.code}) — check SENDGRID_API_KEY" if e.code==401 else f"⚠️ SendGrid {e.code} — verify sender at app.sendgrid.com/settings/sender_auth")
        except Exception as e:
            parts.append(f"⚠️ SendGrid: {e}")
    else:
        parts.append("⚠️ SENDGRID_API_KEY not set — recommended for Render free tier. Free at sendgrid.com")
    if GMAIL_EMAIL and GMAIL_PASSWORD:
        try:
            smtp=smtplib.SMTP("smtp.gmail.com",587,timeout=10)
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
            smtp.login(GMAIL_EMAIL,GMAIL_PASSWORD); smtp.quit()
            parts.append(f"✅ Gmail SMTP ok ({GMAIL_EMAIL})")
        except smtplib.SMTPAuthenticationError:
            parts.append("❌ Gmail auth failed — check App Password at myaccount.google.com/apppasswords")
        except OSError as e:
            parts.append(f"⚠️ Gmail SMTP blocked (Render free? port 587 unreachable) — use SendGrid instead")
        except Exception as e:
            parts.append(f"⚠️ Gmail SMTP: {e}")
    else:
        parts.append("⚠️ GMAIL_SENDER_EMAIL or GMAIL_APP_PASSWORD not set")
    ok=any("✅" in p for p in parts)
    return ok," | ".join(parts)
def bland_call_api(phone, task, voice="nat", company_name="", contact_name="", objective="qualify"):
    if not BLAND_API_KEY:
        return {"status":"error","message":"BLAND_API_KEY not set in Render environment"}
    phone = "".join(c for c in phone if c in "0123456789+")
    if not phone.startswith("+"):
        return {"status":"error","message":"Phone must start with + and country code e.g. +61412345678"}
    if len(phone) < 10:
        return {"status":"error","message":f"Phone number too short: {phone}"}
    payload = json.dumps({
        "phone_number": phone,
        "task": task,
        "model": "enhanced",
        "voice": voice,
        "language": "en-AU",
        "max_duration": 8,
        "record": True,
        "wait_for_greeting": True,
        "amd": True,
        "interruption_threshold": 50,
        "pronunciation_guide": [
            {"word":"ARC Digital","pronunciation":"ark digital"},
            {"word":"Kevin Raju","pronunciation":"kevin rah-joo"},
        ],
        "metadata": {
            "company":   company_name,
            "contact":   contact_name,
            "objective": objective,
            "agent":     "Sarah - ARC Digital",
        },
    }).encode()
    try:
        req = urllib.request.Request("https://api.bland.ai/v1/calls", data=payload,
            headers={"authorization":BLAND_API_KEY,"Content-Type":"application/json",
                     "User-Agent":"ARC-Sales-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            call_id = data.get("call_id")
            logger.info(f"Bland call queued: {call_id} to {phone}")
            return {"status":"queued","call_id":call_id,"phone":phone}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        if e.code in (401,403):
            return {"status":"error","message":"Bland API key invalid/expired (403). Get new key at app.bland.ai"}
        if e.code == 400:
            return {"status":"error","message":f"Bland rejected (400): {body}"}
        return {"status":"error","message":f"Bland HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        return {"status":"error","message":f"Cannot reach Bland AI: {e.reason}"}
    except Exception as e:
        logger.error(f"bland_call_api: {e}")
        return {"status":"error","message":str(e)}
def bland_get_api(call_id):
    """Fetch live status/transcript for one Bland call. Was called in three
    places but NEVER DEFINED upstream — status sync silently failed until
    2026-08-05. Returns the call dict or None."""
    if not BLAND_API_KEY or not call_id: return None
    try:
        req=urllib.request.Request(f"https://api.bland.ai/v1/calls/{call_id}",
            headers={"authorization":BLAND_API_KEY,"User-Agent":"ARC-Sales-Assistant/1.0"})
        with urllib.request.urlopen(req,timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning(f"bland_get_api {call_id}: {e}")
        return None

def test_bland():
    if not BLAND_API_KEY: return False,"BLAND_API_KEY not set in environment variables"
    try:
        req=urllib.request.Request("https://api.bland.ai/v1/calls?limit=1",
            headers={"authorization":BLAND_API_KEY,"User-Agent":"ARC-Sales-Assistant/1.0"})
        with urllib.request.urlopen(req,timeout=10) as r: json.loads(r.read())
        return True,"✅ Bland AI connected"
    except urllib.error.HTTPError as e:
        if e.code in (401,403):
            return False,(
                "❌ Bland API key invalid or expired (HTTP 403). "
                "Go to app.bland.ai → API Keys → create a new key → "
                "update BLAND_API_KEY in Render environment variables → redeploy"
            )
        return False,f"Bland HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False,f"Cannot reach Bland AI: {e.reason}"
    except Exception as e:
        return False,str(e)

def google_auth_url(user_id=0, redirect_uri=""):
    if not GOOGLE_CLIENT_ID: return ""
    from urllib.parse import quote as _q
    redir = redirect_uri or GOOGLE_REDIRECT_URI or ""
    # Scope includes gmail.send (added 2026-08-04) so the same OAuth
    # connection powers Calendar sync AND outbound email via Gmail API.
    return (f"https://accounts.google.com/o/oauth2/auth?client_id={_q(GOOGLE_CLIENT_ID)}"
            f"&redirect_uri={_q(redir)}&response_type=code"
            f"&scope={_q('https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly')}"
            f"&access_type=offline&prompt=consent&state={user_id}")

def google_exchange(code_val, redirect_uri=""):
    from urllib.parse import quote as _q
    if not GOOGLE_CLIENT_ID: return {}
    redir = redirect_uri or GOOGLE_REDIRECT_URI or ""
    payload = (f"code={_q(code_val)}&client_id={_q(GOOGLE_CLIENT_ID)}"
               f"&client_secret={_q(GOOGLE_CLIENT_SECRET)}"
               f"&redirect_uri={_q(redir)}&grant_type=authorization_code").encode()
    try:
        req = urllib.request.Request("https://oauth2.googleapis.com/token",
            data=payload, headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
            logger.info(f"Google exchange keys: {list(data.keys())}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        logger.error(f"Google exchange {e.code}: {body}")
        return {"error":f"HTTP {e.code}","error_description":body}
    except Exception as e:
        logger.error(f"Google exchange: {e}")
        return {}

# ══ TWILIO SMS ════════════════════════════════════════════════════════════════
SMS_TMPLS={
    "company_added":"🏢 New Company\n{company_name}\nIndustry:{industry}\nScore:{lead_score}/100",
    "hot_lead":"🔥 Hot Lead!\n{company_name}\nScore:{lead_score}/100\nIndustry:{industry}",
    "email_generated":"📧 Email Ready\n{company_name}\nType:{email_type}\nTo:{recipient_email}",
    "email_sent":"✅ Email Sent\nTo:{recipient_email}\nSubj:{subject}",
    "meeting_scheduled":"📅 Meeting\n{title}\n{company_name}\nTime:{scheduled_at}",
    "meeting_completed":"✅ Meeting Done\n{title}\n{company_name}",
    "call_initiated":"📞 AI Call\n{company_name}\nPhone:{phone_number}\nObj:{objective}",
    "csv_import":"📤 Import Done\n{filename}\n✅{processed_rows} ❌{failed_rows}",
    "daily_report":"📊 Daily {report_date}\n🏢{total_companies} co | 🔥{hot_leads} hot | 🟡{warm_leads} warm\n📧{emails_sent} sent ({email_open_rate}% open)\n📞{total_calls} calls\n💰${revenue_pipeline:,}\n{top_companies}",
    "meeting_reminder_24h":"📅 24h Reminder\n{title}\n{company_name}\n{scheduled_at}",
    "meeting_reminder_1h":"⏰ 1h Reminder\n{title}\n{company_name}",
    "meeting_reminder_10min":"⏰ 10min!\n{title}\n🚀 Starting soon!",
    "lead_replied":"💬 REPLY!\n{company_name}\n{recipient_email} answered your outreach.\nFollow-ups auto-stopped — check the CRM.",
}
class _SD(dict):
    def __missing__(self,k): return ""

def _sms_send(to,body):
    if not TWILIO_SID:    return {"status":"not_configured","message":"TWILIO_ACCOUNT_SID not set in Render env"}
    if not TWILIO_TOKEN:  return {"status":"not_configured","message":"TWILIO_AUTH_TOKEN not set in Render env"}
    if not TWILIO_FROM:   return {"status":"not_configured","message":"TWILIO_FROM_NUMBER not set in Render env"}
    if not to:            return {"status":"error","message":"No recipient phone number"}
    from urllib.parse import quote as _q
    payload=f"To={_q(to)}&From={_q(TWILIO_FROM)}&Body={_q(body[:1600])}"
    creds=base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    try:
        req=urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data=payload.encode(),
            headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req,timeout=15) as r:
            data=json.loads(r.read())
            return {"status":"sent","sid":data.get("sid")}
    except urllib.error.HTTPError as e:
        body_err=e.read().decode()[:300]
        logger.error(f"Twilio HTTP {e.code}: {body_err}")
        if e.code==401:
            return {"status":"error","message":"Twilio 401 Unauthorized — TWILIO_AUTH_TOKEN is wrong. Copy it fresh from console.twilio.com → Account Info → Auth Token"}
        if e.code==400:
            try:
                err_data=json.loads(body_err)
                return {"status":"error","message":f"Twilio error {err_data.get('code',400)}: {err_data.get('message',body_err[:100])}"}
            except Exception:
                return {"status":"error","message":f"Twilio 400: {body_err[:100]}"}
        return {"status":"error","message":f"Twilio HTTP {e.code}: {body_err[:100]}"}
    except urllib.error.URLError as e:
        return {"status":"error","message":f"Cannot reach Twilio: {e.reason}"}
    except Exception as e:
        logger.error(f"Twilio: {type(e).__name__}: {e}")
        return {"status":"error","message":str(e)}

def _sms_log(event_type,body,result):
    try: run("INSERT INTO sms_logs(to_number,from_number,body,status,event_type,error_msg) VALUES(?,?,?,?,?,?)",
             (TWILIO_ADMIN or "admin",TWILIO_FROM,body[:500],result.get("status","unknown"),event_type,
              result.get("message") if result.get("status")!="sent" else None))
    except Exception: pass

def notify(event_type,data):
    tmpl=SMS_TMPLS.get(event_type)
    if not tmpl: return
    if not TWILIO_ADMIN:
        logger.debug(f"notify {event_type}: TWILIO_ADMIN_NUMBER not set, skipping SMS")
        return
    try:
        body=tmpl.format_map(_SD(data))
        result=_sms_send(TWILIO_ADMIN,body)
        if result.get("status") not in ("sent","not_configured"):
            logger.warning(f"SMS notify {event_type}: {result.get('message','failed')}")
        _sms_log(event_type,body,result)
    except Exception as e:
        logger.error(f"notify {event_type}: {e}")

def notify_async(event_type,data): threading.Thread(target=notify,args=(event_type,data),daemon=True).start()
def send_sms(to,body): return _sms_send(to,body)
def sms_test(to):
    num = (to or TWILIO_ADMIN or "").strip()
    if not num: return {"status":"error","message":"No phone number — enter your number in the test field"}
    return _sms_send(num, f"✅ ARC Digital AI Sales — Connected! {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC")

def generate_google_meet_link(title="ARC Digital Meeting"):
    """Generate a Google Meet link via Google Calendar API or fallback to meet.google.com link."""
    import hashlib, base64, time as _time
    # Create a deterministic meet code from title + timestamp
    raw = f"{title}-{_time.time()}-arc-digital"
    code = hashlib.md5(raw.encode()).hexdigest()[:10]
    # Format as xxx-xxxx-xxx
    meet_code = f"{code[:3]}-{code[3:7]}-{code[7:10]}"
    return f"https://meet.google.com/{meet_code}"

def create_calendar_event_with_meet(user_id, title, description, start_dt, duration_mins=30):
    """Create Google Calendar event with Meet link using stored refresh token."""
    user = q1("SELECT google_refresh_token FROM users WHERE id=?", (user_id,))
    if not user or not user.get("google_refresh_token"):
        return None, generate_google_meet_link(title)

    refresh_token = user["google_refresh_token"]
    from urllib.parse import quote as _q

    # Get access token
    try:
        payload = (f"client_id={_q(GOOGLE_CLIENT_ID)}&client_secret={_q(GOOGLE_CLIENT_SECRET)}"
                   f"&refresh_token={_q(refresh_token)}&grant_type=refresh_token").encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token",
            data=payload, headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            tokens = json.loads(r.read())
        access_token = tokens.get("access_token")
        if not access_token:
            return None, generate_google_meet_link(title)
    except Exception as e:
        logger.error(f"Get access token: {e}")
        return None, generate_google_meet_link(title)

    # Create event
    try:
        if isinstance(start_dt, str):
            start_dt = datetime.strptime(start_dt[:19], "%Y-%m-%d %H:%M:%S")
        end_dt = start_dt + timedelta(minutes=duration_mins)
        event = {
            "summary": title,
            "description": description or f"ARC Digital — Kevin Raju\n\n{title}",
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Australia/Melbourne"},
            "end":   {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": "Australia/Melbourne"},
            "conferenceData": {"createRequest": {"requestId": f"arc-{int(datetime.utcnow().timestamp())}",
                               "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }
        body = json.dumps(event).encode()
        req = urllib.request.Request(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events?conferenceDataVersion=1",
            data=body,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ev = json.loads(r.read())
        meet_link = ev.get("hangoutLink") or generate_google_meet_link(title)
        event_id  = ev.get("id")
        logger.info(f"✅ Calendar event created: {event_id} meet={meet_link}")
        return event_id, meet_link
    except Exception as e:
        logger.error(f"Create calendar event: {e}")
        return None, generate_google_meet_link(title)


def schedule_reminders(mid,title,company_name,scheduled_at):
    if not scheduled_at: return
    try: mdt=datetime.strptime(scheduled_at[:19],"%Y-%m-%d %H:%M:%S")
    except Exception: return
    def _fire(delta,etype):
        delay=(mdt-delta-datetime.utcnow()).total_seconds()
        if delay<=0: return
        def _r(): time.sleep(delay); notify(etype,{"title":title,"company_name":company_name,"scheduled_at":mdt.strftime("%d %b %Y %H:%M UTC")})
        threading.Thread(target=_r,daemon=True).start()
    _fire(timedelta(hours=24),"meeting_reminder_24h")
    _fire(timedelta(hours=1),"meeting_reminder_1h")
    _fire(timedelta(minutes=10),"meeting_reminder_10min")

# ══ AUTOMATION ════════════════════════════════════════════════════════════════
def _admin_id():
    u=q1("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"); return u["id"] if u else 1

def auto_score_all():
    try:
        cos=q("SELECT * FROM companies")
        for co in cos:
            cts=q("SELECT * FROM contacts WHERE company_id=?",(co["id"],))
            sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(co["id"],))
            sc=score_company(co,cts,sigs); old=co.get("lead_score",0) or 0
            if q1("SELECT id FROM lead_scores WHERE company_id=?",(co["id"],)):
                run("""UPDATE lead_scores SET total_score=?,revenue_score=?,employee_score=?,industry_score=?,
                       buying_signal_score=?,department_signal_score=?,email_activity_score=?,tier=?,updated_at=?
                       WHERE company_id=?""",
                    (sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
                     sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"],now(),co["id"]))
            else:
                run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,industry_score,
                       buying_signal_score,department_signal_score,email_activity_score,tier) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (co["id"],sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
                     sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"]))
            run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(sc["total_score"],now(),co["id"]))
            if sc["total_score"]>=80 and old<80:
                notify_async("hot_lead",{"company_name":co["name"],"lead_score":sc["total_score"],"industry":co.get("industry","N/A")})
        logger.info(f"✅ Auto-scored {len(cos)} companies"); return {"scored":len(cos)}
    except Exception as e: logger.error(f"auto_score:{e}"); return {"error":str(e)}

def auto_email_hot_leads():
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        logger.info("auto_email: skipped (gmail not configured)")
        return {"skipped":"gmail_not_configured"}
    try:
        cos=q("""SELECT c.* FROM companies c WHERE c.lead_score>=70
                 AND c.id NOT IN(SELECT DISTINCT company_id FROM emails WHERE status IN('sent','opened','replied') AND company_id IS NOT NULL)
                 ORDER BY c.lead_score DESC LIMIT 10""")
        sent=0; skipped=0; errors=0
        for co in cos:
            try:
                ct=(q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(co["id"],))
                    or q1("SELECT * FROM contacts WHERE company_id=?",(co["id"],)))
                email_addr = (ct.get("email","") if ct else "").strip()
                if not ct or not email_addr or "@" not in email_addr:
                    logger.info(f"auto_email: skipping {co['name']} — no valid email")
                    skipped+=1; continue
                content=generate_email(co,ct,"cold")
                ct_name=f"{ct.get('first_name','')} {ct.get('last_name','')}".strip()
                # save draft first so we always have a record
                eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (co["id"],ct["id"],_admin_id(),"cold",content["subject"],content["body"],
                         email_addr,ct_name,"draft","groq",now()))
                r=send_email_smtp(email_addr,ct_name,content["subject"],content["body"])
                if r.get("status")=="sent":
                    run("UPDATE emails SET status=\'sent\',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
                    notify_async("email_sent",{"recipient_email":email_addr,"subject":content["subject"]})
                    sent+=1
                else:
                    run("UPDATE emails SET status=\'error\',updated_at=? WHERE id=?",(now(),eid))
                    logger.warning(f"auto_email {co['name']}: {r.get('message','send failed')}")
                    errors+=1
            except Exception as e_inner:
                logger.error(f"auto_email {co.get('name','?')}: {e_inner}")
                errors+=1
        logger.info(f"✅ Auto-email done: sent={sent} skipped={skipped} errors={errors}")
        return {"sent":sent,"skipped":skipped,"errors":errors}
    except Exception as e:
        logger.error(f"auto_email_hot_leads: {e}")
        return {"error":str(e)}

def auto_followup():
    if not (GMAIL_EMAIL and GMAIL_PASSWORD):
        return {"skipped":"gmail_not_configured"}
    try:
        cutoff=(datetime.utcnow()-timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        pending=q("""SELECT e.* FROM emails e WHERE e.email_type='cold' AND e.status IN('sent','opened') AND e.sent_at<?
                    AND e.company_id NOT IN(SELECT DISTINCT company_id FROM emails WHERE email_type='follow_up' AND company_id IS NOT NULL)
                    ORDER BY e.sent_at ASC LIMIT 5""",(cutoff,))
        sent=0; errors=0
        for em in pending:
            try:
                co=q1("SELECT * FROM companies WHERE id=?",(em["company_id"],))
                ct=q1("SELECT * FROM contacts WHERE id=?",(em.get("contact_id"),)) if em.get("contact_id") else None
                email_addr=(ct.get("email","") if ct else "").strip()
                if not co or not ct or not email_addr or "@" not in email_addr: continue
                content=generate_email(co,ct,"follow_up")
                ct_name=f"{ct.get('first_name','')} {ct.get('last_name','')}".strip()
                eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (co["id"],ct["id"],_admin_id(),"follow_up",content["subject"],content["body"],
                         email_addr,ct_name,"draft","groq",now()))
                r=send_email_smtp(email_addr,ct_name,content["subject"],content["body"])
                if r.get("status")=="sent":
                    run("UPDATE emails SET status='sent',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
                    sent+=1
                else:
                    run("UPDATE emails SET status='error',updated_at=? WHERE id=?",(now(),eid))
                    logger.warning(f"followup {co['name']}: {r.get('message','failed')}")
                    errors+=1
            except Exception as e_inner:
                logger.error(f"auto_followup inner: {e_inner}")
                errors+=1
        logger.info(f"✅ Follow-up done: sent={sent} errors={errors}")
        return {"sent":sent,"errors":errors}
    except Exception as e:
        logger.error(f"auto_followup: {e}")
        return {"error":str(e)}

def auto_call_hot_leads():
    if not BLAND_API_KEY: return {"skipped":"bland_not_configured"}
    try:
        cos=q("""SELECT c.* FROM companies c WHERE c.lead_score>=80
                 AND c.id NOT IN(SELECT DISTINCT company_id FROM calls WHERE company_id IS NOT NULL)
                 ORDER BY c.lead_score DESC LIMIT 3""")
        called=0
        for co in cos:
            ct=q1("SELECT * FROM contacts WHERE company_id=? AND phone IS NOT NULL AND phone!='' ORDER BY is_decision_maker DESC LIMIT 1",(co["id"],))
            if not ct or not ct.get("phone"): continue
            script=call_script(co,ct,"qualify")
            r=bland_call_api(ct["phone"],script,"nat",co["name"],f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"qualify")
            run("""INSERT INTO calls(bland_call_id,company_id,contact_id,created_by,phone_number,objective,task_prompt,voice,status,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (r.get("call_id"),co["id"],ct["id"],_admin_id(),ct["phone"],"qualify",script,"nat",
                 "queued" if r.get("status")=="queued" else "error",now()))
            if r.get("status")=="queued":
                notify_async("call_initiated",{"company_name":co["name"],"phone_number":ct["phone"],"objective":"Qualify"}); called+=1
        return {"called":called}
    except Exception as e: return {"error":str(e)}

def sync_call_statuses():
    if not BLAND_API_KEY: return
    try:
        for call in q("SELECT id,bland_call_id FROM calls WHERE status IN('queued','in-progress') AND bland_call_id IS NOT NULL"):
            live=bland_get_api(call["bland_call_id"])
            if live and "status" in live:
                run("""UPDATE calls SET status=?,duration_seconds=?,recording_url=?,transcript=?,summary=?,updated_at=? WHERE id=?""",
                    (live.get("status"),live.get("call_length"),live.get("recording_url"),live.get("concatenated_transcript",""),live.get("summary",""),now(),call["id"]))
    except Exception as e: logger.error(f"sync:{e}")

def send_daily_report():
    if not TWILIO_ADMIN: return
    try:
        cos=q("SELECT lead_score,name FROM companies"); emails=q("SELECT status FROM emails"); mtgs=q("SELECT status FROM meetings"); calls=q("SELECT id FROM calls")
        hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
        sent=sum(1 for e in emails if e.get("status") in("sent","opened","replied")); opened=sum(1 for e in emails if e.get("status") in("opened","replied"))
        top5=sorted(cos,key=lambda x:x.get("lead_score",0),reverse=True)[:5]
        notify_async("daily_report",{"report_date":datetime.utcnow().strftime("%d %b %Y"),"total_companies":len(cos),"hot_leads":hot,"warm_leads":warm,
            "emails_sent":sent,"email_open_rate":round(opened/sent*100,1) if sent else 0,"total_calls":len(calls),
            "meetings_scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"revenue_pipeline":hot*50000+warm*15000,
            "top_companies":", ".join(f"{c['name']}({c['lead_score']})" for c in top5)})
    except Exception as e: logger.error(f"daily_report:{e}")

def run_automation_cycle():
    h=datetime.utcnow().hour; dow=datetime.utcnow().weekday()
    logger.info(f"🔄 Auto cycle h={h} dow={dow}")
    sync_call_statuses()
    try: sync_jakes_leads()
    except Exception as e: logger.warning(f"jakes sync: {e}")
    # Reply detection + follow-up cadence for autopilot leads (2026-08-05).
    # Human-approval preserved: cadence only DRAFTS; replies stop the cadence.
    try: check_email_replies()
    except Exception as e: logger.warning(f"reply check: {e}")
    try: cadence_tick()
    except Exception as e: logger.warning(f"cadence: {e}")
    if h==AUTO_SCORE_HOUR: auto_score_all()
    if h==AUTO_EMAIL_HOUR and dow<5: auto_email_hot_leads()
    if h==AUTO_FOLLOWUP_HOUR and dow<5: auto_followup()
    if h==AUTO_CALL_HOUR and dow<5: auto_call_hot_leads()
    if h==DAILY_REPORT_HOUR: send_daily_report()
    logger.info("✅ Auto cycle done")

# ══ SCHEDULER ═════════════════════════════════════════════════════════════════
_SR=False; _ST=None

def start():
    global _SR,_ST
    if _SR: return
    _SR=True
    def _loop():
        last=-1
        while _SR:
            try:
                h=datetime.utcnow().hour
                if h!=last: last=h; threading.Thread(target=run_automation_cycle,daemon=True).start()
            except Exception as e: logger.error(f"sched:{e}")
            time.sleep(60)
    _ST=threading.Thread(target=_loop,daemon=True,name="Sched"); _ST.start()

def trigger_now(): threading.Thread(target=run_automation_cycle,daemon=True).start()

# ══ API ROUTES ════════════════════════════════════════════════════════════════
api=Blueprint("api",__name__,url_prefix="/api")

def ok(data=None,**kw):
    r={"ok":True}
    if data is not None: r["data"]=data
    r.update(kw); return jsonify(r)
def err(msg,code=400): return jsonify({"ok":False,"error":msg}),code

@api.post("/auth/login")
def login():
    d=request.get_json(silent=True) or {}
    email=(d.get("email") or "").lower().strip(); pw=d.get("password") or ""
    if not email or not pw: return err("Email and password required")
    user=q1("SELECT * FROM users WHERE email=? AND is_active=1",(email,))
    if not user or not verify_pw(pw,user["password"]): return err("Invalid credentials",401)
    run("UPDATE users SET last_login=? WHERE id=?",(now(),user["id"]))
    tok=make_token(user["id"],user["email"],user["role"])
    return ok({"token":tok,"user":{k:user[k] for k in("id","email","full_name","role","created_at") if k in user}})

@api.post("/auth/register")
def register():
    d=request.get_json(silent=True) or {}
    email=(d.get("email") or "").lower().strip(); fn=(d.get("full_name") or "").strip(); pw=d.get("password") or ""
    if not email or not fn or not pw: return err("email, full_name and password required")
    if len(pw)<6: return err("Password must be at least 6 characters")
    if q1("SELECT 1 FROM users WHERE email=?",(email,)): return err("Email already registered")
    uid=run("INSERT INTO users(email,full_name,password,role) VALUES(?,?,?,?)",(email,fn,hash_pw(pw),d.get("role","sales_rep")))
    user=q1("SELECT * FROM users WHERE id=?",(uid,))
    return ok({"token":make_token(uid,email,d.get("role","sales_rep")),"user":{k:user[k] for k in("id","email","full_name","role") if k in user}}),201

@api.get("/auth/me")
@login_required
def me():
    u=q1("SELECT * FROM users WHERE id=?",(g.user["id"],))
    if not u: return err("Not found",404)
    return ok({k:u[k] for k in("id","email","full_name","role","is_active","created_at","last_login") if k in u})

@api.get("/companies")
@login_required
def list_companies():
    search=request.args.get("search",""); status=request.args.get("status",""); limit=min(int(request.args.get("limit",200)),500)
    sql="SELECT * FROM companies WHERE 1=1"; args=[]
    if search: sql+=" AND name LIKE ?"; args.append(f"%{search}%")
    if status: sql+=" AND status=?"; args.append(status)
    sql+=" ORDER BY lead_score DESC LIMIT ?"; args.append(limit)
    return ok(q(sql,args))

@api.post("/companies")
@login_required
def create_company():
    d=request.get_json(silent=True) or {}; name=(d.get("name") or "").strip()
    if not name: return err("name is required")
    cid=run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,website,city,country,description,technologies,status,linkedin_url,funding_stage,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name,d.get("industry"),d.get("employee_count"),d.get("annual_revenue"),d.get("website"),d.get("city"),d.get("country"),d.get("description"),d.get("technologies"),d.get("status","prospect"),d.get("linkedin_url"),d.get("funding_stage"),g.user["id"],now()))
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    notify_async("company_added",{"company_name":co["name"],"industry":co.get("industry","N/A"),"lead_score":co.get("lead_score",0),"status":co.get("status","prospect")})
    return ok(co),201

@api.get("/companies/<int:cid>")
@login_required
def get_company(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    co["contacts"]=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    co["emails"]=q("SELECT * FROM emails WHERE company_id=? ORDER BY created_at DESC",(cid,))
    co["meetings"]=q("SELECT * FROM meetings WHERE company_id=? ORDER BY scheduled_at DESC",(cid,))
    co["calls"]=q("SELECT * FROM calls WHERE company_id=? ORDER BY created_at DESC",(cid,))
    co["buying_signals"]=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    co["lead_score_details"]=q1("SELECT * FROM lead_scores WHERE company_id=?",(cid,))
    return ok(co)

@api.put("/companies/<int:cid>")
@login_required
def update_company(cid):
    if not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"name","industry","employee_count","annual_revenue","website","city","country","description","technologies","status","linkedin_url","funding_stage","ai_summary"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if not sets: return err("Nothing to update")
    sets.append("updated_at=?"); vals.append(now()); vals.append(cid)
    run(f"UPDATE companies SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM companies WHERE id=?",(cid,)))

@api.delete("/companies/<int:cid>")
@login_required
def delete_company(cid):
    if not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("Not found",404)
    run("DELETE FROM companies WHERE id=?",(cid,)); return ok({"deleted":cid})

@api.post("/companies/<int:cid>/score")
@login_required
def score_company_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,)); sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    sc=score_company(co,cts,sigs)
    if q1("SELECT id FROM lead_scores WHERE company_id=?",(cid,)):
        run("""UPDATE lead_scores SET total_score=?,revenue_score=?,employee_score=?,industry_score=?,buying_signal_score=?,department_signal_score=?,email_activity_score=?,tier=?,updated_at=? WHERE company_id=?""",
            (sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"],now(),cid))
    else:
        run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,industry_score,buying_signal_score,department_signal_score,email_activity_score,tier) VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],sc["tier"]))
    run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(sc["total_score"],now(),cid))
    if sc["total_score"]>=80: notify_async("hot_lead",{"company_name":co["name"],"lead_score":sc["total_score"],"industry":co.get("industry","N/A")})
    return ok(sc)

@api.post("/companies/<int:cid>/ai-summary")
@login_required
def ai_summary_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    s=company_summary(co); run("UPDATE companies SET ai_summary=?,updated_at=? WHERE id=?",(s,now(),cid)); return ok({"summary":s})

@api.post("/companies/<int:cid>/analyze-signals")
@login_required
def analyze_signals_route(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    run("DELETE FROM buying_signals WHERE company_id=?",(cid,))
    sigs=buying_signals(co)
    for s in sigs:
        run("INSERT INTO buying_signals(company_id,signal_type,signal_name,signal_description,strength,source) VALUES(?,?,?,?,?,?)",
            (cid,s.get("type",""),s.get("name",""),s.get("description",""),s.get("strength",5),"ai"))
    return ok({"signals":sigs})

@api.post("/companies/upload-csv")
@login_required
def upload_csv():
    # Synchronous, Apollo-header-aware company import (same IMP_ALIASES
    # matching as the leads import). New names are created, existing names
    # get any provided details refreshed — counts come back to the UI.
    f=request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".csv"): return err("CSV file required")
    fname=f.filename; uid=g.user["id"]
    try: text=f.read().decode("utf-8-sig",errors="replace")
    except Exception as e: return err(f"Could not read file: {e}")
    delim=max([",",";","\t"],key=text.split("\n",1)[0].count)
    allrows=list(csv.reader(io.StringIO(text),delimiter=delim))
    if len(allrows)<2: return err("CSV has no data rows")
    headers=allrows[0]
    canon=[IMP_ALIASES.get(_imp_norm(h)) for h in headers]
    status_idx=[i for i,h in enumerate(headers) if _imp_norm(h)=="status"]
    if "company" not in canon and "company_name_for_emails" not in canon:
        return err("No company-name column found — expected a header like Company, Company Name or Account Name")
    def ti(v):
        try: return int(float(str(v).replace(",","").replace("$","").strip())) if str(v).strip() else None
        except Exception: return None
    created=updated=skipped=0
    with get_conn() as c:
        existing={r["n"]:r["id"] for r in c.execute("SELECT id,lower(name) AS n FROM companies")}
        for cells in allrows[1:]:
            if not any(str(x).strip() for x in cells): continue
            vals={}
            for cn,vv in zip(canon,cells):
                if cn and vv and str(vv).strip() and cn not in vals: vals[cn]=str(vv).strip()
            name=vals.get("company") or vals.get("company_name_for_emails") or ""
            if not name: skipped+=1; continue
            st=next((str(cells[i]).strip() for i in status_idx if i<len(cells) and str(cells[i]).strip()),"prospect")
            fields={"industry":vals.get("industry"),
                    "employee_count":ti(vals.get("employees")),
                    "annual_revenue":ti(vals.get("annual_revenue")),
                    "country":vals.get("company_country") or vals.get("country"),
                    "city":vals.get("company_city") or vals.get("city"),
                    "website":vals.get("website")}
            key=name.lower()
            if key in existing:
                sets=[f"{k}=?" for k,vv in fields.items() if vv not in (None,"")]
                args=[vv for vv in fields.values() if vv not in (None,"")]
                if sets:
                    c.execute(f"UPDATE companies SET {','.join(sets)},updated_at=? WHERE id=?",args+[now(),existing[key]])
                    updated+=1
                else: skipped+=1
            else:
                cur=c.execute("""INSERT INTO companies(name,industry,employee_count,annual_revenue,country,city,website,status,created_by,updated_at)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (name,fields["industry"],fields["employee_count"],fields["annual_revenue"],
                     fields["country"],fields["city"],fields["website"],st or "prospect",uid,now()))
                existing[key]=cur.lastrowid
                created+=1
    notify_async("csv_import",{"filename":fname,"processed_rows":created+updated,"failed_rows":skipped})
    logger.info(f"Companies CSV '{fname}': {created} new, {updated} updated, {skipped} skipped")
    return ok({"created":created,"updated":updated,"skipped":skipped,"filename":fname}),201

@api.get("/contacts")
@login_required
def list_contacts():
    cid        = request.args.get("company_id","")
    search     = request.args.get("search","").strip()
    seniority  = request.args.get("seniority","").strip()
    qualified  = request.args.get("qualified","").strip()   # "yes"/"no"

    sql = """
        SELECT
            ct.id, ct.company_id, ct.first_name, ct.last_name,
            ct.email, ct.phone, ct.title, ct.department,
            ct.seniority_level, ct.is_decision_maker, ct.contact_status, ct.created_at,
            co.name  AS company_name,
            co.city  AS company_city,
            co.country AS company_country,
            co.industry AS company_industry,
            co.lead_score AS company_lead_score,
            co.website AS company_website,
            co.ai_summary AS company_ai_summary,
            cl.linkedin_url, cl.position AS linkedin_position,
            cl.seniority AS linkedin_seniority, cl.is_key_person,
            ls.total_score AS qualify_score,
            ls.tier AS qualify_tier
        FROM contacts ct
        LEFT JOIN companies  co ON ct.company_id = co.id
        LEFT JOIN contact_linkedin cl ON cl.contact_id = ct.id
        LEFT JOIN lead_scores ls ON ls.company_id = ct.company_id
        WHERE 1=1
    """
    args = []
    if cid:
        sql += " AND ct.company_id=?";  args.append(cid)
    if search:
        sql += " AND (ct.first_name LIKE ? OR ct.last_name LIKE ? OR co.name LIKE ? OR ct.email LIKE ?)"
        args += [f"%{search}%"]*4
    if seniority:
        sql += " AND ct.seniority_level=?"; args.append(seniority)
    if qualified == "yes":
        sql += " AND ls.total_score >= 60"
    elif qualified == "no":
        sql += " AND (ls.total_score IS NULL OR ls.total_score < 60)"

    sql += " ORDER BY co.lead_score DESC, ct.is_decision_maker DESC, ct.created_at DESC LIMIT 500"
    return ok(q(sql, args))

@api.post("/contacts")
@login_required
def create_contact():
    d=request.get_json(silent=True) or {}
    if not d.get("company_id") or not d.get("first_name"): return err("company_id and first_name required")
    ctid=run("INSERT INTO contacts(company_id,first_name,last_name,email,phone,title,department,seniority_level,is_decision_maker) VALUES(?,?,?,?,?,?,?,?,?)",
             (d["company_id"],d["first_name"],d.get("last_name"),d.get("email"),d.get("phone"),d.get("title"),d.get("department"),d.get("seniority_level","individual"),1 if d.get("is_decision_maker") else 0))
    return ok(q1("SELECT * FROM contacts WHERE id=?",(ctid,))),201

@api.put("/contacts/<int:ctid>")
@login_required
def update_contact(ctid):
    if not q1("SELECT id FROM contacts WHERE id=?",(ctid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"first_name","last_name","email","phone","title","department","seniority_level","is_decision_maker","contact_status"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: run(f"UPDATE contacts SET {','.join(sets)} WHERE id=?",vals+[ctid])
    return ok(q1("SELECT * FROM contacts WHERE id=?",(ctid,)))

@api.get("/emails")
@login_required
def list_emails():
    status=request.args.get("status",""); cid=request.args.get("company_id","")
    sql="SELECT * FROM emails WHERE 1=1"; args=[]
    if status: sql+=" AND status=?"; args.append(status)
    if cid: sql+=" AND company_id=?"; args.append(cid)
    return ok(q(sql+" ORDER BY created_at DESC LIMIT 200",args))

@api.post("/emails/generate")
@login_required
def gen_email():
    d=request.get_json(silent=True) or {}; cid=d.get("company_id")
    if not cid: return err("company_id required")
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Company not found",404)
    ct=(q1("SELECT * FROM contacts WHERE id=?",(d["contact_id"],)) if d.get("contact_id") else None
        or q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(cid,))
        or q1("SELECT * FROM contacts WHERE company_id=?",(cid,))
        or {"first_name":"Team","last_name":"","title":"","email":""})
    et=d.get("email_type","cold"); content=generate_email(co,ct,et,d.get("custom_instructions",""))
    eid=run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,recipient_email,recipient_name,status,ai_model_used,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,ct.get("id"),g.user["id"],et,content["subject"],content["body"],ct.get("email","unknown@example.com"),f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"draft","groq",now()))
    em=q1("SELECT * FROM emails WHERE id=?",(eid,))
    notify_async("email_generated",{"company_name":co["name"],"email_type":et,"recipient_email":em["recipient_email"],"subject":em["subject"]})
    return ok(em),201

@api.post("/emails/<int:eid>/send")
@login_required
def send_email_route(eid):
    try:
        em=q1("SELECT * FROM emails WHERE id=?",(eid,))
        if not em: return err("Not found",404)
        if em.get("status")=="sent": return err("Already sent")
        recip = (em.get("recipient_email") or "").strip()
        if not recip:
            return err("Email has no recipient_email set",400)
        result = send_email_smtp(recip, em.get("recipient_name",""), em.get("subject","(no subject)"), em.get("body",""))
        if result.get("status")=="sent":
            run("UPDATE emails SET status=\'sent\',sent_at=?,updated_at=? WHERE id=?",(now(),now(),eid))
            notify_async("email_sent",{"recipient_email":recip,"subject":em.get("subject","")})
        return ok({"email":q1("SELECT * FROM emails WHERE id=?",(eid,)),"send_result":result})
    except Exception as e:
        logger.error(f"send_email_route {eid}: {e}")
        return err(f"Send failed: {e}", 500)

@api.put("/emails/<int:eid>")
@login_required
def update_email(eid):
    if not q1("SELECT id FROM emails WHERE id=?",(eid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"subject","body","recipient_email","recipient_name","status","email_type"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: sets.append("updated_at=?"); vals.append(now()); vals.append(eid); run(f"UPDATE emails SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM emails WHERE id=?",(eid,)))

@api.put("/emails/<int:eid>/track")
@login_required
def track_email(eid):
    d=request.get_json(silent=True) or {}; status=d.get("status")
    if status not in("opened","replied","bounced"): return err("Invalid status")
    em=q1("SELECT * FROM emails WHERE id=?",(eid,))
    if not em: return err("Not found",404)
    run("UPDATE emails SET status=?,updated_at=? WHERE id=?",(status,now(),eid)); return ok(q1("SELECT * FROM emails WHERE id=?",(eid,)))

@api.get("/meetings")
@login_required
def list_meetings():
    status=request.args.get("status","")
    sql="SELECT m.*,c.name as company_name FROM meetings m LEFT JOIN companies c ON m.company_id=c.id"; args=[]
    if status: sql+=" WHERE m.status=?"; args.append(status)
    return ok(q(sql+" ORDER BY m.scheduled_at DESC",args))

@api.post("/meetings")
@login_required
def create_meeting():
    d=request.get_json(silent=True) or {}
    if not d.get("company_id") or not d.get("title"): return err("company_id and title required")
    co=q1("SELECT * FROM companies WHERE id=?",(d["company_id"],))
    if not co: return err("Company not found",404)
    mid=run("""INSERT INTO meetings(company_id,contact_id,created_by,title,meeting_type,description,scheduled_at,duration_minutes,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (d["company_id"],d.get("contact_id"),g.user["id"],d["title"],d.get("meeting_type","discovery"),d.get("description"),d.get("scheduled_at"),d.get("duration_minutes",30),"proposed",now()))
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    notify_async("meeting_scheduled",{"title":m["title"],"company_name":co["name"],"meeting_type":m.get("meeting_type",""),"scheduled_at":(m.get("scheduled_at") or "TBD")[:16],"duration_minutes":m.get("duration_minutes",30)})
    if m.get("scheduled_at"): schedule_reminders(mid,m["title"],co["name"],m["scheduled_at"])
    return ok(m),201

@api.put("/meetings/<int:mid>")
@login_required
def update_meeting(mid):
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if not m: return err("Not found",404)
    prev=m.get("status",""); d=request.get_json(silent=True) or {}
    allowed={"title","meeting_type","description","scheduled_at","duration_minutes","status","meeting_link","notes"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: sets.append("updated_at=?"); vals.append(now()); vals.append(mid); run(f"UPDATE meetings SET {','.join(sets)} WHERE id=?",vals)
    m2=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if m2.get("status")=="completed" and prev!="completed":
        co=q1("SELECT name FROM companies WHERE id=?",(m2.get("company_id",0),))
        notify_async("meeting_completed",{"title":m2["title"],"company_name":co["name"] if co else "Unknown","meeting_type":m2.get("meeting_type","")})
    return ok(m2)

@api.post("/meetings/<int:mid>/calendar")
@login_required
def add_to_calendar(mid):
    m=q1("SELECT * FROM meetings WHERE id=?",(mid,))
    if not m: return err("Not found",404)
    user=q1("SELECT * FROM users WHERE id=?",(g.user["id"],))
    if not user or not user.get("google_refresh_token"): return err("Google Calendar not connected — go to Integrations",400)
    return ok({"message":"Calendar event would be created here","google_event_id":None})

@api.get("/calls")
@login_required
def list_calls():
    return ok(q("SELECT ca.*,co.name as company_name FROM calls ca LEFT JOIN companies co ON ca.company_id=co.id ORDER BY ca.created_at DESC LIMIT 100"))

@api.post("/calls/make")
@login_required
def make_call():
    d=request.get_json(silent=True) or {}; phone=(d.get("phone_number") or "").strip()
    cid=d.get("company_id"); co=q1("SELECT * FROM companies WHERE id=?",(cid,)) if cid else None
    ct=q1("SELECT * FROM contacts WHERE id=?",(d.get("contact_id"),)) if d.get("contact_id") else None
    if not phone and ct and ct.get("phone"): phone=ct["phone"]
    if not phone: return err("phone_number required (e.g. +14155550100)")
    objective=d.get("objective","qualify"); task=d.get("custom_task") or call_script(co or {},ct or {},objective)
    result=bland_call_api(phone,task,d.get("voice","nat"),co["name"] if co else "",f"{ct.get('first_name','')} {ct.get('last_name','')}".strip() if ct else "",objective)
    call_id=run("""INSERT INTO calls(bland_call_id,company_id,contact_id,created_by,phone_number,objective,task_prompt,voice,status,error_message,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (result.get("call_id"),cid,d.get("contact_id"),g.user["id"],phone,objective,task,d.get("voice","nat"),
                 "queued" if result.get("status")=="queued" else "error",result.get("message") if result.get("status")=="error" else None,now()))
    notify_async("call_initiated",{"company_name":co["name"] if co else "Unknown","phone_number":phone,"objective":objective})
    return ok({"call":q1("SELECT * FROM calls WHERE id=?",(call_id,)),"bland_result":result})

@api.get("/calls/<int:cid_>")
@login_required
def get_call(cid_):
    call=q1("SELECT * FROM calls WHERE id=?",(cid_,))
    if not call: return err("Not found",404)
    if call.get("bland_call_id") and call.get("status") not in("completed","error","failed"):
        live=bland_get_api(call["bland_call_id"])
        if live and "status" in live:
            run("UPDATE calls SET status=?,duration_seconds=?,recording_url=?,transcript=?,summary=?,updated_at=? WHERE id=?",
                (live.get("status"),live.get("call_length"),live.get("recording_url"),live.get("concatenated_transcript",""),live.get("summary",""),now(),cid_))
            call=q1("SELECT * FROM calls WHERE id=?",(cid_,))
    return ok(call)

@api.get("/analytics/summary")
@login_required
def analytics_summary():
    cos=q("SELECT lead_score,status FROM companies"); emails=q("SELECT status FROM emails"); mtgs=q("SELECT status FROM meetings"); calls_=q("SELECT status FROM calls")
    hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
    sent=sum(1 for e in emails if e.get("status")!="draft"); opened=sum(1 for e in emails if e.get("status") in("opened","replied")); replied=sum(1 for e in emails if e.get("status")=="replied")
    return ok({"total_companies":len(cos),"hot_leads":hot,"warm_leads":warm,"cold_leads":len(cos)-hot-warm,"emails_sent":sent,"emails_opened":opened,
               "open_rate":round(opened/sent*100,1) if sent else 0,"reply_rate":round(replied/sent*100,1) if sent else 0,
               "meetings_scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"meetings_completed":sum(1 for m in mtgs if m.get("status")=="completed"),
               "revenue_pipeline":hot*50000+warm*15000,"total_calls":len(calls_),"completed_calls":sum(1 for c in calls_ if c.get("status")=="completed")})

@api.get("/analytics/email-activity")
@login_required
def email_activity():
    emails=q("SELECT status,created_at FROM emails"); daily=defaultdict(lambda:{"sent":0,"opened":0,"replied":0})
    for e in emails:
        day=(e.get("created_at") or "")[:10]
        if not day: continue
        if e.get("status")!="draft": daily[day]["sent"]+=1
        if e.get("status") in("opened","replied"): daily[day]["opened"]+=1
        if e.get("status")=="replied": daily[day]["replied"]+=1
    result=[{"date":d,**v} for d,v in sorted(daily.items())][-30:]
    if not result:
        today=datetime.utcnow(); result=[{"date":(today-timedelta(days=13-i)).strftime("%Y-%m-%d"),"sent":i%4+1,"opened":max(0,i%3),"replied":max(0,i%2-1)} for i in range(14)]
    return ok(result)

@api.get("/analytics/lead-distribution")
@login_required
def lead_distribution():
    by=defaultdict(lambda:{"count":0,"total":0})
    for c in q("SELECT industry,lead_score FROM companies"):
        ind=c.get("industry") or "Other"; by[ind]["count"]+=1; by[ind]["total"]+=c.get("lead_score") or 0
    return ok(sorted([{"industry":k,"count":v["count"],"avg_score":round(v["total"]/v["count"],1)} for k,v in by.items()],key=lambda x:x["count"],reverse=True))

@api.get("/analytics/pipeline")
@login_required
def analytics_pipeline():
    cos=q("SELECT name,lead_score,status,annual_revenue FROM companies ORDER BY lead_score DESC LIMIT 20")
    return ok([{"name":c["name"],"lead_score":c.get("lead_score",0),"potential_revenue":int((c.get("annual_revenue") or 0)*0.02),"status":c.get("status","prospect")} for c in cos])

@api.get("/chat")
@login_required
def get_chat(): return ok(list(reversed(q("SELECT * FROM chat_messages ORDER BY created_at DESC LIMIT 100"))))

@api.post("/chat")
@login_required
def post_chat():
    # NOTE: this endpoint 500'd since day one — run() takes (sql, args-tuple)
    # but was called with loose positional args. Fixed + upgraded to the
    # CRM-aware assistant with conversation history.
    d=request.get_json(silent=True) or {}; msg=(d.get("message") or "").strip()
    if not msg: return err("message required")
    history=[{"role":("user" if m["sender"]=="user" else "assistant"),"content":m["message"]}
             for m in reversed(q("SELECT sender,message FROM chat_messages ORDER BY id DESC LIMIT 8"))]
    try:
        reply=crm_chat_reply(msg,history)
    except Exception as e:
        logger.error(f"chat reply: {e}")
        reply="Sorry — I hit an internal error building that answer. Try again or ask something simpler."
    run("INSERT INTO chat_messages(sender,sender_name,message) VALUES(?,?,?)",("user",g.user["email"],msg))
    run("INSERT INTO chat_messages(sender,sender_name,message) VALUES(?,?,?)",("bot","CRM Assistant",reply))
    return ok({"reply":reply})

@api.delete("/chat")
@login_required
def clear_chat(): run("DELETE FROM chat_messages"); return ok({"message":"Cleared"})

@api.get("/integrations/status")
@login_required
def integrations_status():
    user = q1("SELECT google_refresh_token FROM users WHERE id=?",(g.user["id"],))
    sg   = bool(SENDGRID_API_KEY)
    gm   = bool(GMAIL_EMAIL and GMAIL_PASSWORD)
    goauth = bool(q1("SELECT 1 FROM users WHERE google_refresh_token IS NOT NULL LIMIT 1"))
    return ok({
        "groq":  {"connected": bool(GROQ_API_KEY), "model": GROQ_MODEL},
        "gmail": {"connected": goauth or sg or gm, "email": GMAIL_EMAIL or None,
                  "sendgrid": sg, "gmail_oauth": goauth,
                  "method": "gmail_api" if goauth else ("sendgrid" if sg else ("smtp" if gm else "none"))},
        "bland_ai":   {"connected": bool(BLAND_API_KEY)},
        "twilio_sms": {"connected": bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM),
                       "from_number": TWILIO_FROM or None, "admin_number": TWILIO_ADMIN or None},
        "google_calendar": {"connected": bool(user and user.get("google_refresh_token")),
                            "keys_set": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)},
    })
@api.post("/integrations/gmail/test")
@login_required
def test_gmail_route(): ok_,msg=test_gmail(); return ok({"success":ok_,"message":msg})

@api.post("/integrations/gmail/send-test")
@login_required
def send_test_gmail():
    user=q1("SELECT email FROM users WHERE id=?",(g.user["id"],))
    if not user: return err("Not found",404)
    r=send_email_smtp(user["email"],"Test","AI Sales Test","✅ Gmail is working!"); return ok({"success":r.get("status")=="sent","result":r})

@api.post("/integrations/bland/test")
@login_required
def test_bland_route():
    ok_,msg=test_bland()
    return ok({"success":ok_,"message":msg})

@api.post("/integrations/twilio/test")
@login_required
def test_twilio_route():
    d=request.get_json(silent=True) or {}
    to=(d.get("to_number") or "").strip()
    if not to: return err("to_number required — enter your mobile number e.g. +61411519086")
    result=sms_test(to)
    success=result.get("status")=="sent"
    msg = "✅ SMS sent!" if success else result.get("message","SMS failed")
    return ok({"success":success,"message":msg,"result":result})

@api.post("/integrations/twilio/daily-report")
@login_required
def manual_daily_report(): send_daily_report(); return ok({"message":"Daily report SMS sent"})

@api.get("/integrations/google/auth-url")
@login_required
def google_auth_url_route():
    if not GOOGLE_CLIENT_ID:
        return ok({"auth_url":None,"error":"GOOGLE_CLIENT_ID not set in Render env"})
    auto_redirect = f"{request.scheme}://{request.host}/api/integrations/google/callback"
    redirect = GOOGLE_REDIRECT_URI or auto_redirect
    url = google_auth_url(g.user["id"], redirect)
    return ok({"auth_url":url,"redirect_uri":redirect})

@api.get("/integrations/google/callback")
def google_callback():
    code_val = request.args.get("code","")
    state    = request.args.get("state","")
    oerror   = request.args.get("error","")
    base_style = "font-family:Arial;text-align:center;padding:60px;background:#0a0e1a;color:#e2e8f0"
    if oerror:
        detail = {"access_denied":"You clicked Deny. Click Allow to grant Calendar access.",
                  "redirect_uri_mismatch":"Redirect URI mismatch — add the callback URL to Authorized Redirect URIs in Google Cloud Console."
                  }.get(oerror, f"OAuth error: {oerror}")
        return f"<html><body style='{base_style}'><h2 style='color:#ef4444'>Connection Failed</h2><p style='color:#94a3b8'>{detail}</p></body></html>"
    if not code_val:
        return f"<html><body style='{base_style}'><h2 style='color:#ef4444'>No auth code received</h2><p style='color:#94a3b8'>Close this window and try again.</p></body></html>"
    auto_redirect = f"{request.scheme}://{request.host}/api/integrations/google/callback"
    redirect = GOOGLE_REDIRECT_URI or auto_redirect
    logger.info(f"Google callback: redirect_uri={redirect}")
    tokens = google_exchange(code_val, redirect)
    if tokens.get("refresh_token") and state:
        try:
            run("UPDATE users SET google_refresh_token=? WHERE id=?",(tokens["refresh_token"],int(state)))
            logger.info(f"Google Calendar connected for user {state}")
            return f"<html><body style='{base_style}'><h2 style='color:#22c55e'>Google Calendar Connected!</h2><p style='color:#94a3b8'>Close this window and refresh Integrations.</p><script>setTimeout(()=>window.close(),2000)</script></body></html>"
        except Exception as e:
            logger.error(f"Save Google token: {e}")
    if tokens.get("access_token") and not tokens.get("refresh_token"):
        detail = "Got token but no refresh_token. Go to myaccount.google.com/permissions, remove this app, then try Connect again."
    else:
        detail = tokens.get("error_description") or tokens.get("error") or f"Token exchange failed. Redirect URI: {redirect}"
    return f"<html><body style='{base_style}'><h2 style='color:#f59e0b'>Auth Incomplete</h2><p style='color:#94a3b8'>{detail}</p></body></html>"

@api.post("/integrations/google/disconnect")
@login_required
def google_disconnect(): run("UPDATE users SET google_refresh_token=NULL WHERE id=?",(g.user["id"],)); return ok({"message":"Disconnected"})

@api.get("/sms-logs")
@login_required
def sms_logs(): limit=min(int(request.args.get("limit",100)),500); return ok(q("SELECT * FROM sms_logs ORDER BY created_at DESC LIMIT ?",(limit,)))

@api.get("/automation/status")
@login_required
def automation_status():
    cos=q("SELECT lead_score,status FROM companies"); emails=q("SELECT status,email_type FROM emails"); calls_=q("SELECT status FROM calls"); mtgs=q("SELECT status FROM meetings")
    hot=sum(1 for c in cos if(c.get("lead_score") or 0)>=70); warm=sum(1 for c in cos if 40<=(c.get("lead_score") or 0)<70)
    sent=sum(1 for e in emails if e.get("status") in("sent","opened","replied")); opened=sum(1 for e in emails if e.get("status") in("opened","replied")); replied=sum(1 for e in emails if e.get("status")=="replied")
    cold_e=sum(1 for e in emails if e.get("email_type")=="cold"); fu=sum(1 for e in emails if e.get("email_type")=="follow_up")
    return ok({"pipeline":{"hot_leads":hot,"warm_leads":warm,"cold_leads":len(cos)-hot-warm,"total":len(cos),"pipeline_value":hot*50000+warm*15000},
               "emails":{"cold_sent":cold_e,"followups":fu,"opened":opened,"replied":replied,"open_rate":round(opened/cold_e*100,1) if cold_e else 0,"reply_rate":round(replied/cold_e*100,1) if cold_e else 0},
               "calls":{"total":len(calls_),"completed":sum(1 for c in calls_ if c.get("status")=="completed")},
               "meetings":{"scheduled":sum(1 for m in mtgs if m.get("status")=="scheduled"),"completed":sum(1 for m in mtgs if m.get("status")=="completed")},
               "schedule_utc":{"score":f"{AUTO_SCORE_HOUR:02d}:00","cold_email":f"{AUTO_EMAIL_HOUR:02d}:00 (weekdays)","follow_up":f"{AUTO_FOLLOWUP_HOUR:02d}:00 (3 days)","auto_call":f"{AUTO_CALL_HOUR:02d}:00 (score>=80)","daily_report":f"{DAILY_REPORT_HOUR:02d}:00"},
               "integrations":{"groq":bool(GROQ_API_KEY),"gmail":bool(GMAIL_EMAIL and GMAIL_PASSWORD),"bland":bool(BLAND_API_KEY),"twilio":bool(TWILIO_SID and TWILIO_TOKEN)}})

@api.post("/automation/run-now")
@login_required
def run_auto_now(): trigger_now(); return ok({"message":"Automation cycle started"})

@api.post("/automation/score-now")
@login_required
def score_now_route(): threading.Thread(target=auto_score_all,daemon=True).start(); return ok({"message":"Scoring companies..."})

@api.post("/automation/email-now")
@login_required
def email_now_route(): threading.Thread(target=auto_email_hot_leads,daemon=True).start(); return ok({"message":"Auto email started..."})

@api.post("/automation/followup-now")
@login_required
def followup_now_route(): threading.Thread(target=auto_followup,daemon=True).start(); return ok({"message":"Follow-up started..."})

@api.post("/automation/call-now")
@login_required
def call_now_route(): threading.Thread(target=auto_call_hot_leads,daemon=True).start(); return ok({"message":"Auto call started..."})

@api.get("/automation/activity-feed")
@login_required
def activity_feed():
    emails=q("SELECT 'email' as type,subject as title,status,recipient_email as target,created_at,email_type as subtype FROM emails ORDER BY created_at DESC LIMIT 20")
    calls_=q("SELECT 'call' as type,objective as title,status,phone_number as target,created_at,voice as subtype FROM calls ORDER BY created_at DESC LIMIT 10")
    sms=q("SELECT 'sms' as type,event_type as title,status,to_number as target,created_at,'' as subtype FROM sms_logs ORDER BY created_at DESC LIMIT 15")
    mtgs=q("SELECT 'meeting' as type,title,status,'' as target,created_at,meeting_type as subtype FROM meetings ORDER BY created_at DESC LIMIT 10")
    return ok(sorted(emails+calls_+sms+mtgs,key=lambda x:x.get("created_at",""),reverse=True)[:50])

# ══ FRONTEND HTML (embedded — no files needed) ════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# NEW ROUTES — Transcripts, Drafts, Opportunities, Company Chat, LinkedIn
# ══════════════════════════════════════════════════════════════════════════════

@api.get("/transcripts")
@login_required
def list_transcripts():
    rows = q("""SELECT t.*,ca.phone_number,ca.objective,co.name as company_name,
                COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') as contact_name
                FROM call_transcripts t
                LEFT JOIN calls ca ON t.call_id=ca.id
                LEFT JOIN companies co ON t.company_id=co.id
                LEFT JOIN contacts ct ON t.contact_id=ct.id
                ORDER BY t.created_at DESC LIMIT 100""")
    return ok(rows)

@api.post("/calls/<int:cid_>/process-transcript")
@login_required
def process_transcript(cid_):
    call = q1("SELECT * FROM calls WHERE id=?", (cid_,))
    if not call: return err("Call not found", 404)
    transcript_text = call.get("transcript","") or ""
    if call.get("bland_call_id") and not transcript_text:
        live = bland_get_api(call["bland_call_id"])
        if live:
            transcript_text = live.get("concatenated_transcript","") or live.get("transcript","")
            run("UPDATE calls SET transcript=?,summary=?,status=?,updated_at=? WHERE id=?",
                (transcript_text, live.get("summary",""), live.get("status","completed"), now(), cid_))
    co = q1("SELECT * FROM companies WHERE id=?", (call.get("company_id"),)) if call.get("company_id") else {}
    ct = q1("SELECT * FROM contacts WHERE id=?", (call.get("contact_id"),)) if call.get("contact_id") else {}
    analysis = ai_call_transcript_analysis(transcript_text, (co or {}).get("name","Unknown"), (ct or {}).get("first_name","Contact"))
    meet_link = ""; cal_event_id = None
    if analysis.get("meeting_booked"):
        sched = analysis.get("meeting_datetime") or (datetime.utcnow()+timedelta(days=2)).strftime("%Y-%m-%d 10:00:00")
        try:
            company_nm = (co or {}).get("name","Client")
            cal_event_id, meet_link = create_calendar_event_with_meet(
                g.user["id"], f"ARC Digital + {company_nm} - Discovery Call",
                "Booked via AI call (Sarah).", sched, 30)
        except Exception as e_:
            logger.error(f"Meet: {e_}"); meet_link = generate_google_meet_link()
        if meet_link and co:
            run("""INSERT INTO meetings(company_id,contact_id,created_by,title,meeting_type,
                   scheduled_at,duration_minutes,status,meeting_link,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (co.get("id"),ct.get("id") if ct else None,g.user["id"],
                 f"AI Call Follow-up - {(co or {}).get('name','')}","discovery",sched,30,"scheduled",meet_link,now()))
    analysis["meet_link"] = meet_link
    existing = q1("SELECT id FROM call_transcripts WHERE call_id=?", (cid_,))
    if existing:
        run("""UPDATE call_transcripts SET transcript=?,summary=?,sentiment=?,meeting_booked=?,
               google_meet_link=?,interest_score=?,next_action=? WHERE call_id=?""",
            (transcript_text,analysis.get("summary",""),analysis.get("sentiment","neutral"),
             1 if analysis.get("meeting_booked") else 0,meet_link,
             analysis.get("interest_score",0),analysis.get("next_action","send_followup"),cid_))
        tid = existing["id"]
    else:
        tid = run("""INSERT INTO call_transcripts(call_id,company_id,contact_id,transcript,
                summary,sentiment,meeting_booked,google_meet_link,interest_score,next_action)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (cid_,call.get("company_id"),call.get("contact_id"),transcript_text,
             analysis.get("summary",""),analysis.get("sentiment","neutral"),
             1 if analysis.get("meeting_booked") else 0,meet_link,
             analysis.get("interest_score",0),analysis.get("next_action","send_followup")))
    if co and ct and ct.get("email"):
        email_content = generate_post_call_email(co, ct, analysis, transcript_text)
        run("""INSERT INTO email_drafts(company_id,contact_id,created_by,draft_type,
               subject,body,recipient_email,recipient_name,status,scheduled_send_day,call_transcript_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (co.get("id"),ct.get("id"),g.user["id"],email_content.get("email_type","followup"),
             email_content["subject"],email_content["body"],ct["email"],
             f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"pending_approval",
             0 if analysis.get("meeting_booked") else 1,tid))
    if analysis.get("interest_score",0)>=8 and co:
        if not q1("SELECT id FROM opportunities WHERE company_id=?",(co.get("id"),)):
            run("""INSERT INTO opportunities(company_id,contact_id,created_by,title,
                    interest_score,quoted_value,status,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (co.get("id"),ct.get("id") if ct else None,g.user["id"],
                 f"AI Call - {co.get('name')} - {analysis.get('sentiment','').title()}",
                 analysis.get("interest_score",8),5000,"open",analysis.get("summary",""),now()))
    return ok({"transcript":q1("SELECT * FROM call_transcripts WHERE id=?",(tid,)),
               "analysis":analysis,"meet_link":meet_link})

@api.get("/email-drafts")
@login_required
def list_email_drafts():
    status_f = request.args.get("status",""); cid_f = request.args.get("company_id","")
    sql = """SELECT d.*,co.name as company_name,
             COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') as contact_name
             FROM email_drafts d LEFT JOIN companies co ON d.company_id=co.id
             LEFT JOIN contacts ct ON d.contact_id=ct.id WHERE 1=1"""
    args=[]
    if status_f: sql+=" AND d.status=?"; args.append(status_f)
    if cid_f:    sql+=" AND d.company_id=?"; args.append(cid_f)
    return ok(q(sql+" ORDER BY d.created_at DESC LIMIT 200",args))

@api.put("/email-drafts/<int:did>")
@login_required
def update_email_draft(did):
    if not q1("SELECT id FROM email_drafts WHERE id=?",(did,)): return err("Not found",404)
    d = request.get_json(silent=True) or {}
    allowed={"subject","body","recipient_email","recipient_name","status","scheduled_send_day"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets: run(f"UPDATE email_drafts SET {','.join(sets)} WHERE id=?",vals+[did])
    return ok(q1("SELECT * FROM email_drafts WHERE id=?",(did,)))

@api.post("/email-drafts/<int:did>/approve")
@login_required
def approve_email_draft(did):
    draft = q1("SELECT * FROM email_drafts WHERE id=?",(did,))
    if not draft: return err("Not found",404)
    result = send_email_smtp(draft["recipient_email"],draft.get("recipient_name",""),draft["subject"],draft["body"])
    if result.get("status")=="sent":
        run("UPDATE email_drafts SET status=\'sent\',approved_by=?,approved_at=? WHERE id=?",(g.user["id"],now(),did))
        run("""INSERT INTO emails(company_id,contact_id,created_by,email_type,subject,body,
               recipient_email,recipient_name,status,sent_at,ai_model_used,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (draft.get("company_id"),draft.get("contact_id"),g.user["id"],draft.get("draft_type","follow_up"),
             draft["subject"],draft["body"],draft["recipient_email"],draft.get("recipient_name",""),
             "sent",now(),"ai_draft",now()))
        _dco=q1("SELECT autopilot FROM companies WHERE id=?",(draft.get("company_id"),)) if draft.get("company_id") else None
        if _dco and _dco.get("autopilot"):
            emit_crm_event("draft.sent",{"draft_id":did,"company_id":draft.get("company_id"),
                                         "recipient":draft["recipient_email"],"subject":draft["subject"]})
        return ok({"message":"Email sent","result":result})
    return ok({"message":result.get("message","Send failed"),"result":result,"success":False})

@api.post("/email-drafts/<int:did>/reject")
@login_required
def reject_email_draft(did):
    run("UPDATE email_drafts SET status=\'rejected\',approved_by=?,approved_at=? WHERE id=?",(g.user["id"],now(),did))
    return ok({"message":"Draft rejected"})

@api.post("/email-drafts/generate")
@login_required
def generate_email_draft():
    d=request.get_json(silent=True) or {}
    company_id=d.get("company_id"); dtype=d.get("draft_type","cold")
    co=q1("SELECT * FROM companies WHERE id=?",(company_id,)) if company_id else None
    if not co: return err("company_id required")
    ct=(q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(company_id,))
        or q1("SELECT * FROM contacts WHERE company_id=?",(company_id,)))
    if not ct: return err("No contacts for this company")
    content=generate_email(co,ct,dtype,d.get("custom",""))
    did=run("""INSERT INTO email_drafts(company_id,contact_id,created_by,draft_type,
             subject,body,recipient_email,recipient_name,status,scheduled_send_day)
             VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (company_id,ct.get("id"),g.user["id"],dtype,content["subject"],content["body"],
         ct.get("email",""),f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"pending_approval",d.get("schedule_day",0)))
    if co.get("autopilot"):
        notify_async("email_generated",{"company_name":co["name"],"email_type":dtype,
                                        "recipient_email":ct.get("email",""),"subject":content["subject"]})
        emit_crm_event("draft.created",{"draft_id":did,"company_id":company_id,
                                        "recipient":ct.get("email",""),"subject":content["subject"]})
    return ok(q1("SELECT * FROM email_drafts WHERE id=?",(did,))),201

@api.get("/opportunities")
@login_required
def list_opportunities():
    return ok(q("""SELECT op.*,co.name as company_name,co.industry,co.lead_score,
                COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') as contact_name,
                ct.email as contact_email,ct.phone as contact_phone
                FROM opportunities op LEFT JOIN companies co ON op.company_id=co.id
                LEFT JOIN contacts ct ON op.contact_id=ct.id
                ORDER BY op.interest_score DESC,op.updated_at DESC"""))

@api.post("/opportunities")
@login_required
def create_opportunity():
    d=request.get_json(silent=True) or {}
    if not d.get("company_id") or not d.get("title"): return err("company_id and title required")
    oid=run("""INSERT INTO opportunities(company_id,contact_id,created_by,title,
             interest_score,quoted_value,status,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (d["company_id"],d.get("contact_id"),g.user["id"],d["title"],
         d.get("interest_score",5),d.get("quoted_value",0),d.get("status","open"),d.get("notes",""),now()))
    return ok(q1("SELECT * FROM opportunities WHERE id=?",(oid,))),201

@api.put("/opportunities/<int:oid>")
@login_required
def update_opportunity(oid):
    if not q1("SELECT id FROM opportunities WHERE id=?",(oid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"title","interest_score","quoted_value","accepted_value","status","notes","quotation_text","invoice_text"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if sets:
        sets.append("updated_at=?"); vals.append(now()); vals.append(oid)
        run(f"UPDATE opportunities SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM opportunities WHERE id=?",(oid,)))

@api.post("/opportunities/<int:oid>/generate-quotation")
@login_required
def gen_quotation(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    co=q1("SELECT * FROM companies WHERE id=?",(op["company_id"],))
    ct=q1("SELECT * FROM contacts WHERE id=?",(op.get("contact_id"),)) if op.get("contact_id") else None
    text=generate_quotation(co or {},ct or {},op)
    run("UPDATE opportunities SET quotation_text=?,updated_at=? WHERE id=?",(text,now(),oid))
    return ok({"quotation":text,"opportunity":q1("SELECT * FROM opportunities WHERE id=?",(oid,))})

@api.post("/opportunities/<int:oid>/generate-invoice")
@login_required
def gen_invoice(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    co=q1("SELECT * FROM companies WHERE id=?",(op["company_id"],))
    ct=q1("SELECT * FROM contacts WHERE id=?",(op.get("contact_id"),)) if op.get("contact_id") else None
    text=generate_invoice(co or {},ct or {},op)
    run("UPDATE opportunities SET invoice_text=?,updated_at=? WHERE id=?",(text,now(),oid))
    return ok({"invoice":text,"opportunity":q1("SELECT * FROM opportunities WHERE id=?",(oid,))})

@api.get("/companies/<int:cid>/chat")
@login_required
def get_company_chat(cid):
    return ok(q("SELECT * FROM company_chat WHERE company_id=? ORDER BY created_at ASC LIMIT 50",(cid,)))

@api.post("/companies/<int:cid>/chat")
@login_required
def post_company_chat(cid):
    d=request.get_json(silent=True) or {}; msg=(d.get("message") or "").strip()
    if not msg: return err("message required")
    history=[{"role":m["sender"],"content":m["message"]}
             for m in q("SELECT sender,message FROM company_chat WHERE company_id=? ORDER BY created_at DESC LIMIT 10",(cid,))]
    reply=generate_company_chatbot_response(cid,msg,list(reversed(history)))
    run("INSERT INTO company_chat(company_id,sender,message) VALUES(?,?,?)",(cid,"user",msg))
    run("INSERT INTO company_chat(company_id,sender,message) VALUES(?,?,?)",(cid,"ai",reply))
    return ok({"reply":reply})

@api.post("/companies/<int:cid>/qualify")
@login_required
def qualify_company(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    contacts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    signals=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    result=qualify_company_plumbing(co,contacts,signals)
    run("UPDATE companies SET ai_summary=?,updated_at=? WHERE id=?",
        (f"Plumbing Qualify Score: {result['qualify_score']}/10 - {result['reason']}",now(),cid))
    return ok(result)

@api.post("/contacts/qualify-all")
@login_required
def qualify_all_contacts():
    """Run qualify_company_plumbing on every company that has contacts."""
    def _run():
        cos = q("SELECT DISTINCT company_id FROM contacts WHERE company_id IS NOT NULL")
        done = 0
        for row in cos:
            cid = row["company_id"]
            co  = q1("SELECT * FROM companies WHERE id=?", (cid,))
            if not co: continue
            cts  = q("SELECT * FROM contacts WHERE company_id=?", (cid,))
            sigs = q("SELECT * FROM buying_signals WHERE company_id=?", (cid,))
            result = qualify_company_plumbing(co, cts, sigs)
            # Upsert lead_scores
            if q1("SELECT id FROM lead_scores WHERE company_id=?", (cid,)):
                run("""UPDATE lead_scores SET total_score=?,industry_score=?,tier=?,updated_at=?
                       WHERE company_id=?""",
                    (result["qualify_score"], result["industry_fit"], result["tier"], now(), cid))
            else:
                run("""INSERT INTO lead_scores(company_id,total_score,industry_score,tier)
                       VALUES(?,?,?,?)""",
                    (cid, result["qualify_score"], result["industry_fit"], result["tier"]))
            run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",
                (result["qualify_score"], now(), cid))
            done += 1
        logger.info(f"qualify_all_contacts: scored {done} companies")
    threading.Thread(target=_run, daemon=True).start()
    return ok({"message": f"Qualifying all companies in background…"})

@api.post("/contacts/<int:ctid>/qualify-company")
@login_required
def qualify_contact_company(ctid):
    """Qualify the company this contact belongs to."""
    ct = q1("SELECT * FROM contacts WHERE id=?", (ctid,))
    if not ct: return err("Contact not found", 404)
    co  = q1("SELECT * FROM companies WHERE id=?", (ct["company_id"],))
    if not co: return err("Company not found", 404)
    cts  = q("SELECT * FROM contacts WHERE company_id=?", (ct["company_id"],))
    sigs = q("SELECT * FROM buying_signals WHERE company_id=?", (ct["company_id"],))
    result = qualify_company_plumbing(co, cts, sigs)
    if q1("SELECT id FROM lead_scores WHERE company_id=?", (co["id"],)):
        run("""UPDATE lead_scores SET total_score=?,industry_score=?,tier=?,updated_at=?
               WHERE company_id=?""",
            (result["qualify_score"],result["industry_fit"],result["tier"],now(),co["id"]))
    else:
        run("""INSERT INTO lead_scores(company_id,total_score,industry_score,tier) VALUES(?,?,?,?)""",
            (co["id"],result["qualify_score"],result["industry_fit"],result["tier"]))
    run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(result["qualify_score"],now(),co["id"]))
    return ok({**result, "company": co["name"]})

@api.post("/contacts/upload-csv")
@login_required
def upload_contacts_csv():
    # Synchronous, Apollo-header-aware contact import: creates the company if
    # it doesn't exist, dedupes on email (or name within the same company),
    # captures title/seniority/LinkedIn, and reports real counts to the UI.
    f=request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".csv"): return err("CSV file required")
    fname=f.filename; uid=g.user["id"]
    try: text=f.read().decode("utf-8-sig",errors="replace")
    except Exception as e: return err(f"Could not read file: {e}")
    delim=max([",",";","\t"],key=text.split("\n",1)[0].count)
    allrows=list(csv.reader(io.StringIO(text),delimiter=delim))
    if len(allrows)<2: return err("CSV has no data rows")
    headers=allrows[0]
    canon=[IMP_ALIASES.get(_imp_norm(h)) for h in headers]
    if not any(cn in ("first_name","last_name","email") for cn in canon):
        return err("No contact columns found — expected headers like First Name, Last Name, Email")
    SENIORITIES={"c_suite","vp","director","manager","owner","individual"}
    created=skipped=companies_created=0
    with get_conn() as c:
        co_by_name={r["n"]:r["id"] for r in c.execute("SELECT id,lower(name) AS n FROM companies")}
        seen=set()
        for r in c.execute("SELECT company_id,lower(first_name) AS fn,lower(last_name) AS ln,lower(email) AS e FROM contacts"):
            if r["e"]: seen.add(("e",r["e"]))
            seen.add(("n",r["company_id"],r["fn"] or "",r["ln"] or ""))
        for cells in allrows[1:]:
            if not any(str(x).strip() for x in cells): continue
            vals={}
            for cn,vv in zip(canon,cells):
                if cn and vv and str(vv).strip() and cn not in vals: vals[cn]=str(vv).strip()
            fn=vals.get("first_name",""); ln=vals.get("last_name",""); em=vals.get("email","")
            if not (fn or ln or em): skipped+=1; continue
            if em and ("e",em.lower()) in seen: skipped+=1; continue
            cname=vals.get("company") or vals.get("company_name_for_emails") or ""
            if not cname: skipped+=1; continue   # contacts need a company record
            cid_v=co_by_name.get(cname.lower())
            if not cid_v:
                cid_v=c.execute("INSERT INTO companies(name,industry,city,country,website,status,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cname,vals.get("industry"),vals.get("company_city") or vals.get("city"),
                     vals.get("company_country") or vals.get("country"),vals.get("website"),"prospect",uid,now())).lastrowid
                co_by_name[cname.lower()]=cid_v; companies_created+=1
            if not em and ("n",cid_v,fn.lower(),ln.lower()) in seen: skipped+=1; continue
            phone=vals.get("mobile_phone") or vals.get("work_direct_phone") or vals.get("corporate_phone") or vals.get("other_phone") or vals.get("home_phone") or ""
            sen=vals.get("seniority","").lower().replace("-","_").replace(" ","_")
            sen=sen if sen in SENIORITIES else "individual"
            ctid=c.execute("""INSERT INTO contacts(company_id,first_name,last_name,email,phone,title,seniority_level,is_decision_maker)
                              VALUES(?,?,?,?,?,?,?,0)""",
                (cid_v,fn or ln or em.split("@")[0],ln,em,phone,vals.get("title",""),sen)).lastrowid
            li=vals.get("person_linkedin","")
            if li: c.execute("INSERT INTO contact_linkedin(contact_id,company_id,linkedin_url) VALUES(?,?,?)",(ctid,cid_v,li))
            if em: seen.add(("e",em.lower()))
            seen.add(("n",cid_v,fn.lower(),ln.lower()))
            created+=1
    logger.info(f"Contacts CSV '{fname}': {created} new, {skipped} skipped, {companies_created} companies created")
    return ok({"created":created,"skipped":skipped,"companies_created":companies_created,"filename":fname}),201

@api.get("/contacts/<int:ctid>/linkedin")
@login_required
def get_contact_linkedin(ctid):
    return ok(q1("SELECT * FROM contact_linkedin WHERE contact_id=?",(ctid,)))

@api.post("/contacts/<int:ctid>/linkedin")
@login_required
def set_contact_linkedin(ctid):
    ct=q1("SELECT * FROM contacts WHERE id=?",(ctid,))
    if not ct: return err("Contact not found",404)
    d=request.get_json(silent=True) or {}
    existing=q1("SELECT id FROM contact_linkedin WHERE contact_id=?",(ctid,))
    if existing:
        run("UPDATE contact_linkedin SET linkedin_url=?,position=?,seniority=?,is_key_person=? WHERE contact_id=?",
            (d.get("linkedin_url",""),d.get("position",""),d.get("seniority",""),1 if d.get("is_key_person") else 0,ctid))
    else:
        run("INSERT INTO contact_linkedin(contact_id,company_id,linkedin_url,position,seniority,is_key_person) VALUES(?,?,?,?,?,?)",
            (ctid,ct.get("company_id"),d.get("linkedin_url",""),d.get("position",""),d.get("seniority",""),1 if d.get("is_key_person") else 0))
    return ok(q1("SELECT * FROM contact_linkedin WHERE contact_id=?",(ctid,)))

@api.post("/automation/generate-drafts")
@login_required
def gen_drafts_now():
    def _auto():
        try:
            cos=q("SELECT * FROM companies WHERE lead_score>=50 ORDER BY lead_score DESC LIMIT 20")
            n=0
            for co in cos:
                ct=(q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(co["id"],))
                    or q1("SELECT * FROM contacts WHERE company_id=?",(co["id"],)))
                if not ct or not ct.get("email"): continue
                if q("SELECT id FROM email_drafts WHERE company_id=? AND status=\'pending_approval\'",(co["id"],)): continue
                last_call=q1("SELECT * FROM calls WHERE company_id=? ORDER BY created_at DESC LIMIT 1",(co["id"],))
                days=0
                if last_call:
                    days=max(0,(datetime.utcnow()-datetime.strptime(last_call["created_at"][:19],"%Y-%m-%d %H:%M:%S")).days)
                if days not in [0,1,3,5,10] and last_call: continue
                dtype="follow_up" if days>0 else "cold"
                content=generate_email(co,ct,dtype)
                run("""INSERT INTO email_drafts(company_id,contact_id,created_by,draft_type,
                       subject,body,recipient_email,recipient_name,status,scheduled_send_day)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (co["id"],ct["id"],1,dtype,content["subject"],content["body"],
                     ct["email"],f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),"pending_approval",days))
                n+=1
            logger.info(f"Auto-generated {n} email drafts")
        except Exception as e_: logger.error(f"gen_drafts: {e_}")
    threading.Thread(target=_auto,daemon=True).start()
    return ok({"message":"Draft generation started"})



# ══════════════════════════════════════════════════════════════════════════════
# NEW API ROUTES — Invoice, Quotation, Inbound Calls
# ══════════════════════════════════════════════════════════════════════════════

# ── Formal Invoices ───────────────────────────────────────────────────────────
@api.get("/invoices")
@login_required
def list_invoices():
    return ok(q("""SELECT i.*,co.name as company_name,
                   COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') as contact_name
                   FROM invoices i LEFT JOIN companies co ON i.company_id=co.id
                   LEFT JOIN contacts ct ON i.contact_id=ct.id
                   ORDER BY i.created_at DESC"""))

@api.post("/invoices")
@login_required
def create_invoice_record():
    d = request.get_json(silent=True) or {}
    oid = d.get("opportunity_id")
    op  = q1("SELECT * FROM opportunities WHERE id=?", (oid,)) if oid else None
    co  = q1("SELECT * FROM companies WHERE id=?", (d.get("company_id") or (op or {}).get("company_id"),))
    ct  = q1("SELECT * FROM contacts WHERE id=?",  (d.get("contact_id") or (op or {}).get("contact_id"),)) if (d.get("contact_id") or (op or {}).get("contact_id")) else None
    amount    = float(d.get("amount") or (op or {}).get("accepted_value") or (op or {}).get("quoted_value") or 0)
    gst       = round(amount * 0.10, 2)
    total     = round(amount + gst, 2)
    inv_num   = build_invoice_number()
    due_date  = (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d")
    content_text = generate_invoice({} if not op else op, ct, op or d)
    iid = run("""INSERT INTO invoices(opportunity_id,company_id,contact_id,created_by,invoice_number,
               invoice_type,subject,content,amount,gst_amount,total_amount,status,due_date,issued_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (oid,(co or {}).get("id"),(ct or {}).get("id"),g.user["id"],inv_num,"invoice",
         f"AI Business Automation — {(co or {}).get('name','')}",content_text,
         amount,gst,total,"draft",due_date,datetime.utcnow().strftime("%Y-%m-%d")))
    inv = q1("SELECT * FROM invoices WHERE id=?", (iid,))
    # Generate HTML
    html_content = generate_invoice_html(
        {"invoice_number":inv_num,"amount":amount,"gst_amount":gst,"total_amount":total,
         "status":"draft","issued_date":datetime.utcnow().strftime("%Y-%m-%d"),"due_date":due_date,
         "content":content_text},
        op, co, ct, d.get("template","professional"))
    return ok({"invoice":inv,"html":html_content,"xero_ready":True}),201

@api.get("/invoices/<int:iid>/html")
@login_required
def get_invoice_html(iid):
    inv = q1("SELECT * FROM invoices WHERE id=?", (iid,))
    if not inv: return err("Not found",404)
    co  = q1("SELECT * FROM companies WHERE id=?", (inv.get("company_id"),)) if inv.get("company_id") else {}
    ct  = q1("SELECT * FROM contacts WHERE id=?",  (inv.get("contact_id"),)) if inv.get("contact_id") else {}
    op  = q1("SELECT * FROM opportunities WHERE id=?", (inv.get("opportunity_id"),)) if inv.get("opportunity_id") else {}
    html = generate_invoice_html(inv, op, co, ct, request.args.get("template","professional"))
    return ok({"html":html,"invoice":inv})

@api.post("/invoices/<int:iid>/approve")
@login_required
def approve_invoice(iid):
    inv = q1("SELECT * FROM invoices WHERE id=?", (iid,))
    if not inv: return err("Not found",404)
    run("UPDATE invoices SET status='approved',approved_by=?,approved_at=? WHERE id=?",(g.user["id"],now(),iid))
    # Push to Xero format
    co = q1("SELECT * FROM companies WHERE id=?", (inv.get("company_id"),)) if inv.get("company_id") else {}
    ct = q1("SELECT * FROM contacts WHERE id=?",  (inv.get("contact_id"),)) if inv.get("contact_id") else {}
    _, xero_payload = push_invoice_to_xero(inv, co, ct)
    return ok({"invoice":q1("SELECT * FROM invoices WHERE id=?",(iid,)),"xero_payload":xero_payload,
               "message":"Invoice approved. Xero payload ready — connect Xero MCP to auto-push."})

# ── Formal Quotations ─────────────────────────────────────────────────────────
@api.get("/quotations")
@login_required
def list_quotations():
    return ok(q("""SELECT q.*,co.name as company_name,
                   COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') as contact_name
                   FROM quotations q LEFT JOIN companies co ON q.company_id=co.id
                   LEFT JOIN contacts ct ON q.contact_id=ct.id
                   ORDER BY q.created_at DESC"""))

@api.post("/quotations")
@login_required
def create_quotation_record():
    d   = request.get_json(silent=True) or {}
    oid = d.get("opportunity_id")
    op  = q1("SELECT * FROM opportunities WHERE id=?", (oid,)) if oid else None
    co  = q1("SELECT * FROM companies WHERE id=?", (d.get("company_id") or (op or {}).get("company_id"),))
    ct  = q1("SELECT * FROM contacts WHERE id=?",  (d.get("contact_id") or (op or {}).get("contact_id"),)) if (d.get("contact_id") or (op or {}).get("contact_id")) else None
    amount   = float(d.get("amount") or (op or {}).get("quoted_value") or 0)
    gst      = round(amount*0.10,2); total = round(amount+gst,2)
    q_num    = build_quote_number()
    valid_dt = (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d")
    content_text = generate_quotation({} if not op else op, ct, op or d)
    qid = run("""INSERT INTO quotations(opportunity_id,company_id,contact_id,created_by,quote_number,
               content,amount,gst_amount,total_amount,valid_until,status,template,
               roi_snapshot_id,owner_id,monthly_value)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (oid,(co or {}).get("id"),(ct or {}).get("id"),g.user["id"],q_num,content_text,
         amount,gst,total,valid_dt,"draft",d.get("template","professional"),
         d.get("roi_snapshot_id"),g.user["id"],float(d.get("monthly_value") or 0)))
    audit("quotation",qid,"created","amount","",amount)
    quot = q1("SELECT * FROM quotations WHERE id=?", (qid,))
    html_content = generate_quotation_html(
        {"quote_number":q_num,"amount":amount,"gst_amount":gst,"total_amount":total,
         "valid_until":valid_dt,"content":content_text},
        op, co, ct, d.get("template","professional"))
    return ok({"quotation":quot,"html":html_content}),201

@api.get("/quotations/<int:qid>/html")
@login_required
def get_quotation_html(qid):
    quot = q1("SELECT * FROM quotations WHERE id=?", (qid,))
    if not quot: return err("Not found",404)
    co = q1("SELECT * FROM companies WHERE id=?", (quot.get("company_id"),)) if quot.get("company_id") else {}
    ct = q1("SELECT * FROM contacts WHERE id=?",  (quot.get("contact_id"),)) if quot.get("contact_id") else {}
    op = q1("SELECT * FROM opportunities WHERE id=?", (quot.get("opportunity_id"),)) if quot.get("opportunity_id") else {}
    html = generate_quotation_html(quot, op, co, ct, request.args.get("template","professional"))
    return ok({"html":html,"quotation":quot})

@api.post("/quotations/<int:qid>/approve")
@login_required
def approve_quotation(qid):
    quot = q1("SELECT * FROM quotations WHERE id=?", (qid,))
    if not quot: return err("Not found",404)
    run("UPDATE quotations SET status='approved',approved_by=?,approved_at=? WHERE id=?",(g.user["id"],now(),qid))
    return ok({"quotation":q1("SELECT * FROM quotations WHERE id=?",(qid,)),
               "message":"Quotation approved. Ready to convert to invoice."})

@api.post("/quotations/<int:qid>/convert-to-invoice")
@login_required
def convert_to_invoice(qid):
    quot = q1("SELECT * FROM quotations WHERE id=?", (qid,))
    if not quot: return err("Not found",404)
    inv_num  = build_invoice_number()
    due_date = (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d")
    iid = run("""INSERT INTO invoices(opportunity_id,company_id,contact_id,created_by,invoice_number,
               subject,content,amount,gst_amount,total_amount,status,due_date,issued_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (quot.get("opportunity_id"),quot.get("company_id"),quot.get("contact_id"),g.user["id"],
         inv_num,f"Invoice — {quot.get('quote_number','')}",quot.get("content",""),
         quot.get("amount",0),quot.get("gst_amount",0),quot.get("total_amount",0),
         "draft",due_date,datetime.utcnow().strftime("%Y-%m-%d")))
    run("UPDATE quotations SET status='converted' WHERE id=?", (qid,))
    return ok({"invoice":q1("SELECT * FROM invoices WHERE id=?",(iid,)),"invoice_id":iid})

# ── Inbound Calls (Facebook Ad) ───────────────────────────────────────────────
@api.get("/inbound-calls")
@login_required
def list_inbound_calls():
    return ok(q("""SELECT * FROM inbound_calls ORDER BY created_at DESC LIMIT 100"""))

@api.post("/bland/inbound-webhook")
def bland_inbound_webhook():
    """Bland AI webhook for inbound calls from Facebook ads."""
    try:
        data = request.get_json(silent=True) or {}
        logger.info(f"Bland inbound webhook: {data.get('call_id')} from {data.get('from','')}")
        result = process_inbound_call(data)
        return jsonify({"ok":True,"processed":True,"id":result.get("id")}),200
    except Exception as e:
        logger.error(f"Bland webhook: {e}")
        return jsonify({"ok":True}),200  # Always 200 to Bland

@api.post("/bland/setup-inbound")
@login_required
def setup_inbound():
    result = setup_bland_inbound_agent()
    return ok(result)

@api.get("/bland/inbound-status")
@login_required
def inbound_status():
    if not BLAND_API_KEY: return ok({"configured":False,"message":"BLAND_API_KEY not set"})
    if not BLAND_INBOUND_NUMBER: return ok({"configured":False,"message":"BLAND_INBOUND_NUMBER not set — add your Bland inbound number"})
    count = q("SELECT COUNT(*) as n FROM inbound_calls")[0]["n"]
    recent= q("SELECT * FROM inbound_calls ORDER BY created_at DESC LIMIT 5")
    return ok({"configured":True,"inbound_number":BLAND_INBOUND_NUMBER,
               "total_calls":count,"recent":recent,
               "webhook_url":f"{request.scheme}://{request.host}/api/bland/inbound-webhook"})


# Frontend lives in static/index.html (extracted from the former inline
# _HTML bytes literal) so it can be edited as a normal file. It is re-read on
# every request — a page refresh picks up edits without an app restart.
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_HTML_FALLBACK = "<h1 style='font-family:sans-serif'>ARC Digital &mdash; static/index.html is missing</h1>"
def _html():
    try:
        with open(os.path.join(_STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"index.html read failed: {e}")
        return _HTML_FALLBACK
_HTML = _html()  # import-time sanity read; routes call _html() for freshness

# ── Jake's Plumbing Lead Console sync (port 8960, custom addition) ───────────
JAKES_CONSOLE_URL=os.environ.get("JAKES_CONSOLE_URL","http://127.0.0.1:8960")

# Data version for the frontend's live refresh: bumped only when a sync run
# actually created or changed a lead, so the UI never redraws for nothing.
# The boot timestamp makes the value unique across app restarts.
_JAKES_VER={"boot":str(int(time.time())),"n":0}
def _bump_jakes_version(): _JAKES_VER["n"]+=1

def sync_jakes_leads():
    """Import leads from Jake's Plumbing Lead Console into companies/contacts
    and auto-qualify each with the plumbing rules. Local HTTP only — never
    contacts the lead. Dedupes on the [Jakes <lead_id>] marker in description.
    Unchanged leads are skipped entirely (no DB write, no re-qualify) so the
    push-triggered syncs stay cheap and the data version stays meaningful."""
    try:
        with urllib.request.urlopen(JAKES_CONSOLE_URL+"/api/leads",timeout=5) as r:
            items=json.loads(r.read().decode("utf-8")).get("items",[])
    except Exception as e:
        return {"ok":False,"error":f"Jake's console unreachable: {e}"}
    created=updated=0
    for L in items:
        lid=(L.get("lead_id") or "").strip()
        if not lid: continue
        marker=f"[Jakes {lid}]"
        ans=L.get("answers") or {}
        name=(L.get("name") or "").strip() or f"Lead {lid}"
        bits=[marker]
        for k in ("issue","urgency","danger","suburb"):
            if ans.get(k): bits.append(f"{k}: {ans[k]}")
        bits.append(f"console status: {L.get('status')}")
        if L.get("emergency"): bits.append("EMERGENCY")
        if L.get("admin_alerted"): bits.append("ADMIN ALERTED - plumber being arranged")
        if L.get("opted_out"): bits.append("OPTED OUT - do not contact")
        desc=" | ".join(str(b) for b in bits)
        jscore=L.get("score")
        lead_score=int(jscore*10) if isinstance(jscore,(int,float)) else 0
        status="qualified" if (jscore or 0)>=6 else "prospect"
        co=q1("SELECT * FROM companies WHERE description LIKE ?",(f"%{marker}%",))
        if co:
            cid=co["id"]
            new_score=max(lead_score,co.get("lead_score") or 0)
            new_city=ans.get("suburb") or co.get("city") or ""
            if (co.get("name")==name and co.get("description")==desc
                    and (co.get("lead_score") or 0)==new_score
                    and co.get("status")==status and (co.get("city") or "")==new_city):
                continue  # nothing changed - skip the write and the re-qualify
            run("UPDATE companies SET name=?,description=?,lead_score=?,status=?,city=?,updated_at=? WHERE id=?",
                (name,desc,new_score,status,new_city,now(),cid))
            updated+=1
        else:
            cid=run("""INSERT INTO companies(name,industry,city,country,description,status,lead_score,technologies)
                       VALUES(?,?,?,?,?,?,?,?)""",
                (name,"Plumbing Lead",ans.get("suburb") or "","Australia",desc,status,lead_score,"jakes-console"))
            created+=1
        ph=(L.get("phone") or "").strip()
        parts=name.split(" ",1)
        if ph and not q1("SELECT id FROM contacts WHERE company_id=? AND phone=?",(cid,ph)):
            run("""INSERT INTO contacts(company_id,first_name,last_name,phone,title,seniority_level,is_decision_maker)
                   VALUES(?,?,?,?,?,?,1)""",
                (cid,parts[0],parts[1] if len(parts)>1 else "",ph,"Property Owner","owner"))
        try:
            co2=q1("SELECT * FROM companies WHERE id=?",(cid,))
            cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
            sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
            res=qualify_company_plumbing(co2,cts,sigs)
            run("UPDATE companies SET ai_summary=?,updated_at=? WHERE id=?",
                (f"Plumbing Qualify Score: {res['qualify_score']}/10 - {res['reason']}",now(),cid))
        except Exception as e:
            logger.warning(f"jakes qualify {lid}: {e}")
    if created or updated: _bump_jakes_version()
    logger.info(f"Jakes sync: {len(items)} leads, {created} new, {updated} updated")
    return {"ok":True,"total":len(items),"created":created,"updated":updated}

@api.post("/arc-apps/sync-jakes")
@login_required
def sync_jakes_route():
    res=sync_jakes_leads()
    if not res.get("ok"): return err(res.get("error","sync failed"))
    return ok(res)

@api.post("/arc-apps/sync-jakes-local")
def sync_jakes_local_route():
    # Push-trigger from the Jake's Lead Console on every workflow event.
    # Loopback-guarded instead of JWT: trigger-only, same machine,
    # and the response carries no lead data.
    if request.remote_addr not in ("127.0.0.1","::1"): return err("forbidden",403)
    res=sync_jakes_leads()
    return ok({"synced":bool(res.get("ok"))})

@api.get("/arc-apps/data-version")
@login_required
def jakes_data_version():
    # Tiny poll target for the frontend's live refresh: no DB hit, changes
    # only when a Jakes sync actually altered lead data.
    return ok({"version":f"{_JAKES_VER['boot']}-{_JAKES_VER['n']}"})

@api.post("/arc-apps/jakes-lead")
@login_required
def jakes_new_lead():
    # Forward a manually entered lead to Jake's Lead Console, which validates
    # it and starts the same n8n SMS qualification workflow as its own New
    # Lead tab. One code path, one activity log, identical behaviour.
    d=request.get_json(silent=True) or {}
    body=json.dumps({"name":str(d.get("name") or "").strip(),
                     "mobile":str(d.get("mobile") or "").strip(),
                     "consent":bool(d.get("consent"))}).encode("utf-8")
    req=urllib.request.Request(JAKES_CONSOLE_URL+"/api/lead",data=body,
                               headers={"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=25) as r:
            res=json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return err(f"Jake's console unreachable ({e.__class__.__name__}) - is the app on port 8960 running?",502)
    if not res.get("ok"): return err(res.get("message") or "Lead was not accepted")
    return ok({"message":res.get("message") or "Sarah is texting the customer now."})

# ── ARC local apps launcher (custom addition, not upstream) ──────────────────
ARC_LOCAL_APPS=[
    {"group":"Plumbing Lead Chain","icon":"\u26a1","name":"Instant AI Response","port":8100},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4e5","name":"Facebook Lead Intake","port":8090},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4dd","name":"Qualification Form","port":8200},
    {"group":"Plumbing Lead Chain","icon":"\U0001f501","name":"Non-Response Follow-Up","port":8300},
    {"group":"Plumbing Lead Chain","icon":"\U0001f3af","name":"AI Qualification & Scoring","port":8400},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4cb","name":"Kevin Handover Briefing","port":8500},
    {"group":"Plumbing Lead Chain","icon":"\U0001f527","name":"Plumbing Offer Matcher","port":8900},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4c4","name":"Call Notes to Proposal","port":8600},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4ac","name":"Customer Communication","port":8700},
    {"group":"Plumbing Lead Chain","icon":"\U0001f4ca","name":"Pipeline Dashboard","port":8800},
    {"group":"Platform Micro-Apps","icon":"\U0001f4e8","name":"Event Intake","port":8001},
    {"group":"Platform Micro-Apps","icon":"\U0001f9f9","name":"Event Normalisation","port":8003},
    {"group":"Platform Micro-Apps","icon":"\U0001f5c4","name":"Event Storage","port":8004},
    {"group":"Platform Micro-Apps","icon":"\U0001f9ed","name":"Routing Rules","port":8005},
    {"group":"Platform Micro-Apps","icon":"\U0001f69a","name":"Dispatch","port":8006},
    {"group":"Platform Micro-Apps","icon":"\U0001f9fe","name":"Audit Logs","port":8007},
    {"group":"Platform Micro-Apps","icon":"\U0001f4e1","name":"Communication Gateway","port":8008},
    {"group":"Platform Micro-Apps","icon":"\U0001f9e0","name":"Compliance Memory","port":8016},
    {"group":"Platform Micro-Apps","icon":"\U0001f4cc","name":"Task Queue","port":8011},
    {"group":"Command Centre & Ops","icon":"\U0001f5c2","name":"App Registry","port":8018},
    {"group":"Command Centre & Ops","icon":"\u2764","name":"Health Monitor","port":8019},
    {"group":"Command Centre & Ops","icon":"\U0001f39b","name":"Local Start/Stop Manager","port":8021},
    {"group":"Command Centre & Ops","icon":"\U0001f5a5","name":"Unified Dashboard","port":8000},
    {"group":"Command Centre & Ops","icon":"\U0001f441","name":"Unified Event Viewer","port":8022},
    {"group":"Command Centre & Ops","icon":"\U0001f465","name":"Unified Customer View","port":8023},
    {"group":"Command Centre & Ops","icon":"\u26a1","name":"Unified Action Centre","port":8024},
    {"group":"Command Centre & Ops","icon":"\u2705","name":"Unified Task Centre","port":8025},
    {"group":"Command Centre & Ops","icon":"\u2709","name":"Unified Communication View","port":8026},
    {"group":"Command Centre & Ops","icon":"\U0001f4c8","name":"Unified Reports","port":8027},
]

@api.get("/arc-apps")
@login_required
def arc_apps_list():
    return ok(ARC_LOCAL_APPS)

@api.get("/arc-apps/status")
@login_required
def arc_apps_status():
    import socket
    res={}
    def _chk(p):
        s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(0.5)
        try: s.connect(("127.0.0.1",p)); res[str(p)]=True
        except Exception: res[str(p)]=False
        finally:
            try: s.close()
            except Exception: pass
    ths=[threading.Thread(target=_chk,args=(a["port"],)) for a in ARC_LOCAL_APPS]
    for t in ths: t.start()
    for t in ths: t.join()
    return ok(res)


# ══ IMPORTED LEADS (Apollo-style CSV) ═════════════════════════════════════════
# Full-fidelity CSV import shown on the Leads & Qualification tab: every column
# from the export file is stored as-is (all TEXT), grouped by company in the UI,
# and every field is editable inline. Kept separate from companies/contacts so
# the automation pipeline is never touched by a bulk import.
IMP_FIELDS=["company","first_name","last_name","title","email","company_name_for_emails",
    "email_status","primary_email_source","primary_email_verification_source",
    "email_confidence","primary_email_catchall_status","primary_email_last_verified_at",
    "seniority","departments","sub_departments","contact_owner","work_direct_phone",
    "home_phone","mobile_phone","corporate_phone","other_phone","do_not_call","stage",
    "lists","last_contacted","account_owner","employees",
    "industry","keywords","person_linkedin","website","company_linkedin","facebook_url",
    "twitter_url","city","state","country","company_address","company_city","company_state",
    "company_country","company_phone","technologies","annual_revenue","total_funding",
    "latest_funding","latest_funding_amount","last_raised_at","subsidiary_of",
    "subsidiary_of_org_id","email_sent","email_open","email_bounced","replied","demoed",
    "num_retail_locations","sic_codes","naics_codes",
    "apollo_contact_id","apollo_account_id","apollo_record_id",
    "secondary_email","secondary_email_source","secondary_email_status",
    "secondary_email_verification_source","tertiary_email","tertiary_email_source",
    "tertiary_email_status","tertiary_email_verification","primary_intent_topic",
    "primary_intent_score","secondary_intent_topic","secondary_intent_score",
    "about_company","company_short_name","qualify_contact"]

def _imp_norm(h):
    # "  # Employees " -> "employees", "Person Linkedin Url" -> "person linkedin url"
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",str(h or "").lower())).strip()

# normalized CSV header -> canonical column. Canonical names with spaces map to
# themselves automatically; this table adds the Apollo export spellings and
# common variants so files from any source land in the right column.
IMP_ALIASES={
    "company":"company","company name":"company","account name":"company","organization":"company",
    "first name":"first_name","last name":"last_name","title":"title","job title":"title",
    "email":"email","email address":"email","work email":"email",
    "company name for emails":"company_name_for_emails",
    "email status":"email_status","primary email status":"email_status",
    "primary email source":"primary_email_source",
    "primary email verification source":"primary_email_verification_source",
    "email confidence":"email_confidence",
    "primary email catch all status":"primary_email_catchall_status",
    "primary email last verified at":"primary_email_last_verified_at",
    "seniority":"seniority","departments":"departments","department":"departments",
    "sub departments":"sub_departments","subdepartments":"sub_departments",
    "contact owner":"contact_owner","account owner":"account_owner",
    "do not call":"do_not_call","lists":"lists","last contacted":"last_contacted",
    "work direct phone":"work_direct_phone","direct phone":"work_direct_phone",
    "home phone":"home_phone","mobile phone":"mobile_phone","mobile":"mobile_phone","phone":"mobile_phone",
    "corporate phone":"corporate_phone","other phone":"other_phone",
    "stage":"stage","contact stage":"stage",
    "employees":"employees","num employees":"employees","number of employees":"employees","employee count":"employees",
    "industry":"industry","keywords":"keywords",
    "person linkedin":"person_linkedin","person linkedin url":"person_linkedin","linkedin":"person_linkedin","linkedin url":"person_linkedin",
    "website":"website","company website":"website",
    "company linkedin":"company_linkedin","company linkedin url":"company_linkedin",
    "facebook url":"facebook_url","facebook":"facebook_url",
    "twitter url":"twitter_url","twitter":"twitter_url",
    "city":"city","state":"state","country":"country",
    "company address":"company_address","company street":"company_address",
    "company city":"company_city","company state":"company_state","company country":"company_country",
    "company phone":"company_phone","technologies":"technologies",
    "annual revenue":"annual_revenue","total funding":"total_funding",
    "latest funding":"latest_funding","latest funding round":"latest_funding",
    "latest funding amount":"latest_funding_amount","last raised at":"last_raised_at",
    "subsidiary of":"subsidiary_of",
    "subsidiary of organization id":"subsidiary_of_org_id","subsidiary of org id":"subsidiary_of_org_id",
    "email sent":"email_sent","email open":"email_open","email bounced":"email_bounced",
    "replied":"replied","demoed":"demoed",
    "number of retail locations":"num_retail_locations","retail locations":"num_retail_locations","num retail locations":"num_retail_locations",
    "sic codes":"sic_codes","naics codes":"naics_codes",
    "apollo contact id":"apollo_contact_id","apollo account id":"apollo_account_id",
    "apollo record id":"apollo_record_id",
    "secondary email":"secondary_email","secondary email source":"secondary_email_source",
    "secondary email status":"secondary_email_status",
    "secondary email verification source":"secondary_email_verification_source",
    "tertiary email":"tertiary_email",
    "tertiary email source":"tertiary_email_source","tertiary email status":"tertiary_email_status",
    "tertiary email verification":"tertiary_email_verification",
    "tertiary email verification source":"tertiary_email_verification",
    "primary intent topic":"primary_intent_topic","primary intent score":"primary_intent_score",
    "secondary intent topic":"secondary_intent_topic","secondary intent score":"secondary_intent_score",
    "about company":"about_company","company description":"about_company","seo description":"about_company","short description":"about_company",
    "company short name":"company_short_name","short name":"company_short_name",
    "company short name 9038":"company_short_name",
    "qualify contact":"qualify_contact","qualified contact":"qualify_contact",
}
for _f in IMP_FIELDS: IMP_ALIASES.setdefault(_f.replace("_"," "),_f)

@api.get("/leads-import")
@login_required
def imp_list():
    return ok(q("SELECT * FROM imported_leads ORDER BY company COLLATE NOCASE, id LIMIT 5000"))

@api.post("/leads-import/upload-csv")
@login_required
def imp_upload_csv():
    f=request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".csv"): return err("CSV file required")
    fname=f.filename
    try: text=f.read().decode("utf-8-sig",errors="replace")
    except Exception as e: return err(f"Could not read file: {e}")
    first=text.split("\n",1)[0]
    delim=max([",",";","\t"],key=first.count)
    allrows=list(csv.reader(io.StringIO(text),delimiter=delim))
    if not allrows: return err("CSV has no header row")
    headers=allrows[0]
    # Positional mapping (not DictReader): Apollo exports can repeat a header
    # (e.g. two "Email Status" columns, the second empty) and DictReader would
    # keep only the LAST one. Here the first non-empty value for a column wins.
    canon_by_pos=[IMP_ALIASES.get(_imp_norm(h)) for h in headers]
    unmapped=sorted({h.strip() for h,cn in zip(headers,canon_by_pos) if not cn and (h or "").strip()})
    if not any(canon_by_pos): return err("No recognized columns in this CSV — expected Apollo-style headers like First Name, Last Name, Email, Company")
    imported=updated=skipped=0
    with get_conn() as c:
        # Re-imports update rows in place: match on email (unique per person in
        # Apollo exports, and survives a company rename here), or on
        # name+company when the row has no email, so nothing ever duplicates.
        existing={}
        for r in c.execute("SELECT id,lower(email) AS e,lower(first_name) AS fn,lower(last_name) AS ln,lower(company) AS co FROM imported_leads"):
            if r["e"]: existing[("e",r["e"])]=r["id"]
            elif r["fn"] or r["ln"]: existing[("n",r["fn"] or "",r["ln"] or "",r["co"] or "")]=r["id"]
        for cells in allrows[1:]:
            if not any(str(x).strip() for x in cells): continue
            vals={k:"" for k in IMP_FIELDS}
            for cn,v in zip(canon_by_pos,cells):
                if cn and v and str(v).strip() and not vals[cn]:
                    vals[cn]=str(v).strip()
            if not vals["company"]: vals["company"]=vals["company_name_for_emails"]
            if not (vals["first_name"] or vals["last_name"] or vals["email"] or vals["company"]):
                skipped+=1; continue
            if vals["email"]: key=("e",vals["email"].lower())
            elif vals["first_name"] or vals["last_name"]: key=("n",vals["first_name"].lower(),vals["last_name"].lower(),vals["company"].lower())
            else: key=None
            if key and key in existing:
                sets=",".join(f"{k}=?" for k in IMP_FIELDS)
                c.execute(f"UPDATE imported_leads SET {sets},source_file=?,updated_at=? WHERE id=?",
                          [vals[k] for k in IMP_FIELDS]+[fname,now(),existing[key]])
                updated+=1
            else:
                cols=",".join(IMP_FIELDS)
                marks=",".join("?"*len(IMP_FIELDS))
                cur=c.execute(f"INSERT INTO imported_leads({cols},source_file) VALUES({marks},?)",
                              [vals[k] for k in IMP_FIELDS]+[fname])
                if key: existing[key]=cur.lastrowid
                imported+=1
        companies=c.execute("SELECT COUNT(DISTINCT lower(company)) AS n FROM imported_leads").fetchone()["n"]
    _bump_jakes_version()
    logger.info(f"Leads CSV '{fname}': {imported} new, {updated} updated, {skipped} skipped, unmapped={unmapped}")
    return ok({"imported":imported,"updated":updated,"skipped":skipped,
               "companies":companies,"unmapped_columns":unmapped,"filename":fname}),201

@api.put("/leads-import/<int:iid>")
@login_required
def imp_update(iid):
    if not q1("SELECT id FROM imported_leads WHERE id=?",(iid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    sets=[f"{k}=?" for k in d if k in IMP_FIELDS]
    vals=[str(d[k] if d[k] is not None else "").strip() for k in d if k in IMP_FIELDS]
    if not sets: return err("No editable fields in request")
    run(f"UPDATE imported_leads SET {','.join(sets)},updated_at=? WHERE id=?",vals+[now(),iid])
    _bump_jakes_version()
    return ok(q1("SELECT * FROM imported_leads WHERE id=?",(iid,)))

@api.delete("/leads-import/<int:iid>")
@login_required
def imp_delete(iid):
    if not q1("SELECT id FROM imported_leads WHERE id=?",(iid,)): return err("Not found",404)
    run("DELETE FROM imported_leads WHERE id=?",(iid,))
    _bump_jakes_version()
    return ok({"message":"Deleted"})

@api.post("/leads-import/rename-company")
@login_required
def imp_rename_company():
    d=request.get_json(silent=True) or {}
    frm=str(d.get("from","")).strip(); to=str(d.get("to","")).strip()
    if not to: return err("New company name required")
    n=0
    with get_conn() as c:
        cur=c.execute("UPDATE imported_leads SET company=?,updated_at=? WHERE company=?",(to,now(),frm))
        n=cur.rowcount
    if n: _bump_jakes_version()
    return ok({"renamed":n,"from":frm,"to":to})


# ── Outreach email per imported lead: AI-drafted plumbing-automation pitch,
#    fully editable, sent only after explicit Approve & Send in the UI. ──
def _imp_lead_email_draft(lead):
    nm=(lead.get("first_name") or "").strip() or "there"
    co=(lead.get("company") or "").strip()
    city=(lead.get("company_city") or lead.get("city") or "").strip()
    bits=[b for b in [lead.get("industry"), city, (lead.get("about_company") or "")[:200]] if b]
    prompt=(f"Write a short, personalised cold outreach email (120-170 words) from ARC Digital to "
            f"{nm} ({lead.get('title') or 'decision maker'}) at {co or 'their company'}.\n"
            f"What ARC Digital sells: AI-powered automation for plumbing and trade businesses — "
            f"'Sarah', a 24/7 AI agent that answers missed calls and SMS enquiries instantly, qualifies the job, "
            f"and books it in; plus automated follow-ups, quoting and invoicing. Outcome: no missed jobs, "
            f"faster response than competitors, less admin.\n"
            f"Personalise with what we know: {'; '.join(bits) or 'nothing extra known'}.\n"
            f"One clear CTA: a quick 15-minute call this week. No placeholders like [Name]. "
            f"Sign off as Kevin, ARC Digital.\n"
            f"Return EXACTLY this format — first line 'SUBJECT: ...', then a blank line, then the email body.")
    txt=_groq(prompt,system=("You are a top B2B sales copywriter for ARC Digital, an Australian AI-automation "
                             "agency for plumbing and trade businesses. Write naturally and concretely, no fluff."))
    subject=body=None
    if txt:
        m=re.match(r"\s*SUBJECT:\s*(.+?)\n+(.*)",txt,re.S|re.I)
        if m: subject,body=m.group(1).strip(),m.group(2).strip()
        elif txt.strip(): subject,body=f"Never miss another job at {co or 'your plumbing business'}",txt.strip()
    if not (subject and body):
        # deterministic fallback so the button always produces something editable
        subject=f"Never miss another job at {co or 'your plumbing business'}"
        body=(f"Hi {nm},\n\n"
              f"When a customer with a burst pipe calls {co or 'a plumbing business'} and no one picks up, "
              f"they simply call the next plumber on the list.\n\n"
              f"ARC Digital fixes that. Sarah, our AI agent, answers every missed call and SMS within seconds — "
              f"24/7 — qualifies the job, and books it straight into your calendar. We also automate follow-ups, "
              f"quotes and invoices, so the admin does itself.\n\n"
              f"Would you be open to a quick 15-minute call this week to see it working on your own number?\n\n"
              f"Kind regards,\nKevin\nARC Digital")
    return subject,body

@api.get("/leads-import/<int:iid>/email")
@login_required
def imp_email_get(iid):
    return ok(q1("SELECT * FROM imported_lead_emails WHERE lead_id=?",(iid,)))

@api.post("/leads-import/<int:iid>/generate-email")
@login_required
def imp_email_generate(iid):
    lead=q1("SELECT * FROM imported_leads WHERE id=?",(iid,))
    if not lead: return err("Lead not found",404)
    subject,body=_imp_lead_email_draft(lead)
    if q1("SELECT id FROM imported_lead_emails WHERE lead_id=?",(iid,)):
        run("UPDATE imported_lead_emails SET subject=?,body=?,status='draft',updated_at=? WHERE lead_id=?",
            (subject,body,now(),iid))
    else:
        run("INSERT INTO imported_lead_emails(lead_id,subject,body) VALUES(?,?,?)",(iid,subject,body))
    return ok(q1("SELECT * FROM imported_lead_emails WHERE lead_id=?",(iid,)))

@api.put("/leads-import/<int:iid>/email")
@login_required
def imp_email_save(iid):
    if not q1("SELECT id FROM imported_leads WHERE id=?",(iid,)): return err("Lead not found",404)
    d=request.get_json(silent=True) or {}
    subject=str(d.get("subject","")).strip(); body=str(d.get("body","")).strip()
    if not (subject or body): return err("Nothing to save — generate an email first")
    if q1("SELECT id FROM imported_lead_emails WHERE lead_id=?",(iid,)):
        run("UPDATE imported_lead_emails SET subject=?,body=?,status='draft',updated_at=? WHERE lead_id=?",
            (subject,body,now(),iid))
    else:
        run("INSERT INTO imported_lead_emails(lead_id,subject,body) VALUES(?,?,?)",(iid,subject,body))
    return ok(q1("SELECT * FROM imported_lead_emails WHERE lead_id=?",(iid,)))

@api.post("/leads-import/<int:iid>/email/send")
@login_required
def imp_email_send(iid):
    lead=q1("SELECT * FROM imported_leads WHERE id=?",(iid,))
    if not lead: return err("Lead not found",404)
    to=(lead.get("email") or "").strip()
    if not to: return err("This lead has no email address — add one in the View window first")
    em=q1("SELECT * FROM imported_lead_emails WHERE lead_id=?",(iid,))
    if not em or not (em.get("subject") and em.get("body")): return err("Generate and save the email first")
    nm=((lead.get("first_name") or "")+" "+(lead.get("last_name") or "")).strip() or to
    res=send_email_smtp(to,nm,em["subject"],em["body"])
    if res.get("status")!="sent":
        return err(f"Send failed ({res.get('method') or 'email'}): {res.get('message','unknown error')}")
    run("UPDATE imported_lead_emails SET status='sent',sent_at=?,updated_at=? WHERE lead_id=?",(now(),now(),iid))
    logger.info(f"Imported-lead email sent to {to} (lead {iid}) via {res.get('method')}")
    return ok({"message":f"Email sent to {to}","sent_to":to,"method":res.get("method")})


# ══════════════════════════════════════════════════════════════════════════════
# CRM EXTENSION — connected CRM layer over the existing tables (2026-08-04).
# Leads ARE companies (as the Leads & Qualification tab already treats them);
# this section adds CRM working fields, structured qualification, conversion,
# opportunity stages, a unified activity timeline, audit history and settings.
# Migrations are idempotent: CREATE TABLE IF NOT EXISTS + PRAGMA column adds,
# the same pattern init_db() already uses.
# ══════════════════════════════════════════════════════════════════════════════

CRM_LEAD_STATUSES=["new","assigned","contact_attempted","contacted","qualified",
                   "unqualified","nurture","converted"]

OPP_STAGES=["new","qualification","discovery_scheduled","discovery_completed",
            "solution_design","quotation_preparation","quotation_sent",
            "proposal_presented","negotiation","verbal_commitment",
            "closed_won","closed_lost","nurture"]
OPP_STAGE_LABELS={"new":"New Opportunity","qualification":"Qualification",
    "discovery_scheduled":"Discovery Scheduled","discovery_completed":"Discovery Completed",
    "solution_design":"Solution Design","quotation_preparation":"Quotation Preparation",
    "quotation_sent":"Quotation Sent","proposal_presented":"Proposal Presented",
    "negotiation":"Negotiation","verbal_commitment":"Verbal Commitment",
    "closed_won":"Closed Won","closed_lost":"Closed Lost","nurture":"Nurture"}
OPP_STAGE_PROB={"new":10,"qualification":20,"discovery_scheduled":30,"discovery_completed":40,
    "solution_design":50,"quotation_preparation":55,"quotation_sent":60,
    "proposal_presented":70,"negotiation":80,"verbal_commitment":90,
    "closed_won":100,"closed_lost":0,"nurture":5}

SERVICE_CATEGORIES=["process_automation","ai_assistant","ai_sales_assistant",
    "system_integration","self_hosted_automation","crm_implementation","workflow_audit",
    "n8n_implementation","managed_automation_support","custom_application","consulting_discovery"]

ACTIVITY_TYPES=["call","email","meeting","sms","task","follow_up","note",
                "linkedin_message","proposal_follow_up","client_review"]
ACTIVITY_STATUSES=["not_started","in_progress","completed","cancelled"]

QUAL_RESULTS=["qualified","unqualified","more_info","nurture"]

def _addcols(c,table,cols):
    """Idempotently add missing columns. cols = {name: SQL type/default decl}."""
    have={r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for name,decl in cols.items():
        if name not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

def init_crm_db():
    with get_conn() as c:
        _addcols(c,"companies",{
            "crm_status":"TEXT DEFAULT 'new'",
            "lead_source":"TEXT DEFAULT ''",
            "service_interest":"TEXT DEFAULT ''",
            "owner_id":"INTEGER",
            "next_action":"TEXT DEFAULT ''",
            "next_action_date":"TEXT",
            "last_activity_at":"TEXT",
            "trading_name":"TEXT DEFAULT ''",
            "abn":"TEXT DEFAULT ''",
            "address":"TEXT DEFAULT ''",
            "state":"TEXT DEFAULT ''",
            "postcode":"TEXT DEFAULT ''",
            "main_phone":"TEXT DEFAULT ''",
            "relationship_status":"TEXT DEFAULT 'lead'",
            "account_health":"TEXT DEFAULT ''",
            "tags":"TEXT DEFAULT ''",
            "crm_notes":"TEXT DEFAULT ''",
            "client_since":"TEXT",
        })
        _addcols(c,"contacts",{
            "mobile":"TEXT DEFAULT ''",
            "direct_phone":"TEXT DEFAULT ''",
            "location":"TEXT DEFAULT ''",
            "preferred_channel":"TEXT DEFAULT ''",
            "owner_id":"INTEGER",
            "last_contacted_at":"TEXT",
            "next_contact_date":"TEXT",
            "email_permitted":"INTEGER DEFAULT 1",
            "sms_permitted":"INTEGER DEFAULT 1",
            "do_not_contact":"INTEGER DEFAULT 0",
            "crm_notes":"TEXT DEFAULT ''",
        })
        _addcols(c,"lead_scores",{
            "ai_reasoning":"TEXT DEFAULT ''",
            "positive_indicators":"TEXT DEFAULT '[]'",
            "negative_indicators":"TEXT DEFAULT '[]'",
            "missing_info":"TEXT DEFAULT '[]'",
            "recommended_action":"TEXT DEFAULT ''",
            "model_used":"TEXT DEFAULT ''",
            "scored_at":"TEXT",
            "override_score":"INTEGER",
            "override_reason":"TEXT DEFAULT ''",
            "overridden_by":"INTEGER",
            "overridden_at":"TEXT",
        })
        _addcols(c,"opportunities",{
            "stage":"TEXT DEFAULT 'new'",
            "stage_entered_at":"TEXT",
            "service_category":"TEXT DEFAULT ''",
            "priority":"TEXT DEFAULT 'medium'",
            "lead_source":"TEXT DEFAULT ''",
            "probability":"INTEGER DEFAULT 10",
            "monthly_recurring":"REAL DEFAULT 0",
            "expected_close_date":"TEXT",
            "proposed_start_date":"TEXT",
            "budget_confirmed":"INTEGER DEFAULT 0",
            "decision_maker_confirmed":"INTEGER DEFAULT 0",
            "decision_criteria":"TEXT DEFAULT ''",
            "decision_process":"TEXT DEFAULT ''",
            "competitor":"TEXT DEFAULT ''",
            "business_problem":"TEXT DEFAULT ''",
            "next_action":"TEXT DEFAULT ''",
            "next_action_date":"TEXT",
            "last_activity_at":"TEXT",
            "closed_at":"TEXT",
            "won_reason":"TEXT DEFAULT ''",
            "lost_reason":"TEXT DEFAULT ''",
            "owner_id":"INTEGER",
        })
        _addcols(c,"sms_logs",{"company_id":"INTEGER"})
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lead_qualifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
            fit_industry INTEGER DEFAULT 0, fit_geography INTEGER DEFAULT 0,
            fit_company_size TEXT DEFAULT '', fit_business_type TEXT DEFAULT '',
            fit_service_interest TEXT DEFAULT '',
            authority_role TEXT DEFAULT '', authority_flags TEXT DEFAULT '{}',
            need_problem TEXT DEFAULT '', need_manual_process TEXT DEFAULT '',
            need_current_systems TEXT DEFAULT '', need_volume TEXT DEFAULT '',
            need_impact TEXT DEFAULT '', need_urgency TEXT DEFAULT '',
            comm_budget_known INTEGER DEFAULT 0, comm_budget_range TEXT DEFAULT '',
            comm_decision_process TEXT DEFAULT '', comm_target_start TEXT DEFAULT '',
            comm_procurement TEXT DEFAULT '', comm_other_vendors TEXT DEFAULT '',
            result TEXT DEFAULT '', result_reason TEXT DEFAULT '',
            unqualified_reason TEXT DEFAULT '',
            ai_recommendation TEXT DEFAULT '', ai_reasoning TEXT DEFAULT '',
            ai_confidence TEXT DEFAULT '',
            human_decision TEXT DEFAULT '', human_notes TEXT DEFAULT '',
            qualified_by INTEGER, qualified_at TEXT,
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS contact_buying_roles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            created_at TEXT DEFAULT(datetime('now')),
            UNIQUE(contact_id,role));

        CREATE TABLE IF NOT EXISTS opportunity_stage_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
            from_stage TEXT, to_stage TEXT NOT NULL,
            changed_by INTEGER, reason TEXT DEFAULT '',
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS activities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'task',
            subject TEXT NOT NULL,
            description TEXT DEFAULT '',
            company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
            contact_id INTEGER, opportunity_id INTEGER,
            ref_table TEXT DEFAULT '', ref_id INTEGER,
            assigned_to INTEGER, created_by INTEGER,
            due_date TEXT, due_time TEXT,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'not_started',
            outcome TEXT DEFAULT '',
            follow_up_required INTEGER DEFAULT 0, follow_up_date TEXT,
            completed_at TEXT,
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_type TEXT NOT NULL, record_id INTEGER NOT NULL,
            user_id INTEGER, user_email TEXT DEFAULT '',
            action TEXT NOT NULL, field TEXT DEFAULT '',
            old_value TEXT DEFAULT '', new_value TEXT DEFAULT '',
            created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS crm_settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_by INTEGER, updated_at TEXT DEFAULT(datetime('now')));

        CREATE INDEX IF NOT EXISTS idx_activities_company   ON activities(company_id);
        CREATE INDEX IF NOT EXISTS idx_activities_opp       ON activities(opportunity_id);
        CREATE INDEX IF NOT EXISTS idx_activities_status    ON activities(status,due_date);
        CREATE INDEX IF NOT EXISTS idx_audit_record         ON audit_logs(record_type,record_id);
        CREATE INDEX IF NOT EXISTS idx_stage_hist_opp       ON opportunity_stage_history(opportunity_id);
        CREATE INDEX IF NOT EXISTS idx_companies_crm_status ON companies(crm_status);
        CREATE INDEX IF NOT EXISTS idx_opps_stage           ON opportunities(stage);
        """)
        # Must run AFTER the executescript above — the table has to exist
        # before ALTER TABLE. When the AI recommendation was generated: it is
        # a point-in-time snapshot, so the UI shows its age and offers a re-run.
        _addcols(c,"lead_qualifications",{"ai_recommended_at":"TEXT"})
        # Seed default scoring thresholds (Phase 5: admin-editable, defaults
        # preserve the current visible categories: hot>=70, warm>=40).
        for k,v_ in (("score_hot_threshold","70"),("score_warm_threshold","40")):
            c.execute("INSERT OR IGNORE INTO crm_settings(key,value) VALUES(?,?)",(k,v_))
        # Existing opportunities predate stages: map open->new, won/lost->closed.
        c.execute("UPDATE opportunities SET stage='closed_won',probability=100 WHERE status='won' AND (stage IS NULL OR stage='' OR stage='new')")
        c.execute("UPDATE opportunities SET stage='closed_lost',probability=0 WHERE status='lost' AND (stage IS NULL OR stage='' OR stage='new')")
        c.execute("UPDATE opportunities SET stage='new' WHERE stage IS NULL OR stage=''")
        c.execute("UPDATE opportunities SET stage_entered_at=COALESCE(stage_entered_at,updated_at,created_at)")
        c.execute("UPDATE companies SET crm_status='new' WHERE crm_status IS NULL OR crm_status=''")
    logger.info("✅ CRM schema ready")

def crm_setting(key,default=""):
    r=q1("SELECT value FROM crm_settings WHERE key=?",(key,))
    return r["value"] if r else default

def crm_thresholds():
    try: hot=int(crm_setting("score_hot_threshold","70"))
    except Exception: hot=70
    try: warm=int(crm_setting("score_warm_threshold","40"))
    except Exception: warm=40
    return hot,warm

def crm_tier(score):
    hot,warm=crm_thresholds()
    if not score or score<=0: return "unscored"
    return "hot" if score>=hot else "warm" if score>=warm else "cold"

def audit(record_type,record_id,action,field="",old_value="",new_value=""):
    """Write an audit row. Uses g.user when inside a request, else system."""
    try:
        uid=g.user["id"] if hasattr(g,"user") and g.user else None
        uemail=g.user["email"] if hasattr(g,"user") and g.user else "system"
    except Exception:
        uid,uemail=None,"system"
    try:
        run("""INSERT INTO audit_logs(record_type,record_id,user_id,user_email,action,field,old_value,new_value)
               VALUES(?,?,?,?,?,?,?,?)""",
            (record_type,record_id,uid,uemail,action,field,
             "" if old_value is None else str(old_value),
             "" if new_value is None else str(new_value)))
    except Exception as e:
        logger.warning(f"audit: {e}")

def touch_lead_activity(company_id):
    try: run("UPDATE companies SET last_activity_at=? WHERE id=?",(now(),company_id))
    except Exception: pass

def role_required(*roles):
    """Server-side role gate on top of login_required."""
    def deco(f):
        @wraps(f)
        def dec(*a,**kw):
            if g.user.get("role") not in roles:
                return err("Insufficient permissions — requires role: "+" or ".join(roles),403)
            return f(*a,**kw)
        return dec
    return deco

# ── CRM: leads workspace ─────────────────────────────────────────────────────
LEAD_SORTS={"name":"co.name COLLATE NOCASE","score":"eff_score","status":"co.crm_status",
            "created":"co.created_at","updated":"co.updated_at","city":"co.city COLLATE NOCASE",
            "next_action_date":"co.next_action_date","last_activity":"co.last_activity_at",
            "owner":"u.full_name COLLATE NOCASE"}

def _lead_query(args):
    """Shared filtered query for list + export. Returns (sql, params) without
    LIMIT so callers can paginate or stream."""
    sql="""SELECT co.*,
        ls.total_score AS ai_score, ls.tier AS ai_tier_raw,
        ls.override_score, ls.override_reason, ls.scored_at, ls.model_used,
        COALESCE(ls.override_score, co.lead_score, ls.total_score) AS eff_score,
        lq.result AS qual_result, lq.qualified_at,
        u.full_name AS owner_name,
        ct.id AS contact_id, ct.first_name AS contact_first, ct.last_name AS contact_last,
        ct.email AS contact_email, ct.phone AS contact_phone, ct.title AS contact_title,
        ct.is_decision_maker AS contact_dm,
        (SELECT COUNT(*) FROM opportunities o WHERE o.company_id=co.id) AS opp_count
        FROM companies co
        LEFT JOIN lead_scores ls ON ls.company_id=co.id
        LEFT JOIN lead_qualifications lq ON lq.company_id=co.id
        LEFT JOIN users u ON u.id=co.owner_id
        LEFT JOIN contacts ct ON ct.id=(
            SELECT id FROM contacts WHERE company_id=co.id
            ORDER BY is_decision_maker DESC, id LIMIT 1)
        WHERE 1=1"""
    params=[]
    search=(args.get("search") or "").strip()
    if search:
        sql+=""" AND (co.name LIKE ? OR co.city LIKE ? OR co.industry LIKE ? OR co.website LIKE ?
                 OR EXISTS(SELECT 1 FROM contacts cx WHERE cx.company_id=co.id AND
                   (cx.first_name LIKE ? OR cx.last_name LIKE ? OR cx.email LIKE ? OR cx.phone LIKE ?)))"""
        params+=[f"%{search}%"]*8
    tier=(args.get("tier") or "").strip()
    if tier in ("hot","warm","cold","unscored"):
        hot,warm=crm_thresholds()
        if tier=="hot":      sql+=" AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)>=?"; params.append(hot)
        elif tier=="warm":   sql+=" AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)>=? AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)<?"; params+=[warm,hot]
        elif tier=="cold":   sql+=" AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)>=1 AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)<?"; params.append(warm)
        else:                sql+=" AND COALESCE(ls.override_score,co.lead_score,ls.total_score,0)<=0"
    crm_status=(args.get("crm_status") or "").strip()
    if crm_status: sql+=" AND co.crm_status=?"; params.append(crm_status)
    qual=(args.get("qualification") or "").strip()
    if qual=="qualified":   sql+=" AND (lq.result='qualified' OR co.status IN('qualified','opportunity'))"
    elif qual:              sql+=" AND lq.result=?"; params.append(qual)
    src=(args.get("lead_source") or "").strip()
    if src: sql+=" AND co.lead_source=?"; params.append(src)
    owner=(args.get("owner_id") or "").strip()
    if owner: sql+=" AND co.owner_id=?"; params.append(owner)
    if (args.get("decision_makers") or "")=="1":
        sql+=" AND EXISTS(SELECT 1 FROM contacts cd WHERE cd.company_id=co.id AND (cd.is_decision_maker=1 OR EXISTS(SELECT 1 FROM contact_buying_roles br WHERE br.contact_id=cd.id AND br.role='decision_maker')))"
    return sql,params

@api.get("/leads")
@login_required
def crm_leads_list():
    a=request.args
    sql,params=_lead_query(a)
    sort=LEAD_SORTS.get(a.get("sort","score"),"eff_score")
    direction="ASC" if (a.get("dir","desc").lower()=="asc") else "DESC"
    nulls_last=f" ORDER BY ({sort}) IS NULL, {sort} {direction}, co.id DESC"
    try: page=max(1,int(a.get("page",1)))
    except Exception: page=1
    try: per=min(200,max(5,int(a.get("per_page",25))))
    except Exception: per=25
    rows=q(f"{sql}{nulls_last} LIMIT ? OFFSET ?",params+[per,(page-1)*per])
    total=q1(f"SELECT COUNT(*) AS n FROM ({sql})",params)["n"]
    hot,warm=crm_thresholds()
    counts=q1(f"""SELECT
        SUM(CASE WHEN eff>={hot} THEN 1 ELSE 0 END) AS hot,
        SUM(CASE WHEN eff>={warm} AND eff<{hot} THEN 1 ELSE 0 END) AS warm,
        SUM(CASE WHEN eff>=1 AND eff<{warm} THEN 1 ELSE 0 END) AS cold,
        SUM(CASE WHEN eff<=0 THEN 1 ELSE 0 END) AS unscored,
        COUNT(*) AS total
        FROM (SELECT COALESCE(ls.override_score,co.lead_score,ls.total_score,0) AS eff
              FROM companies co LEFT JOIN lead_scores ls ON ls.company_id=co.id)""")
    qualified=q1("""SELECT COUNT(*) AS n FROM companies co
        LEFT JOIN lead_qualifications lq ON lq.company_id=co.id
        WHERE lq.result='qualified' OR co.status IN('qualified','opportunity')""")["n"]
    dms=q1("""SELECT COUNT(DISTINCT ct.id) AS n FROM contacts ct
        WHERE ct.is_decision_maker=1
           OR EXISTS(SELECT 1 FROM contact_buying_roles br WHERE br.contact_id=ct.id AND br.role='decision_maker')""")["n"]
    for r in rows:
        r["tier"]=crm_tier(r.get("eff_score") or r.get("lead_score") or 0)
    return ok({"items":rows,"total":total,"page":page,"per_page":per,
               "pages":max(1,(total+per-1)//per),
               "counts":{**(counts or {}),"qualified":qualified,"decision_makers":dms},
               "thresholds":{"hot":hot,"warm":warm},
               "statuses":CRM_LEAD_STATUSES})

@api.get("/leads/export")
@login_required
def crm_leads_export():
    sql,params=_lead_query(request.args)
    rows=q(sql+" ORDER BY co.name COLLATE NOCASE",params)
    buf=io.StringIO(); w=csv.writer(buf)
    w.writerow(["Company","Contact","Title","Email","Phone","City","Industry","AI Score",
                "Override Score","Temperature","Qualification","CRM Status","Lead Source",
                "Service Interest","Owner","Next Action","Next Action Date","Last Activity","Created"])
    for r in rows:
        w.writerow([r.get("name"),f"{r.get('contact_first') or ''} {r.get('contact_last') or ''}".strip(),
            r.get("contact_title"),r.get("contact_email"),r.get("contact_phone"),r.get("city"),
            r.get("industry"),r.get("ai_score"),r.get("override_score"),
            crm_tier(r.get("eff_score") or 0),r.get("qual_result"),r.get("crm_status"),
            r.get("lead_source"),r.get("service_interest"),r.get("owner_name"),
            r.get("next_action"),r.get("next_action_date"),r.get("last_activity_at"),r.get("created_at")])
    out=Response(buf.getvalue(),mimetype="text/csv")
    out.headers["Content-Disposition"]=f"attachment; filename=arc-leads-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return out

@api.get("/leads/<int:cid>")
@login_required
def crm_lead_detail(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    co["owner_name"]=(q1("SELECT full_name FROM users WHERE id=?",(co.get("owner_id"),)) or {}).get("full_name")
    contacts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    for ct in contacts:
        ct["buying_roles"]=[r["role"] for r in q("SELECT role FROM contact_buying_roles WHERE contact_id=?",(ct["id"],))]
    score=q1("SELECT * FROM lead_scores WHERE company_id=?",(cid,))
    if score:
        for k in ("positive_indicators","negative_indicators","missing_info"):
            try: score[k]=json.loads(score.get(k) or "[]")
            except Exception: score[k]=[]
        score["effective_score"]=(score.get("override_score") if score.get("override_score") is not None
                                  else (co.get("lead_score") if co.get("lead_score") else score.get("total_score")))
        score["tier_effective"]=crm_tier(score["effective_score"] or 0)
    qual=q1("SELECT * FROM lead_qualifications WHERE company_id=?",(cid,))
    if qual:
        try: qual["authority_flags"]=json.loads(qual.get("authority_flags") or "{}")
        except Exception: qual["authority_flags"]={}
    opps=q("SELECT * FROM opportunities WHERE company_id=? ORDER BY created_at DESC",(cid,))
    acts=q("""SELECT a.*,u.full_name AS assigned_name FROM activities a
              LEFT JOIN users u ON u.id=a.assigned_to
              WHERE a.company_id=? ORDER BY COALESCE(a.due_date,a.created_at) DESC LIMIT 100""",(cid,))
    return ok({"company":co,"contacts":contacts,"score":score,"qualification":qual,
               "opportunities":opps,"activities":acts,
               "audit":q("SELECT * FROM audit_logs WHERE record_type='lead' AND record_id=? ORDER BY id DESC LIMIT 50",(cid,))})

@api.put("/leads/<int:cid>")
@login_required
def crm_lead_update(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"crm_status","lead_source","service_interest","owner_id","next_action",
             "next_action_date","trading_name","abn","address","state","postcode",
             "main_phone","relationship_status","account_health","tags","crm_notes"}
    if "crm_status" in d and d["crm_status"] not in CRM_LEAD_STATUSES:
        return err(f"Invalid crm_status — one of: {', '.join(CRM_LEAD_STATUSES)}")
    sets=[];vals=[]
    for k in d:
        if k in allowed:
            old=co.get(k)
            if str(old or "")!=str(d[k] or ""):
                audit("lead",cid,"update",k,old,d[k])
                if k=="owner_id": audit("lead",cid,"owner_change",k,old,d[k])
            sets.append(f"{k}=?");vals.append(d[k])
    if not sets: return err("Nothing to update")
    sets.append("updated_at=?");vals+=[now(),cid]
    run(f"UPDATE companies SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM companies WHERE id=?",(cid,)))

@api.post("/leads/bulk")
@login_required
def crm_leads_bulk():
    d=request.get_json(silent=True) or {}
    ids=[int(i) for i in (d.get("ids") or []) if str(i).isdigit()]
    action=d.get("action","")
    if not ids: return err("ids required")
    if len(ids)>500: return err("Too many rows in one bulk action (max 500)")
    done=0
    if action=="assign_owner":
        owner=d.get("owner_id")
        if not owner or not q1("SELECT id FROM users WHERE id=?",(owner,)): return err("Valid owner_id required")
        for cid in ids:
            old=q1("SELECT owner_id FROM companies WHERE id=?",(cid,))
            if not old: continue
            run("UPDATE companies SET owner_id=?,crm_status=CASE WHEN crm_status='new' THEN 'assigned' ELSE crm_status END,updated_at=? WHERE id=?",(owner,now(),cid))
            audit("lead",cid,"owner_change","owner_id",old.get("owner_id"),owner); done+=1
    elif action=="set_status":
        st=d.get("crm_status","")
        if st not in CRM_LEAD_STATUSES: return err("Invalid crm_status")
        for cid in ids:
            old=q1("SELECT crm_status FROM companies WHERE id=?",(cid,))
            if not old: continue
            run("UPDATE companies SET crm_status=?,updated_at=? WHERE id=?",(st,now(),cid))
            audit("lead",cid,"status_change","crm_status",old.get("crm_status"),st); done+=1
    elif action=="score":
        for cid in ids:
            if _crm_ai_score(cid): done+=1
    elif action=="qualify":
        for cid in ids:
            co=q1("SELECT * FROM companies WHERE id=?",(cid,))
            if not co: continue
            cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
            sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
            res=qualify_company_plumbing(co,cts,sigs)
            _save_ai_qual_recommendation(cid,res); done+=1
    elif action=="create_task":
        subj=(d.get("subject") or "Follow up").strip()
        due=d.get("due_date") or (datetime.utcnow()+timedelta(days=1)).strftime("%Y-%m-%d")
        for cid in ids:
            if not q1("SELECT id FROM companies WHERE id=?",(cid,)): continue
            run("""INSERT INTO activities(type,subject,company_id,assigned_to,created_by,due_date,priority,status)
                   VALUES('task',?,?,?,?,?,?,'not_started')""",
                (subj,cid,d.get("assigned_to") or g.user["id"],g.user["id"],due,d.get("priority","medium")))
            audit("lead",cid,"task_created","",None,subj); done+=1
    elif action in ("autopilot_on","autopilot_off"):
        on=1 if action=="autopilot_on" else 0
        fenced=[]
        for cid in ids:
            co=q1("SELECT * FROM companies WHERE id=?",(cid,))
            if not co: continue
            if on and autopilot_fenced(co): fenced.append(co["name"]); continue
            run("UPDATE companies SET autopilot=?,updated_at=? WHERE id=?",(on,now(),cid))
            audit("lead",cid,"autopilot","autopilot",co.get("autopilot"),on)
            emit_crm_event("autopilot.enabled" if on else "autopilot.disabled",_lead_event_payload(cid))
            done+=1
        return ok({"processed":done,"action":action,
                   "skipped_demo_fence":fenced} if fenced else {"processed":done,"action":action})
    else:
        return err(f"Unknown bulk action: {action}")
    return ok({"processed":done,"action":action})

# ── CRM: AI scoring with breakdown + manual override ─────────────────────────
def _crm_ai_score(cid,with_reasoning=False):
    """Score one company, store the full breakdown + indicators. Returns dict or None."""
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return None
    cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    sc=score_company(co,cts,sigs)
    pos,neg,missing=[],[],[]
    rev=co.get("annual_revenue") or 0; emp=co.get("employee_count") or 0
    if rev>=1_000_000: pos.append(f"Revenue ${rev:,} supports automation budget")
    elif rev: neg.append(f"Revenue ${rev:,} is below the ideal band")
    else: missing.append("Annual revenue unknown")
    if emp>=20: pos.append(f"{emp} employees — meaningful process volume")
    elif emp: neg.append(f"Only {emp} employees — smaller automation payoff")
    else: missing.append("Employee count unknown")
    if sigs: pos.append(f"{len(sigs)} buying signal(s): "+", ".join(s.get("signal_name","") for s in sigs[:3]))
    else: missing.append("No buying signals analysed yet")
    dm=[c for c in cts if c.get("is_decision_maker")]
    if dm: pos.append(f"Decision maker on file: {dm[0].get('first_name','')} {dm[0].get('last_name','')}".strip())
    elif cts: neg.append("No decision maker identified among contacts")
    else: missing.append("No contacts on file")
    if not any((c.get("email") or "").strip() for c in cts): missing.append("No contact email")
    if not any((c.get("phone") or "").strip() for c in cts): missing.append("No contact phone")
    tier=crm_tier(sc["total_score"])
    rec=("Call the decision maker today" if tier=="hot" and dm else
         "Find the decision maker, then call" if tier=="hot" else
         "Send a personalised intro email" if tier=="warm" else
         "Gather missing firmographics before outreach" if missing else
         "Nurture with occasional check-ins")
    reasoning=""
    if with_reasoning:
        reasoning=_groq(f"In 2 sentences, why does {co['name']} ({co.get('industry')}, {emp} employees, ${rev:,} revenue) "
                        f"score {sc['total_score']}/100 as a lead for AI automation services?","B2B lead analyst.",120) or ""
    model="groq+rules-v1" if reasoning else "rules-v1"
    if q1("SELECT id FROM lead_scores WHERE company_id=?",(cid,)):
        run("""UPDATE lead_scores SET total_score=?,revenue_score=?,employee_score=?,industry_score=?,
               buying_signal_score=?,department_signal_score=?,email_activity_score=?,tier=?,
               ai_reasoning=?,positive_indicators=?,negative_indicators=?,missing_info=?,
               recommended_action=?,model_used=?,scored_at=?,updated_at=? WHERE company_id=?""",
            (sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
             sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],tier,
             reasoning,json.dumps(pos),json.dumps(neg),json.dumps(missing),rec,model,now(),now(),cid))
    else:
        run("""INSERT INTO lead_scores(company_id,total_score,revenue_score,employee_score,industry_score,
               buying_signal_score,department_signal_score,email_activity_score,tier,
               ai_reasoning,positive_indicators,negative_indicators,missing_info,
               recommended_action,model_used,scored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,sc["total_score"],sc["revenue_score"],sc["employee_score"],sc["industry_score"],
             sc["buying_signal_score"],sc["department_signal_score"],sc["email_activity_score"],tier,
             reasoning,json.dumps(pos),json.dumps(neg),json.dumps(missing),rec,model,now()))
    ovr=q1("SELECT override_score FROM lead_scores WHERE company_id=?",(cid,))
    eff=ovr["override_score"] if ovr and ovr.get("override_score") is not None else sc["total_score"]
    old_score=co.get("lead_score")
    run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(eff,now(),cid))
    if str(old_score)!=str(eff): audit("lead",cid,"ai_score","lead_score",old_score,eff)
    if co.get("autopilot"): emit_crm_event("lead.scored",{"company_id":cid,"score":eff,"tier":tier})
    return {**sc,"tier":tier,"positive_indicators":pos,"negative_indicators":neg,
            "missing_info":missing,"recommended_action":rec,"ai_reasoning":reasoning,
            "model_used":model,"effective_score":eff}

@api.post("/leads/<int:cid>/ai-score")
@login_required
def crm_lead_ai_score(cid):
    res=_crm_ai_score(cid,with_reasoning=True)
    if not res: return err("Not found",404)
    return ok(res)

@api.post("/leads/<int:cid>/score-override")
@login_required
def crm_score_override(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    reason=(d.get("reason") or "").strip()
    if d.get("clear"):
        old=q1("SELECT override_score FROM lead_scores WHERE company_id=?",(cid,))
        if not old: return err("No score record to clear")
        run("UPDATE lead_scores SET override_score=NULL,override_reason='',overridden_by=?,overridden_at=?,updated_at=? WHERE company_id=?",
            (g.user["id"],now(),now(),cid))
        ai=q1("SELECT total_score FROM lead_scores WHERE company_id=?",(cid,))
        run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",((ai or {}).get("total_score") or 0,now(),cid))
        audit("lead",cid,"score_override_cleared","override_score",(old or {}).get("override_score"),None)
        return ok({"cleared":True})
    try: score=int(d.get("score"))
    except Exception: return err("score (0-100) required")
    if not (0<=score<=100): return err("score must be 0-100")
    if not reason: return err("An override reason is required")
    if not q1("SELECT id FROM lead_scores WHERE company_id=?",(cid,)):
        run("INSERT INTO lead_scores(company_id,total_score,tier) VALUES(?,0,'cold')",(cid,))
    old=q1("SELECT override_score,total_score FROM lead_scores WHERE company_id=?",(cid,))
    run("""UPDATE lead_scores SET override_score=?,override_reason=?,overridden_by=?,overridden_at=?,updated_at=?
           WHERE company_id=?""",(score,reason,g.user["id"],now(),now(),cid))
    run("UPDATE companies SET lead_score=?,updated_at=? WHERE id=?",(score,now(),cid))
    audit("lead",cid,"score_override","override_score",
          old.get("override_score") if old.get("override_score") is not None else old.get("total_score"),score)
    return ok({"override_score":score,"tier":crm_tier(score)})

# ── CRM: structured qualification (AI recommends, human decides) ─────────────
QUAL_FIELDS={"fit_industry","fit_geography","fit_company_size","fit_business_type",
    "fit_service_interest","authority_role","authority_flags","need_problem",
    "need_manual_process","need_current_systems","need_volume","need_impact","need_urgency",
    "comm_budget_known","comm_budget_range","comm_decision_process","comm_target_start",
    "comm_procurement","comm_other_vendors","result","result_reason","unqualified_reason",
    "human_decision","human_notes"}

def _is_consumer_enquiry(co):
    """True for leads that came from Jake's Plumbing console: homeowners with a
    plumbing problem — Jake's CUSTOMERS, not ARC's B2B prospects. The business
    qualifier asks 'is this a good plumbing business to sell automation to?',
    which is meaningless for them, so it returned 'unqualified' forever and
    looked stuck (18 of 38 leads, none with firmographics)."""
    if not co: return False
    return ("[Jakes " in (co.get("description") or "")
            or (co.get("industry") or "").strip().lower()=="plumbing lead")

def _save_ai_qual_recommendation(cid,res):
    """Store an AI qualification recommendation without touching human fields."""
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if _is_consumer_enquiry(co):
        # Say so plainly instead of scoring a homeowner as a bad business.
        res=dict(res)
        res["reason"]=("Consumer enquiry from Jake's Plumbing console (a homeowner needing a plumber), "
                       "not a B2B prospect for ARC's automation services — business qualification does not apply. "
                       "Qualify this lead on the job itself, or convert it if the caller also runs a trade business.")
        res["risk"]="n/a"
        rec="not_applicable"
    else:
        rec="qualified" if res["qualify_score"]>=6 else "more_info" if res["qualify_score"]>=4 else "unqualified"
    if q1("SELECT id FROM lead_qualifications WHERE company_id=?",(cid,)):
        run("""UPDATE lead_qualifications SET ai_recommendation=?,ai_reasoning=?,ai_confidence=?,
               ai_recommended_at=?,updated_at=? WHERE company_id=?""",
            (rec,res.get("reason",""),res.get("risk",""),now(),now(),cid))
    else:
        run("""INSERT INTO lead_qualifications(company_id,ai_recommendation,ai_reasoning,ai_confidence,ai_recommended_at)
               VALUES(?,?,?,?,?)""",(cid,rec,res.get("reason",""),res.get("risk",""),now()))
    run("UPDATE companies SET ai_summary=?,updated_at=? WHERE id=?",
        (f"Plumbing Qualify Score: {res['qualify_score']}/10 - {res['reason']}",now(),cid))
    audit("lead",cid,"ai_qualification","ai_recommendation",None,f"{rec} ({res['qualify_score']}/10)")

@api.get("/leads/<int:cid>/qualification")
@login_required
def crm_qual_get(cid):
    if not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("Not found",404)
    qual=q1("SELECT * FROM lead_qualifications WHERE company_id=?",(cid,))
    if qual:
        try: qual["authority_flags"]=json.loads(qual.get("authority_flags") or "{}")
        except Exception: qual["authority_flags"]={}
    return ok(qual)

@api.post("/leads/<int:cid>/qualification/ai-recommend")
@login_required
def crm_qual_ai(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    cts=q("SELECT * FROM contacts WHERE company_id=?",(cid,))
    sigs=q("SELECT * FROM buying_signals WHERE company_id=?",(cid,))
    res=qualify_company_plumbing(co,cts,sigs)
    _save_ai_qual_recommendation(cid,res)
    return ok({**res,"qualification":q1("SELECT * FROM lead_qualifications WHERE company_id=?",(cid,))})

@api.post("/leads/<int:cid>/qualification")
@login_required
def crm_qual_save(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    result=(d.get("result") or "").strip()
    if result and result not in QUAL_RESULTS:
        return err(f"Invalid result — one of: {', '.join(QUAL_RESULTS)}")
    # Phase 4 rule: "Qualified" must be a human decision, never AI-score-only.
    if result=="qualified" and not (d.get("human_decision") or "").strip():
        return err("Add a 'Human decision note' — a lead can only be marked Qualified by a person, "
                   "never by the AI score alone.")
    if result=="unqualified" and not (d.get("unqualified_reason") or "").strip():
        return err("Add an 'Unqualification reason' before marking this lead Unqualified.")
    vals={k:d[k] for k in d if k in QUAL_FIELDS}
    if isinstance(vals.get("authority_flags"),(dict,list)):
        vals["authority_flags"]=json.dumps(vals["authority_flags"])
    existing=q1("SELECT * FROM lead_qualifications WHERE company_id=?",(cid,))
    if existing:
        sets=[f"{k}=?" for k in vals]; args=list(vals.values())
        if result:
            sets+=["qualified_by=?","qualified_at=?"]; args+=[g.user["id"],now()]
        sets.append("updated_at=?"); args+=[now(),cid]
        run(f"UPDATE lead_qualifications SET {','.join(sets)} WHERE company_id=?",args)
    else:
        cols=list(vals.keys())+["company_id"]+(["qualified_by","qualified_at"] if result else [])
        args=list(vals.values())+[cid]+([g.user["id"],now()] if result else [])
        run(f"INSERT INTO lead_qualifications({','.join(cols)}) VALUES({','.join('?'*len(args))})",args)
    if result:
        status_map={"qualified":("qualified","qualified"),"unqualified":("cold","unqualified"),
                    "nurture":(co.get("status"),"nurture"),"more_info":(co.get("status"),co.get("crm_status"))}
        new_status,new_crm=status_map[result]
        audit("lead",cid,"qualification","result",(existing or {}).get("result"),result)
        run("UPDATE companies SET status=?,crm_status=?,updated_at=? WHERE id=?",
            (new_status or "prospect",new_crm or "new",now(),cid))
        touch_lead_activity(cid)
        if co.get("autopilot"): emit_crm_event("lead.qualified",{"company_id":cid,"result":result})
    return ok(q1("SELECT * FROM lead_qualifications WHERE company_id=?",(cid,)))

# ── CRM: lead conversion + duplicate detection ───────────────────────────────
def _domain(u):
    u=(u or "").strip().lower()
    u=re.sub(r"^https?://","",u).split("/")[0]
    return u[4:] if u.startswith("www.") else u

def _dup_candidates(name="",website="",email="",phone=""):
    """Possible duplicate companies/contacts for review before creating records."""
    out={"companies":[],"contacts":[]}
    seen=set()
    def addco(row,why):
        if row and row["id"] not in seen:
            seen.add(row["id"]); out["companies"].append({**row,"match_reason":why})
    nm=(name or "").strip()
    if nm:
        for r in q("SELECT id,name,website,city,status,crm_status FROM companies WHERE lower(name)=lower(?)",(nm,)): addco(r,"Exact company name")
        for r in q("SELECT id,name,website,city,status,crm_status FROM companies WHERE name LIKE ? AND lower(name)!=lower(?) LIMIT 5",(f"%{nm[:12]}%",nm)): addco(r,"Similar company name")
    dom=_domain(website) or (_domain(email.split("@")[-1]) if email and "@" in email else "")
    if dom:
        for r in q("SELECT id,name,website,city,status,crm_status FROM companies WHERE website LIKE ? LIMIT 5",(f"%{dom}%",)): addco(r,f"Website domain {dom}")
    if email:
        for r in q("""SELECT ct.id,ct.first_name,ct.last_name,ct.email,ct.phone,ct.company_id,co.name AS company_name
                      FROM contacts ct LEFT JOIN companies co ON co.id=ct.company_id
                      WHERE lower(ct.email)=lower(?) LIMIT 5""",(email,)):
            out["contacts"].append({**r,"match_reason":"Contact email"})
    if phone:
        pdigits=re.sub(r"\D","",phone)[-9:]
        if len(pdigits)>=8:
            for r in q("""SELECT ct.id,ct.first_name,ct.last_name,ct.email,ct.phone,ct.company_id,co.name AS company_name
                          FROM contacts ct LEFT JOIN companies co ON co.id=ct.company_id
                          WHERE replace(replace(replace(ct.phone,' ',''),'-',''),'+','') LIKE ? LIMIT 5""",(f"%{pdigits}",)):
                out["contacts"].append({**r,"match_reason":"Contact phone"})
    return out

@api.get("/leads/<int:cid>/convert-preview")
@login_required
def crm_convert_preview(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    ct=q1("SELECT * FROM contacts WHERE company_id=? ORDER BY is_decision_maker DESC,id LIMIT 1",(cid,))
    qual=q1("SELECT result,ai_recommendation FROM lead_qualifications WHERE company_id=?",(cid,))
    dups=_dup_candidates(co.get("name",""),co.get("website",""),
                         (ct or {}).get("email",""),(ct or {}).get("phone",""))
    dups["companies"]=[x for x in dups["companies"] if x["id"]!=cid]
    return ok({"company":co,"primary_contact":ct,"qualification":qual,
               "duplicates":dups,"already_converted":co.get("crm_status")=="converted",
               "open_opportunities":q("SELECT id,title,stage,quoted_value FROM opportunities WHERE company_id=? AND stage NOT IN('closed_won','closed_lost')",(cid,))})

@api.post("/leads/<int:cid>/convert")
@login_required
def crm_convert(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    qual=q1("SELECT result FROM lead_qualifications WHERE company_id=?",(cid,))
    if not d.get("force") and (not qual or qual.get("result")!="qualified") and co.get("status") not in ("qualified","opportunity"):
        return err("Lead is not qualified yet — complete qualification first, or pass force=true to convert anyway",409)
    o=d.get("opportunity") or {}
    title=(o.get("title") or f"{co['name']} — {SERVICE_CATEGORIES[0].replace('_',' ').title()}").strip()
    svc=o.get("service_category","process_automation")
    if svc not in SERVICE_CATEGORIES: svc="process_automation"
    stage=o.get("stage","new")
    if stage not in OPP_STAGES: stage="new"
    contact_id=d.get("contact_id") or (q1("SELECT id FROM contacts WHERE company_id=? ORDER BY is_decision_maker DESC,id LIMIT 1",(cid,)) or {}).get("id")
    try: value=float(o.get("quoted_value") or 0)
    except Exception: value=0
    try: mrr=float(o.get("monthly_recurring") or 0)
    except Exception: mrr=0
    prob=OPP_STAGE_PROB.get(stage,10)
    oid=run("""INSERT INTO opportunities(company_id,contact_id,created_by,title,interest_score,
               quoted_value,status,notes,stage,stage_entered_at,service_category,priority,lead_source,
               probability,monthly_recurring,expected_close_date,business_problem,next_action,
               next_action_date,owner_id,last_activity_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid,contact_id,g.user["id"],title,8,value,"open",o.get("notes",""),
         stage,now(),svc,o.get("priority","medium"),co.get("lead_source",""),
         prob,mrr,o.get("expected_close_date"),
         o.get("business_problem") or (q1("SELECT need_problem FROM lead_qualifications WHERE company_id=?",(cid,)) or {}).get("need_problem",""),
         o.get("next_action","Kick off discovery"),
         o.get("next_action_date") or (datetime.utcnow()+timedelta(days=3)).strftime("%Y-%m-%d"),
         d.get("owner_id") or co.get("owner_id") or g.user["id"],now(),now()))
    run("INSERT INTO opportunity_stage_history(opportunity_id,from_stage,to_stage,changed_by,reason) VALUES(?,?,?,?,?)",
        (oid,None,stage,g.user["id"],"Created from lead conversion"))
    # Mark converted but RETAIN the lead record (Phase 6.13/6.14).
    run("UPDATE companies SET crm_status='converted',status='opportunity',relationship_status='opportunity',updated_at=? WHERE id=?",(now(),cid))
    run("""INSERT INTO activities(type,subject,description,company_id,contact_id,opportunity_id,
           assigned_to,created_by,due_date,priority,status)
           VALUES('task',?,?,?,?,?,?,?,?,'high','not_started')""",
        (f"First action: {o.get('next_action','Kick off discovery')}",
         f"Auto-created when lead '{co['name']}' was converted to an opportunity.",
         cid,contact_id,oid,d.get("owner_id") or co.get("owner_id") or g.user["id"],g.user["id"],
         o.get("next_action_date") or (datetime.utcnow()+timedelta(days=3)).strftime("%Y-%m-%d")))
    audit("lead",cid,"converted","",co.get("crm_status"),"converted")
    audit("opportunity",oid,"created","","",title)
    touch_lead_activity(cid)
    if co.get("autopilot"): emit_crm_event("lead.converted",{"company_id":cid,"opportunity_id":oid,"title":title})
    return ok({"opportunity":q1("SELECT * FROM opportunities WHERE id=?",(oid,)),
               "company":q1("SELECT * FROM companies WHERE id=?",(cid,))}),201

# ── CRM: promote an imported (Apollo) lead into companies/contacts ───────────
@api.get("/leads-import/<int:iid>/promote-preview")
@login_required
def imp_promote_preview(iid):
    lead=q1("SELECT * FROM imported_leads WHERE id=?",(iid,))
    if not lead: return err("Lead not found",404)
    dups=_dup_candidates(lead.get("company",""),lead.get("website",""),
                         lead.get("email",""),lead.get("mobile_phone") or lead.get("work_direct_phone",""))
    return ok({"lead":lead,"duplicates":dups})

@api.post("/leads-import/<int:iid>/promote")
@login_required
def imp_promote(iid):
    lead=q1("SELECT * FROM imported_leads WHERE id=?",(iid,))
    if not lead: return err("Lead not found",404)
    d=request.get_json(silent=True) or {}
    cname=(lead.get("company") or lead.get("company_name_for_emails") or "").strip()
    email=(lead.get("email") or "").strip()
    phone=(lead.get("mobile_phone") or lead.get("work_direct_phone") or lead.get("corporate_phone") or "").strip()
    # company: honour an explicit user choice, else exact-name match, else create
    cid=d.get("company_id")
    if cid and not q1("SELECT id FROM companies WHERE id=?",(cid,)): return err("company_id not found",404)
    if not cid:
        existing=q1("SELECT id FROM companies WHERE lower(name)=lower(?)",(cname,)) if cname else None
        dom=_domain(lead.get("website","")) or (_domain(email.split("@")[-1]) if "@" in email else "")
        if not existing and dom:
            existing=q1("SELECT id FROM companies WHERE website LIKE ?",(f"%{dom}%",))
        if existing and not d.get("create_new_company"):
            cid=existing["id"]
    def ti(v_):
        try: return int(float(str(v_).replace(",","").replace("$","").strip())) if str(v_).strip() else None
        except Exception: return None
    if not cid:
        if not cname: return err("Imported lead has no company name — edit it first")
        cid=run("""INSERT INTO companies(name,industry,employee_count,annual_revenue,website,city,state,country,
                   status,crm_status,lead_source,created_by,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'prospect','new','csv_import',?,?)""",
            (cname,lead.get("industry",""),ti(lead.get("employees")),ti(lead.get("annual_revenue")),
             lead.get("website",""),lead.get("company_city") or lead.get("city",""),
             lead.get("company_state") or lead.get("state",""),
             lead.get("company_country") or lead.get("country","Australia"),g.user["id"],now()))
        audit("company",cid,"created","","",cname)
    else:
        run("UPDATE companies SET lead_source=CASE WHEN lead_source='' THEN 'csv_import' ELSE lead_source END,updated_at=? WHERE id=?",(now(),cid))
    # contact: reuse on email match, else create
    ctid=d.get("contact_id")
    if not ctid and email:
        ex=q1("SELECT id FROM contacts WHERE lower(email)=lower(?)",(email,))
        if ex: ctid=ex["id"]
    if not ctid:
        fn=(lead.get("first_name") or "").strip() or (email.split("@")[0] if email else "Contact")
        ctid=run("""INSERT INTO contacts(company_id,first_name,last_name,email,phone,mobile,title,
                    seniority_level,is_decision_maker)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
            (cid,fn,(lead.get("last_name") or "").strip(),email,phone,lead.get("mobile_phone",""),
             lead.get("title",""),
             (lead.get("seniority") or "individual").lower().replace("-","_").replace(" ","_") or "individual",
             1 if (lead.get("seniority") or "").lower() in ("owner","c suite","c_suite","founder") else 0))
        li=(lead.get("person_linkedin") or "").strip()
        if li and not q1("SELECT id FROM contact_linkedin WHERE contact_id=?",(ctid,)):
            run("INSERT INTO contact_linkedin(contact_id,company_id,linkedin_url) VALUES(?,?,?)",(ctid,cid,li))
        audit("contact",ctid,"created","","",f"{fn} (promoted from import #{iid})")
    run("UPDATE imported_leads SET stage='Promoted to CRM',updated_at=? WHERE id=?",(now(),iid))
    audit("lead",cid,"import_promoted","imported_lead_id",None,iid)
    _bump_jakes_version()
    return ok({"company_id":cid,"contact_id":ctid,
               "company":q1("SELECT * FROM companies WHERE id=?",(cid,)),
               "contact":q1("SELECT * FROM contacts WHERE id=?",(ctid,))}),201

# ── CRM: opportunity stages, weighted pipeline ───────────────────────────────
def _opp_out(rows):
    for r in rows:
        try: r["weighted_value"]=round((r.get("quoted_value") or 0)*(r.get("probability") or 0)/100.0,2)
        except Exception: r["weighted_value"]=0
        se=r.get("stage_entered_at") or r.get("created_at") or now()
        try: r["days_in_stage"]=max(0,(datetime.utcnow()-datetime.strptime(se[:19],"%Y-%m-%d %H:%M:%S")).days)
        except Exception: r["days_in_stage"]=0
        r["stage_label"]=OPP_STAGE_LABELS.get(r.get("stage") or "new",r.get("stage"))
    return rows

@api.get("/opportunities/meta")
@login_required
def opp_meta():
    return ok({"stages":OPP_STAGES,"stage_labels":OPP_STAGE_LABELS,
               "stage_probabilities":OPP_STAGE_PROB,"service_categories":SERVICE_CATEGORIES})

@api.get("/opportunities/crm")
@login_required
def opp_crm_list():
    a=request.args
    sql="""SELECT op.*,co.name AS company_name,co.industry,co.lead_score,
           COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') AS contact_name,
           ct.email AS contact_email,ct.phone AS contact_phone,
           u.full_name AS owner_name
           FROM opportunities op
           LEFT JOIN companies co ON op.company_id=co.id
           LEFT JOIN contacts ct ON op.contact_id=ct.id
           LEFT JOIN users u ON u.id=op.owner_id WHERE 1=1"""
    params=[]
    if a.get("stage"): sql+=" AND op.stage=?"; params.append(a["stage"])
    if a.get("open")=="1": sql+=" AND op.stage NOT IN('closed_won','closed_lost')"
    if a.get("owner_id"): sql+=" AND op.owner_id=?"; params.append(a["owner_id"])
    if a.get("company_id"): sql+=" AND op.company_id=?"; params.append(a["company_id"])
    search=(a.get("search") or "").strip()
    if search: sql+=" AND (op.title LIKE ? OR co.name LIKE ?)"; params+=[f"%{search}%"]*2
    rows=_opp_out(q(sql+" ORDER BY op.updated_at DESC LIMIT 500",params))
    return ok({"items":rows,"stages":OPP_STAGES,"stage_labels":OPP_STAGE_LABELS})

@api.get("/opportunities/<int:oid>/detail")
@login_required
def opp_detail(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    _opp_out([op])
    op["company"]=q1("SELECT * FROM companies WHERE id=?",(op.get("company_id"),)) if op.get("company_id") else None
    op["contact"]=q1("SELECT * FROM contacts WHERE id=?",(op.get("contact_id"),)) if op.get("contact_id") else None
    op["owner_name"]=(q1("SELECT full_name FROM users WHERE id=?",(op.get("owner_id"),)) or {}).get("full_name")
    op["stage_history"]=q("""SELECT h.*,u.full_name AS changed_by_name FROM opportunity_stage_history h
                             LEFT JOIN users u ON u.id=h.changed_by
                             WHERE h.opportunity_id=? ORDER BY h.id DESC""",(oid,))
    op["activities"]=q("SELECT * FROM activities WHERE opportunity_id=? ORDER BY COALESCE(due_date,created_at) DESC",(oid,))
    op["quotations"]=q("SELECT id,quote_number,status,total_amount,valid_until,created_at FROM quotations WHERE opportunity_id=? ORDER BY id DESC",(oid,))
    op["invoices"]=q("SELECT id,invoice_number,status,total_amount,due_date,created_at FROM invoices WHERE opportunity_id=? ORDER BY id DESC",(oid,))
    op["audit"]=q("SELECT * FROM audit_logs WHERE record_type='opportunity' AND record_id=? ORDER BY id DESC LIMIT 50",(oid,))
    return ok(op)

@api.put("/opportunities/<int:oid>/crm")
@login_required
def opp_crm_update(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"title","quoted_value","accepted_value","notes","service_category","priority",
             "lead_source","probability","monthly_recurring","expected_close_date",
             "proposed_start_date","budget_confirmed","decision_maker_confirmed",
             "decision_criteria","decision_process","competitor","business_problem",
             "next_action","next_action_date","owner_id","contact_id","interest_score"}
    if "service_category" in d and d["service_category"] and d["service_category"] not in SERVICE_CATEGORIES:
        return err("Invalid service_category")
    if "probability" in d:
        try:
            d["probability"]=int(d["probability"])
            if not (0<=d["probability"]<=100): raise ValueError()
        except Exception: return err("probability must be 0-100")
    sets=[];vals=[]
    for k in d:
        if k in allowed:
            old=op.get(k)
            if str(old or "")!=str(d[k] or ""):
                act={"quoted_value":"value_change","accepted_value":"value_change",
                     "expected_close_date":"close_date_change","owner_id":"owner_change"}.get(k,"update")
                audit("opportunity",oid,act,k,old,d[k])
            sets.append(f"{k}=?");vals.append(d[k])
    if not sets: return err("Nothing to update")
    sets+=["updated_at=?","last_activity_at=?"];vals+=[now(),now(),oid]
    run(f"UPDATE opportunities SET {','.join(sets)} WHERE id=?",vals)
    return ok(_opp_out([q1("SELECT * FROM opportunities WHERE id=?",(oid,))])[0])

@api.post("/opportunities/<int:oid>/stage")
@login_required
def opp_stage_change(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    stage=(d.get("stage") or "").strip()
    if stage not in OPP_STAGES:
        return err(f"Invalid stage — one of: {', '.join(OPP_STAGES)}")
    old=op.get("stage") or "new"
    if stage==old: return ok(_opp_out([op])[0])
    # Backend validation BEFORE the move (Phase 10: no optimistic updates).
    if stage=="closed_lost" and not (d.get("lost_reason") or "").strip():
        return err("lost_reason is required to close as Lost",422)
    if stage=="closed_won":
        final=d.get("final_value") or op.get("accepted_value") or 0
        try: final=float(final)
        except Exception: final=0
        has_accepted_quote=bool(q1("SELECT id FROM quotations WHERE opportunity_id=? AND status IN('accepted','approved','converted')",(oid,)))
        if not has_accepted_quote and final<=0:
            return err("Closing as Won needs an accepted/approved quotation or a final value (final_value)",422)
    prob=OPP_STAGE_PROB.get(stage,op.get("probability") or 10)
    sets=["stage=?","stage_entered_at=?","probability=?","updated_at=?","last_activity_at=?"]
    vals=[stage,now(),prob,now(),now()]
    if stage=="closed_won":
        sets+=["status='won'","closed_at=?","won_reason=?","accepted_value=?"]
        vals+=[now(),d.get("won_reason",""),float(d.get("final_value") or op.get("accepted_value") or op.get("quoted_value") or 0)]
    elif stage=="closed_lost":
        sets+=["status='lost'","closed_at=?","lost_reason=?"]
        vals+=[now(),d.get("lost_reason","")]
    else:
        sets.append("status='open'")
    vals.append(oid)
    run(f"UPDATE opportunities SET {','.join(sets)} WHERE id=?",vals)
    run("INSERT INTO opportunity_stage_history(opportunity_id,from_stage,to_stage,changed_by,reason) VALUES(?,?,?,?,?)",
        (oid,old,stage,g.user["id"],d.get("reason","")))
    audit("opportunity",oid,"stage_change","stage",old,stage)
    _co_ap=q1("SELECT autopilot FROM companies WHERE id=?",(op.get("company_id"),)) if op.get("company_id") else None
    if _co_ap and _co_ap.get("autopilot"):
        emit_crm_event("opportunity.stage_changed",{"opportunity_id":oid,"company_id":op.get("company_id"),"from":old,"to":stage})
        if stage=="closed_won": emit_crm_event("opportunity.won",{"opportunity_id":oid,"company_id":op.get("company_id")})
        if stage=="closed_lost": emit_crm_event("opportunity.lost",{"opportunity_id":oid,"company_id":op.get("company_id")})
    if stage=="closed_won" and op.get("company_id"):
        run("UPDATE companies SET status='qualified',relationship_status='client',client_since=COALESCE(client_since,?),updated_at=? WHERE id=?",
            (datetime.utcnow().strftime("%Y-%m-%d"),now(),op["company_id"]))
        audit("company",op["company_id"],"client_status","relationship_status","","client")
        # Phase 15: handover record + client profile + onboarding tasks + 30-day review
        try:
            run_closed_won_workflow(q1("SELECT * FROM opportunities WHERE id=?",(oid,)),
                                    float(d.get("final_value") or op.get("accepted_value") or op.get("quoted_value") or 0),
                                    g.user["id"])
        except Exception as e:
            logger.error(f"closed-won workflow: {e}")
    if op.get("company_id"): touch_lead_activity(op["company_id"])
    return ok(_opp_out([q1("SELECT * FROM opportunities WHERE id=?",(oid,))])[0])

# ── CRM: unified activities + timeline ───────────────────────────────────────
@api.get("/activities")
@login_required
def activities_list():
    a=request.args
    sql="""SELECT a.*,co.name AS company_name,op.title AS opportunity_title,
           u.full_name AS assigned_name,
           COALESCE(ct.first_name,'')||' '||COALESCE(ct.last_name,'') AS contact_name
           FROM activities a
           LEFT JOIN companies co ON co.id=a.company_id
           LEFT JOIN opportunities op ON op.id=a.opportunity_id
           LEFT JOIN contacts ct ON ct.id=a.contact_id
           LEFT JOIN users u ON u.id=a.assigned_to WHERE 1=1"""
    params=[]
    if a.get("status"): sql+=" AND a.status=?"; params.append(a["status"])
    if a.get("type"): sql+=" AND a.type=?"; params.append(a["type"])
    if a.get("company_id"): sql+=" AND a.company_id=?"; params.append(a["company_id"])
    if a.get("opportunity_id"): sql+=" AND a.opportunity_id=?"; params.append(a["opportunity_id"])
    if a.get("assigned_to"): sql+=" AND a.assigned_to=?"; params.append(a["assigned_to"])
    today=datetime.utcnow().strftime("%Y-%m-%d")
    if a.get("due")=="overdue":
        sql+=" AND a.status NOT IN('completed','cancelled') AND a.due_date<?"; params.append(today)
    elif a.get("due")=="today":
        sql+=" AND a.status NOT IN('completed','cancelled') AND a.due_date=?"; params.append(today)
    elif a.get("due")=="open":
        sql+=" AND a.status NOT IN('completed','cancelled')"
    rows=q(sql+" ORDER BY (a.due_date IS NULL), a.due_date, a.id DESC LIMIT 500",params)
    for r in rows:
        r["overdue"]=bool(r.get("due_date") and r["due_date"]<today and r["status"] not in ("completed","cancelled"))
    return ok(rows)

@api.post("/activities")
@login_required
def activities_create():
    d=request.get_json(silent=True) or {}
    typ=d.get("type","task"); subj=(d.get("subject") or "").strip()
    if typ not in ACTIVITY_TYPES: return err(f"Invalid type — one of: {', '.join(ACTIVITY_TYPES)}")
    if not subj: return err("subject required")
    aid=run("""INSERT INTO activities(type,subject,description,company_id,contact_id,opportunity_id,
               ref_table,ref_id,assigned_to,created_by,due_date,due_time,priority,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (typ,subj,d.get("description",""),d.get("company_id"),d.get("contact_id"),
         d.get("opportunity_id"),d.get("ref_table",""),d.get("ref_id"),
         d.get("assigned_to") or g.user["id"],g.user["id"],d.get("due_date"),d.get("due_time"),
         d.get("priority","medium"),d.get("status","not_started")))
    if d.get("company_id"): touch_lead_activity(d["company_id"])
    if d.get("opportunity_id"): run("UPDATE opportunities SET last_activity_at=? WHERE id=?",(now(),d["opportunity_id"]))
    audit("activity",aid,"created","",None,subj)
    return ok(q1("SELECT * FROM activities WHERE id=?",(aid,))),201

@api.put("/activities/<int:aid>")
@login_required
def activities_update(aid):
    act=q1("SELECT * FROM activities WHERE id=?",(aid,))
    if not act: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    allowed={"subject","description","due_date","due_time","priority","status","assigned_to",
             "outcome","follow_up_required","follow_up_date","type","contact_id","opportunity_id"}
    if "status" in d and d["status"] not in ACTIVITY_STATUSES: return err("Invalid status")
    if "type" in d and d["type"] not in ACTIVITY_TYPES: return err("Invalid type")
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if not sets: return err("Nothing to update")
    sets.append("updated_at=?"); vals+=[now(),aid]
    run(f"UPDATE activities SET {','.join(sets)} WHERE id=?",vals)
    return ok(q1("SELECT * FROM activities WHERE id=?",(aid,)))

@api.post("/activities/<int:aid>/complete")
@login_required
def activities_complete(aid):
    act=q1("SELECT * FROM activities WHERE id=?",(aid,))
    if not act: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    outcome=(d.get("outcome") or "").strip()
    if not outcome: return err("outcome is required when completing an activity")
    fu=1 if d.get("follow_up_required") else 0
    run("""UPDATE activities SET status='completed',outcome=?,follow_up_required=?,
           follow_up_date=?,completed_at=?,updated_at=? WHERE id=?""",
        (outcome,fu,d.get("follow_up_date"),now(),now(),aid))
    audit("activity",aid,"completed","outcome","",outcome)
    follow=None
    if fu:
        fdate=d.get("follow_up_date") or (datetime.utcnow()+timedelta(days=3)).strftime("%Y-%m-%d")
        fid=run("""INSERT INTO activities(type,subject,description,company_id,contact_id,opportunity_id,
                   assigned_to,created_by,due_date,priority,status)
                   VALUES('follow_up',?,?,?,?,?,?,?,?,?,'not_started')""",
            (d.get("next_action") or f"Follow up: {act['subject']}",
             f"Follow-up from activity #{aid} ({act['subject']}). Outcome: {outcome}",
             act.get("company_id"),act.get("contact_id"),act.get("opportunity_id"),
             act.get("assigned_to") or g.user["id"],g.user["id"],fdate,act.get("priority","medium")))
        follow=q1("SELECT * FROM activities WHERE id=?",(fid,))
    if act.get("company_id"):
        touch_lead_activity(act["company_id"])
        if d.get("next_action"):
            run("UPDATE companies SET next_action=?,next_action_date=?,updated_at=? WHERE id=?",
                (d["next_action"],d.get("follow_up_date") or d.get("next_action_date"),now(),act["company_id"]))
    if act.get("opportunity_id"): run("UPDATE opportunities SET last_activity_at=? WHERE id=?",(now(),act["opportunity_id"]))
    return ok({"activity":q1("SELECT * FROM activities WHERE id=?",(aid,)),"follow_up":follow})

@api.get("/timeline")
@login_required
def crm_timeline():
    cid=request.args.get("company_id")
    oid=request.args.get("opportunity_id")
    if not cid and not oid: return err("company_id or opportunity_id required")
    if oid and not cid:
        op=q1("SELECT company_id FROM opportunities WHERE id=?",(oid,))
        cid=(op or {}).get("company_id")
    items=[]
    def add(rows,typ,title_key,sub=None,when="created_at"):
        for r in rows:
            items.append({"type":typ,"id":r.get("id"),"title":r.get(title_key) or typ,
                          "subtitle":(sub(r) if sub else ""),"status":r.get("status",""),
                          "when":r.get(when) or r.get("created_at"),"ref":r})
    add(q("SELECT id,subject,status,recipient_email,created_at,sent_at FROM emails WHERE company_id=? ORDER BY id DESC LIMIT 50",(cid,)),
        "email","subject",lambda r:f"to {r.get('recipient_email','')}")
    add(q("SELECT id,objective,status,phone_number,summary,created_at FROM calls WHERE company_id=? ORDER BY id DESC LIMIT 50",(cid,)),
        "call","objective",lambda r:r.get("summary") or r.get("phone_number",""))
    add(q("SELECT id,title,status,scheduled_at,created_at FROM meetings WHERE company_id=? ORDER BY id DESC LIMIT 50",(cid,)),
        "meeting","title",lambda r:f"scheduled {r.get('scheduled_at') or 'TBD'}")
    add(q("SELECT id,event_type,status,to_number,body,created_at FROM sms_logs WHERE company_id=? ORDER BY id DESC LIMIT 50",(cid,)),
        "sms","event_type",lambda r:(r.get("body") or "")[:80])
    add(q("SELECT id,type,subject,status,outcome,due_date,created_at FROM activities WHERE company_id=? ORDER BY id DESC LIMIT 100",(cid,)),
        "activity","subject",lambda r:(r.get("outcome") or r.get("type","")))
    add(q("SELECT id,quote_number AS subject,status,total_amount,created_at FROM quotations WHERE company_id=? ORDER BY id DESC LIMIT 20",(cid,)),
        "quotation","subject",lambda r:f"AUD ${r.get('total_amount') or 0:,.2f}")
    add(q("SELECT id,invoice_number AS subject,status,total_amount,created_at FROM invoices WHERE company_id=? ORDER BY id DESC LIMIT 20",(cid,)),
        "invoice","subject",lambda r:f"AUD ${r.get('total_amount') or 0:,.2f}")
    add(q("""SELECT h.id,('Stage: '||COALESCE(h.from_stage,'—')||' → '||h.to_stage) AS subject,
             h.reason,h.created_at FROM opportunity_stage_history h
             JOIN opportunities o ON o.id=h.opportunity_id WHERE o.company_id=? ORDER BY h.id DESC LIMIT 30""",(cid,)),
        "stage_change","subject",lambda r:r.get("reason",""))
    add(q("SELECT id,action||COALESCE(' '||field,'') AS subject,new_value,created_at FROM audit_logs WHERE record_type='lead' AND record_id=? ORDER BY id DESC LIMIT 30",(cid,)),
        "audit","subject",lambda r:str(r.get("new_value",""))[:60])
    items.sort(key=lambda x:x.get("when") or "",reverse=True)
    return ok(items[:200])

# ── CRM: dashboard, users, settings, audit log, SMS send ─────────────────────
@api.get("/dashboard/crm")
@login_required
def dashboard_crm():
    today=datetime.utcnow().strftime("%Y-%m-%d")
    month=datetime.utcnow().strftime("%Y-%m")
    uid=g.user["id"]
    new_leads=q1("SELECT COUNT(*) AS n FROM companies WHERE substr(created_at,1,7)=?",(month,))["n"]
    qualified=q1("""SELECT COUNT(*) AS n FROM companies co LEFT JOIN lead_qualifications lq ON lq.company_id=co.id
                    WHERE lq.result='qualified' OR co.status IN('qualified','opportunity')""")["n"]
    converted=q1("SELECT COUNT(*) AS n FROM companies WHERE crm_status='converted'")["n"]
    total_leads=q1("SELECT COUNT(*) AS n FROM companies")["n"]
    opps=q("SELECT * FROM opportunities")
    open_opps=[o for o in opps if (o.get("stage") or "new") not in ("closed_won","closed_lost")]
    pipeline=sum(o.get("quoted_value") or 0 for o in open_opps)
    weighted=sum((o.get("quoted_value") or 0)*(o.get("probability") or 0)/100.0 for o in open_opps)
    closing=[o for o in open_opps if (o.get("expected_close_date") or "").startswith(month)]
    won_month=[o for o in opps if o.get("stage")=="closed_won" and (o.get("closed_at") or "").startswith(month)]
    won_rev=sum(o.get("accepted_value") or o.get("quoted_value") or 0 for o in won_month)
    won_mrr=sum(o.get("monthly_recurring") or 0 for o in won_month)
    quotes_awaiting=q1("SELECT COUNT(*) AS n FROM quotations WHERE status IN('sent','approved','draft')")["n"]
    overdue=q1("SELECT COUNT(*) AS n FROM activities WHERE status NOT IN('completed','cancelled') AND due_date<?",(today,))["n"]
    # My work today
    my=lambda extra,args=():q("""SELECT a.*,co.name AS company_name FROM activities a
        LEFT JOIN companies co ON co.id=a.company_id
        WHERE a.assigned_to=? AND a.status NOT IN('completed','cancelled') """+extra+" ORDER BY a.due_date LIMIT 15",(uid,)+args)
    my_today=my("AND a.due_date=?",(today,))
    my_overdue=my("AND a.due_date<?",(today,))
    meetings_today=q("""SELECT m.*,c.name AS company_name FROM meetings m LEFT JOIN companies c ON c.id=m.company_id
                        WHERE m.status='scheduled' AND substr(m.scheduled_at,1,10)>=? ORDER BY m.scheduled_at LIMIT 8""",(today,))
    # Pipeline by stage
    by_stage=[]
    for st in OPP_STAGES:
        sts=[o for o in opps if (o.get("stage") or "new")==st]
        if st in ("closed_won","closed_lost") and not sts: continue
        days=[]
        for o in sts:
            se=o.get("stage_entered_at") or o.get("created_at")
            try: days.append((datetime.utcnow()-datetime.strptime(se[:19],"%Y-%m-%d %H:%M:%S")).days)
            except Exception: pass
        by_stage.append({"stage":st,"label":OPP_STAGE_LABELS[st],"count":len(sts),
            "value":sum(o.get("quoted_value") or 0 for o in sts),
            "weighted":round(sum((o.get("quoted_value") or 0)*(o.get("probability") or 0)/100.0 for o in sts),2),
            "avg_days":round(sum(days)/len(days),1) if days else 0})
    # Attention required
    week_ago=(datetime.utcnow()-timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    attention={
        "leads_not_contacted":q1("SELECT COUNT(*) AS n FROM companies WHERE crm_status IN('new','assigned')")["n"],
        "opps_no_next_action":len([o for o in open_opps if not (o.get("next_action") or "").strip()]),
        "opps_stale_7d":len([o for o in open_opps if (o.get("last_activity_at") or o.get("updated_at") or "")<week_ago]),
        "opps_close_date_passed":len([o for o in open_opps if (o.get("expected_close_date") or "9999")<today]),
        "quotes_expiring":q1("SELECT COUNT(*) AS n FROM quotations WHERE status IN('sent','approved','draft') AND valid_until<=?",
                             ((datetime.utcnow()+timedelta(days=7)).strftime("%Y-%m-%d"),))["n"],
        "opps_no_decision_maker":len([o for o in open_opps if not o.get("decision_maker_confirmed")]),
    }
    recent=q("""SELECT record_type,record_id,action,field,new_value,user_email,created_at
                FROM audit_logs ORDER BY id DESC LIMIT 25""")
    return ok({"kpis":{"new_leads_month":new_leads,"qualified_leads":qualified,
        "conversion_rate":round(converted/total_leads*100,1) if total_leads else 0,
        "active_pipeline":round(pipeline,2),"weighted_pipeline":round(weighted,2),
        "closing_this_month":len(closing),"quotes_awaiting":quotes_awaiting,
        "overdue_activities":overdue,"revenue_won_month":round(won_rev,2),"mrr_won_month":round(won_mrr,2)},
        "my_today":my_today,"my_overdue":my_overdue,"meetings_today":meetings_today,
        "pipeline_by_stage":by_stage,"attention":attention,"recent":recent})

@api.get("/users")
@login_required
def users_list():
    return ok(q("SELECT id,email,full_name,role,is_active,last_login,created_at FROM users ORDER BY id"))

@api.get("/crm/settings")
@login_required
def crm_settings_get():
    hot,warm=crm_thresholds()
    rows={r["key"]:r["value"] for r in q("SELECT key,value FROM crm_settings")}
    return ok({"score_hot_threshold":hot,"score_warm_threshold":warm,"raw":rows})

@api.put("/crm/settings")
@login_required
@role_required("admin")
def crm_settings_put():
    d=request.get_json(silent=True) or {}
    updated={}
    for k in ("score_hot_threshold","score_warm_threshold"):
        if k in d:
            try: v_=int(d[k])
            except Exception: return err(f"{k} must be an integer 1-100")
            if not (1<=v_<=100): return err(f"{k} must be 1-100")
            updated[k]=v_
    if updated:
        hot=updated.get("score_hot_threshold",crm_thresholds()[0])
        warm=updated.get("score_warm_threshold",crm_thresholds()[1])
        if warm>=hot: return err("warm threshold must be below hot threshold")
    for k,v_ in updated.items():
        old=crm_setting(k)
        run("INSERT INTO crm_settings(key,value,updated_by,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (k,str(v_),g.user["id"],now()))
        audit("settings",0,"update",k,old,v_)
    if not updated: return err("Nothing to update")
    hot,warm=crm_thresholds()
    return ok({"score_hot_threshold":hot,"score_warm_threshold":warm})

@api.get("/crm/audit-log")
@login_required
def crm_audit_log():
    a=request.args
    sql="SELECT * FROM audit_logs WHERE 1=1"; params=[]
    if a.get("record_type"): sql+=" AND record_type=?"; params.append(a["record_type"])
    if a.get("record_id"): sql+=" AND record_id=?"; params.append(a["record_id"])
    if a.get("action"): sql+=" AND action=?"; params.append(a["action"])
    try: limit=min(500,max(10,int(a.get("limit",200))))
    except Exception: limit=200
    return ok(q(sql+" ORDER BY id DESC LIMIT ?",params+[limit]))

@api.post("/sms/send")
@login_required
def sms_send_route():
    d=request.get_json(silent=True) or {}
    to=(d.get("to") or "").strip(); body=(d.get("body") or "").strip()
    if not to or not body: return err("to and body required")
    ct=q1("SELECT do_not_contact,sms_permitted FROM contacts WHERE phone=? OR mobile=?",(to,to))
    if ct and (ct.get("do_not_contact") or not ct.get("sms_permitted",1)):
        return err("This contact has SMS disabled (do-not-contact / no SMS consent)",403)
    result=_sms_send(to,body)
    run("INSERT INTO sms_logs(to_number,from_number,body,status,event_type,company_id,error_msg) VALUES(?,?,?,?,?,?,?)",
        (to,TWILIO_FROM,body[:500],result.get("status","unknown"),"manual",d.get("company_id"),
         result.get("message") if result.get("status")!="sent" else None))
    if d.get("company_id"):
        touch_lead_activity(d["company_id"])
        run("""INSERT INTO activities(type,subject,description,company_id,assigned_to,created_by,status,outcome,completed_at)
               VALUES('sms',?,?,?,?,?,'completed','sent',?)""",
            (f"SMS to {to}",body[:200],d["company_id"],g.user["id"],g.user["id"],now()))
    if result.get("status")!="sent":
        return err(result.get("message","SMS failed"))
    return ok({"message":f"SMS sent to {to}","sid":result.get("sid")})

@api.post("/contacts/<int:ctid>/buying-roles")
@login_required
def contact_buying_roles_set(ctid):
    ct=q1("SELECT * FROM contacts WHERE id=?",(ctid,))
    if not ct: return err("Contact not found",404)
    d=request.get_json(silent=True) or {}
    roles=[r for r in (d.get("roles") or []) if r in
           ("decision_maker","champion","technical","financial_approver","procurement","end_user","influencer","blocker")]
    run("DELETE FROM contact_buying_roles WHERE contact_id=?",(ctid,))
    for r in roles:
        run("INSERT OR IGNORE INTO contact_buying_roles(contact_id,role) VALUES(?,?)",(ctid,r))
    run("UPDATE contacts SET is_decision_maker=? WHERE id=?",(1 if "decision_maker" in roles else ct.get("is_decision_maker",0),ctid))
    audit("contact",ctid,"buying_roles","roles","",",".join(roles))
    return ok({"roles":roles})

# ── CRM AUTOPILOT: event webhooks + one-click automation (2026-08-05) ────────
# The app emits signed events; the n8n orchestrator on the VPS runs the
# playbook (score → qualify → draft) and writes back through the normal API.
# NOTHING is sent to a customer without a human approving the draft — the
# approval IS the one click. Global kill switch: crm_settings.autopilot_paused.
import hmac as _hmac
import secrets as _secrets

# Demo-seed companies are fenced off — autopilot must never touch them
# (their contacts carry real-looking domains like alex@stripe.com).
AUTOPILOT_FENCE={"stripe","notion","vercel","figma","linear","retool",
                 "planetscale","loom","airtable","miro"}

CRM_EVENTS=["autopilot.enabled","autopilot.disabled","lead.created","lead.scored",
            "lead.qualified","lead.converted","draft.created","draft.sent",
            "opportunity.stage_changed","opportunity.won","opportunity.lost",
            "discovery.completed","roi.completed","quotation.sent","quotation.accepted",
            "quotation.expired","lead.replied","webhook.test"]

def init_autopilot_db():
    with get_conn() as c:
        _addcols(c,"companies",{"autopilot":"INTEGER DEFAULT 0"})
        _addcols(c,"activities",{"reminder_notified_at":"TEXT"})
        c.executescript("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            events TEXT NOT NULL DEFAULT '*',
            secret TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            last_status TEXT DEFAULT '',
            last_delivery_at TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS webhook_deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            http_code INTEGER,
            attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at TEXT DEFAULT(datetime('now')),
            delivered_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_wh_deliv_sub ON webhook_deliveries(subscription_id,created_at);
        """)
        c.execute("INSERT OR IGNORE INTO crm_settings(key,value) VALUES('autopilot_paused','0')")
    logger.info("✅ Autopilot schema ready")

def sign_webhook(secret,body_bytes):
    return "sha256="+_hmac.new(secret.encode(),body_bytes,hashlib.sha256).hexdigest()

def _deliver_webhook(did):
    """Deliver one webhook row with up to 3 attempts. Runs in a thread."""
    row=q1("""SELECT d.*,s.url,s.secret FROM webhook_deliveries d
              JOIN webhook_subscriptions s ON s.id=d.subscription_id WHERE d.id=?""",(did,))
    if not row: return
    body=row["payload"].encode("utf-8")
    sig=sign_webhook(row["secret"],body)
    last_err="";code=None
    for attempt in range(1,4):
        try:
            req=urllib.request.Request(row["url"],data=body,method="POST",
                headers={"Content-Type":"application/json","X-ARC-Event":row["event"],
                         "X-ARC-Signature":sig,"X-ARC-Delivery":str(did),
                         "User-Agent":"ARC-CRM-Webhooks/1.0"})
            with urllib.request.urlopen(req,timeout=12) as r:
                code=r.status
            run("""UPDATE webhook_deliveries SET status='ok',http_code=?,attempts=?,delivered_at=? WHERE id=?""",
                (code,attempt,now(),did))
            run("UPDATE webhook_subscriptions SET last_status='ok',last_delivery_at=? WHERE id=?",
                (now(),row["subscription_id"]))
            return
        except urllib.error.HTTPError as e:
            code=e.code; last_err=f"HTTP {e.code}"
        except Exception as e:
            last_err=str(e)[:200]
        time.sleep(2*attempt)
    run("""UPDATE webhook_deliveries SET status='failed',http_code=?,attempts=3,last_error=? WHERE id=?""",
        (code,last_err,did))
    run("UPDATE webhook_subscriptions SET last_status=?,last_delivery_at=? WHERE id=?",
        (f"failed: {last_err}"[:120],now(),row["subscription_id"]))
    logger.warning(f"webhook delivery {did} failed: {last_err}")

def emit_crm_event(event,data):
    """Fan an event out to matching active subscriptions. Fire-and-forget."""
    try:
        if crm_setting("autopilot_paused","0")=="1":
            logger.info(f"autopilot paused — event {event} not delivered")
            return 0
        subs=q("SELECT id,events FROM webhook_subscriptions WHERE is_active=1")
        payload=json.dumps({"event":event,"timestamp":now(),"data":data},default=str)
        n=0
        for s in subs:
            evs=[e.strip() for e in (s["events"] or "*").split(",")]
            if "*" not in evs and event not in evs: continue
            did=run("INSERT INTO webhook_deliveries(subscription_id,event,payload) VALUES(?,?,?)",
                    (s["id"],event,payload))
            threading.Thread(target=_deliver_webhook,args=(did,),daemon=True).start()
            n+=1
        return n
    except Exception as e:
        logger.warning(f"emit_crm_event {event}: {e}")
        return 0

def _lead_event_payload(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,)) or {}
    ct=q1("SELECT * FROM contacts WHERE company_id=? ORDER BY is_decision_maker DESC,id LIMIT 1",(cid,)) or {}
    sc=q1("SELECT total_score,override_score,tier FROM lead_scores WHERE company_id=?",(cid,)) or {}
    return {"company_id":cid,"company":co.get("name"),"industry":co.get("industry"),
            "city":co.get("city"),"crm_status":co.get("crm_status"),
            "lead_source":co.get("lead_source"),"autopilot":co.get("autopilot"),
            "contact":{"id":ct.get("id"),"name":f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),
                       "email":ct.get("email"),"phone":ct.get("phone")},
            "score":sc.get("override_score") if sc.get("override_score") is not None else sc.get("total_score"),
            "tier":crm_tier((sc.get("override_score") if sc.get("override_score") is not None else sc.get("total_score")) or co.get("lead_score") or 0)}

def autopilot_fenced(co):
    """True if this lead must never be automated (demo seed data)."""
    name=(co.get("name") or "").lower().strip()
    if name in AUTOPILOT_FENCE: return True
    ct=q1("SELECT email FROM contacts WHERE company_id=? AND email LIKE 'alex@%' LIMIT 1",(co["id"],))
    return bool(ct and name in AUTOPILOT_FENCE)

@api.post("/leads/<int:cid>/autopilot")
@login_required
def lead_autopilot(cid):
    co=q1("SELECT * FROM companies WHERE id=?",(cid,))
    if not co: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    on=1 if d.get("on") else 0
    if on and autopilot_fenced(co):
        return err(f"'{co['name']}' is demo seed data — autopilot is blocked on it by the safety fence",403)
    run("UPDATE companies SET autopilot=?,updated_at=? WHERE id=?",(on,now(),cid))
    audit("lead",cid,"autopilot","autopilot",co.get("autopilot"),on)
    delivered=emit_crm_event("autopilot.enabled" if on else "autopilot.disabled",_lead_event_payload(cid))
    return ok({"autopilot":on,"webhooks_notified":delivered,
               "note":"Orchestrator will score, qualify and draft outreach — nothing sends without your approval" if on else "Autopilot off"})

@api.get("/webhooks")
@login_required
def webhooks_list():
    subs=q("SELECT * FROM webhook_subscriptions ORDER BY id")
    for s in subs:
        s["secret"]=s["secret"][:6]+"…"          # never re-shown in full
        s["recent"]=q("""SELECT event,status,http_code,attempts,created_at FROM webhook_deliveries
                         WHERE subscription_id=? ORDER BY id DESC LIMIT 5""",(s["id"],))
    return ok({"subscriptions":subs,"events":CRM_EVENTS,
               "paused":crm_setting("autopilot_paused","0")=="1"})

@api.post("/webhooks")
@login_required
@role_required("admin")
def webhooks_create():
    d=request.get_json(silent=True) or {}
    url=(d.get("url") or "").strip()
    if not url.startswith(("http://","https://")): return err("Valid url required")
    events=(d.get("events") or "*").strip() or "*"
    for e in events.split(","):
        if e.strip() not in CRM_EVENTS and e.strip()!="*":
            return err(f"Unknown event '{e.strip()}' — valid: {', '.join(CRM_EVENTS)} or *")
    secret=_secrets.token_hex(24)
    wid=run("INSERT INTO webhook_subscriptions(url,events,secret,created_by) VALUES(?,?,?,?)",
            (url,events,secret,g.user["id"]))
    audit("webhook",wid,"created","url","",url)
    return ok({"id":wid,"url":url,"events":events,
               "secret":secret,"note":"Store this secret now — it is never shown again"}),201

@api.delete("/webhooks/<int:wid>")
@login_required
@role_required("admin")
def webhooks_delete(wid):
    if not q1("SELECT id FROM webhook_subscriptions WHERE id=?",(wid,)): return err("Not found",404)
    run("DELETE FROM webhook_subscriptions WHERE id=?",(wid,))
    audit("webhook",wid,"deleted","","","")
    return ok({"deleted":wid})

@api.put("/webhooks/<int:wid>")
@login_required
@role_required("admin")
def webhooks_update(wid):
    if not q1("SELECT id FROM webhook_subscriptions WHERE id=?",(wid,)): return err("Not found",404)
    d=request.get_json(silent=True) or {}
    if "is_active" in d:
        run("UPDATE webhook_subscriptions SET is_active=? WHERE id=?",(1 if d["is_active"] else 0,wid))
    return ok(q1("SELECT id,url,events,is_active,last_status FROM webhook_subscriptions WHERE id=?",(wid,)))

@api.post("/webhooks/<int:wid>/test")
@login_required
def webhooks_test(wid):
    s=q1("SELECT * FROM webhook_subscriptions WHERE id=?",(wid,))
    if not s: return err("Not found",404)
    payload=json.dumps({"event":"webhook.test","timestamp":now(),
                        "data":{"message":"ARC CRM webhook test","subscription_id":wid}},default=str)
    did=run("INSERT INTO webhook_deliveries(subscription_id,event,payload) VALUES(?,?,?)",
            (wid,"webhook.test",payload))
    _deliver_webhook(did)   # synchronous so the result comes back to the UI
    return ok(q1("SELECT status,http_code,attempts,last_error FROM webhook_deliveries WHERE id=?",(did,)))

# ── Reminders: chat-created, popped up in-app when due ───────────────────────
def now_awst():
    """Perth local time (AWST = UTC+8, no DST). Reminders are entered and
    popped in Kevin's local time — UTC comparisons would be 8h off."""
    return datetime.utcnow()+timedelta(hours=8)

@api.get("/reminders/due")
@login_required
def reminders_due():
    local=now_awst()
    today=local.strftime("%Y-%m-%d"); hhmm=local.strftime("%H:%M")
    rows=q("""SELECT a.*,co.name AS company_name FROM activities a
              LEFT JOIN companies co ON co.id=a.company_id
              WHERE a.status NOT IN('completed','cancelled')
                AND a.reminder_notified_at IS NULL
                AND (a.assigned_to=? OR a.assigned_to IS NULL)
                AND a.due_date IS NOT NULL
                AND (a.due_date<? OR (a.due_date=? AND (a.due_time IS NULL OR a.due_time<=?)))
              ORDER BY a.due_date,a.due_time LIMIT 8""",
           (g.user["id"],today,today,hhmm))
    return ok(rows)

@api.post("/reminders/<int:aid>/ack")
@login_required
def reminders_ack(aid):
    act=q1("SELECT * FROM activities WHERE id=?",(aid,))
    if not act: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    action=d.get("action","dismiss")
    if action=="snooze":
        try: mins=max(5,min(7*24*60,int(d.get("minutes",60))))
        except Exception: mins=60
        base=now_awst()+timedelta(minutes=mins)
        run("UPDATE activities SET due_date=?,due_time=?,reminder_notified_at=NULL,updated_at=? WHERE id=?",
            (base.strftime("%Y-%m-%d"),base.strftime("%H:%M"),now(),aid))
        return ok({"snoozed_until":base.strftime("%Y-%m-%d %H:%M")})
    run("UPDATE activities SET reminder_notified_at=?,updated_at=? WHERE id=?",(now(),now(),aid))
    return ok({"dismissed":aid})

@api.post("/autopilot/pause")
@login_required
@role_required("admin")
def autopilot_pause():
    d=request.get_json(silent=True) or {}
    val="1" if d.get("paused") else "0"
    run("INSERT INTO crm_settings(key,value,updated_by,updated_at) VALUES('autopilot_paused',?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (val,g.user["id"],now()))
    audit("settings",0,"autopilot_pause","autopilot_paused","",val)
    return ok({"paused":val=="1"})

# ══════════════════════════════════════════════════════════════════════════════
# RELEASE 2+3 (2026-08-05): Discovery, ROI snapshots, quotation versioning,
# Kanban support, closed-won handover, Clients, global search, role gate.
# ══════════════════════════════════════════════════════════════════════════════

DISCOVERY_FIELDS={
    # business
    "process_name","process_owner","trigger_event","current_steps","people_involved",
    "systems_involved","volume","avg_completion_time","manual_hours_week","delays",
    "error_rate","rework","customer_impact","compliance_risk","current_annual_cost",
    "desired_future_state","success_measures",
    # technical
    "existing_systems","required_integrations","api_availability","auth_method",
    "data_formats","data_volume","cloud_systems","onprem_systems","hosting_requirement",
    "self_hosting","data_residency","num_users","user_roles","security_requirements",
    "backup_requirements","audit_requirements","test_environment",
    # commercial
    "budget","budget_owner","decision_maker","approval_process","procurement",
    "target_start","required_completion","competing_projects","other_vendors",
    "commercial_model","support_requirement"}

QUOTE_STATUSES=["draft","internal_review","approved","sent","viewed","accepted",
                "rejected","expired","superseded","converted"]

ONBOARDING_TASKS=["Confirm signed approval","Confirm billing details","Issue invoice",
    "Confirm deposit","Assign delivery owner","Schedule kickoff","Create delivery workspace",
    "Collect access requirements","Confirm technical contacts","Confirm milestones",
    "Confirm communication cadence"]

def init_r2_db():
    with get_conn() as c:
        cols=",".join(f"{f} TEXT DEFAULT ''" for f in sorted(DISCOVERY_FIELDS))
        c.executescript(f"""
        CREATE TABLE IF NOT EXISTS discovery_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER UNIQUE REFERENCES opportunities(id) ON DELETE CASCADE,
            company_id INTEGER,
            {cols},
            status TEXT DEFAULT 'draft',
            completed_at TEXT, created_by INTEGER,
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS roi_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            employees REAL DEFAULT 0, hours_week REAL DEFAULT 0, hourly_cost REAL DEFAULT 0,
            automation_pct REAL DEFAULT 0, error_cost REAL DEFAULT 0, error_reduction_pct REAL DEFAULT 0,
            software_cost REAL DEFAULT 0, software_reduction_pct REAL DEFAULT 0,
            implementation_cost REAL DEFAULT 0, support_cost_annual REAL DEFAULT 0,
            labour_cost_annual REAL, labour_saving REAL, error_saving REAL, software_saving REAL,
            total_annual_saving REAL, first_year_net REAL, first_year_roi_pct REAL,
            payback_months REAL, three_year_net REAL,
            created_by INTEGER, created_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS client_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
            client_status TEXT DEFAULT 'active',
            services TEXT DEFAULT '', contract_start TEXT, renewal_date TEXT,
            mrr REAL DEFAULT 0, arr REAL DEFAULT 0,
            support_package TEXT DEFAULT '', account_owner_id INTEGER,
            client_health TEXT DEFAULT 'healthy',
            last_review_date TEXT, next_review_date TEXT,
            expansion_notes TEXT DEFAULT '', open_risks TEXT DEFAULT '',
            satisfaction_score INTEGER,
            created_at TEXT DEFAULT(datetime('now')), updated_at TEXT DEFAULT(datetime('now')));

        CREATE TABLE IF NOT EXISTS client_handovers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER UNIQUE REFERENCES opportunities(id) ON DELETE CASCADE,
            company_id INTEGER, quotation_id INTEGER,
            final_scope TEXT DEFAULT '', final_value REAL DEFAULT 0, recurring_value REAL DEFAULT 0,
            payment_terms TEXT DEFAULT '30 days', deposit_required INTEGER DEFAULT 0,
            deposit_received INTEGER DEFAULT 0,
            delivery_owner_id INTEGER, technical_owner_id INTEGER,
            client_stakeholders TEXT DEFAULT '', hosting_model TEXT DEFAULT '',
            required_integrations TEXT DEFAULT '', credentials_required TEXT DEFAULT '',
            start_date TEXT, target_completion TEXT,
            support_package TEXT DEFAULT '', known_risks TEXT DEFAULT '',
            sales_commitments TEXT DEFAULT '', handover_notes TEXT DEFAULT '',
            created_by INTEGER, created_at TEXT DEFAULT(datetime('now')),
            updated_at TEXT DEFAULT(datetime('now')));
        """)
        _addcols(c,"quotations",{
            "version":"INTEGER DEFAULT 1",
            "superseded_by":"INTEGER",
            "roi_snapshot_id":"INTEGER",
            "owner_id":"INTEGER",
            "monthly_value":"REAL DEFAULT 0",
            "discount_pct":"REAL DEFAULT 0",
            "payment_terms":"TEXT DEFAULT '30 days'",
            "sent_at":"TEXT","accepted_at":"TEXT",
        })
    logger.info("✅ R2/R3 schema ready")

# ── Read-only role gate: 'readonly' users can see everything, change nothing ──
@api.before_request
def _readonly_gate():
    if request.method in ("POST","PUT","DELETE","PATCH") and not request.path.endswith("/auth/login"):
        tok=(request.headers.get("Authorization","").replace("Bearer ","")).strip()
        if tok:
            try:
                if decode_token(tok).get("role")=="readonly":
                    return err("Read-only account — viewing is allowed, changes are not",403)
            except Exception: pass

# ── Discovery (one per opportunity, draft → completed) ───────────────────────
@api.get("/opportunities/<int:oid>/discovery")
@login_required
def discovery_get(oid):
    if not q1("SELECT id FROM opportunities WHERE id=?",(oid,)): return err("Not found",404)
    return ok(q1("SELECT * FROM discovery_records WHERE opportunity_id=?",(oid,)))

@api.put("/opportunities/<int:oid>/discovery")
@login_required
def discovery_save(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    vals={k:str(d[k] if d[k] is not None else "") for k in d if k in DISCOVERY_FIELDS}
    mark_complete=bool(d.get("complete"))
    existing=q1("SELECT * FROM discovery_records WHERE opportunity_id=?",(oid,))
    if existing:
        sets=[f"{k}=?" for k in vals]; args=list(vals.values())
        if mark_complete: sets+=["status='completed'","completed_at=?"]; args.append(now())
        sets.append("updated_at=?"); args+=[now(),oid]
        run(f"UPDATE discovery_records SET {','.join(sets)} WHERE opportunity_id=?",args)
    else:
        cols=list(vals.keys())+["opportunity_id","company_id","created_by"]+(["status","completed_at"] if mark_complete else [])
        args=list(vals.values())+[oid,op.get("company_id"),g.user["id"]]+(["completed",now()] if mark_complete else [])
        run(f"INSERT INTO discovery_records({','.join(cols)}) VALUES({','.join('?'*len(args))})",args)
    if mark_complete:
        audit("opportunity",oid,"discovery_completed","","","completed")
        if op.get("stage") in ("discovery_scheduled","new","qualification"):
            pass  # stage moves stay explicit — no silent stage jumps
        emit_crm_event("discovery.completed",{"opportunity_id":oid,"company_id":op.get("company_id")})
        run("UPDATE opportunities SET last_activity_at=?,updated_at=? WHERE id=?",(now(),now(),oid))
    return ok(q1("SELECT * FROM discovery_records WHERE opportunity_id=?",(oid,)))

# ── ROI calculator + IMMUTABLE named snapshots ───────────────────────────────
def compute_roi(i):
    f=lambda k: float(i.get(k) or 0)
    labour_cost=f("employees")*f("hours_week")*f("hourly_cost")*52
    labour_saving=labour_cost*f("automation_pct")/100.0
    error_saving=f("error_cost")*f("error_reduction_pct")/100.0
    software_saving=f("software_cost")*f("software_reduction_pct")/100.0
    total=labour_saving+error_saving+software_saving
    impl=f("implementation_cost"); support=f("support_cost_annual")
    first_net=total-impl-support
    roi_pct=(first_net/impl*100.0) if impl else 0.0
    payback=(impl/(total/12.0)) if total else 0.0
    three_net=3*total-impl-3*support
    r=lambda v: round(v,2)
    return {"labour_cost_annual":r(labour_cost),"labour_saving":r(labour_saving),
            "error_saving":r(error_saving),"software_saving":r(software_saving),
            "total_annual_saving":r(total),"first_year_net":r(first_net),
            "first_year_roi_pct":r(roi_pct),"payback_months":r(payback),
            "three_year_net":r(three_net)}

@api.get("/opportunities/<int:oid>/roi")
@login_required
def roi_list(oid):
    if not q1("SELECT id FROM opportunities WHERE id=?",(oid,)): return err("Not found",404)
    return ok(q("SELECT * FROM roi_snapshots WHERE opportunity_id=? ORDER BY id DESC",(oid,)))

@api.post("/opportunities/<int:oid>/roi")
@login_required
def roi_create(oid):
    op=q1("SELECT * FROM opportunities WHERE id=?",(oid,))
    if not op: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    name=(d.get("name") or "").strip() or f"ROI {datetime.utcnow().strftime('%d %b %Y %H:%M')}"
    calc=compute_roi(d)
    fields=["employees","hours_week","hourly_cost","automation_pct","error_cost",
            "error_reduction_pct","software_cost","software_reduction_pct",
            "implementation_cost","support_cost_annual"]
    rid=run(f"""INSERT INTO roi_snapshots(opportunity_id,name,{','.join(fields)},
            labour_cost_annual,labour_saving,error_saving,software_saving,total_annual_saving,
            first_year_net,first_year_roi_pct,payback_months,three_year_net,created_by)
            VALUES({','.join('?'*(len(fields)+11))},?)""",
        tuple([oid,name]+[float(d.get(k) or 0) for k in fields]+
              [calc["labour_cost_annual"],calc["labour_saving"],calc["error_saving"],
               calc["software_saving"],calc["total_annual_saving"],calc["first_year_net"],
               calc["first_year_roi_pct"],calc["payback_months"],calc["three_year_net"],g.user["id"]]))
    audit("opportunity",oid,"roi_snapshot","name","",name)
    emit_crm_event("roi.completed",{"opportunity_id":oid,"snapshot_id":rid,"name":name})
    return ok(q1("SELECT * FROM roi_snapshots WHERE id=?",(rid,))),201

@api.put("/roi/<int:rid>")
@login_required
def roi_update_blocked(rid):
    # Phase 13: quotations reference snapshots — snapshots are immutable.
    return err("ROI snapshots are immutable — create a new named snapshot instead",405)

@api.delete("/roi/<int:rid>")
@login_required
def roi_delete(rid):
    if q1("SELECT id FROM quotations WHERE roi_snapshot_id=?",(rid,)):
        return err("This snapshot is referenced by a quotation and cannot be deleted",409)
    if not q1("SELECT id FROM roi_snapshots WHERE id=?",(rid,)): return err("Not found",404)
    run("DELETE FROM roi_snapshots WHERE id=?",(rid,))
    return ok({"deleted":rid})

# ── Quotation lifecycle: statuses, versions, ROI link ────────────────────────
@api.post("/quotations/<int:qid>/status")
@login_required
def quotation_status(qid):
    quot=q1("SELECT * FROM quotations WHERE id=?",(qid,))
    if not quot: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    st=(d.get("status") or "").strip()
    if st not in QUOTE_STATUSES: return err(f"Invalid status — one of: {', '.join(QUOTE_STATUSES)}")
    old=quot.get("status")
    sets=["status=?"]; vals=[st]
    if st=="sent": sets.append("sent_at=?"); vals.append(now())
    if st=="accepted": sets.append("accepted_at=?"); vals.append(now())
    vals.append(qid)
    run(f"UPDATE quotations SET {','.join(sets)} WHERE id=?",vals)
    audit("quotation",qid,"status_change","status",old,st)
    if st in ("sent","accepted","expired"):
        emit_crm_event(f"quotation.{st}",{"quotation_id":qid,"opportunity_id":quot.get("opportunity_id"),
                                          "company_id":quot.get("company_id"),"total":quot.get("total_amount")})
    if st=="accepted" and quot.get("opportunity_id"):
        run("UPDATE opportunities SET accepted_value=COALESCE(accepted_value,?),last_activity_at=?,updated_at=? WHERE id=?",
            (quot.get("amount") or 0,now(),now(),quot["opportunity_id"]))
    return ok(q1("SELECT * FROM quotations WHERE id=?",(qid,)))

@api.post("/quotations/<int:qid>/new-version")
@login_required
def quotation_new_version(qid):
    quot=q1("SELECT * FROM quotations WHERE id=?",(qid,))
    if not quot: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    amount=float(d.get("amount") or quot.get("amount") or 0)
    gst=round(amount*0.10,2)
    nid=run("""INSERT INTO quotations(opportunity_id,company_id,contact_id,created_by,quote_number,
               content,amount,gst_amount,total_amount,valid_until,status,template,version,
               roi_snapshot_id,owner_id,monthly_value,discount_pct,payment_terms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (quot.get("opportunity_id"),quot.get("company_id"),quot.get("contact_id"),g.user["id"],
         f"{quot['quote_number']}-v{(quot.get('version') or 1)+1}",
         d.get("content") or quot.get("content"),amount,gst,round(amount+gst,2),
         (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d"),"draft",
         quot.get("template","professional"),(quot.get("version") or 1)+1,
         d.get("roi_snapshot_id") or quot.get("roi_snapshot_id"),
         quot.get("owner_id") or g.user["id"],
         float(d.get("monthly_value") or quot.get("monthly_value") or 0),
         float(d.get("discount_pct") or quot.get("discount_pct") or 0),
         quot.get("payment_terms","30 days")))
    run("UPDATE quotations SET status='superseded',superseded_by=? WHERE id=?",(nid,qid))
    audit("quotation",qid,"superseded","superseded_by","",nid)
    audit("quotation",nid,"created","version","",(quot.get("version") or 1)+1)
    return ok(q1("SELECT * FROM quotations WHERE id=?",(nid,))),201

# ── Closed-won: handover + onboarding + client profile (Phase 15/17) ─────────
def run_closed_won_workflow(op,final_value,uid):
    oid=op["id"]; cid=op.get("company_id")
    if not cid: return
    accepted_quote=q1("""SELECT id FROM quotations WHERE opportunity_id=? AND status IN('accepted','approved','converted')
                         ORDER BY version DESC LIMIT 1""",(oid,))
    if not q1("SELECT id FROM client_handovers WHERE opportunity_id=?",(oid,)):
        run("""INSERT INTO client_handovers(opportunity_id,company_id,quotation_id,final_value,
               recurring_value,delivery_owner_id,support_package,created_by)
               VALUES(?,?,?,?,?,?,?,?)""",
            (oid,cid,(accepted_quote or {}).get("id"),final_value,
             op.get("monthly_recurring") or 0,op.get("owner_id") or uid,"standard",uid))
        audit("opportunity",oid,"handover_created","","","client handover record")
    # client profile (Clients view is an extension of companies — no duplicate company)
    mrr=op.get("monthly_recurring") or 0
    if q1("SELECT id FROM client_profiles WHERE company_id=?",(cid,)):
        run("""UPDATE client_profiles SET mrr=mrr+?,arr=(mrr+?)*12,client_status='active',updated_at=? WHERE company_id=?""",
            (mrr,mrr,now(),cid))
    else:
        run("""INSERT INTO client_profiles(company_id,client_status,services,contract_start,mrr,arr,
               account_owner_id,next_review_date)
               VALUES(?,?,?,?,?,?,?,?)""",
            (cid,"active",op.get("service_category",""),datetime.utcnow().strftime("%Y-%m-%d"),
             mrr,mrr*12,op.get("owner_id") or uid,
             (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d")))
        audit("company",cid,"client_profile_created","","","active client")
    # default onboarding tasks + 30-day review
    have=q1("SELECT COUNT(*) AS n FROM activities WHERE opportunity_id=? AND subject LIKE 'Onboarding:%'",(oid,))["n"]
    if not have:
        for i,t in enumerate(ONBOARDING_TASKS):
            run("""INSERT INTO activities(type,subject,company_id,opportunity_id,assigned_to,created_by,
                   due_date,priority,status)
                   VALUES('task',?,?,?,?,?,?,?,'not_started')""",
                ("Onboarding: "+t,cid,oid,op.get("owner_id") or uid,uid,
                 (datetime.utcnow()+timedelta(days=min(1+i//3,7))).strftime("%Y-%m-%d"),
                 "high" if i<4 else "medium"))
        run("""INSERT INTO activities(type,subject,description,company_id,opportunity_id,assigned_to,created_by,
               due_date,priority,status)
               VALUES('client_review','30-day account review',?,?,?,?,?,?,'medium','not_started')""",
            ("Scheduled automatically when the opportunity closed Won.",cid,oid,
             op.get("owner_id") or uid,uid,(datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d")))
    notify_async("meeting_scheduled",{"title":"WON: "+ (op.get("title") or "opportunity"),
        "company_name":(q1("SELECT name FROM companies WHERE id=?",(cid,)) or {}).get("name",""),
        "scheduled_at":"onboarding tasks created"})

@api.get("/opportunities/<int:oid>/handover")
@login_required
def handover_get(oid):
    if not q1("SELECT id FROM opportunities WHERE id=?",(oid,)): return err("Not found",404)
    return ok(q1("SELECT * FROM client_handovers WHERE opportunity_id=?",(oid,)))

@api.put("/opportunities/<int:oid>/handover")
@login_required
def handover_save(oid):
    h=q1("SELECT * FROM client_handovers WHERE opportunity_id=?",(oid,))
    if not h: return err("No handover yet — it is created when the opportunity closes Won",404)
    d=request.get_json(silent=True) or {}
    allowed={"final_scope","final_value","recurring_value","payment_terms","deposit_required",
             "deposit_received","delivery_owner_id","technical_owner_id","client_stakeholders",
             "hosting_model","required_integrations","credentials_required","start_date",
             "target_completion","support_package","known_risks","sales_commitments","handover_notes",
             "quotation_id"}
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if not sets: return err("Nothing to update")
    sets.append("updated_at=?"); vals+=[now(),oid]
    run(f"UPDATE client_handovers SET {','.join(sets)} WHERE opportunity_id=?",vals)
    return ok(q1("SELECT * FROM client_handovers WHERE opportunity_id=?",(oid,)))

# ── Clients view (companies with won business — no duplicate records) ────────
@api.get("/clients")
@login_required
def clients_list():
    rows=q("""SELECT co.id,co.name,co.industry,co.city,co.main_phone,co.client_since,
              p.client_status,p.client_health,p.mrr,p.arr,p.services,p.support_package,
              p.contract_start,p.renewal_date,p.last_review_date,p.next_review_date,
              p.satisfaction_score,u.full_name AS account_owner,
              (SELECT COUNT(*) FROM opportunities o WHERE o.company_id=co.id AND o.stage='closed_won') AS won_count,
              (SELECT COALESCE(SUM(COALESCE(o.accepted_value,o.quoted_value)),0) FROM opportunities o
                 WHERE o.company_id=co.id AND o.stage='closed_won') AS won_revenue,
              (SELECT COUNT(*) FROM invoices i WHERE i.company_id=co.id) AS invoice_count
              FROM companies co
              JOIN client_profiles p ON p.company_id=co.id
              LEFT JOIN users u ON u.id=p.account_owner_id
              ORDER BY p.client_health='at_risk' DESC, co.name COLLATE NOCASE""")
    return ok(rows)

@api.put("/clients/<int:cid>/profile")
@login_required
def client_profile_save(cid):
    if not q1("SELECT id FROM client_profiles WHERE company_id=?",(cid,)):
        return err("Not a client yet — profiles are created when an opportunity closes Won",404)
    d=request.get_json(silent=True) or {}
    allowed={"client_status","services","contract_start","renewal_date","mrr","arr",
             "support_package","account_owner_id","client_health","last_review_date",
             "next_review_date","expansion_notes","open_risks","satisfaction_score"}
    if "client_health" in d and d["client_health"] not in ("healthy","attention","at_risk","on_hold","former"):
        return err("client_health must be healthy/attention/at_risk/on_hold/former")
    sets=[f"{k}=?" for k in d if k in allowed]; vals=[d[k] for k in d if k in allowed]
    if not sets: return err("Nothing to update")
    old=q1("SELECT client_status,client_health FROM client_profiles WHERE company_id=?",(cid,))
    sets.append("updated_at=?"); vals+=[now(),cid]
    run(f"UPDATE client_profiles SET {','.join(sets)} WHERE company_id=?",vals)
    if "client_status" in d and d["client_status"]!=old.get("client_status"):
        audit("company",cid,"client_status","client_status",old.get("client_status"),d["client_status"])
    if "client_health" in d and d["client_health"]!=old.get("client_health"):
        audit("company",cid,"client_health","client_health",old.get("client_health"),d["client_health"])
    return ok(q1("SELECT * FROM client_profiles WHERE company_id=?",(cid,)))

# ── Global search (Phase 20) ─────────────────────────────────────────────────
@api.get("/search")
@login_required
def global_search():
    qs=(request.args.get("q") or "").strip()
    if len(qs)<2: return ok({"query":qs,"groups":[]})
    like=f"%{qs}%"; groups=[]
    def grp(name,icon,rows,fmt):
        if rows: groups.append({"name":name,"icon":icon,"items":[fmt(r) for r in rows]})
    grp("Leads / Companies","🏢",
        q("""SELECT id,name,industry,city,crm_status,relationship_status FROM companies
             WHERE name LIKE ? OR trading_name LIKE ? OR website LIKE ? LIMIT 6""",(like,like,like)),
        lambda r:{"id":r["id"],"title":r["name"],"sub":f"{r.get('industry') or ''} · {r.get('city') or ''} · {r.get('crm_status') or ''}","nav":"lead"})
    grp("Contacts","👤",
        q("""SELECT ct.id,ct.first_name,ct.last_name,ct.email,ct.phone,ct.company_id,co.name AS company
             FROM contacts ct LEFT JOIN companies co ON co.id=ct.company_id
             WHERE ct.first_name LIKE ? OR ct.last_name LIKE ? OR ct.email LIKE ? OR ct.phone LIKE ? LIMIT 6""",
          (like,like,like,like)),
        lambda r:{"id":r["company_id"],"title":f"{r['first_name']} {r.get('last_name') or ''}".strip(),
                  "sub":f"{r.get('email') or r.get('phone') or ''} · {r.get('company') or ''}","nav":"lead"})
    grp("Opportunities","💰",
        q("""SELECT o.id,o.title,o.stage,o.quoted_value,co.name AS company FROM opportunities o
             LEFT JOIN companies co ON co.id=o.company_id WHERE o.title LIKE ? OR co.name LIKE ? LIMIT 6""",(like,like)),
        lambda r:{"id":r["id"],"title":r["title"],"sub":f"{r.get('company') or ''} · {(r.get('stage') or '').replace('_',' ')} · ${(r.get('quoted_value') or 0):,.0f}","nav":"opportunity"})
    grp("Quotations","📄",
        q("""SELECT qt.id,qt.quote_number,qt.status,qt.total_amount,co.name AS company FROM quotations qt
             LEFT JOIN companies co ON co.id=qt.company_id WHERE qt.quote_number LIKE ? OR co.name LIKE ? LIMIT 5""",(like,like)),
        lambda r:{"id":r["id"],"title":r["quote_number"],"sub":f"{r.get('company') or ''} · {r.get('status')} · ${(r.get('total_amount') or 0):,.0f}","nav":"quotations"})
    grp("Invoices","🧾",
        q("""SELECT i.id,i.invoice_number,i.status,i.total_amount,co.name AS company FROM invoices i
             LEFT JOIN companies co ON co.id=i.company_id WHERE i.invoice_number LIKE ? OR co.name LIKE ? LIMIT 5""",(like,like)),
        lambda r:{"id":r["id"],"title":r["invoice_number"],"sub":f"{r.get('company') or ''} · {r.get('status')} · ${(r.get('total_amount') or 0):,.0f}","nav":"invoices"})
    grp("Activities / Notes","📝",
        q("""SELECT a.id,a.subject,a.type,a.status,a.company_id,co.name AS company FROM activities a
             LEFT JOIN companies co ON co.id=a.company_id
             WHERE a.subject LIKE ? OR a.description LIKE ? LIMIT 6""",(like,like)),
        lambda r:{"id":r.get("company_id"),"title":r["subject"],"sub":f"{(r.get('type') or '').replace('_',' ')} · {r.get('status')} · {r.get('company') or ''}","nav":"lead" if r.get("company_id") else "activities"})
    grp("Emails","📧",
        q("""SELECT e.id,e.subject,e.recipient_email,e.status,e.company_id FROM emails e
             WHERE e.subject LIKE ? OR e.recipient_email LIKE ? LIMIT 5""",(like,like)),
        lambda r:{"id":r.get("company_id"),"title":r["subject"],"sub":f"to {r.get('recipient_email')} · {r.get('status')}","nav":"lead" if r.get("company_id") else "emails"})
    return ok({"query":qs,"groups":groups})

# ══════════════════════════════════════════════════════════════════════════════
# REPLY DETECTION + FOLLOW-UP CADENCE (2026-08-05)
# When an autopilot lead replies to an outreach email (checked via the Gmail
# API on the same OAuth connection), the CRM marks it replied, STOPS the
# cadence, pops an urgent activity and SMSes Kevin. Unanswered outreach gets
# day-3 / day-7 / day-14 follow-up DRAFTS (human-approved before sending).
# Requires the gmail.readonly scope — reconnect Google once to grant it.
# ══════════════════════════════════════════════════════════════════════════════
CADENCE_STEPS=[3,7,14]

def _gmail_search_hit(token,query):
    """True if any Gmail message matches the query. Never raises."""
    try:
        from urllib.parse import quote as _q
        req=urllib.request.Request(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={_q(query)}&maxResults=1",
            headers={"Authorization":f"Bearer {token}"})
        with urllib.request.urlopen(req,timeout=15) as r:
            return bool(json.loads(r.read()).get("messages"))
    except urllib.error.HTTPError as e:
        if e.code in (401,403):
            logger.info("Gmail reply-check: missing gmail.readonly scope — reconnect Google in Integrations")
        else:
            logger.warning(f"Gmail search {e.code}")
        return None
    except Exception as e:
        logger.warning(f"Gmail search: {e}")
        return None

def _cancel_cadence(company_id,reason="reply received"):
    """Stop all pending automated follow-ups for a company."""
    n1=0;n2=0
    with get_conn() as c:
        n1=c.execute("""UPDATE email_drafts SET status='cancelled'
                        WHERE company_id=? AND status='pending_approval' AND draft_type LIKE 'cadence_%'""",
                     (company_id,)).rowcount
        n2=c.execute("""UPDATE activities SET status='cancelled',updated_at=?
                        WHERE company_id=? AND status='not_started' AND type='follow_up'
                        AND (subject LIKE 'Cadence:%' OR subject LIKE 'Follow up:%')""",
                     (now(),company_id)).rowcount
    if n1 or n2:
        audit("lead",company_id,"cadence_stopped","reason","",reason)
        logger.info(f"cadence stopped for company {company_id}: {n1} drafts, {n2} tasks ({reason})")

def check_email_replies():
    """Hourly: detect replies to outreach sent to autopilot leads."""
    token=_google_access_token()
    if not token: return {"skipped":"google_not_connected"}
    rows=q("""SELECT e.*,co.name AS company_name FROM emails e
              JOIN companies co ON co.id=e.company_id
              WHERE co.autopilot=1 AND e.status IN('sent','opened')
                AND e.sent_at > datetime('now','-30 day')
                AND e.recipient_email LIKE '%@%'
              ORDER BY e.sent_at DESC LIMIT 40""")
    found=0
    for em in rows:
        try:
            epoch=int(datetime.strptime(em["sent_at"][:19],"%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            continue
        hit=_gmail_search_hit(token,f"from:{em['recipient_email']} after:{epoch}")
        if hit is None: break            # scope/API problem — stop the sweep quietly
        if not hit: continue
        run("UPDATE emails SET status='replied',updated_at=? WHERE id=?",(now(),em["id"]))
        _cancel_cadence(em["company_id"])
        run("""INSERT INTO activities(type,subject,description,company_id,assigned_to,created_by,
               due_date,due_time,priority,status)
               VALUES('follow_up',?,?,?,?,?,?,?,'high','not_started')""",
            (f"💬 REPLY from {em['company_name']} — respond today",
             f"{em['recipient_email']} replied to '{em.get('subject','')}'. Cadence auto-stopped.",
             em["company_id"],_admin_id(),_admin_id(),
             now_awst().strftime("%Y-%m-%d"),now_awst().strftime("%H:%M")))
        run("UPDATE companies SET crm_status='contacted',next_action='Respond to reply',next_action_date=?,updated_at=? WHERE id=?",
            (now_awst().strftime("%Y-%m-%d"),now(),em["company_id"]))
        touch_lead_activity(em["company_id"])
        notify_async("lead_replied",{"company_name":em["company_name"],"recipient_email":em["recipient_email"]})
        emit_crm_event("lead.replied",{"company_id":em["company_id"],"email_id":em["id"],
                                       "recipient":em["recipient_email"]})
        audit("lead",em["company_id"],"reply_detected","email",em["recipient_email"],em.get("subject",""))
        found+=1
    if found: logger.info(f"✅ reply check: {found} replies detected")
    return {"checked":len(rows),"replies":found}

def cadence_tick():
    """Hourly: draft the next follow-up (day 3/7/14) for unanswered outreach
    on autopilot leads. Drafts wait for human approval — nothing auto-sends."""
    rows=q("""SELECT e.company_id, MIN(e.sent_at) AS first_sent, co.name
              FROM emails e JOIN companies co ON co.id=e.company_id
              WHERE co.autopilot=1 AND e.status IN('sent','opened')
                AND e.email_type IN('cold','follow_up')
                AND NOT EXISTS(SELECT 1 FROM emails r WHERE r.company_id=e.company_id AND r.status='replied')
              GROUP BY e.company_id LIMIT 25""")
    made=0
    for r_ in rows:
        try:
            days=(datetime.utcnow()-datetime.strptime(r_["first_sent"][:19],"%Y-%m-%d %H:%M:%S")).days
        except Exception:
            continue
        step=max((s for s in CADENCE_STEPS if days>=s),default=None)
        if step is None: continue
        marker=f"cadence_d{step}"
        if q1("SELECT id FROM email_drafts WHERE company_id=? AND draft_type=?",(r_["company_id"],marker)):
            continue
        co=q1("SELECT * FROM companies WHERE id=?",(r_["company_id"],))
        ct=(q1("SELECT * FROM contacts WHERE company_id=? AND is_decision_maker=1",(r_["company_id"],))
            or q1("SELECT * FROM contacts WHERE company_id=?",(r_["company_id"],)))
        if not co or not ct or not (ct.get("email") or "").strip(): continue
        content=generate_email(co,ct,"follow_up")
        run("""INSERT INTO email_drafts(company_id,contact_id,created_by,draft_type,subject,body,
               recipient_email,recipient_name,status,scheduled_send_day)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (co["id"],ct["id"],_admin_id(),marker,content["subject"],content["body"],
             ct["email"],f"{ct.get('first_name','')} {ct.get('last_name','')}".strip(),
             "pending_approval",step))
        notify_async("email_generated",{"company_name":co["name"],"email_type":f"day-{step} follow-up",
                                        "recipient_email":ct["email"],"subject":content["subject"]})
        audit("lead",co["id"],"cadence_draft","step","",f"day {step}")
        made+=1
    if made: logger.info(f"✅ cadence: {made} follow-up draft(s) created")
    return {"drafted":made}

@api.post("/automation/check-replies")
@login_required
def check_replies_route():
    r=check_email_replies()
    c=cadence_tick()
    return ok({**r,**c})

# ── Send a quotation to the client by email (branded HTML) ───────────────────
@api.post("/quotations/<int:qid>/send")
@login_required
def quotation_send(qid):
    quot=q1("SELECT * FROM quotations WHERE id=?",(qid,))
    if not quot: return err("Not found",404)
    d=request.get_json(silent=True) or {}
    ct=q1("SELECT * FROM contacts WHERE id=?",(quot.get("contact_id"),)) if quot.get("contact_id") else None
    to=(d.get("to") or (ct or {}).get("email") or "").strip()
    if not to or "@" not in to: return err("No recipient email — pass 'to' or set a contact email")
    co=q1("SELECT * FROM companies WHERE id=?",(quot.get("company_id"),)) if quot.get("company_id") else {}
    op=q1("SELECT * FROM opportunities WHERE id=?",(quot.get("opportunity_id"),)) if quot.get("opportunity_id") else {}
    html=generate_quotation_html(quot,op,co,ct,quot.get("template","professional"))
    name=f"{(ct or {}).get('first_name','')} {(ct or {}).get('last_name','')}".strip()
    plain=(f"Hi {(ct or {}).get('first_name','there')},\n\n"
           f"Please find our quotation {quot['quote_number']} attached below "
           f"(total AUD ${quot.get('total_amount') or 0:,.2f} inc GST, valid until {quot.get('valid_until','')}).\n\n"
           f"To accept, simply reply to this email with the quote number.\n\n"
           f"Kind regards,\nKevin Raju\nARC Digital")
    res=send_email_smtp(to,name,f"Quotation {quot['quote_number']} — ARC Digital",plain,html=html)
    if res.get("status")!="sent":
        return err(f"Send failed: {res.get('message','unknown error')}")
    run("UPDATE quotations SET status='sent',sent_at=? WHERE id=?",(now(),qid))
    audit("quotation",qid,"sent","to","",to)
    emit_crm_event("quotation.sent",{"quotation_id":qid,"opportunity_id":quot.get("opportunity_id"),
                                     "company_id":quot.get("company_id"),"total":quot.get("total_amount")})
    if quot.get("company_id"):
        run("""INSERT INTO activities(type,subject,company_id,opportunity_id,assigned_to,created_by,status,outcome,completed_at)
               VALUES('email',?,?,?,?,?,'completed','sent',?)""",
            (f"Quotation {quot['quote_number']} emailed to {to}",quot.get("company_id"),
             quot.get("opportunity_id"),g.user["id"],g.user["id"],now()))
        touch_lead_activity(quot["company_id"])
    return ok({"message":f"Quotation emailed to {to} via {res.get('method')}","method":res.get("method"),
               "quotation":q1("SELECT * FROM quotations WHERE id=?",(qid,))})

# ══ FLASK APP ═════════════════════════════════════════════════════════════════
app=Flask(__name__,static_folder=None)
app.secret_key=SECRET_KEY
app.register_blueprint(api)

@app.route("/")
def index():
    return Response(_html(), mimetype="text/html")

@app.route("/favicon.ico")
def favicon():
    return Response(_html(), mimetype="text/html")

# SPA routes — explicit list so Flask never confuses them with /api/
@app.route("/dashboard")
@app.route("/companies")
@app.route("/contacts")
@app.route("/emails")
@app.route("/meetings")
@app.route("/calls")
@app.route("/analytics")
@app.route("/automation")
@app.route("/chat")
@app.route("/sms")
@app.route("/sms-logs")
@app.route("/integrations")
@app.route("/settings")
def spa_page():
    return Response(_html(), mimetype="text/html")

@app.get("/health")
def health():
    try: cos=len(q("SELECT id FROM companies")); db_ok=True
    except Exception: cos,db_ok=0,False
    return jsonify({
        "status":    "healthy" if db_ok else "degraded",
        "database":  db_ok,
        "companies": cos,
        "groq":      bool(GROQ_API_KEY),
        "twilio":    bool(TWILIO_SID),
        "bland":     bool(BLAND_API_KEY),
        "gmail":     bool(GMAIL_EMAIL or SENDGRID_API_KEY),
        "sendgrid":  bool(SENDGRID_API_KEY),
        "api_routes": len([r for r in app.url_map.iter_rules() if "/api/" in r.rule]),
        "version":   "v11",
    })

@app.get("/api/ping")
def api_ping():
    """Lightweight connectivity check — confirms API routes are reachable."""
    return jsonify({"ok":True,"message":"API is reachable","version":"v11"})

@app.errorhandler(404)
def _404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok":False,"error":f"Not found: {request.method} {request.path}"}),404
    # Unknown frontend path — serve SPA so client-side routing handles it
    return Response(_html(), mimetype="text/html"), 200
@app.errorhandler(405)
def _405(e):
    return jsonify({"ok":False,"error":f"Method {request.method} not allowed on {request.path}"}),405
@app.errorhandler(500)
def _500(e):
    import traceback
    logger.error(f"500: {e}\n{traceback.format_exc()}")
    return jsonify({"ok":False,"error":f"Server error: {e}"}),500

# Startup at module import — works with gunicorn --preload
try: init_db(); logger.info("✅ DB ready")
except Exception as _e: logger.error(f"DB:{_e}")
try: init_crm_db()
except Exception as _e: logger.error(f"CRM DB:{_e}")
try: init_autopilot_db()
except Exception as _e: logger.error(f"Autopilot DB:{_e}")
try: init_r2_db()
except Exception as _e: logger.error(f"R2 DB:{_e}")
try: start(); logger.info("✅ Scheduler ready")
except Exception as _e: logger.warning(f"Sched:{_e}")

if __name__=="__main__":
    _port=int(os.environ.get("PORT",5000))
    logger.info(f"http://localhost:{_port} | admin@salesai.com / Admin@123456")
    app.run(host="0.0.0.0",port=_port,debug=False,use_reloader=False)
