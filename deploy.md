# 🚀 AlphaScanner Pro – End‑to‑End Deployment on Hostinger VPS

**Author:** Rahul  
**Domain:** `rahulintratrading.online`  
**VPS IP:** `187.127.187.235`  
**GitHub Repo:** `https://github.com/Rahul070994/Screener.git`  

This guide will help you deploy the **AlphaScanner Pro** trading bot from scratch on a Hostinger VPS running Ubuntu 24.04. It includes all steps – from installing dependencies to SSL setup and updating the code.

---

## 📋 Prerequisites

Before you start, make sure you have:

- SSH access to your VPS (root or sudo user)
- Your Kite Connect **API Key** and **API Secret** from the [Kite developer dashboard](https://developers.kite.trade/apps)
- Your Kite app’s **Redirect URL** set to `https://rahulintratrading.online/api/broker/callback`
- A domain (or subdomain) pointed to your VPS IP (already done in Hostinger DNS)

---

## 🔐 Step 1 – SSH into Your VPS

```bash
ssh root@187.127.187.235


🧹 Step 2 – Clean Up Any Previous Installations (Optional)
This ensures no leftover services or files interfere.

bash
# Stop and disable old services
sudo systemctl stop nginx gunicorn 2>/dev/null
sudo systemctl disable nginx gunicorn 2>/dev/null

# Kill any leftover Python processes
sudo pkill -f "python.*ultimate_scanner" || true

# Remove old project directories
sudo rm -rf /opt/alpha_scanner /var/www/alpha_scanner
📦 Step 3 – Install System Dependencies
bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git nginx
📁 Step 4 – Clone the GitHub Repository
bash
sudo mkdir -p /opt/alpha_scanner
sudo chown $USER:$USER /opt/alpha_scanner
cd /opt/alpha_scanner
git clone https://github.com/Rahul070994/Screener.git .
Check the contents:

bash
ls -la
# Expected: ultimate_scanner.py, strategies/, encrypt_keys.py, .env.example, etc.
🐍 Step 5 – Set Up Python Virtual Environment
bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
If you don’t have requirements.txt, install the core packages manually:

bash
pip install flask kiteconnect pandas numpy python-dotenv cryptography markupsafe gunicorn
🔑 Step 6 – Configure Environment Variables (.env)
Create the .env file:

bash
nano .env
Add the following content:

ini
MASTER_SECRET_KEY=FcsG9NoiuQ1y_Ml-JpDkk_kG-ZvMUNAriddrX_VdbEw
REDIRECT_URL=https://rahulintratrading.online/api/broker/callback
Important: The MASTER_SECRET_KEY is used to encrypt your Kite API credentials. Keep it secret. If you change it later, you must re‑encrypt users.json.

Save and exit (Ctrl+O, Enter, Ctrl+X).

🔒 Step 7 – Encrypt Kite Credentials (users.json)
The repository includes encrypt_keys.py which reads MASTER_SECRET_KEY from .env and encrypts your API key/secret.

Open the script:

bash
nano encrypt_keys.py
Locate the users dictionary and fill in your actual Kite credentials:

python
users = {
    "rahul": {
        "name": "Rahul",
        "kite_api_key": "z31wkyd5g3lo0vqx",           # <-- your API key
        "kite_api_secret": "qvvedczf5oli2zxlubkcn4p10rjy3yg0"   # <-- your secret
    }
}
Save and exit, then run the script:

bash
python encrypt_keys.py
This will generate users.json with encrypted credentials. You should see:

text
✅ users.json generated.
Verify it:

bash
cat users.json
🧩 Step 8 – Ensure Strategies Load Correctly
Your strategies/ folder contains strategy modules. The __init__.py inside must flatten the registry so the main app gets a flat dictionary of strategy functions.

Check the file:

bash
cat strategies/__init__.py
If it doesn’t contain the flattening logic, replace it with:

python
import os
import importlib

STRATEGY_REGISTRY = {}

for file in os.listdir(os.path.dirname(__file__)):
    if file.endswith('.py') and file != '__init__.py':
        module_name = file[:-3]
        try:
            module = importlib.import_module(f'strategies.{module_name}')
            if hasattr(module, 'all_strategies'):
                # Flatten: add all strategies from this module into the global registry
                STRATEGY_REGISTRY.update(module.all_strategies)
        except Exception as e:
            print(f"Error loading strategy {module_name}: {e}")
Save and exit.

Also, ensure v4_high_trust.py defines all_strategies as a dict of functions. If it’s missing, you can add a placeholder:

bash
echo "all_strategies = {'v4_high_trust': lambda df, ind: True}" >> strategies/v4_high_trust.py
(Replace with real strategies later.)

🧪 Step 9 – Test the Application Manually
bash
python ultimate_scanner.py
Open your browser and visit http://187.127.187.235:5000. You should see the login page.
Press Ctrl+C to stop the server.

⚙️ Step 10 – Set Up Gunicorn as a System Service
Create the systemd service file:

bash
sudo nano /etc/systemd/system/alpha_scanner.service
Paste the following:

ini
[Unit]
Description=AlphaScanner Pro
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/alpha_scanner
Environment="PATH=/opt/alpha_scanner/venv/bin"
EnvironmentFile=/opt/alpha_scanner/.env
ExecStart=/opt/alpha_scanner/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 ultimate_scanner:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
Save and exit. Then:

bash
sudo systemctl daemon-reload
sudo systemctl enable alpha_scanner
sudo systemctl start alpha_scanner
Check status:

bash
sudo systemctl status alpha_scanner
It should say active (running).

🌐 Step 11 – Configure Nginx as a Reverse Proxy
Create the Nginx site configuration:

bash
sudo nano /etc/nginx/sites-available/alpha_scanner
Paste:

nginx
server {
    listen 80;
    server_name rahulintratrading.online www.rahulintratrading.online;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /opt/alpha_scanner/static/;
        expires 30d;
    }
}
Enable the site and restart Nginx:

bash
sudo ln -s /etc/nginx/sites-available/alpha_scanner /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
🔒 Step 12 – Set Up SSL with Let’s Encrypt
Install Certbot:

bash
sudo apt install certbot python3-certbot-nginx -y
Obtain and install the SSL certificate:

bash
sudo certbot --nginx -d rahulintratrading.online -d www.rahulintratrading.online
Follow the interactive prompts:

Enter your email address.

Agree to the terms.

Choose whether to redirect HTTP to HTTPS (recommended: 2).

After success, your site will be available at https://rahulintratrading.online.

✅ Step 13 – Final Test
Visit https://rahulintratrading.online in your browser.

You should see the login page.

Select the user rahul (or whichever you set) and click Login.

You will be redirected to Kite for OAuth – authorize the app.

After redirect, you should see the AlphaScanner Pro dashboard.

🔁 Updating the Code from GitHub
When you push new changes to your repository, you can update the live server with a single command:

One‑line update
bash
cd /opt/alpha_scanner && git pull && sudo systemctl restart alpha_scanner
Full manual update (if you need to install new dependencies)
bash
cd /opt/alpha_scanner
git pull origin main   # or master
source venv/bin/activate
pip install -r requirements.txt   # if requirements changed
sudo systemctl restart alpha_scanner
Optional: Create a shortcut alias
bash
echo 'alias deploy="cd /opt/alpha_scanner && git pull && sudo systemctl restart alpha_scanner"' >> ~/.bashrc
source ~/.bashrc
Now typing deploy from anywhere will update and restart the app.

🧪 Troubleshooting Common Issues
InvalidToken error (cryptography.fernet)
Cause: The MASTER_SECRET_KEY in .env does not match the one used to encrypt users.json.

Fix: Regenerate users.json using encrypt_keys.py with the correct key. Ensure .env contains the same key.

502 Bad Gateway (Nginx)
Cause: Gunicorn is not running or not listening on 127.0.0.1:8000.

Fix: Restart the service:

bash
sudo systemctl restart alpha_scanner
sudo systemctl status alpha_scanner
ModuleNotFoundError (e.g., strategies)
Cause: The strategies/ folder is missing or __init__.py does not define STRATEGY_REGISTRY.

Fix: Create the folder and __init__.py as described in Step 8.

Strategy KeyError
Cause: The main app expects a flat dict of strategy functions, but STRATEGY_REGISTRY is nested.

Fix: Ensure __init__.py flattens the registry using .update(module.all_strategies).

📁 Final Directory Structure
text
/opt/alpha_scanner/
├── ultimate_scanner.py          # Main Flask app
├── strategies/                  # Strategy modules
│   ├── __init__.py              # Builds STRATEGY_REGISTRY (flattened)
│   └── v4_high_trust.py         # Contains all_strategies dict
├── encrypt_keys.py              # Encrypts Kite credentials
├── users.json                   # Encrypted user credentials
├── .env                         # Environment variables (MASTER_SECRET_KEY, REDIRECT_URL)
├── venv/                        # Python virtual environment
├── static/                      # Static assets (if any)
└── requirements.txt             # Python dependencies (if present)
🔐 Security Best Practices
Never commit .env or users.json to public repositories – they are already in .gitignore.

Keep your MASTER_SECRET_KEY safe. If lost, you must regenerate users.json with a new key.

Regularly update your Kite API secret if you suspect it is compromised.

Use strong, unique passwords and enable two‑factor authentication on your Kite account.