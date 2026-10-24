import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the real footer button handler; no browser navigation or network.
const source=fs.readFileSync('src/app/static/concierge.js','utf8');
const chip=source.slice(source.indexOf('  function setupChip('),source.indexOf('  function paintSetupCta('));
const context={window:{location:{href:''}},document:{createElement(){return {addEventListener(name,fn){this[name]=fn;}};}}};
vm.createContext(context);vm.runInContext(chip,context);
for(const key of ['diary','records','charges']){
  const button=context.setupChip({key,label:key,href:'/app/setup/'+key,substeps:[{id:key+'.section'}]},true);
  assert.equal(button.textContent,'Start guided setup');
  button.click();assert.equal(context.window.location.href,'/app/setup/guide');
}
const legacy=context.setupChip({key:'invoicing',label:'Getting paid',href:'/app/invoices/settings'},true);
legacy.click();assert.equal(context.window.location.href,'/app/invoices/settings');
console.log('Grouped setup nudges enter the guide; existing legacy links retain their destination.');
