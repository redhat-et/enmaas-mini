// Isolated, short-lived dashboard to qualify native mixed-unit chart rendering.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {X509Certificate,createHash,randomBytes}=require('node:crypto');
const assert=require('node:assert/strict');
const root=process.argv[2];
const certificate=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(certificate.publicKey.export({type:'spki',format:'der'})).digest('base64');
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const name='enmaas-format-test-'+randomBytes(6).toString('hex');
const url=`https://localhost:19443/api/v1/projects/praxis/dashboards/${name}`;
const panel=JSON.parse(readFileSync(`${root}/monitoring-dashboard.json`)).spec.panels.memory;
panel.spec.display.name='Memory pressure and absolute capacity';
const document={kind:'Dashboard',metadata:{name,project:'praxis'},spec:{display:{name:'Temporary resource-unit verification'},duration:'15m',refreshInterval:'0s',panels:{memory:panel},layouts:[{kind:'Grid',spec:{items:[{x:0,y:0,width:24,height:16,content:{$ref:'#/spec/panels/memory'}}]}}]}};
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 let created=false;
 try {
  const page=await context.newPage();await page.goto('https://localhost:19443/');
  await page.locator('#username').fill('admin');await page.locator('#password').fill(credentials.admin);await page.locator('#kc-login').click();
  await page.waitForURL(u=>u.origin==='https://localhost:19443');
  const result=await context.request.post('https://localhost:19443/api/v1/projects/praxis/dashboards',{data:document});
  assert.equal(result.status(),200,'temporary mixed-unit dashboard accepted');created=true;
  await page.goto(`https://localhost:19443/projects/praxis/dashboards/${name}?start=15m&refresh=0s`);
  await page.getByText('Memory pressure and absolute capacity',{exact:true}).waitFor();
  await page.getByText('Used',{exact:true}).first().waitFor();
  await page.getByText('Total',{exact:true}).first().waitFor();
  const canvas=page.locator('canvas').first();await canvas.waitFor();
  const box=await canvas.boundingBox();assert(box);
  await page.mouse.move(box.x+box.width*.65,box.y+box.height*.5);
  await page.waitForTimeout(1500);
  const body=await page.locator('body').innerText();
  assert(/GiB|MiB/.test(body),'absolute units are rendered');
  assert(body.includes('%'),'percentage units are rendered');
  await page.screenshot({path:`${root}/mixed-unit-preview.png`});
  writeFileSync(`${root}/mixed-unit-preview-evidence.json`,JSON.stringify({nativePerQueryUnits:true,percentageAndBytesRendered:true},null,2),{mode:0o600});
  console.log('PASS: native percentage and absolute resource units render together');
 } finally {
  if(created){const result=await context.request.delete(url);assert([200,204].includes(result.status()),'temporary dashboard removed');}
  await context.close();await browser.close();
 }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
