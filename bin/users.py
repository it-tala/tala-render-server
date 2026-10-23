"""Kelola akun web Tala Render.

Password disimpan sebagai hash PBKDF2-SHA256 (600.000 iterasi, salt acak per user).
Password asli tidak pernah disimpan.

Pemakaian (CMD sebagai Administrator):
  python users.py list
  python users.py add <username> [--role user|admin]   (password diketik tersembunyi)
  python users.py passwd <username>
  python users.py remove <username>
  python users.py check <username>                     (tes login)
"""
import base64
import datetime
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys

USERS_FILE = r'D:\RenderServer\app\config\users.json'
ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    b64 = lambda b: base64.b64encode(b).decode('ascii')
    return f'pbkdf2_sha256${ITERATIONS}${b64(salt)}${b64(dk)}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        calc = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                   base64.b64decode(salt), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def load():
    if not os.path.exists(USERS_FILE):
        return {'users': {}}
    with open(USERS_FILE, encoding='utf-8') as f:
        return json.load(f)


def save(data):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, USERS_FILE)


def set_user(username, password, role='user'):
    if not re.fullmatch(r'[a-z0-9_.-]{2,32}', username):
        raise ValueError('username: huruf kecil/angka/._- (2-32 karakter)')
    if len(password) < 4:
        raise ValueError('password minimal 4 karakter')
    data = load()
    now = datetime.datetime.now().isoformat(timespec='seconds')
    old = data['users'].get(username, {})
    data['users'][username] = {
        'password': hash_password(password),
        'role': role or old.get('role', 'user'),
        'created': old.get('created', now),
        'updated': now,
    }
    save(data)


def _ask_password():
    p1 = getpass.getpass('Password baru: ')
    p2 = getpass.getpass('Ulangi password: ')
    if p1 != p2:
        sys.exit('Password tidak sama.')
    return p1


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    data = load()
    if cmd == 'list':
        for u, info in sorted(data['users'].items()):
            print(f"{u:<16} {info['role']:<6} dibuat {info['created']}  diubah {info['updated']}")
        return
    if len(argv) < 3:
        sys.exit(__doc__)
    user = argv[2].lower()
    if cmd == 'add':
        role = argv[argv.index('--role') + 1] if '--role' in argv else 'user'
        if user in data['users']:
            sys.exit(f'{user} sudah ada. Pakai: passwd {user}')
        set_user(user, _ask_password(), role)
        print(f'User {user} ({role}) dibuat.')
    elif cmd == 'passwd':
        if user not in data['users']:
            sys.exit(f'{user} tidak ada.')
        set_user(user, _ask_password(), data['users'][user]['role'])
        print(f'Password {user} diganti.')
    elif cmd == 'remove':
        if data['users'].pop(user, None) is None:
            sys.exit(f'{user} tidak ada.')
        save(data)
        print(f'User {user} dihapus.')
    elif cmd == 'check':
        info = data['users'].get(user)
        ok = bool(info) and verify_password(getpass.getpass('Password: '), info['password'])
        print('Login BENAR' if ok else 'Login SALAH')
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
