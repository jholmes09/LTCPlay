const {chromium}=require('playwright');
(async()=>{const port=process.argv[2];const b=await chromium.launch();const p=await b.newPage({viewport:{width:390,height:844},hasTouch:true});
const errs=[];p.on('pageerror',e=>errs.push('pageerror '+e.message));p.on('console',m=>{if(m.type()==='error')errs.push('console '+m.text().slice(0,200))});
const out=[];const say=(k,v)=>out.push(k+': '+v);
const txt=async s=>{try{return (await p.locator(s).first().innerText({timeout:1500})).replace(/\s+/g,' ')}catch(e){return '<none>'}};
await p.goto(`http://127.0.0.1:${port}/phone`);await p.waitForTimeout(1500);
say('top',await txt('#top'));say('link',await txt('#link'));
await p.screenshot({path:'r-show.png'});
await p.click('[data-act=tab][data-n=arm]');await p.waitForTimeout(1200);
say('arm tab',(await txt('#main')).slice(0,260));
await p.screenshot({path:'r-arm.png'});
// expand first group, list machines
const opens=await p.locator('#main [data-act=open]').count();say('expandable groups',opens);
if(opens){await p.locator('#main [data-act=open]').first().click();await p.waitForTimeout(400);say('opened',(await txt('#main .card.open')).slice(0,200));}
// arm first group: tap, confirm
const arm=p.locator('#main .card:not(.all) [data-act=arm]').first();say('arm btn',await arm.innerText().catch(()=>'<none>'));
await arm.click();await p.waitForTimeout(700);say('bar',await txt('#bar'));await p.screenshot({path:'r-confirm.png'});
await p.locator('[data-act=ok]').click();await p.waitForTimeout(600);say('after confirm',(await txt('#main .card:not(.all)')).slice(0,80));
await p.waitForTimeout(2500);say('2.5 s later',(await txt('#main .card:not(.all)')).slice(0,80));say('pill',await txt('#t-arm, #top .pill'));
await p.screenshot({path:'r-armed.png'});
await p.locator('#main .card:not(.all) [data-act=dis]').first().click();await p.waitForTimeout(1500);say('after disarm',(await txt('#main .card:not(.all)')).slice(0,80));
// service: purge
await p.click('[data-act=tab][data-n=service]');await p.waitForTimeout(1500);say('service',(await txt('#main')).slice(0,300));
await p.screenshot({path:'r-service.png'});
const pg=p.locator('#main [data-act=pg]').first();say('purge btn',await pg.innerText().catch(()=>'<none>'));
await p.locator('#main [data-act=psec][data-n="2"]').click().catch(()=>say('psec','<none>'));
await pg.click();await p.waitForTimeout(700);say('purge bar',await txt('#bar'));
await p.locator('[data-act=ok]').click();
for(const t of [400,900,1600,2600,4200]){await p.waitForTimeout(t===400?400:t-(([400,900,1600,2600,4200][[400,900,1600,2600,4200].indexOf(t)-1])));say('purge +'+t+'ms',(await txt('#main .card')).slice(0,60)+' | bar: '+(await txt('#bar')).slice(0,80));if(t===1600)await p.screenshot({path:'r-purging.png'});}
// second purge then stop it
await p.waitForTimeout(2500);
await p.locator('#main [data-act=pg]').first().click();await p.waitForTimeout(700);await p.locator('[data-act=ok]').click();await p.waitForTimeout(500);
say('second run',(await txt('#main .card')).slice(0,60));
await p.locator('#main [data-act=pg-stop]').first().click().catch(()=>say('stop','<no stop button>'));await p.waitForTimeout(1200);
say('after stop',(await txt('#main .card')).slice(0,60)+' | bar: '+(await txt('#bar')).slice(0,120));
await p.screenshot({path:'r-stopped.png'});
console.log(out.join('\n'));console.log('ERRORS',JSON.stringify(errs.slice(0,8)));await b.close()})()
