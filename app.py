import os, base64, requests, sqlite3, logging
from flask import Flask, render_template, request, jsonify
from nacl import encoding, public
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = "/app/data/sniper.db"

scheduler = BackgroundScheduler()
scheduler.start()

# Infrastructure Credentials
GH_AUTH = {
    'SPL': {
        'token': os.getenv('SPL_GH_TOKEN', ''),
        'owner': os.getenv('SPL_GH_OWNER', ''),
        'repo': os.getenv('SPL_GH_REPO', '')
    },
    'KCLS': {
        'token': os.getenv('KCLS_GH_TOKEN', ''),
        'owner': os.getenv('KCLS_GH_OWNER', ''),
        'repo': os.getenv('KCLS_GH_REPO', '')
    }
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    # Added workflow_file column
    conn.execute('''CREATE TABLE IF NOT EXISTS configs (
                        system TEXT PRIMARY KEY,
                        base_url TEXT, lib_user TEXT, lib_pass TEXT, patron_email TEXT,
                        ntfy_topic TEXT, drop_time TEXT, app_mode TEXT,
                        priority_museums TEXT, auto_book_days TEXT,
                        museum_config TEXT, museum_ids TEXT, strike_minutes TEXT,
                        offset_ms TEXT, workflow_file TEXT
                    )''')
    for sys in ['SPL', 'KCLS']:
        conn.execute('INSERT OR IGNORE INTO configs (system, workflow_file) VALUES (?, ?)', (sys, "actions.yml"))
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
    
    # 1. Get Public Key
    r = requests.get(f"{url_base}/public-key", headers=headers)
    if r.status_code != 200:
        return f"Error getting public key: {r.status_code} - {r.text}"
    
    pk_data = r.json()
    
    # 2. Sync loop
    success_count = 0
    for key, value in secrets_dict.items():
        # Skip internal dashboard keys that aren't GitHub secrets
        if key in ['system', 'workflow_file'] or not value: continue
        
        gh_key = key.upper()
        encrypted = encrypt_secret(pk_data['key'], str(value))
        resp = requests.put(f"{url_base}/{gh_key}", headers=headers, json={
            "encrypted_value": encrypted, "key_id": pk_data['key_id']
        })
        if resp.status_code in [201, 204]: success_count += 1
        else: return f"Error updating {gh_key}: {resp.status_code} - {resp.text}"
        
    return f"Successfully synced {success_count} secrets to GitHub."

def trigger_dispatch(system, workflow_filename=None):
    auth = GH_AUTH[system]
    
    # If filename not provided, fetch from DB
    if not workflow_filename:
        conn = get_db_connection()
        row = conn.execute('SELECT workflow_file FROM configs WHERE system=?', (system,)).fetchone()
        workflow_filename = row['workflow_file'] if row else "actions.yml"
        conn.close()

    url = f"https://api.github.com/repos/{auth['owner']}/{auth['repo']}/actions/workflows/{workflow_filename}/dispatches"
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/vnd.github+json"}
    
    resp = requests.post(url, headers=headers, json={"ref": "main"})
    if resp.status_code == 204:
        return "Remote Strike Initialized (204 OK)"
    elif resp.status_code == 404:
        return f"Error: Workflow '{workflow_filename}' not found (404)."
    else:
        return f"Trigger Failed: {resp.status_code} - {resp.text}"

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
        museum_config=?, museum_ids=?, strike_minutes=?, offset_ms=?, workflow_file=? 
        WHERE system=?''', 
        (s['base_url'], s['lib_user'], s['lib_pass'], s['patron_email'], s['ntfy_topic'],
         s['drop_time'], s['app_mode'], s['priority_museums'], s['auto_book_days'],
         s['museum_config'], s['museum_ids'], s['strike_minutes'], s['offset_ms'], 
         s['workflow_file'], system))
    conn.commit()
    conn.close()

    # 2. Sync to GitHub & Get Forensic Report
    gh_report = update_gh_secrets(system, s)
    
    # 3. Handle Scheduling
    try:
        drop_dt = datetime.strptime(s['drop_time'], "%H:%M:%S")
        now = datetime.now()
        target_dt = now.replace(hour=drop_dt.hour, minute=drop_dt.minute, second=drop_dt.second, microsecond=0)
        if target_dt < now: target_dt += timedelta(days=1)
        trigger_dt = target_dt - timedelta(minutes=5)
        
        job_id = f"trigger_{system}"
        if scheduler.get_job(job_id): scheduler.remove_job(job_id)
        scheduler.add_job(trigger_dispatch, 'date', run_date=trigger_dt, args=[system, s['workflow_file']], id=job_id)
        
        return jsonify({"status": f"{gh_report}\nStrike scheduled: {trigger_dt.strftime('%H:%M:%S')} UTC"})
    except:
        return jsonify({"status": f"{gh_report}\n(Scheduling failed: Check time format)"})

@app.route('/run_now', methods=['POST'])
def run_now():
    system = request.json['system']
    msg = trigger_dispatch(system)
    return jsonify({"status": msg})

@app.route('/status')
def get_status():
    jobs = scheduler.get_jobs()
    res = {"SPL": "IDLE", "KCLS": "IDLE"}
    for j in jobs:
        sys = "SPL" if "SPL" in j.id else "KCLS"
        res[sys] = f"READY: Signal at {j.next_run_time.strftime('%H:%M:%S')}"
    return jsonify(res)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
