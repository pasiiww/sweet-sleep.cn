#!/usr/bin/env python3
"""Run as root from an uploaded release directory on the existing Sweet Sleep server."""
from pathlib import Path
import os
import pwd
import secrets
import shutil
import subprocess
import time
import urllib.request

source = Path(__file__).resolve().parent
target = Path('/opt/sweet-knowledge')
config_path = Path('/www/server/panel/vhost/nginx/wordpress.local.conf')
env_path = Path('/etc/sweet-knowledge.env')
unit_path = Path('/etc/systemd/system/sweet-knowledge.service')
old_config = config_path.read_text()
anchor = '    listen 443 ssl;'
if old_config.count(anchor) != 1 or 'server_name sweet-sleep.cn www.sweet-sleep.cn;' not in old_config:
    raise SystemExit('Unexpected Nginx config; inspect before installing')
if target.exists() and not unit_path.exists():
    raise SystemExit('Existing unmanaged /opt/sweet-knowledge; inspect before installing')
if 'location ^~ /knowledge/' not in old_config and 'location /knowledge' in old_config:
    raise SystemExit('Existing knowledge route; inspect before installing')
try:
    pwd.getpwnam('sweet-knowledge')
except KeyError:
    subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/sweet-knowledge', '--shell', '/sbin/nologin', 'sweet-knowledge'], check=True)
target.mkdir(exist_ok=True, mode=0o755)
(target / 'static').mkdir(exist_ok=True, mode=0o755)
backup_dir = Path('/var/backups/sweet-knowledge') / time.strftime('%Y%m%d-%H%M%S')
backup_dir.mkdir(parents=True, mode=0o700)
shutil.copy2(config_path, backup_dir / 'nginx.conf')
if (target / 'server.py').exists():
    shutil.copytree(target, backup_dir / 'app')
if unit_path.exists():
    shutil.copy2(unit_path, backup_dir / 'sweet-knowledge.service')
for filename in ('server.py', 'answers.py', 'entities.py', 'traces.py', 'qa.py', 'learning.py', 'stickers.py', 'notifications.py'):
    shutil.copy2(source / filename, target / filename)
    (target / filename).chmod(0o644)
for filename in ('index.html', 'style.css', 'app.js'):
    shutil.copy2(source / 'static' / filename, target / 'static' / filename)
    (target / 'static' / filename).chmod(0o644)
if not env_path.exists():
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(f'KB_ADMIN_TOKEN={secrets.token_urlsafe(32)}\nKB_READ_TOKEN={secrets.token_urlsafe(32)}\n'
                'KB_DATA_DIR=/var/lib/sweet-knowledge\nKB_STATIC_DIR=/opt/sweet-knowledge/static\nKB_PORT=8765\n')
shutil.copy2(source / 'sweet-knowledge.service', unit_path)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'sweet-knowledge'], check=True)
subprocess.run(['systemctl', 'restart', 'sweet-knowledge'], check=True)
for attempt in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/knowledge/', timeout=2) as response:
            assert response.status == 200
        break
    except OSError:
        if attempt == 19:
            raise SystemExit('Backend failed to start; Nginx remains unchanged')
        time.sleep(.5)
config = old_config
if 'zone=knowledge_api:' not in config:
    config = 'limit_req_zone $binary_remote_addr zone=knowledge_api:10m rate=5r/s;\n\n' + config
if 'location ^~ /knowledge/' not in config:
    config = config.replace(anchor, '''    location = /knowledge { return 301 /knowledge/; }
    location ^~ /knowledge/ {
        limit_req zone=knowledge_api burst=30 nodelay;
        limit_req_status 429;
        client_max_body_size 1m;
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 90s;
    }

''' + anchor)
# Allow binary sticker uploads; other JSON endpoints retain their own 1 MB limit.
start=config.index('location ^~ /knowledge/')
end=config.index('}',start)
config=config[:start]+config[start:end].replace('client_max_body_size 1m;', 'client_max_body_size 6m;')+config[end:]
config_path.write_text(config)
try:
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['nginx', '-s', 'reload'], check=True)
except subprocess.CalledProcessError:
    config_path.write_text(old_config)
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['nginx', '-s', 'reload'], check=True)
    raise
print(f'Deployed knowledge service. Nginx backup: {backup_dir}')
