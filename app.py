import os, base64, requests, sqlite3
from flask import Flask, render_template, request, jsonify
from nacl import encoding, public
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = "/app/data/sniper.db"

scheduler = BackgroundScheduler()
scheduler.start()

# Infrastructure Credentials (from Docker Env)
GH_AUTH = {
    'SPL': {'token': os.getenv('SPL_GH_TOKEN', ''), 'owner': os.getenv('SPL_GH_OWNER', ''), 'repo': os.getenv('SPL_GH_REPO', '')},
    'KCLS': {'token': os.getenv('KCLS_GH_TOKEN', ''), 'owner': os.getenv('KCLS_GH_OWNER', ''), 'repo': os.getenv('KCLS_GH_REPO', '')}
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    # Comprehensive Schema for all GHA Secrets
    conn.execute('''CREATE TABLE IF NOT EXISTS configs (
                        system TEXT PRIMARY KEY,
                        base_url TEXT,
                        lib_user TEXT,
                        lib_pass TEXT,
                        patron_email TEXT,
                        ntfy_topic TEXT,
                        drop_time TEXT,
                        app_mode TEXT,
                        priority_museums TEXT,
                        auto_book_days TEXT,
                        museum_config TEXT,
                        museum_ids TEXT,
                        strike_minutes TEXT,
                        offset_ms TEXT
                    )''')
    # Default seeds with safe placeholders
    for sys in ['SPL', 'KCLS']:
        conn.execute('''INSERT OR IGNORE INTO configs VALUES 
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
            (sys, "https://", "user", "pass", "email@io.com", "topic", "18:00:00", "alert", "slug1", "Saturday,Sunday", "slug:Name", "slug:ID", "1.0", "-150"))
    conn.commit()
    conn.close()

def encrypt_secret(public_key: str, secret_value: str) -> str:
    public_key_obj = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder)
    sealed_box = public.SealedBox(public_key_obj)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")

def update_gh_secrets(system, secrets_dict):
    auth = GH_AUTH[system]
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/vnd.github+json"}
    url_base = f"https://api.github.com/repos/{auth['owner']}/{auth['repo']}/actions/secrets"
    
    # Get Public Key
    pk_data = requests.get(f"{url_base}/public-key", headers=headers).json()
    
    # Upload each secret from the DB
    for key, value in secrets_dict.items():
        if not value or key == 'system': continue
        # Convert DB keys to GitHub naming convention (upper case)
        gh_key = key.upper()
        encrypted = encrypt_secret(pk_data['key'], str(value))
        requests.put(f"{url_base}/{gh_key}", headers=headers, json={
            "encrypted_value": encrypted, "key_id": pk_data['key_id']
        })

def trigger_dispatch(system):
    auth = GH_AUTH[system]
    url = f"https://api.github.com/repos/{auth['owner']}/{auth['repo']}/actions/workflows/actions.yml/dispatches"
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/vnd.github+json"}
    requests.post(url, headers=headers, json={"ref": "main"})

@app.route('/')
def index():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM configs').fetchall()
    saved_data = {row['system']: dict(row) for row in rows}
    conn.close()
    return render_template('index.html', auth=GH_AUTH, saved=saved_data)

@app.route('/save', methods=['POST'])
def save():
    data = request.json
    system = data['system']
    s = data['SECRETS']
    
    # 1. Update SQLite
    conn = get_db_connection()
    conn.execute('''UPDATE configs SET 
        base_url=?, lib_user=?, lib_pass=?, patron_email=?, ntfy_topic=?, 
        drop_time=?, app_mode=?, priority_museums=?, auto_book_days=?, 
        museum_config=?, museum_ids=?, strike_minutes=?, offset_ms=? 
        WHERE system=?''', 
        (s['base_url'], s['lib_user'], s['lib_pass'], s['patron_email'], s['ntfy_topic'],
         s['drop_time'], s['app_mode'], s['priority_museums'], s['auto_book_days'],
         s['museum_config'], s['museum_ids'], s['strike_minutes'], s['offset_ms'], system))
    conn.commit()
    conn.close()

    # 2. Sync to GitHub
    update_gh_secrets(system, s)
    
    # 3. Handle Scheduling
    try:
        drop_dt = datetime.strptime(s['drop_time'], "%H:%M:%S")
        target_dt = datetime.now().replace(hour=drop_dt.hour, minute=drop_dt.minute, second=drop_dt.second, microsecond=0)
        if target_dt < datetime.now(): target_dt += timedelta(days=1)
        trigger_dt = target_dt - timedelta(minutes=5)
        
        job_id = f"trigger_{system}"
        if scheduler.get_job(job_id): scheduler.remove_job(job_id)
        scheduler.add_job(trigger_dispatch, 'date', run_date=trigger_dt, args=[system], id=job_id)
        return jsonify({"status": f"SYNCED: Secrets updated & Scheduled for {trigger_dt.strftime('%H:%M:%S')} UTC"})
    except:
        return jsonify({"status": "SAVED: Secrets updated. Time format error, no schedule set."})

@app.route('/run_now', methods=['POST'])
def run_now():
    trigger_dispatch(request.json['system'])
    return jsonify({"status": "DISPATCHED: Remote runner started."})

@app.route('/status')
def status():
    jobs = scheduler.get_jobs()
    res = {"SPL": "Idle", "KCLS": "Idle"}
    for j in jobs:
        sys = "SPL" if "SPL" in j.id else "KCLS"
        res[sys] = f"Strike Signal at {j.next_run_time.strftime('%H:%M:%S')}"
    return jsonify(res)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
