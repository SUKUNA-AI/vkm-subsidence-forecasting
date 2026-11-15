import assert from 'node:assert/strict';
import {decodeProviderFrames as decode} from '../tools/budget_guard.js';
const a={id:'gen-one',choices:[{delta:{content:'literal [DONE] remains'}}]};
const b={id:'gen-one',usage:{cost:.001}};
let count=0;const check=(input,type='text/event-stream')=>{assert.deepEqual(decode(input,type),[a,b]);count++;};
check(`data: ${JSON.stringify(a)}\n\ndata: ${JSON.stringify(b)}\n\ndata: [DONE]\n\n`);
check(`: OPENROUTER PROCESSING\n\ndata: ${JSON.stringify(a)}\n\n: heartbeat\n\ndata: ${JSON.stringify(b)}\n\ndata:[DONE]\n\n`);
check(`\uFEFF: ping\r\nevent: message\r\nid: 1\r\nretry: 1000\r\ndata:${JSON.stringify(a)}\r\n\r\ndata:${JSON.stringify(b)}`);
check('data: {"id":"gen-one",\ndata: "choices":[{"delta":{"content":"literal [DONE] remains"}}]}\n\ndata: '+JSON.stringify(b)+'\n\n');
assert.deepEqual(decode(JSON.stringify(b),'application/json'),[b]);count++;
assert.deepEqual(decode('data: '+JSON.stringify(b)+'\n\n','application/json'),[b]);count++;
for(const bad of [': only heartbeat\n\n','data: {bad}\n\n','data: [DONE]\n\n']){assert.throws(()=>decode(bad));count++;}
console.log(JSON.stringify({status:'PASS',cases:count,model_calls:0}));
