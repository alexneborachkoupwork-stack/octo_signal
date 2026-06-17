import json, re

har = json.load(open('c:/Users/Administrator/Documents/Maru/octo_connection/auto_api/har/apply.har', encoding='utf-8'))
entries = har.get('log', {}).get('entries', [])

for e in entries:
    url = e.get('request', {}).get('url', '')
    if 'Questionario' in url and 'Next' not in url and e.get('request', {}).get('method') == 'GET':
        content = e.get('response', {}).get('content', {})
        text = content.get('text', '') or ''
        print('=== Questionario GET response ===')
        print('Size:', len(text))
        forms = re.findall(r'<form[^>]*>', text, re.I)
        for f in forms[:5]:
            print('FORM:', f[:300])
        # All input fields
        inputs = re.findall(r'<input[^>]*/?>|<input[^>]*>', text, re.I)
        print('\nALL INPUTS (%d):' % len(inputs))
        for inp in inputs[:40]:
            print(' ', inp[:200])
        # textarea
        textareas = re.findall(r'<textarea[^>]*>[^<]*</textarea>', text, re.I)
        print('\nTEXTAREAS (%d):' % len(textareas))
        for t in textareas[:10]:
            print(' ', t[:200])
        break

print('\n=== Formulario POST fields from HAR ===')
for e in entries:
    url = e.get('request', {}).get('url', '')
    if 'Formulario' in url and e.get('request', {}).get('method') == 'POST':
        body = e.get('request', {}).get('postData', {})
        print('URL:', url)
        print('Body text:', (body.get('text') or '')[:1000])
        params = body.get('params', [])
        print('Params:')
        for p in params:
            print(f"  {p['name']}={p['value']}")
        print('Response size:', e.get('response',{}).get('content',{}).get('size',0))
        break
