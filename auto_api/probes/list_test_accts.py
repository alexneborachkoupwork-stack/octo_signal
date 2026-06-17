import csv, sys
rows = list(csv.DictReader(open('auto_api/data/test_accounts.csv', encoding='utf-8')))
print('Available test accounts:')
for r in rows[:15]:
    print(f"  {r['username']}  status={r.get('status','')}  nat={r.get('nationality','')}  res={r.get('residence','')}")
