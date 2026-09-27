// Real Edge/CDP report checks. Requires Node >=22; no npm packages.
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';
const [edge,input,output,profile,flavor="candidate"]=process.argv.slice(2);
if(!profile) throw new Error('usage: node browser_report_check.mjs EDGE REPORT JSON PROFILE');
await fs.mkdir(profile,{recursive:true});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=spawn(edge,['--headless=new','--remote-debugging-port=0','--no-first-run',
    '--no-default-browser-check','--disable-background-networking','--window-size=1280,900',
    `--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe']});
let diagnostic='';child.stderr.on('data',c=>{diagnostic+=c.toString();});
let ws;const waiting=new Map();let serial=0;const errors=[];
async function call(method,params={}){
    const id=++serial;
    const reply=new Promise((resolve,reject)=>{
        const timeout=setTimeout(()=>{waiting.delete(id);reject(new Error('CDP timeout: '+method));},20000);
        waiting.set(id,{resolve:x=>{clearTimeout(timeout);resolve(x);},reject:e=>{clearTimeout(timeout);reject(e);}});
    });
    ws.send(JSON.stringify({id,method,params}));return reply;
}
async function evaluate(expression){
    const answer=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
    return answer.result.value;
}
async function key(key,code,n){
    await call('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode:n,nativeVirtualKeyCode:n});
    await call('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode:n,nativeVirtualKeyCode:n});
    await sleep(100);
}
async function click(selector){
    const point=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
    await sleep(100);
}
const result={input,checks:{},scope:'Real headless Edge. CDP keyboard/mouse events; snapshots are not whole-system peak memory.'};
try{
    let port;
    for(let i=0;i<100;i++){
        try{port=(await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0];break;}catch{await sleep(100);}
    }
    if(!port)throw new Error('Edge did not start: '+diagnostic);
    result.browser=await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws=new WebSocket(pages.find(x=>x.type==='page').webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
    ws.addEventListener('message',event=>{
        const m=JSON.parse(event.data);
        if(m.id){const p=waiting.get(m.id);if(p){waiting.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}}
        else if(m.method==='Runtime.exceptionThrown'||(m.method==='Runtime.consoleAPICalled'&&m.params.type==='error'))errors.push(m);
    });
    await call('Runtime.enable');await call('Page.enable');
    await call('Page.navigate',{url:pathToFileURL(path.resolve(input)).href});
    for(let i=0;i<100;i++){try{if(await evaluate("document.readyState==='complete' && typeof fileData!=='undefined'"))break;}catch{}await sleep(100);}
    result.initial=await evaluate("({files:fileData.length,htmlStrings:fileData.filter(f=>typeof f.html==='string').length,templates:document.querySelectorAll('template.diff-source').length,nodes:document.querySelectorAll('*').length,manifestComplete:manifestData===fileData})");
    if(flavor==='candidate'){
        assert.equal(result.initial.htmlStrings,0);
        assert.equal(result.initial.files,result.initial.templates);
    }
    assert.equal(result.initial.manifestComplete,true);
    result.initial.treeRows=await evaluate("document.querySelectorAll('#tree .row').length");
    result.initial.loadMilliseconds=await evaluate("performance.getEntriesByType('navigation')[0]?.duration");
    await call('HeapProfiler.collectGarbage');result.loadHeap=await call('Runtime.getHeapUsage');result.loadDOM=await call('Memory.getDOMCounters');
    if(flavor==='candidate'){
        result.checks.lazyTree=await evaluate(`(()=>{
            const initial=document.querySelectorAll('#tree [data-file-path]').length;
            if(initial>=fileData.length)throw Error('All file nodes were mounted at startup');
            toggleAll(true);
            if(document.querySelectorAll('#tree [data-file-path]').length!==fileData.length)throw Error('Expand-all incomplete');
            const nodes=document.querySelectorAll('#tree *').length;toggleAll(true);
            if(nodes!==document.querySelectorAll('#tree *').length)throw Error('Duplicate expansion');
            for(let mask=0;mask<32;mask++){
                ['A','M','F','D','R'].forEach((t,i)=>filterState[t]=!!(mask&(1<<i)));
                applyFilter();toggleAll(true);
                const expected=fileData.filter(f=>filterState[f.type]).length;
                if(document.querySelectorAll('#tree [data-file-path]').length!==expected)throw Error('Unopened filtered paths lost');
            }
            const mixed=buildTree([{path:'mixed/direct',displayPath:'mixed/direct',type:'A'},
                {path:'mixed/sub/a',displayPath:'mixed/sub/a',type:'M'},
                {path:'mixed/sub/b',displayPath:'mixed/sub/b',type:'D'}]);
            if(getDirType(mixed.children.mixed)!=='')throw Error('Mixed directory mislabeled');
            setFilter('all');return {initialFiles:initial,completeFiles:fileData.length,filterCombinations:32};
        })()`);
    }
    await call('HeapProfiler.collectGarbage');result.initialHeap=await call('Runtime.getHeapUsage');result.initialDOM=await call('Memory.getDOMCounters');
    await evaluate("showDiff(fileData.find(f=>f.path.endsWith('plain.txt')))");
    assert.ok(await evaluate('currentDiffGroups.length>=2'));
    await evaluate("showDiff(fileData.find(f=>f.archiveDetails))");
    for(let i=0;i<30;i++){
        const expanded=await evaluate("(()=>{const d=document.querySelector('#diffContainer details.archive-member:not([open])');if(!d)return false;d.open=true;return true;})()");
        if(!expanded)break;await sleep(30);
    }
    assert.ok(await evaluate("document.querySelectorAll('#diffContainer .archive-leaf').length>=2"));
    await click('#diffContainer [data-archive-step="1"]');
    assert.equal(await evaluate("document.activeElement===document.querySelector('#diffContainer .archive-leaf')"),true);
    await evaluate("document.querySelector('#diffContainer .archive-leaf').scrollLeft=0;document.querySelector('#diffContainer .diff-panel').scrollLeft=0");
    await key('ArrowRight','ArrowRight',39);
    result.rightScroll=await evaluate("({leaf:document.querySelector('#diffContainer .archive-leaf').scrollLeft,panel:document.querySelector('#diffContainer .diff-panel').scrollLeft,other:document.querySelectorAll('#diffContainer .archive-leaf')[1].scrollLeft})");
    assert.ok(result.rightScroll.leaf>0);assert.equal(result.rightScroll.panel,0);assert.equal(result.rightScroll.other,0);
    await key('ArrowLeft','ArrowLeft',37);
    assert.equal(await evaluate("document.querySelector('#diffContainer .archive-leaf').scrollLeft"),0);
    await evaluate("document.querySelector('#diffContainer [data-archive-step=\"1\"]').focus()");
    await key('Enter','Enter',13);
    assert.equal(await evaluate("document.activeElement===document.querySelector('#diffContainer [data-archive-step=\"1\"]')"),true);
    result.checks.keyboardAndFocus=true;
    await evaluate("showDiff(fileData.find(f=>f.path.endsWith('plain.txt')))");
    assert.ok(await evaluate('currentDiffGroups.length>=2'));
    await evaluate("toggleFilter('M');setFilter('all');toggleAll(true);toggleAll(false)");
    assert.equal(await evaluate('fileData.length===manifestData.length'),true);
    const manifest=await evaluate("(()=>{let text='';const saved=window.open;window.open=()=>({document:{open(){},write(){},close(){},getElementById(){return {set textContent(v){text=v;}}}}});try{openSummary();}finally{window.open=saved;}return text;})()");
    assert.ok(manifest.includes('plain.txt'));assert.ok(manifest.includes('server.tar'));
    assert.equal(await evaluate("typeof window.__reportXss==='undefined'"),true);
    result.checks.manifestAndFiltering=true;result.checks.normalNavigation=true;
    for(let i=0;i<10;i++)await evaluate("showDiff(fileData.find(f=>f.archiveDetails));showDiff(fileData.find(f=>f.path.endsWith('plain.txt')))");
    assert.equal(await evaluate("document.querySelectorAll('#diffContainer .diff-panel').length"),1);
    await call('HeapProfiler.collectGarbage');result.finalHeap=await call('Runtime.getHeapUsage');result.finalDOM=await call('Memory.getDOMCounters');
    result.errors=errors;assert.equal(errors.length,0);
    const image=await call('Page.captureScreenshot',{format:'png'});
    await fs.writeFile(output+'.png',Buffer.from(image.data,'base64'));
    result.success=true;
}catch(error){result.success=false;result.failure=String(error.stack||error);result.errors=errors;process.exitCode=1;}
finally{
    await fs.writeFile(output,JSON.stringify(result,null,2),'utf8');
    if(ws){try{await call('Browser.close');}catch{}ws.close();}
    child.kill();
}
console.log(JSON.stringify({success:result.success,checks:result.checks,failure:result.failure,output}));
