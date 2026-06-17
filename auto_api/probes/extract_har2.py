import json, re, base64

har = json.load(open('c:/Users/Administrator/Documents/Maru/octo_connection/auto_api/har/apply.har', encoding='utf-8'))
entries = har.get('log', {}).get('entries', [])

for e in entries:
    url = e.get('request', {}).get('url', '')
    if 'Questionario' in url and 'Next' not in url and e.get('request', {}).get('method') == 'GET':
        content = e.get('response', {}).get('content', {})
        text = content.get('text', '') or ''
        enc = content.get('encoding', '')
        raw_size = content.get('size', 0)
        print('=== Questionario GET ===')
        print('URL:', url)
        print('encoding:', enc, 'size:', raw_size, 'text_len:', len(text))
        if enc == 'base64' and text:
            decoded = base64.b64decode(text).decode('utf-8', errors='replace')
            print('decoded size:', len(decoded))
            # Show form actions and hidden inputs
            forms = re.findall(r'<form[^>]*>', decoded, re.I)
            print('FORMs:', forms[:3])
            inputs = re.findall(r'<input[^>]*/>', decoded, re.I)
            print('INPUTS (%d):' % len(inputs))
            for i in inputs[:30]:
                print(' ', i[:200])
        break
