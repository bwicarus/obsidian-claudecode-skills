import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';
const sandbox = { window:{} };
vm.runInNewContext(readFileSync(new URL('../../../../_server_deploy/static/pdf/rc-ink.js', import.meta.url),'utf8'),sandbox);
const reference = sandbox.window.RCInk;
const shapes = [
  {t:'pen',p:[[0.2,0.3],[0.7,0.6],[0.4,0.9]]},
  {pts:[[0.5,0.5]]}, {t:'line',p:[[0,0],[1,1]]},
  {t:'arrow',p:[[0.1,0.8],[0.9,0.1]]}, {t:'rect',p:[[0.2,0.2],[0.8,0.8]]},
  {t:'region',p:[[0.2,0.2],[0.8,0.2],[0.8,0.8],[0.5,0.5],[0.2,0.8]]},
  {t:'region',p:[[0.4,0.4],[0.4,0.4],[0.4,0.4]]}, {t:'pen',p:[]}
];
const hit = [];
for (const stroke of shapes) for (let x = 0; x <= 20; x++) for (let y = 0; y <= 20; y++) {
  const point=[x/20,y/20];
  hit.push({stroke,point,expected:reference.hit(stroke,point,0.018)});
}
const ordinal=[];
for (const stamps of [[3,1,2],[0,0,0],[null,-1,4]]) for (const values of [[null,null,null],[1,1,9],[4,2,7]]) {
  const input = stamps.map((t,i)=>({t:'region',id:['z','b','a'][i],createdAtEpochMs:t,ordinal:values[i]}));
  const expected = structuredClone(input); reference.ensureRegionOrdinals(expected);
  ordinal.push({input,expected});
}
writeFileSync(process.argv[2],JSON.stringify({hit,ordinal}));
console.log(`${hit.length} eraser cases and ${ordinal.length} region-number cases`);
