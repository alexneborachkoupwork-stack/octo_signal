"""Trace portal navigation via primp — try multiple proxies until one responds."""
import primp

# Try several proxies across geos
PROXIES = [
    "http://Mylist1234-RO-1:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-RO-5:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-GB-3:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-GB-10:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-PT-2:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-ES-7:Saulo12345@p.webshare.io:80",
    "http://Mylist1234-IT-4:Saulo12345@p.webshare.io:80",
]

URLS = [
    "https://pedidodevistos.mne.gov.pt/VistosOnline/",
    "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp",
]

for proxy in PROXIES:
    print(f"\n{'='*60}")
    print(f"PROXY: {proxy}")
    for url in URLS:
        try:
            c = primp.Client(proxy=proxy, verify=False, timeout=15, follow_redirects=False)
            r = c.get(url)
            title_s = r.text.find("<title>")
            title_e = r.text.find("</title>")
            title   = r.text[title_s+7:title_e].strip() if title_s != -1 else "(none)"
            has_form  = 'name="username"' in r.text or 'type="password"' in r.text
            has_error = "Não foi" in r.text
            loc = ""
            for k, v in r.headers.items():
                if k.lower() == "location":
                    loc = v
            print(f"  {r.status_code} {url.split('/')[-1] or '/':<30s}  title={title[:50]!r}  form={has_form}  err={has_error}  loc={loc!r}")
        except Exception as e:
            print(f"  ERR {url.split('/')[-1] or '/':<30s}  {e}")
