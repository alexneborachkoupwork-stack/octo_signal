import sys, json
sys.path.insert(0, '.')
import session as sess
import solver as sol
import csv

# First test account
with open('data/test_accounts.csv') as f:
    acct = list(csv.DictReader(f))[0]
print(f"Account: {acct['username']}")

# First SOAX proxy
with open('data/proxies_soax.txt') as f:
    proxy = f.readline().strip()
print(f"Proxy: {proxy[:70]}...")

# Session
print("Getting session...")
s = sess.get_session(proxy)
print("Session ready.")

# Solver keys from .env
env = {}
for line in open('.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('=')
        env[k.strip()] = v.strip()

def keys(k):
    return [x.strip() for x in env.get(k, '').split(',') if x.strip()]

cap  = keys('CAPSOLVER_KEYS')
ant  = keys('ANTICAPTCHA_KEYS')
cap2 = keys('TWOCAPTCHA_KEYS')
cap3 = keys('CAPMONSTER_KEYS')
print(f"Solvers: capsolver={len(cap)} anticaptcha={len(ant)} twocaptcha={len(cap2)} capmonster={len(cap3)}")

# CAPTCHA
print("Solving CAPTCHA...")
token = sol.race_all(cap, ant, cap2, cap3, 'LOGIN_EVISA', None, min_score=65)
print(f"Token len={len(token)} score={sol.score_token(token)}")

# Login POST
LOGIN_URL = 'https://pedidodevistos.mne.gov.pt/VistosOnline/login'
payload = {
    'username':        acct['username'],
    'password':        acct['password'],
    'language':        'PT',
    'rgpd':            'Y',
    'captchaResponse': token,
}
print("POSTing login via browser_fetch...")
result = s.browser_fetch(LOGIN_URL, data=payload, headers=sess.HEADERS_XHR, timeout=25)
print(f"HTTP status : {result.get('status')}")
safe_body = (result.get('body','')[:400]).encode('ascii', errors='replace').decode('ascii')
print(f"Body (400)  : {safe_body!r}")
