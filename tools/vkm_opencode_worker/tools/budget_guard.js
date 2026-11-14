// Native OpenCode V2 server plugin; accounting control only, no provider proxy.
import crypto from "node:crypto";
import fs from "node:fs";
export default {
  id: "vkm-budget-guard",
  async setup(ctx) {
    const base = process.env.VKM_BUDGET_GATE;
    const token = process.env.VKM_BUDGET_NONCE;
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base || "") || !token) throw new Error("VKM_GATE_MISSING");
    let pending = null;
    async function rpc(path, value) {
      const response = await fetch(base + path, {method:"POST",redirect:"error",
        headers:{"Content-Type":"application/json",Authorization:"Bearer " + token},
        body:JSON.stringify(value),signal:AbortSignal.timeout(30000)});
      if (!response.ok) throw new Error("VKM_ADMISSION_DENIED");
      return response.json();
    }
    await rpc('/ready',{protocol:2,plugin_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex')});
    await ctx.tool.hook('execute.before',async e=>{
      const result=await rpc('/tool',{tool:e.tool,input:e.input});
      if(result.permit!==true)throw new Error('VKM_TOOL_SCOPE_DENIED');
    });
    await ctx.shell.hook('create.before',e=>{
      for(const name of Object.keys(e.env)) {
        if(/key|token|secret|password|credential|authorization|VKM_BUDGET_|VKM_WORKER_AUTH/i.test(name))delete e.env[name];
      }
    });
    await ctx.session.hook("http.request", async (e) => {
      if (pending) throw new Error("VKM_CONCURRENT_HTTP_DENIED");
      const body_text = await e.request.clone().text();
      const admission = await rpc("/before", {body_text,url:e.request.url,kind:e.kind});
      if (admission.permit !== true) throw new Error("VKM_NO_PERMIT");
      pending = admission.attempt_id;
      // The SDK has only an invalid sentinel credential. A missing or failed
      // plugin cannot issue a paid request. Grant the credential only AFTER
      // durable admission, on this exact native transport Request.
      const auth = process.env.VKM_WORKER_AUTH;
      if (!auth) throw new Error("VKM_PROVIDER_AUTH_MISSING");
      e.request.headers.set("Authorization", "Bearer " + auth);
    });
    await ctx.session.hook("http.response", async (e) => {
      if (!pending) throw new Error("VKM_UNADMITTED_RESPONSE");
      const response = e.response.clone();
      const reader = response.body.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const {value,done} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 64 * 1024 * 1024) {await reader.cancel();throw new Error("VKM_RESPONSE_LIMIT");}
        chunks.push(Buffer.from(value));
      }
      const raw = Buffer.concat(chunks); const text = raw.toString("utf8");
      const response_sha256=crypto.createHash('sha256').update(raw).digest('hex');
      async function unresolved(reason, parsed) {
        const error=parsed?.error || {};
        let message=String(error.message || '').slice(0,2000);
        const auth=process.env.VKM_WORKER_AUTH;
        if(auth)message=message.split(auth).join('[REDACTED]');
        message=message.replace(/sk-or-[A-Za-z0-9_-]+/g,'[REDACTED]');
        await rpc('/unresolved',{attempt_id:pending,http_status:e.response.status,response_sha256,reason,
          generation_id:typeof parsed?.id==='string'?parsed.id:null,
          error_code:typeof error.code==='string'||typeof error.code==='number'?String(error.code):null,
          error_message:message});
        throw new Error('VKM_COST_UNRESOLVED');
      }
      let items;
      try {
        items=text.trimStart().startsWith('data:')
          ? text.split(/\r?\n/).filter(x=>x.startsWith('data:')&&!x.includes('[DONE]')).map(x=>JSON.parse(x.slice(5).trim()))
          : [JSON.parse(text)];
      } catch {await unresolved('NON_JSON_RESPONSE',null);}
      if(!e.response.ok)await unresolved('HTTP_ERROR',items[0]);
      const ids = [...new Set(items.map(x=>x.id).filter(Boolean))];
      const tail = items.findLast(x=>x.usage && x.usage.cost !== undefined);
      if (ids.length !== 1 || !tail) await unresolved('GENERATION_COST_UNKNOWN',items[0]);
      await rpc("/after", {attempt_id:pending,generation_id:ids[0],cost:String(tail.usage.cost),
        response_sha256:crypto.createHash("sha256").update(raw).digest("hex"),http_status:e.response.status});
      pending = null;
    });
    await ctx.session.hook("retry", e => {e.decision={retry:false};});
  }
};
