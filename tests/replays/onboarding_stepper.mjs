import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const template=fs.readFileSync('src/app/templates/onboarding/base.html','utf8');
const start=template.indexOf('// Step names are disclosures');
const source=template.slice(start,template.indexOf('// Auto-grow wizard textareas',start));
const buttons=Array.from({length:9},(_,i)=>({attributes:{'aria-expanded':i===3?'true':'false','aria-controls':'label-'+i,...(i===3?{'data-current-step':''}:{})},listeners:{},
  getAttribute(name){return this.attributes[name];},setAttribute(name,value){this.attributes[name]=value;},hasAttribute(name){return Object.hasOwn(this.attributes,name);},
  addEventListener(name,handler){this.listeners[name]=handler;},focus(){this.focused=true;}}));
const labels=Object.fromEntries(buttons.map((button,i)=>['label-'+i,{hidden:i!==3}]));
const stepper={inspecting:false,querySelectorAll(){return buttons;},toggleAttribute(name,value){assert.equal(name,'data-stepper-inspecting');this.inspecting=value;}};
vm.runInNewContext(source,{document:{querySelectorAll(){return [stepper];},getElementById(id){return labels[id];}}});
const shown=()=>buttons.map((b,i)=>!labels['label-'+i].hidden?i:null).filter(i=>i!==null);
assert.deepEqual(shown(),[3]);
buttons[4].listeners.click();assert.deepEqual(shown(),[3,4]);assert.equal(buttons[4].attributes['aria-expanded'],'true');assert(stepper.inspecting);
buttons[5].listeners.click();assert.deepEqual(shown(),[3,5]);assert.equal(buttons[4].attributes['aria-expanded'],'false');
buttons[5].listeners.click();assert.deepEqual(shown(),[3]);assert(!stepper.inspecting);
buttons[0].listeners.click();buttons[0].listeners.keydown({key:'Escape'});assert.deepEqual(shown(),[3]);assert(buttons[0].focused);
buttons[8].listeners.click();buttons[3].listeners.click();assert.deepEqual(shown(),[3]);assert.equal(buttons[3].attributes['aria-expanded'],'true');
assert(buttons.every((b,i)=>b.hasAttribute('data-current-step')===(i===3)));
console.log('Stepper disclosures retain current state, toggle a single other name and close with Escape without navigation.');
