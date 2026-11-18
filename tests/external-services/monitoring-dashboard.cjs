// Real dashboard data checks and a credential-free walkthrough recording.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync,mkdirSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
function canonical(value) {
 if(Array.isArray(value))return value.map(canonical);
 if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).filter(([key,v])=>!((key==='targetBlank'&&v===false)||(key==='description'&&v===''))).map(([key,v])=>[key,canonical(v)]));
 return value;
}
const root=process.argv[2];const record=process.argv.includes('--record');
const requirePolicy=process.argv.includes('--require-policy-alert');
const requireStale=process.argv.includes('--require-stale-alert');
const onlyMain=process.argv.includes('--only-main');
const requireStress=process.argv.includes('--require-stress');
const requireContinuous=process.argv.includes('--require-continuous-data');
const requireGenerator=process.argv.includes('--require-generator-alert');
const definitionIndex=process.argv.indexOf('--definitions');
const definitions=definitionIndex<0?root:process.argv[definitionIndex+1];
if(!definitions)throw new Error('Missing dashboard definition directory');
const atIndex=process.argv.indexOf('--at');
const at=atIndex<0?null:Number(process.argv[atIndex+1]);
if(at!==null && (!Number.isSafeInteger(at)||at<=0||at>Date.now()/1000))throw new Error('Invalid historical end time');
const windowIndex=process.argv.indexOf('--window-minutes');
const windowMinutes=windowIndex<0?120:Number(process.argv[windowIndex+1]);
if(!Number.isSafeInteger(windowMinutes)||windowMinutes<1||windowMinutes>1440)throw new Error('Invalid dashboard window');
const windowSeconds=windowMinutes*60;
const range=at===null?`start=${windowMinutes}m&refresh=0s`:`start=${(at-windowSeconds)*1000}&end=${at*1000}&refresh=0s`;
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const certificate=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(certificate.publicKey.export({type:'spki',format:'der'})).digest('base64');
// Perses virtualizes rows below the fold; scroll before waiting for an alert cell.
async function reveal(page, locator) {
 for(let attempt=0;attempt<40;attempt++) {
  if(await locator.count()) {
   try {await locator.scrollIntoViewIfNeeded({timeout:1000});if(await locator.isVisible())return;} catch {}
  }
  await page.mouse.wheel(0,400);await page.waitForTimeout(250);
 }
 throw new Error('Expected alert row not visible after scanning dashboard');
}
let stage='start';
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 let context,page;
 try {
  const login=await browser.newContext();const auth=await login.newPage();
  await auth.goto('https://localhost:19443/');await auth.locator('#username').fill('admin');
  await auth.locator('#password').fill(credentials.admin);await auth.locator('#kc-login').click();
  await auth.waitForURL(url=>url.origin==='https://localhost:19443' && !url.pathname.startsWith('/oauth2/'));
  await auth.waitForLoadState('domcontentloaded');
  // Authentication is outside the recording; session cookies remain in memory.
  const storageState=await login.storageState();await login.close();
  if(record)mkdirSync(`${root}/monitoring-video`,{recursive:true,mode:0o700});
  context=await browser.newContext({storageState,viewport:{width:1440,height:1000},...(record?{recordVideo:{dir:`${root}/monitoring-video`,size:{width:1440,height:1000}}}:{})});
  page=await context.newPage();const evidence={dashboards:[],checkedQueries:0,recording:record,panelDataCoverage:[]};
  for(const name of (onlyMain?['monitoring']:['monitoring','benchmark','components','accounting','gateway','host','alerts'])) {
   stage=name;
   const definition=JSON.parse(readFileSync(`${definitions}/${name}-dashboard.json`));
   const api=await context.request.get(`https://localhost:19443/api/v1/projects/praxis/dashboards/enmaas-${name}`);
   assert.equal(api.status(),200,`${name} provisioned API HTTP ${api.status()}`);
   assert.deepEqual(canonical((await api.json()).spec.panels),canonical(definition.spec.panels),`${name}: provisioned panels match source`);
   const selected={response_class:'(2xx|1xx|unknown)'};
   const interpolate=(query,overrides={})=>query.replace(/\$(host|component|replica|database|response_class)\b/g,(_,key)=>overrides[key]??selected[key]??'.+');
   for(const variable of definition.spec.variables||[]) {
    const spec=variable.spec;
    if(!['host','component','replica','database'].includes(spec.name))continue;
    if(spec.customAllValue!==undefined){selected[spec.name]=spec.customAllValue;continue;}
    const params=new URLSearchParams();
    for(const matcher of spec.plugin.spec.matchers||[])params.append('match[]',interpolate(matcher));
    const response=await context.request.get('https://localhost:19443/proxy/globaldatasources/prometheus/api/v1/label/'+spec.plugin.spec.labelName+'/values?'+params);
    assert.equal(response.status(),200);const values=(await response.json()).data;
    assert(Array.isArray(values)&&values.length,`${name}: discovered ${spec.name} values`);
    assert(values.every(value=>/^[a-z0-9][a-z0-9-]{0,39}$/.test(value)),`${name}: bounded resource aliases`);
    selected[spec.name]='('+values.join('|')+')';
   }
   for(const panel of Object.values(definition.spec.panels))for(const query of panel.spec.queries||[]) {
    const expression=interpolate(query.spec.plugin.spec.query);
    const instant=query.spec.plugin.spec.instant===true;
    const now=at??Math.floor(Date.now()/1000);
    const suffix=instant?`query?time=${now}&`:'query_range?start='+(now-windowSeconds)+'&end='+now+'&step=15&';
    const response=await context.request.get('https://localhost:19443/proxy/globaldatasources/prometheus/api/v1/'+suffix+'query='+encodeURIComponent(expression));
    assert.equal(response.status(),200,`${name}: query accepted`);
    const data=await response.json();assert.equal(data.status,'success');
    const series=data.data?.result||[];
    if(requirePolicy && name==='monitoring' && expression.startsWith('ALERTS') && (!expression.includes('severity=') || expression.includes('severity="warning"'))) {
     assert(series.some(row=>row.metric.alertname==='EnmaasRateLimitActive'),'live rate-limit warning is firing');
     evidence.livePolicyAlert=true;
    }
    if(requireGenerator && name==='benchmark' && expression.startsWith('ALERTS') && (!expression.includes('severity=') || expression.includes('severity="warning"'))) {
     assert(series.some(row=>/^EnmaasGenerator(Concurrency|Scheduler)Limited$/.test(row.metric.alertname)),'actual generator saturation alert');
     evidence.liveGeneratorAlert=true;
    }
    if(requireStale && name==='monitoring' && expression.startsWith('ALERTS') && (!expression.includes('severity=') || expression.includes('severity="critical"'))) {
     assert(series.some(row=>row.metric.alertname==='EnmaasGatewayMetricsStale'),'real stale-metric critical alert fires');
     evidence.liveStaleAlert=true;
    }
    const stressOnly=expression.includes('enmaas_stress_');
    if(stressOnly && !requireStress && !series.length) {evidence.stressPanelsUnavailable=true;continue;}
    const stateBand=/(?:^| · )(Healthy|Warning|Critical)$/.test(query.spec.plugin.spec.seriesNameFormat || '');
    if(stateBand && !series.length) {evidence.emptyStateBands=(evidence.emptyStateBands||0)+1;continue;}
    if(!expression.startsWith('ALERTS')) {
     assert(series.length,`${name}: ${panel.spec.display.name} has data`);
     assert(series.some(row=>(row.values||[row.value]).some(value=>value&&Number.isFinite(Number(value[1])))),`${name}: ${panel.spec.display.name}: finite sample in the displayed range`);
     if(!instant) {
      const points=new Set(series.flatMap(row=>(row.values||[]).filter(value=>Number.isFinite(Number(value[1]))).map(value=>value[0])));
      const fraction=points.size/(Math.floor(windowSeconds/15)+1);
      evidence.panelDataCoverage.push({dashboard:name,panel:panel.spec.display.name,query:panel.spec.queries.indexOf(query)+1,finitePointFraction:fraction});
      // Allow initial rate/histogram warm-up, but reject a panel that only had
      // isolated samples somewhere in an otherwise empty measured run.
      if(requireContinuous)assert(fraction>=1-150/windowSeconds,`${name}: ${panel.spec.display.name}: missing too much of the measured window`);
     }
    }
    if(name==='components' && expression.includes('enmaas_container_')) {
     const expected=['gateway-sso','maas','metering-sso','provider','db','keycloak','auth','perses','prometheus','loki','tempo','collector','gateway-relay'];
     for(const component of expected) {
      const measured=series.filter(row=>row.metric.component===component);
      assert(measured.some(row=>(row.values||[row.value]).some(value=>value&&Number.isFinite(Number(value[1])))),`${name}: ${component} has measured data`);
      if(requireContinuous&&!instant) {
       const points=new Set(measured.flatMap(row=>(row.values||[]).filter(value=>Number.isFinite(Number(value[1]))).map(value=>value[0])));
       assert(points.size/(Math.floor(windowSeconds/15)+1)>=1-150/windowSeconds,`${name}: ${component} has gaps in the measured window`);
      }
     }
     evidence.measuredComponents=expected.length;
    }
    evidence.checkedQueries++;
   }
   if(definition.spec.variables?.some(v=>v.spec.name==='host')) {
    const first=Object.values(definition.spec.panels).flatMap(p=>p.spec.queries||[]).map(q=>q.spec.plugin.spec.query).find(q=>q.includes('$host'));
    assert(first,`${name}: host selector has a scoped resource query`);
    for(const [variable,value] of [['host','no-such-host'],...(name==='components'?[['component','no-such-service'],['replica','no-such-instance']]:[])]) {
     const scoped=interpolate(first,{[variable]:value});
     const response=await context.request.get('https://localhost:19443/proxy/globaldatasources/prometheus/api/v1/query?query='+encodeURIComponent(scoped));
     assert.equal(response.status(),200);const result=await response.json();assert.equal(result.status,'success');
     assert.equal(result.data.result.length,0,`${name}: ${variable} selector excludes unrelated measurements`);
    }
    evidence.resourceSelectorsChecked=(evidence.resourceSelectorsChecked||0)+1;
   }
   await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-${name}?${range}`);
   if(name==='monitoring') {
    await page.getByText('Main monitoring',{exact:true}).waitFor();
    await page.waitForTimeout(1500);
    const responseFilter=page.getByRole('combobox').nth(1);
    await responseFilter.click();
    const successOption=page.getByRole('option',{name:'2xx — successes (including 200)',exact:true});
    const rejectionOption=page.getByRole('option',{name:'4xx — client / policy rejections',exact:true});
    assert.equal(await successOption.getAttribute('aria-selected'),'true');
    assert.equal(await rejectionOption.getAttribute('aria-selected'),'false');
    await rejectionOption.click();await page.keyboard.press('Escape');
    await responseFilter.click();
    assert.equal(await rejectionOption.getAttribute('aria-selected'),'true');
    await rejectionOption.click();await page.keyboard.press('Escape');
    evidence.responseClassFilterChecked=true;
    await page.screenshot({path:`${root}/monitoring-overview.png`});
   }
   // Reset once per dashboard; keep moving forward through its visual rows.
   await page.evaluate(()=>{window.scrollTo(0,0);for(const node of document.querySelectorAll('*'))if(node.scrollTop)node.scrollTop=0;});
   await page.waitForTimeout(700);
   const visualPanels=definition.spec.layouts.flatMap(layout=>layout.spec.items).sort((a,b)=>a.y-b.y||a.x-b.x).map(item=>definition.spec.panels[item.content.$ref.split('/').at(-1)]);
   for(const panel of visualPanels) {
    const heading=page.getByText(panel.spec.display.name,{exact:true}).first();
    // Bounded, forward-only scrolling also handles virtualized Perses rows.
    let visible=false;
    await page.mouse.move(10,500);
    for(let attempt=0;attempt<60;attempt++) {
     if(await heading.count()) {
      let box=await heading.boundingBox();
      if(box && box.y>=650) {
       await heading.evaluate(node=>node.scrollIntoView({block:'center',behavior:'smooth'}));
       await page.waitForTimeout(700);box=await heading.boundingBox();
      }
      if(box && box.y>=60 && box.y<950 && await heading.isVisible()){visible=true;break;}
     }
     await page.mouse.wheel(0,180);await page.waitForTimeout(record?350:150);
    }
    assert(visible,`${name}: visible panel ${panel.spec.display.name}`);
    if(panel.spec.display.description) {
     stage=name+': '+panel.spec.display.name;
     await heading.hover();
     await page.getByRole('tooltip').waitFor({state:'hidden'});
     const info=heading.locator('xpath=..').getByRole('button',{name:'panel description',exact:true});
     await info.hover();
     const tooltip=page.getByRole('tooltip');await tooltip.waitFor();
     const expected=panel.spec.display.description.replace(/\s+/g,' ').trim();
     let displayed='';
     for(let attempt=0;attempt<20;attempt++) {
      displayed=(await tooltip.innerText()).replace(/\s+/g,' ').trim();
      if(displayed===expected)break;
      await page.waitForTimeout(100);
     }
     assert.equal(displayed,expected,`${name}: explanation is rendered`);
     assert(!displayed.includes('**'),`${name}: tooltip uses supported plain-text formatting`);
     evidence.panelDescriptionsChecked=(evidence.panelDescriptionsChecked||0)+1;
     if(record)await page.waitForTimeout(1200);
     await heading.hover();
    }
    if(record)await page.waitForTimeout(700);
   }
   assert.equal(await page.getByText('Unknown plugin',{exact:false}).count(),0,'supported panel plugins');
   await page.screenshot({path:`${root}/monitoring-${name}.png`});
   if(requirePolicy && name==='monitoring') {
    await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?${range}`);
    await page.getByRole('cell',{name:'Rate limiting is rejecting traffic',exact:true}).waitFor();
    evidence.policyAlertRendered=true;
    await page.screenshot({path:`${root}/monitoring-policy-alert.png`});
    const row=page.getByRole('row').filter({has:page.getByRole('cell',{name:'Rate limiting is rejecting traffic',exact:true})});
    const detail=row.getByRole('link',{name:'gateway',exact:true});
    const destination=new URL(await detail.getAttribute('href'),'https://localhost:19443');
    assert.equal(destination.origin,'https://localhost:19443');
    assert.equal(destination.pathname,'/projects/praxis/dashboards/enmaas-gateway','alert expands its real detail link');
    await detail.scrollIntoViewIfNeeded();const target=await detail.boundingBox();assert(target);
    await page.mouse.click(target.x+target.width/2,target.y+target.height/2);
    await page.waitForURL('**/enmaas-gateway*');evidence.alertDetailNavigation=true;
   }
   if(requireGenerator && name==='benchmark') {
    await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-benchmark?${range}`);
    const condition=page.getByRole('cell',{name:/Generator (concurrency cap skips|scheduling misses) offered requests/}).first();
    await reveal(page,condition);
    await page.screenshot({path:`${root}/monitoring-generator-alert.png`});
    evidence.generatorAlertRendered=true;
    if(record)await page.waitForTimeout(3000);
   }
   if(requireStale && name==='monitoring') {
    await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?${range}`);
    await page.getByRole('cell',{name:'Gateway metric samples are stale',exact:true}).waitFor();
    evidence.staleAlertRendered=true;
    await page.screenshot({path:`${root}/monitoring-stale-alert.png`});
    if(record)await page.waitForTimeout(4000);
   }
   evidence.dashboards.push(name);
  }
  stage='links';await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?${range}`);
  const link=page.getByRole('link',{name:'Database & usage metering',exact:true}).first();
  await link.scrollIntoViewIfNeeded();await page.waitForTimeout(500);
  const bounds=await link.boundingBox();assert(bounds,'visible details link');
  await page.mouse.click(bounds.x+bounds.width/2,bounds.y+bounds.height/2);
  await page.waitForURL('**/enmaas-accounting*');
  evidence.detailNavigation=true;

  stage='status log links';evidence.statusLogNavigation=[];
  const linkEnd=at===null?Date.now():at*1000,linkStart=linkEnd-windowSeconds*1000;
  for(const [status,title] of [['3xx','redirects'],['4xx','client and policy rejections'],['5xx','server errors']]) {
   await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?start=${linkStart}&end=${linkEnd}&refresh=0s`);
   await page.getByText('Host CPU used',{exact:true}).first().waitFor();
   const heading=page.getByText(status+' — '+title,{exact:true}).first();
   await page.mouse.move(12,600);await reveal(page,heading);await heading.hover();
   const menu=heading.locator('..').locator('button[aria-label="Panel-links"]:visible').last();
   const box=await menu.boundingBox();assert(box,'status panel links visible');
   await page.mouse.click(box.x+box.width/2,box.y+box.height/2);
   const expression=status[0]+'[0-9][0-9]';
   const pending=page.waitForResponse(response=>response.url().includes('loki/api/v1/query_range')&&decodeURIComponent(response.url()).includes(expression));
   await page.getByRole('menuitem',{name:'View '+status+' logs',exact:true}).click();
   await page.waitForURL('**/enmaas-logs?*');
   const destination=new URL(page.url());
   assert.equal(destination.searchParams.get('start'),String(linkStart));
   assert.equal(destination.searchParams.get('end'),String(linkEnd));
   assert.equal(destination.searchParams.get('var-log_status'),expression);
   const response=await pending;assert.equal(response.status(),200);
   assert.equal((await response.json()).status,'success','filtered Loki query succeeds, including empty results');
   evidence.statusLogNavigation.push(status);
  }

  if(!onlyMain) {
   stage='traces';
   const traceEnd=at??Math.floor(Date.now()/1000);
   const search=await context.request.get('https://localhost:19443/proxy/globaldatasources/tempo/api/search?q='+encodeURIComponent('{resource.service.name =~ "enmaas-gateway.*" && span.enmaas.request.kind = "inference"}')+'&limit=20&start='+(traceEnd-windowSeconds)+'&end='+traceEnd);
   assert.equal(search.status(),200);assert((await search.json()).traces?.length,'actual inference traces');
   const errors=await context.request.get('https://localhost:19443/proxy/globaldatasources/tempo/api/search?q='+encodeURIComponent('{resource.service.name =~ "enmaas-gateway.*" && span.enmaas.request.kind = "inference" && status = error}')+'&limit=20&start='+(traceEnd-windowSeconds)+'&end='+traceEnd);
   assert.equal(errors.status(),200);const failedTraces=(await errors.json()).traces||[];
   assert(Array.isArray(failedTraces),'failed trace search succeeds, including an empty sampled result');
   evidence.failedTraceSearch=true;
   await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-traces?${range}`);
   await page.getByText('Recent inference traces',{exact:true}).waitFor();
   await reveal(page,page.getByText('Failed inference traces',{exact:true}));
   stage='trace detail navigation';
   const href=await page.locator('a[href*="enmaas-trace-detail?"]').evaluateAll(links=>links.find(link=>{const r=link.getBoundingClientRect();return r.top>160&&r.bottom<innerHeight;})?.getAttribute('href'));
   assert(href?.startsWith('/projects/praxis/dashboards/enmaas-trace-detail?'),'visible trace link');
   const trace=page.locator('a[href="'+href+'"]').first();
   if(record)await page.waitForTimeout(3000);
   // Perses' non-selectable table inherits aria-disabled; its links still accept normal pointer clicks.
   const bounds=await trace.boundingBox();assert(bounds);
   const point={x:bounds.x+bounds.width/2,y:bounds.y+bounds.height/2};
   assert(await trace.evaluate((link,p)=>link.contains(document.elementFromPoint(p.x,p.y)),point),'trace link is not covered');
   await page.mouse.click(point.x,point.y);
   await page.getByText('Request spans',{exact:true}).waitFor();await page.getByText('Service & Operation',{exact:true}).waitFor();
   if(record)await page.waitForTimeout(4000);
   evidence.inferenceTraceNavigation=true;
   stage='logs';
   const logQuery=encodeURIComponent('{service_name="enmaas-mock-benchmark"}');
   const logEnd=at??Math.floor(Date.now()/1000);
   const logs=await context.request.get('https://localhost:19443/proxy/globaldatasources/loki/loki/api/v1/query_range?query='+logQuery+'&limit=100&start='+String(BigInt(logEnd-windowSeconds)*1000000000n)+'&end='+String(BigInt(logEnd)*1000000000n));
   assert.equal(logs.status(),200);
   const entries=(await logs.json()).data.result.flatMap(row=>row.values);
   assert(entries.length&&entries.every(row=>row[1]==='gateway event'),'sanitized benchmark events');
   await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-logs?${range}`);
   await page.getByText('Sanitized service log events',{exact:true}).waitFor();
   await page.getByText('gateway event',{exact:true}).first().waitFor();
   if(record)await page.waitForTimeout(4000);
   evidence.sanitizedLogTable=true;
  }
  await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?${range}`);
  await page.getByText('Main monitoring',{exact:true}).waitFor();
  if(record)await page.waitForTimeout(3000);
  writeFileSync(`${root}/monitoring-evidence.json`,JSON.stringify(evidence,null,2),{mode:0o600});
  const video=record?page.video():null;await context.close();context=null;
  if(video)console.log('Video: '+await video.path());
  console.log(`PASS: ${evidence.dashboards.length} dashboards, ${evidence.checkedQueries} queries, authenticated detail navigation`);
 } catch(error) {if(page)await page.screenshot({path:`${root}/monitoring-failure.png`,fullPage:true});throw error;} finally {if(context)await context.close();await browser.close();}
})().catch(error=>{console.error('FAIL at '+stage+': '+error.name+(error.code==='ERR_ASSERTION'?' '+String(error.message).split('\n')[0].slice(0,240):'')+'; inspect the private failure screenshot (request logs are suppressed to protect session cookies)');process.exitCode=1;});
