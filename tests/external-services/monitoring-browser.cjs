// Render only the disposable synthetic-data fixture; no credentials or live deployment.
const {chromium}=require('playwright');
const {readFileSync,mkdirSync}=require('node:fs');
const assert=require('node:assert/strict');

const [origin,index,evidence]=process.argv.slice(2);
assert(/^http:\/\/127\.0\.0\.1:[1-9][0-9]*$/.test(origin),'expected isolated loopback fixture');
const definitions=JSON.parse(readFileSync(index,'utf8'));

(async()=>{
 const browser=await chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage();
 if(evidence) {
  mkdirSync(evidence,{recursive:true,mode:0o700});
  await context.tracing.start({screenshots:true,snapshots:true});
 }
 try {
  const errors=[];page.on('pageerror',error=>errors.push(error.name));
  for(const name of ['monitoring','host','components','accounting','gateway','alerts','traces','trace-detail','logs']) {
   const definition=definitions.find(d=>d.metadata.name==='enmaas-'+name);
   assert(definition,`missing definition: ${name}`);
   const trace=name==='trace-detail'?'&var-traceId=1234567890abcdef1234567890abcdef':'';
   const response=await page.goto(`${origin}/projects/praxis/dashboards/enmaas-${name}?start=15m&refresh=0s${trace}`);
   assert.equal(response.status(),200);
   await page.getByRole('button',{name:'Refresh',exact:true}).waitFor({timeout:15000});
   if(name==='monitoring') {
    await page.getByText('Main monitoring',{exact:true}).waitFor();
    const responseFilter=page.getByRole('combobox').nth(1);
    await responseFilter.click();
    const success=page.getByRole('option',{name:'2xx — successes (including 200)',exact:true});
    const denied=page.getByRole('option',{name:'4xx — client / policy rejections',exact:true});
    assert.equal(await success.getAttribute('aria-selected'),'true');
    assert.equal(await denied.getAttribute('aria-selected'),'false');
    await denied.click();await page.keyboard.press('Escape');
    if(evidence)await page.screenshot({path:`${evidence}/monitoring-overview.png`});
    await responseFilter.click();assert.equal(await denied.getAttribute('aria-selected'),'true');
    await denied.click();await page.keyboard.press('Escape');
   }
   const panels=definition.spec.layouts.flatMap(layout=>layout.spec.items)
     .sort((a,b)=>a.y-b.y||a.x-b.x).map(item=>definition.spec.panels[item.content.$ref.split('/').at(-1)]);
   for(const panel of panels) {
    const heading=page.getByText(panel.spec.display.name,{exact:true}).first();
    let visible=false;
    for(let attempt=0;attempt<65;attempt++) {
     if(await heading.count()) {
      try {await heading.scrollIntoViewIfNeeded({timeout:500});visible=await heading.isVisible();} catch {}
      if(visible)break;
     }
     await page.mouse.move(12,500);await page.mouse.wheel(0,200);await page.waitForTimeout(100);
    }
    assert(visible,`${name}: panel is unreachable: ${panel.spec.display.name}`);
   }
   assert.deepEqual(errors,[],`${name}: browser rendering errors`);
   if(evidence)await page.screenshot({path:`${evidence}/${name}.png`});
   console.log(`PASS: ${name} renders ${panels.length} reachable panels`);
  }
 } catch(error) {
  if(evidence) {
   await page.screenshot({path:`${evidence}/failure.png`}).catch(()=>{});
   await context.tracing.stop({path:`${evidence}/failure-trace.zip`}).catch(()=>{});
  }
  throw error;
 } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
