import os, base64, requests, json
from flask import Flask, render_template, request, jsonify
from nacl import encoding, public
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# Load credentials from Environment Variables
GH_CONFIG = {
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

def encrypt_secret(public_key: str, secret_value: str) -> str:
    public_key_obj = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder)
    sealed_box = public.SealedBox(public_key_obj)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")

def gh_api_call(method, path, token, owner, repo, data=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    if method == "GET": return requests.get(url, headers=headers).json()
    if method == "PUT": return requests.put(url, headers=headers, json=data)
    if method == "POST": return requests.post(url, headers=headers, json=data)

def update_gh_secrets(system, secrets_dict):
    cfg = GH_CONFIG[system]
    pk_data = gh_api_call("GET", "actions/secrets/public-key", cfg['token'], cfg['owner'], cfg['repo'])
    
    for key, value in secrets_dict.items():
        if not value: continue
        encrypted = encrypt_secret(pk_data['key'], value)
        gh_api_call("PUT", f"actions/secrets/{key}", cfg['token'], cfg['owner'], cfg['repo'], 
                    {"encrypted_value": encrypted, "key_id": pk_data['key_id']})

def trigger_dispatch(system):
    cfg = GH_CONFIG[system]
    print(f"[{datetime.now()}] Triggering workflow for {system} ({cfg['repo']})...")
    gh_api_call("POST", "actions/workflows/actions.yml/dispatches", 
                cfg['token'], cfg['owner'], cfg['repo'], {"ref": "main"})

@app.route('/')
def index():
    # Pass the config to the UI so fields are pre-filled (but token is hidden)
    return render_template('index.html', config=GH_CONFIG)

@app.route('/save', methods=['POST'])
def save():
    data = request.json
    system = data['system']
    
    # 1. Update Secrets
    update_gh_secrets(system, data['SECRETS'])
    
    # 2. Handle Scheduling
    drop_time_str = data['SECRETS'].get('DROP_TIME')
    if drop_time_str:
        # Expected format HH:MM:SS (UTC)
        try:
            drop_dt = datetime.strptime(drop_time_str, "%H:%M:%S")
            now = datetime.now()
            target_dt = now.replace(hour=drop_dt.hour, minute=drop_dt.minute, second=drop_dt.second, microsecond=0)
            
            # If target time already passed today, assume it's for tomorrow
            if target_dt < now:
                target_dt += timedelta(days=1)

            # Trigger 5 minutes before the drop
            trigger_dt = target_dt - timedelta(minutes=5)
            
            job_id = f"trigger_{system}"
            if scheduler.get_job(job_id): scheduler.remove_job(job_id)
            scheduler.add_job(trigger_dispatch, 'date', run_date=trigger_dt, args=[system], id=job_id)
            
            return jsonify({"status": f"SUCCESS: Secrets updated. {system} runner scheduled for {trigger_dt.strftime('%H:%M:%S')} UTC"})
        except Exception as e:
            return jsonify({"status": f"ERROR: Invalid time format: {str(e)}"}), 400
    
    return jsonify({"status": "SUCCESS: Secrets updated (No trigger scheduled)"})

@app.route('/run_now', methods=['POST'])
def run_now():
    system = request.json['system']
    trigger_dispatch(system)
    return jsonify({"status": f"SUCCESS: {system} Workflow triggered manually."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
