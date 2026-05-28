'use strict';

// Cape Verdean identity generator — ported from extension/background.js
// Produces fake but culturally authentic CPV person data.

const FIRST_M = ['João','Carlos','Hélder','António','Manuel','Sérgio','Leandro','Dário','Orlando',
                 'Arlindo','Paulo','Filipe','Osvaldo','Wilfredo','Adílson','Valdemar','Hailton',
                 'Pedro','Rui','Nuno','Décio','Sandro','Edílson','Lúcio','Gilberto'];

const FIRST_F = ['Maria','Ana','Edna','Rosa','Lúcia','Eunice','Arminda','Sandra','Graça','Noemia',
                 'Filomena','Carla','Nair','Isadora','Vera','Conceição','Milena','Suzete','Lisete',
                 'Ercília','Anilsa','Dulce','Odete','Yara','Valdira'];

const LAST   = ['Semedo','Tavares','Correia','Lima','Varela','Monteiro','Évora','Fernandes',
                'Rodrigues','Furtado','Mendes','Barros','Cruz','Veiga','Delgado','Pires',
                'Andrade','Soares','Cardoso','Lopes','Brito','Gonçalves','Neves','Spencer',
                'Borges','Moreno','Duarte','Fontes','Mascarenhas','Santos'];

const PARTICLES = ['da','de','do','dos','das'];

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function rand(n)   { return Math.floor(Math.random() * n); }

function genGivenName(gender) {
  const pool = gender === 'M' ? FIRST_M : FIRST_F;
  const a = pick(pool);
  const r = rand(10);
  if (r < 4) {
    let b = pick(pool);
    if (b === a) b = pick(pool);
    return `${a} ${b}`;
  }
  if (r < 7) {
    const p = pick(PARTICLES);
    let b = pick(pool);
    if (b === a) b = pick(pool);
    return `${a} ${p} ${b}`;
  }
  return a;
}

function genSurname() {
  const a = pick(LAST);
  if (rand(2)) {
    let b = pick(LAST);
    if (b === a) b = pick(LAST);
    return `${a} ${b}`;
  }
  return a;
}

function genPassword() {
  const specials = '!@#$%&*_+';
  const digits   = '0123456789';
  const uppers   = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const lowers   = 'abcdefghijklmnopqrstuvwxyz';
  const all      = specials + digits + uppers + lowers;
  let chars = [
    specials[rand(specials.length)],
    digits[rand(digits.length)],
    uppers[rand(uppers.length)],
  ];
  for (let i = 0; i < 13; i++) chars.push(all[rand(all.length)]);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = rand(i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

function genDob() {
  const year  = 1964 + rand(41);
  const month = 1  + rand(12);
  const day   = 1  + rand(28);
  return `${year}/${String(month).padStart(2,'0')}/${String(day).padStart(2,'0')}`;
}

function genTraveldoc() {
  const prefix = pick(['PA','PB','PC']);
  const digits = String(rand(900000) + 100000);
  return prefix + digits;
}

function genUsername(firstName, lastName) {
  const f = firstName.split(' ')[0].toLowerCase().replace(/[^a-z]/g, '');
  const l = lastName.split(' ')[0].toLowerCase().replace(/[^a-z]/g, '');
  const n = String(rand(90000000) + 10000000);
  return (f.slice(0,3) + l.slice(0,3) + n).slice(0, 20);
}

/**
 * Generate a fake CPV person.
 * Returns fields sent as the realPerson payload to the extension.
 */
function generatePerson() {
  const gender    = rand(2) === 0 ? 'M' : 'F';
  const firstName = genGivenName(gender);
  const lastName  = genSurname();
  return {
    firstName,
    lastName,
    dob:         genDob(),
    gender,
    nationality: 'CPV',
    traveldoc:   genTraveldoc(),
    username:    genUsername(firstName, lastName),
    password:    genPassword(),
  };
}

module.exports = { generatePerson };
