import primp

c = primp.Client(proxy="http://Mylist1234-RO-1:Saulo12345@p.webshare.io:80", verify=False, timeout=15)
r = c.get("https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp")
print("status:", r.status_code)
has_form = 'name="username"' in r.text or 'type="password"' in r.text
has_error = "Nao foi" in r.text or "could not be processed" in r.text.lower() or "Não foi" in r.text
print("login_form:", has_form, "  error_page:", has_error)
print("title:", r.text[r.text.find("<title>")+7:r.text.find("</title>")])
print("body[:600]:", r.text[:600])
