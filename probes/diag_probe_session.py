"""
Diagnose what the portal actually returns via a single Webshare proxy.
Step 1: GET home page (get session cookies + follow redirects).
Step 2: GET Authentication.jsp WITH those cookies.
Prints full response headers and body snippet for each step.
"""
import ssl, socket, base64, sys
from urllib.parse import urlparse

PROXY   = "http://Mylist1234-RO-1:Saulo12345@p.webshare.io:80"
HOME    = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH    = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
TIMEOUT = 15

def tls_conn(proxy_line, host, port):
    p = urlparse(proxy_line)
    creds = base64.b64encode(f"{p.username}:{p.password}".encode()).decode()
    s = socket.create_connection((p.hostname, p.port or 80), timeout=TIMEOUT)
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nProxy-Authorization: Basic {creds}\r\n\r\n"
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    first = resp.decode(errors="replace").split("\r\n")[0]
    print(f"  CONNECT → {first}")
    if "200" not in first:
        raise ConnectionError(first)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(s, server_hostname=host)

def http_get(ts, host, path, extra_headers=""):
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36\r\n"
        f"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
        f"Accept-Language: en-US,en;q=0.5\r\n"
        f"Connection: close\r\n"
        f"{extra_headers}"
        "\r\n"
    )
    ts.sendall(req.encode())
    raw = b""
    while True:
        chunk = ts.recv(8192)
        if not chunk:
            break
        raw += chunk
        if len(raw) > 65536:
            break
    return raw.decode(errors="replace")

def parse_headers(raw):
    head, _, body = raw.partition("\r\n\r\n")
    lines = head.split("\r\n")
    status = lines[0]
    headers = {}
    for l in lines[1:]:
        if ":" in l:
            k, _, v = l.partition(":")
            headers[k.strip().lower()] = v.strip()
    return status, headers, body

def extract_cookies(headers):
    cookies = []
    for k, v in headers.items():
        if k == "set-cookie":
            cookies.append(v.split(";")[0].strip())
    return cookies

# We need to collect ALL set-cookie headers, but our dict collapses duplicates.
# Re-parse manually:
def extract_all_cookies(raw_head):
    cookies = []
    for line in raw_head.split("\r\n"):
        if line.lower().startswith("set-cookie:"):
            val = line.split(":", 1)[1].strip().split(";")[0].strip()
            cookies.append(val)
    return cookies

print("=" * 60)
print(f"Proxy: {PROXY}")
print()

# --- Step 1: GET home page ---
print("STEP 1: GET /VistosOnline/")
ts = tls_conn(PROXY, "pedidodevistos.mne.gov.pt", 443)
raw1 = http_get(ts, "pedidodevistos.mne.gov.pt", "/VistosOnline/")
head1, _, body1 = raw1.partition("\r\n\r\n")
print(f"  Status line: {head1.split(chr(10))[0].strip()}")
cookies = extract_all_cookies(head1)
print(f"  Cookies set: {cookies}")
location = ""
for line in head1.split("\r\n"):
    if line.lower().startswith("location:"):
        location = line.split(":", 1)[1].strip()
print(f"  Location: {location!r}")
print(f"  Body snippet: {body1[:300]!r}")
print()

# --- Step 2: GET Authentication.jsp WITH cookies ---
print("STEP 2: GET /VistosOnline/Authentication.jsp (with cookies)")
cookie_header = ""
if cookies:
    cookie_header = "Cookie: " + "; ".join(cookies) + "\r\n"
    print(f"  Sending cookies: {cookie_header.strip()}")

ts2 = tls_conn(PROXY, "pedidodevistos.mne.gov.pt", 443)
raw2 = http_get(ts2, "pedidodevistos.mne.gov.pt", "/VistosOnline/Authentication.jsp",
                extra_headers=cookie_header)
head2, _, body2 = raw2.partition("\r\n\r\n")
print(f"  Status line: {head2.split(chr(10))[0].strip()}")
print(f"  Body snippet: {body2[:600]!r}")
print()

# --- Step 3: GET Authentication.jsp WITHOUT cookies (control) ---
print("STEP 3 (control): GET /VistosOnline/Authentication.jsp WITHOUT cookies")
ts3 = tls_conn(PROXY, "pedidodevistos.mne.gov.pt", 443)
raw3 = http_get(ts3, "pedidodevistos.mne.gov.pt", "/VistosOnline/Authentication.jsp")
head3, _, body3 = raw3.partition("\r\n\r\n")
print(f"  Status line: {head3.split(chr(10))[0].strip()}")
print(f"  Body snippet: {body3[:400]!r}")

# Check what keyword is in body3 vs body2
print()
print("=" * 60)
for label, body in [("with cookies", body2), ("no cookies", body3)]:
    has_form = 'name="username"' in body or 'type="password"' in body
    has_error = "Não foi possível" in body or "Request could not" in body
    print(f"  [{label}]  login-form={has_form}  error-page={has_error}")
