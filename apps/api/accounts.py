"""Persistent password accounts. Short fixture credentials exist only in local demo profiles."""
import hashlib
import os
import re
import secrets
import sqlite3

from . import domain, store

ITERATIONS = 600_000  # OWASP Password Storage Cheat Sheet: PBKDF2-HMAC-SHA256.
PUBLIC_FIELDS = ('id', 'username', 'name', 'role', 'org_id')


def config(demo_mode=None):
    demo = os.getenv('DEBTOFF_DEMO_MODE', '1') == '1' if demo_mode is None else demo_mode
    profile = ('fresh' if os.getenv('DEBTOFF_START_PROFILE') == 'fresh' else 'demo') if demo else 'production'
    return {'demo_mode': demo, 'start_profile': profile,
            'allow_demo_shortcuts': profile == 'demo', 'local_test_accounts': profile == 'fresh'}


def hash_password(password):
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${value}'


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 600_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def public(row):
    return {key: row[key] for key in PUBLIC_FIELDS}


def bootstrap(demo_mode):
    profile = config(demo_mode)['start_profile']
    if profile == 'fresh':
        fixtures = [('staff', 'staff', '담당 직원', 'staff', 'staff'),
                    ('lawyer', 'lawyer', '담당 변호사', 'lawyer', 'lawyer'),
                    ('customer', 'customer', '김새봄', 'client', 'customer')]
    elif profile == 'demo':
        fixtures = [('staff', 'demo-staff', '담당 직원', 'staff', 'debtoff-demo'),
                    ('lawyer', 'demo-lawyer', '담당 변호사', 'lawyer', 'debtoff-demo'),
                    ('client', 'demo-client', '김예시', 'client', 'debtoff-demo')]
    else:
        fixtures = [(role, os.getenv(f'DEBTOFF_{role.upper()}_LOGIN', role), name, role,
                     os.getenv(f'DEBTOFF_{role.upper()}_PASSWORD', ''))
                    for role, name in [('staff', '담당 직원'), ('lawyer', '담당 변호사')]]
    with store.db() as con:
        for user_id, username, name, role, password in fixtures:
            existing = con.execute('SELECT * FROM accounts WHERE id=?', (user_id,)).fetchone()
            if existing:
                if existing['auth_profile'] != profile:
                    raise RuntimeError('Account profile differs from this database. Use an explicitly backed-up new profile database.')
                continue  # Restarting never resets an existing password.
            if profile == 'production' and (len(password) < 16 or password == 'debtoff-demo'):
                raise RuntimeError('Non-demo mode requires staff/lawyer credentials of at least 16 characters.')
            con.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (user_id, username, name, role, 'office-1', hash_password(password), 1,
                         profile, store.now(), store.now()))


def by_id(user_id, demo_mode):
    with store.db() as con:
        row = con.execute('SELECT * FROM accounts WHERE id=? AND active=1', (user_id,)).fetchone()
    return public(row) if row and row['auth_profile'] in {'custom', config(demo_mode)['start_profile']} else None


def authenticate(username, password, demo_mode):
    with store.db() as con:
        row = con.execute('SELECT * FROM accounts WHERE username=? AND active=1', (username,)).fetchone()
    # Perform the same password derivation for unknown usernames, avoiding an account timing oracle.
    dummy = f'pbkdf2_sha256${ITERATIONS}$' + '00' * 16 + '$' + '00' * 32
    valid = verify_password(password, row['password_hash'] if row else dummy)
    if not row or not valid or row['auth_profile'] not in {'custom', config(demo_mode)['start_profile']}:
        return None
    return public(row)


def create(actor, username, name, role, password):
    domain.require(actor['role'] == 'lawyer' or (actor['role'] == 'staff' and role == 'client'),
                   'ACCOUNT_ROLE_FORBIDDEN', '직원은 고객 계정만 만들 수 있습니다. 내부 계정은 변호사가 관리합니다.')
    domain.require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,79}', username)),
                   'ACCOUNT_USERNAME', '계정은 영문·숫자·점·밑줄·하이픈 3~80자로 입력해주세요.')
    domain.require(len(password) >= 16, 'ACCOUNT_PASSWORD', '새 계정 비밀번호는 16자 이상이어야 합니다.')
    domain.require(bool(name.strip()), 'ACCOUNT_NAME', '이름을 입력해주세요.')
    row = {'id': store.uid('user'), 'username': username, 'name': name.strip(), 'role': role,
           'org_id': actor['org_id']}
    try:
        with store.db() as con:
            con.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (*[row[key] for key in PUBLIC_FIELDS], hash_password(password), 1,
                         'custom', store.now(), store.now()))
    except sqlite3.IntegrityError:
        raise domain.DomainError('ACCOUNT_EXISTS', '이미 사용 중인 계정입니다.')
    return row


def assignees(org_id):
    with store.db() as con:
        return [public(row) for row in con.execute(
            "SELECT * FROM accounts WHERE org_id=? AND active=1 AND role IN ('staff','lawyer') ORDER BY role DESC,created_at,id",
            (org_id,)).fetchall()]
