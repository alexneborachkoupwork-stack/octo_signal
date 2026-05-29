'use strict';

const fs   = require('fs');
const path = require('path');

function uuidv4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function escapeCsv(v) {
  const s = String(v ?? '');
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

// YYYY/MM/DD → DD-MM-YYYY
function convertDate(d) {
  const [y, m, day] = d.split('/');
  return `${day}-${m}-${y}`;
}

const src  = path.join(__dirname, '..', 'data', 'identities.json');
const out  = path.join(__dirname, '..', 'test_accounts.csv');

const identities = JSON.parse(fs.readFileSync(src, 'utf8'));

const HEADERS = [
  'Account Id', 'First Name', 'Last Name', 'Date of Birth', 'Gender',
  'Nationality', 'Passport Number', 'User Name', 'Password',
  'Email Address', 'Registered Session Id', 'Logged In', 'Status', 'File Name',
  'Worker Id', 'Workflow', 'Workflow State', 'Started At', 'Finished At',
  'Retry Count', 'Terminated', 'Failure Reason',
];

const rows = [HEADERS.map(escapeCsv).join(',')];

for (const id of identities) {
  const fields = [
    uuidv4(),
    id.name,
    id.surname,
    convertDate(id.birth_date),
    id.gender,
    id.nationality,
    id.traveldoc,
    id.username,
    id.password,
    '', '', '', '', '', '', '', '', '', '', '0', '', '',
  ];
  rows.push(fields.map(escapeCsv).join(','));
}

fs.writeFileSync(out, rows.join('\n') + '\n', 'utf8');
console.log(`Written ${identities.length} accounts to ${out}`);
