'use strict';

const fs   = require('fs');
const path = require('path');
// simple UUID v4 without external dep
function uuidv4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

// Raw records parsed from paria_unique.txt (duplicates removed — FRANCISCO GOMES appears twice)
const RAW = [
  { fn: 'MADALENA',          ln: 'DE PINA MOREIRA',                    dob: '01-11-1963', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA559873' },
  { fn: 'DEINILSON',         ln: 'PEREIRA BORGES',                     dob: '09-11-2009', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA479335' },
  { fn: 'OCTAVIO',           ln: 'PEREIRA REBELO',                     dob: '06-06-1965', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA477165' },
  { fn: 'DJEISON',           ln: 'DE PINA KTAVARES',                   dob: '19-08-2020', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA457223' },
  { fn: 'AYLA ALCIONE',      ln: 'DA VEIGACVIELRA',                    dob: '28-03-2017', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA399882' },
  { fn: 'ANA MARIA',         ln: 'DELGADO',                            dob: '20-04-1973', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA342232' },
  { fn: 'MARIA',             ln: 'CORREITA',                           dob: '29-06-1960', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA565240' },
  { fn: 'KEMILLY DANILA',    ln: 'SEMEDO FERREIRA',                    dob: '13-08-2017', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA505767' },
  { fn: 'KLEBER',            ln: 'SEMEDO LOPES',                       dob: '28-04-2009', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA554536' },
  { fn: 'KENNEDY HENRIQUE',  ln: 'SEMEDO FERREIRA',                    dob: '03-01-2019', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA505768' },
  { fn: 'ELISABETH SOFIA',   ln: 'MONTEIRO ORTET',                     dob: '02-11-1991', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA543360' },
  { fn: 'SOFIA',             ln: 'SOARES FORTES',                      dob: '20-09-1994', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA491993' },
  { fn: 'LEONICE',           ln: 'GONCALVES MENDES',                   dob: '10-05-2002', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA524740' },
  { fn: 'ERMELINDA',         ln: 'PEREIRA FERNANDES BENOLIEL',         dob: '14-07-1961', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA574563' },
  { fn: 'MARIA JESUS',       ln: 'VIEIRA LOPES GOMES FERNANDES',       dob: '01-04-1963', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA297648' },
  { fn: 'DAVIDSON',          ln: 'GARCIA CARDOSO',                     dob: '17-07-2012', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA365374' },
  { fn: 'MANUEL MARIA',      ln: 'GOMES FERNANDES',                    dob: '24-02-1965', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA370698' },
  { fn: 'IVAN GABRIEL',      ln: 'CARVALHO ALMADA',                    dob: '12-08-2017', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA411643' },
  { fn: 'DELCIO',            ln: 'SOARES MARTINS',                     dob: '25-12-2021', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA564594' },
  { fn: 'MARCOS GABRLIEL',   ln: 'ALMEIDA MENDES',                     dob: '02-07-2019', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA563413' },
  { fn: 'GABRIEL ANGEL',     ln: 'MENDES SEMEDO',                      dob: '15-12-2020', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA569892' },
  { fn: 'WINNE KINO',        ln: 'CORREIA MOREIRA',                    dob: '28-02-2008', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA514367' },
  { fn: 'DOMINGAS',          ln: 'CORREIA VAZ',                        dob: '11-06-1976', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA514760' },
  { fn: 'ORISANDRA HELENA',  ln: 'MONIZ FERNANDES',                    dob: '21-10-1994', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA304093' },
  { fn: 'KELVIN RICARDO',    ln: 'SEMEDO MONTEIRO',                    dob: '12-01-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA422863' },
  { fn: 'HELLEN ALESSANDRA', ln: 'GONCALVES DA VEIGA',                 dob: '18-07-2019', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA518045' },
  { fn: 'THAMIRIS',          ln: 'RAMOS ORTET',                        dob: '30-04-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA559329' },
  { fn: 'MARIA SALOME',      ln: 'BARBOSA MOREIRA',                    dob: '20-08-1969', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA431064' },
  { fn: 'EDGAR ANTONIO',     ln: 'DA SILVA MONTEIRO',                  dob: '16-06-1995', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA322188' },
  { fn: 'ABNER',             ln: 'DOS SANTOS MOREIRA',                 dob: '08-07-1995', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA543860' },
  { fn: 'MANUEL ANTONIO',    ln: 'COSTA TAVARES GOMES',                dob: '06-09-1975', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA403282' },
  { fn: 'ANDREIA CARINA',    ln: 'DIAS LOPES',                         dob: '21-05-1989', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA523478' },
  { fn: 'ELIANE NADINE',     ln: 'DA VEIGA MONTEIRO',                  dob: '02-02-1998', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA487481' },
  { fn: 'ARIANE PATRICIA',   ln: 'SEMEDO SANCHES',                     dob: '13-01-2015', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA485259' },
  { fn: 'WILLIAN FILIPE',    ln: 'DA KNOURA DA VEIGA',                 dob: '18-01-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA408480' },
  { fn: 'ENZO PATRICK',      ln: 'GOMES MORENO',                       dob: '12-10-2021', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA573772' },
  { fn: 'AYANNA SOFIA',      ln: 'DE PINA LOBO',                       dob: '20-02-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA280577' },
  { fn: 'JOANA',             ln: 'MENDES GONCALVES',                   dob: '15-03-1953', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA506368' },
  { fn: 'SAMIRA DA CONCEICAO', ln: 'BORGES FERNANDES',                 dob: '02-05-1982', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA426465' },
  { fn: 'ELLEN MARIA',       ln: 'DA MOURA DA VEIGA',                  dob: '16-06-2013', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA408479' },
  { fn: 'ARIEL',             ln: 'MOREIRA BORGES',                     dob: '24-12-2014', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA526911' },
  { fn: 'KELLY PATRICIA',    ln: 'FIDALGO CABRAL',                     dob: '06-05-2018', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA543854' },
  { fn: 'MARIA JESUS ESTER', ln: 'DA SILVA FIDALGO',                   dob: '17-06-1971', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA567785' },
  { fn: 'ALHAJI IBRAHIMK',   ln: 'KAMARA',                             dob: '20-04-1978', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA548390' },
  { fn: 'KILIANY',           ln: 'FREIRE MENDES',                      dob: '22-12-2020', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA553723' },
  { fn: 'JOYC SLETICIAK',    ln: 'CARDOSO GARCLIA',                    dob: '22-06-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA423681' },
  { fn: 'ANA LUISA',         ln: 'DE PINA FERNANDES',                  dob: '05-01-1980', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA539870' },
  { fn: 'EVELINE SOFILA',    ln: 'LOPES TAVARES',                      dob: '13-03-1990', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA452754' },
  { fn: 'DULCELINA',         ln: 'DE BRITO ANDRADE',                   dob: '29-11-1982', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA556033' },
  { fn: 'ADRIEL',            ln: 'MENDES VARELA',                      dob: '28-09-2020', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA521139' },
  { fn: 'DOMINGOS',          ln: 'ROSA TAVARES DE PINA',               dob: '15-03-1967', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA569635' },
  { fn: 'DENILTON DANIEL',   ln: 'DE PINA MORENO',                     dob: '29-05-2022', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA568086' },
  { fn: 'EDSON DAVID',       ln: 'ANDRADE LIMA',                       dob: '20-08-1993', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA546638' },
  { fn: 'ANA MAFALDA',       ln: 'LOPES SEMEDO BORGES',                dob: '01-11-1970', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA312923' },
  { fn: 'RAFAEL',            ln: 'GOMES SEMEDO NEVES',                 dob: '16-02-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA578854' },
  { fn: 'SEKOUBA',           ln: 'CAMARA GOMES',                       dob: '22-04-1985', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA299135' },
  { fn: 'FRANCISCO DE JESUS', ln: 'CORREIA TAVARES',                   dob: '01-08-2016', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA517118' },
  { fn: 'AUREA TATIANA',     ln: 'VARELA TEIXEIRA',                    dob: '26-10-1990', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA306128' },
  { fn: 'EDUARDO',           ln: 'MIRANDA TAVARES',                    dob: '05-06-2011', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA552670' },
  { fn: 'SANDRA',            ln: 'ALVES DE SILVEIRA',                  dob: '25-08-1981', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA581076' },
  { fn: 'MARIA AUZENDA',     ln: 'CABRAL TAVARES',                     dob: '14-09-1963', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA555164' },
  { fn: 'MARIA',             ln: 'MENDES VARELA PEREIRA',              dob: '25-08-1962', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA498458' },
  { fn: 'KAYLA MICAELA',     ln: 'VARELA SEMEDO',                      dob: '26-09-2023', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA574561' },
  { fn: 'DEBORA NOEMI',      ln: 'VARELA SEMEDO',                      dob: '04-10-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA423914' },
  { fn: 'DIEGO',             ln: 'MONTEIRO MOREIRA',                   dob: '19-06-2012', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA446570' },
  { fn: 'IBRAHIMA',          ln: 'SARR',                               dob: '01-10-1991', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA364389' },
  { fn: 'ANTONIO PEDRO',     ln: 'SILVA VARELA',                       dob: '24-04-1959', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA570762' },
  { fn: 'DILEINA MARIA',     ln: 'MELO FORTES',                        dob: '27-10-1991', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA247080' },
  { fn: 'FRANCISCA PAULA',   ln: 'DE BARROS ALMEIDA',                  dob: '25-10-1980', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA580712' },
  { fn: 'CLAUDIA HELENA',    ln: 'SILVA GOMES',                        dob: '20-07-2018', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA578946' },
  { fn: 'RAFAEL DE JESUS',   ln: 'DA VEIGA TAVARES',                   dob: '24-10-2016', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA411427' },
  { fn: 'MILLEY BLANCA',     ln: 'MONTEIRO FONSECA',                   dob: '25-07-2011', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA432120' },
  { fn: 'ELAINE ROSILHA',    ln: 'FONSECA VEIGA',                      dob: '08-08-1986', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA258386' },
  { fn: 'BENVINDO',          ln: 'LANDIM GOMES',                       dob: '19-05-1963', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA540801' },
  { fn: 'MARLA CELESTE',     ln: 'LOPES SANCHES MONIZ',                dob: '18-04-1980', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA572896' },
  { fn: 'TEREZA',            ln: 'DE PINA BARROS ANDRADE',             dob: '17-11-1968', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA312957' },
  { fn: 'MANUEL',            ln: 'SEMEDO FREIRE',                      dob: '23-07-1976', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA540035' },
  { fn: 'INDIRA',            ln: 'LOPES DOS SANTOS VARELA',            dob: '12-12-1986', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA462793' },
  { fn: 'CANDIDA',           ln: 'LOPES MENDES',                       dob: '25-10-1956', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA568316' },
  { fn: 'ODILIA',            ln: 'GOMES MORENO HORTA',                 dob: '20-10-1981', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA304031' },
  { fn: 'ROBSON EMANUEL',    ln: 'VAZ TEIXEIRA',                       dob: '28-03-2019', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA481641' },
  { fn: 'CINTIA SOFLA',      ln: 'GARCIA MONTEIRO',                    dob: '05-09-1989', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA286585' },
  { fn: 'AUSTIN ELIAS',      ln: 'GOMES FIDALGO',                      dob: '01-02-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA556901' },
  { fn: 'LUNA LAIS',         ln: 'GOMES LANDIM',                       dob: '05-05-2020', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA538038' },
  { fn: 'LUANA ANDRINE',     ln: 'GOMES LANDIM',                       dob: '18-10-2015', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA540447' },
  { fn: 'PAULINA',           ln: 'MENDES CABRAL',                      dob: '02-05-1971', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA565951' },
  { fn: 'CRISTIANE NADINE',  ln: 'ADELINO DA SILVA',                   dob: '06-05-2005', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA562834' },
  { fn: 'JANILSON',          ln: 'VIELRA KTAVARES',                    dob: '29-03-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA546212' },
  { fn: 'ALDEVINO',          ln: 'DOS SANTOS FORTES',                  dob: '02-09-1987', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA310196' },
  { fn: 'ULISSES CESAR',     ln: 'TAVARES SEMEDO',                     dob: '16-07-2014', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA344762' },
  { fn: 'ILCA MARLISE',      ln: 'TAVARES FERNANDES',                  dob: '08-10-1994', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA249274' },
  { fn: 'TERESA',            ln: 'DOS REIS GOMES SEMEDO',              dob: '02-04-1970', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA443132' },
  { fn: 'JEOVANE',           ln: 'CARDOSO VARELA',                     dob: '10-12-2007', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA274051' },
  { fn: 'AMADEUK',           ln: 'ALVES OLIVEIRA',                     dob: '02-12-1988', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA495917' },
  { fn: 'STAFANY',           ln: 'LOPES SILVA FERREIRA',               dob: '30-01-1986', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA577867' },
  { fn: 'TERESA',            ln: 'BORGES SILVA',                       dob: '10-03-1953', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA545149' },
  { fn: 'JELICE',            ln: 'TAVARES ANDRADE',                    dob: '17-02-2017', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA463128' },
  { fn: 'DELCY JULIETA',     ln: 'DELGADO VASCONCELOS DOS SANTOS',     dob: '10-03-1996', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA414265' },
  { fn: 'EDMILSON',          ln: 'FERNANDES BORGES',                   dob: '01-11-2010', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA562094' },
  { fn: 'KELIANE LAIS',      ln: 'DOS SANTOS TAVARES',                 dob: '22-01-2019', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA565699' },
  { fn: 'CRRISTIAN ASSIS',   ln: 'AGUES MENDONCA',                     dob: '26-03-2018', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA538349' },
  { fn: 'TEREZA',            ln: 'DOS SANTOS MOREIRA',                 dob: '28-11-1966', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA522651' },
  { fn: 'FRANCISCA',         ln: 'DA COSTA GOMES SEMEDO',              dob: '16-09-1975', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA380665' },
  { fn: 'CARLOS ANTONIO',    ln: 'BARROS CORREIA',                     dob: '10-01-1996', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA487155' },
  { fn: 'KESSIA ALLANA',     ln: 'DA VEIGA CORREIA',                   dob: '27-04-2023', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA487480' },
  { fn: 'GEBIANA',           ln: 'VIEIRA',                             dob: '11-01-1947', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA538804' },
  { fn: 'MONICA SOFIA',      ln: 'MOREIRA FURTADO',                    dob: '27-05-1989', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA328397' },
  { fn: 'TICIANE',           ln: 'MENDES PIRES VARELA',                dob: '26-05-2015', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA512833' },
  { fn: 'ROSYMERE',          ln: 'MENDES LOPES',                       dob: '17-04-2011', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA481288' },
  { fn: 'JILSON ANTONIO',    ln: 'FIDALGO SEMEDO',                     dob: '12-12-2021', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA492346' },
  { fn: 'MARCIO GABRIEL',    ln: 'PEREIRA CORREIA',                    dob: '26-09-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA542945' },
  { fn: 'NELIDA PATRICIA',   ln: 'MONTEIRO LOPES',                     dob: '20-12-2010', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA503619' },
  { fn: 'NEIMARA HELENA',    ln: 'TAVARES ANDRADE',                    dob: '21-01-2017', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA397675' },
  { fn: 'MARIA DE FATIMA',   ln: 'ALMEIDA MOREIRA',                    dob: '06-02-1969', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA488817' },
  { fn: 'RUDILSON',          ln: 'DE PINA SANCHES MORENO',             dob: '23-04-2012', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA483429' },
  { fn: 'MARIA ISABEL',      ln: 'FERNANDES CABRAL',                   dob: '12-09-1963', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA395191' },
  { fn: 'RONILSE MELANY',    ln: 'GOMES DA VEIGA',                     dob: '10-06-2016', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA500993' },
  { fn: 'GRACIETE',          ln: 'MENDES PEREIRA MOREIRA',             dob: '07-02-1989', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA436150' },
  { fn: 'MARIA',             ln: 'MENDES SEMEDO VIEIRA',               dob: '26-01-1958', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA539495' },
  { fn: 'AIMAR',             ln: 'GOMES VAZ',                          dob: '06-01-2014', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA491961' },
  { fn: 'FRANCISCO',         ln: 'GOMES',                              dob: '01-05-1994', g: 'M', nat: 'GUINEENSE',     pp: 'C00301725' },
  { fn: 'JERIMIAS',          ln: 'ALVES ALMEIDA',                      dob: '16-11-2020', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA532712' },
  { fn: 'MARIA DA LUZ',      ln: 'SOARES DA COSTA',                    dob: '15-04-1978', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA421577' },
  { fn: 'MONICA SOCORRO',    ln: 'VIEIRA CORREIA',                     dob: '12-04-1993', g: 'F', nat: 'CABO-VERDIANA', pp: 'PA576025' },
  { fn: 'LUAN CESAR',        ln: 'VASCONCELOS SANTANA',                dob: '25-09-2021', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA567402' },
  { fn: 'IVANILSON',         ln: 'SEMEDO LOPES',                       dob: '25-01-2009', g: 'M', nat: 'CABO-VERDIANA', pp: 'PA504924' },
];

// ── Helpers ────────────────────────────────────────────────────────────────────

function normalize(str) {
  return str
    .toLowerCase()
    .normalize('NFD').replace(/[̀-ͯ]/g, '') // strip accents
    .replace(/[^a-z0-9]/g, '');                        // keep alphanumeric only
}

function firstWord(str) {
  return str.trim().split(/\s+/)[0];
}

function lastWord(str) {
  const parts = str.trim().split(/\s+/);
  return parts[parts.length - 1];
}

function birthYear(dob) {
  // dob format: DD-MM-YYYY
  return dob.slice(-2); // last 2 digits of year
}

function passportSuffix(pp) {
  // last 4 chars; for short passports pad with 0s
  return pp.slice(-4).padStart(4, '0');
}

function escapeCsv(v) {
  const s = String(v ?? '');
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

// ── Generate ──────────────────────────────────────────────────────────────────

const HEADERS = [
  'Account Id', 'First Name', 'Last Name', 'Date of Birth', 'Gender',
  'Nationality', 'Passport Number', 'User Name', 'Password',
  'Email Address', 'Registered Session Id', 'Logged In', 'Status', 'File Name',
  'Worker Id', 'Workflow', 'Workflow State', 'Started At', 'Finished At',
  'Retry Count', 'Terminated', 'Failure Reason',
];

const rows = [HEADERS.map(escapeCsv).join(',')];

for (const r of RAW) {
  const fields = [
    uuidv4(),   // Account Id
    r.fn,       // First Name
    r.ln,       // Last Name
    r.dob,      // Date of Birth
    r.g,        // Gender
    r.nat,      // Nationality
    r.pp,       // Passport Number
    '',         // User Name  — generated by manager at runtime
    '',         // Password   — generated by manager at runtime
    '',         // Email Address
    '',         // Registered Session Id
    '',         // Logged In
    '',         // Status
    '',         // File Name
    '',         // Worker Id
    '',         // Workflow
    '',         // Workflow State
    '',         // Started At
    '',         // Finished At
    '0',        // Retry Count
    '',         // Terminated
    '',         // Failure Reason
  ];
  rows.push(fields.map(escapeCsv).join(','));
}

const outPath = path.join(__dirname, '..', 'accounts.csv');
fs.writeFileSync(outPath, rows.join('\n') + '\n', 'utf8');
console.log(`Written ${RAW.length} accounts to ${outPath}`);
