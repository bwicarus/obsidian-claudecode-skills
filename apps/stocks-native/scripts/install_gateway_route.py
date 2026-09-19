"""Install the isolated gateway route with nginx validation and rollback."""
from pathlib import Path
import os
import shutil
import subprocess
import time

path = Path('/etc/nginx/sites-enabled/default').resolve()
original = path.read_bytes()
marker = b'    location /stocks   {'
route = b'''    # StocksNative private gateway (independent from the existing stock website)
    location ^~ /stocks-native/ {
        proxy_pass http://127.0.0.1:5012/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 660;
        client_max_body_size 16k;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }
'''
if b'location ^~ /stocks-native/' in original:
    print('StocksNative route already installed')
    raise SystemExit(0)
if original.count(marker) != 1:
    raise RuntimeError('Expected one existing stocks route; no configuration changed')
backup_dir = Path('/opt/stocks-native/backups')
backup_dir.mkdir(mode=0o700, exist_ok=True)
backup = backup_dir / ('nginx-default-' + time.strftime('%Y%m%d-%H%M%S'))
shutil.copy2(path, backup)
temporary = path.with_name(path.name + '.stocks-native.tmp')
temporary.write_bytes(original.replace(marker, route + marker, 1))
os.chmod(temporary, path.stat().st_mode)
os.replace(temporary, path)
try:
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
except BaseException:
    shutil.copy2(backup, path)
    subprocess.run(['nginx', '-t'], check=False)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=False)
    raise
print('Gateway route installed; nginx gracefully reloaded. Backup: ' + str(backup))
