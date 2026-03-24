import os, base64, requests, sqlite3, sys, pytz, json
from flask import Flask, render_template, request, jsonify
from nacl import encoding, public
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = "/app/data/sniper.db"
jobstores = {'default': SQLAlchemyJobStore(url=f'sqlite:///{DB_PATH}')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()

GH_AUTH = {
    'SPL': {'token': os.getenv('SPL_GH_TOKEN', ''), 'owner': os.getenv('SPL_GH_OWNER', ''), 'repo': os.getenv('SPL_GH_REPO', '')},
    'KCLS': {'token': os.getenv('KCLS_GH_TOKEN', ''), 'owner': os.getenv('KCLS_GH_OWNER', ''), 'repo': os.getenv('KCLS_GH_REPO', '')}
}

# Authoritative secret mapping
SECRET_KEYS = [
    'BASE_URL', 'LIB_USER', 'LIB_PASS', 'PATRON_EMAIL', 'NTFY_TOPIC', 
    'DROP_TIME', 'APP_MODE', 'PRIORITY_MUSEUMS', 'AUTO_BOOK_DAYS', 
    'MUSEUM_CONFIG', 'MUSEUM_IDS', 'STRIKE_MINUTES', 'OFFSET_MS'
]

def get_db_connection():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS configs (
                        system TEXT PRIMARY KEY, workflow_file TEXT,
                        base_url TEXT, lib_user TEXT, lib_pass TEXT, patron_email TEXT,
                        ntfy_topic TEXT, drop_time TEXT, app_mode TEXT,
                        priority_museums TEXT, auto_book_days TEXT,
                        museum_config TEXT, museum_ids TEXT, strike_minutes TEXT, offset_ms TEXT
                    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS master_lists (
                        system TEXT PRIMARY KEY, raw_config TEXT, raw_slugs TEXT
                    )''')
    # Robust seeding for first-time or deleted DB
    for sys_key in ['SPL', 'KCLS']:
        conn.execute('INSERT OR IGNORE INTO configs (system, workflow_file, base_url, lib_user, lib_pass, ntfy_topic, drop_time, app_mode, strike_minutes, offset_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                     (sys_key, "actions.yml", "https://", "user", "pass", "topic", "18:00:00", "alert", "1.0", "-150"))
        conn.execute('INSERT OR IGNORE INTO master_lists (system, raw_config, raw_slugs) VALUES (?, ?, ?)', (sys_key, "", ""))
    conn.commit(); conn.close()

def encrypt_secret(public_key: str, secret_value: str) -> str:
    public_key_obj = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder)
    sealed_box = public.SealedBox(public_key_obj)
    return base64.b64encode(sealed_box.encrypt(secret_value.encode("utf-8"))).decode("utf-8")

def update_gh_secrets(system, s):
    auth = GH_AUTH[system]
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/vnd.github+json"}
    url_base = f"https://api.github.com/repos/{auth['owner']}/{auth['repo']}/actions/secrets"
    try:
        pk_r = requests.get(f"{url_base}/public-key", headers=headers, timeout=10).json()
        for key in SECRET_KEYS:
            val = s.get(key.lower(), "")
            if not val: continue 
            enc_val = encrypt_secret(pk_r['key'], str(val))
            requests.put(f"{url_base}/{key}", headers=headers, json={"encrypted_value": enc_val, "key_id": pk_r['key_id']}, timeout=10)
        return "GH Sync: OK"
    except Exception as e:
        return f"GH Sync Error: {str(e)}"

def trigger_dispatch(system, workflow_file):
    auth = GH_AUTH[system]
    url = f"https://api.github.com/repos/{auth['owner']}/{auth['repo']}/actions/workflows/{workflow_file}/dispatches"
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/vnd.github+json"}
    requests.post(url, headers=headers, json={"ref": "main"}, timeout=10)

@app.route('/')
def index():
    conn = get_db_connection()
    saved = {row['system']: dict(row) for row in conn.execute('SELECT * FROM configs').fetchall()}
    masters = {row['system']: dict(row) for row in conn.execute('SELECT * FROM master_lists').fetchall()}
    conn.close()
    return render_template('index.html', auth=GH_AUTH, saved=saved, masters=masters)

@app.route('/save_master', methods=['POST'])
def save_master():
    data = request.json
    conn = get_db_connection()
    conn.execute('UPDATE master_lists SET raw_config=?, raw_slugs=? WHERE system=?', (data['raw_config'], data.get('raw_slugs', ''), data['system']))
    conn.commit(); conn.close()
    return jsonify({"status": "Master Mappings Saved Locally"})

@app.route('/save', methods=['POST'])
def save():
    data = request.json
    system, s, tz_name = data['system'], data['SECRETS'], data.get('timezone', 'UTC')
    conn = get_db_connection()
    conn.execute('''UPDATE configs SET workflow_file=?, base_url=?, lib_user=?, lib_pass=?, 
        patron_email=?, ntfy_topic=?, drop_time=?, app_mode=?, priority_museums=?, 
        auto_book_days=?, museum_config=?, museum_ids=?, strike_minutes=?, offset_ms=? WHERE system=?''', 
        (s.get('workflow_file'), s.get('base_url'), s.get('lib_user'), s.get('lib_pass'), s.get('patron_email'), 
         s.get('ntfy_topic'), s.get('drop_time'), s.get('app_mode'), s.get('priority_museums'), s.get('auto_book_days'), 
         s.get('museum_config'), s.get('museum_ids'), s.get('strike_minutes'), s.get('offset_ms'), system))
    conn.commit(); conn.close()
    try:
        local_tz = pytz.timezone(tz_name)
        dt_obj = datetime.strptime(s['drop_time'], "%H:%M:%S")
        now_local = datetime.now(local_tz)
        local_dt = local_tz.localize(datetime(now_local.year, now_local.month, now_local.day, dt_obj.hour, dt_obj.minute, dt_obj.second))
        if local_dt < datetime.now(local_tz): local_dt += timedelta(days=1)
        utc_dt = local_dt.astimezone(pytz.UTC)
        s_gh = s.copy(); s_gh['drop_time'] = utc_dt.strftime("%H:%M:%S")
        gh_msg = update_gh_secrets(system, s_gh)
        trigger_dt = utc_dt - timedelta(minutes=5)
        job_id = f"trigger_{system}"
        if scheduler.get_job(job_id): scheduler.remove_job(job_id)
        scheduler.add_job(trigger_dispatch, 'date', run_date=trigger_dt, args=[system, s['workflow_file']], id=job_id, misfire_grace_time=3600)
        return jsonify({"status": f"{gh_msg} | Scheduled: {utc_dt.strftime('%H:%M:%S')} UTC"})
    except Exception as e:
        return jsonify({"status": f"Local Saved. Sched Error: {str(e)}"}), 400

@app.route('/clear', methods=['POST'])
def clear_schedule():
    job_id = f"trigger_{request.json['system']}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id); return jsonify({"status": "Schedule Cleared."})
    return jsonify({"status": "No active schedule."})

@app.route('/run_now', methods=['POST'])
def run_now():
    trigger_dispatch(request.json['system'], request.json['workflow_file'])
    return jsonify({"status": "Manual Strike Dispatched."})

@app.route('/status')
def status():
    res = {"SPL": "IDLE", "KCLS": "IDLE"}
    for j in scheduler.get_jobs():
        sys_key = "SPL" if "SPL" in j.id else "KCLS"
        res[sys_key] = f"SIG @ {j.next_run_time.strftime('%H:%M:%S')} UTC"
    return jsonify(res)

if __name__ == '__main__':
    init_db(); app.run(host='0.0.0.0', port=5000)
