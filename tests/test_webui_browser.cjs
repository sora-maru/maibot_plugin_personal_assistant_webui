// Uses the original Host CSP/SDK/iframe permissions with the actual Python plugin.
// Environment: PLAYWRIGHT_MODULE, BROWSER_EXECUTABLE, MAIBOT_HOST_ROOT, SDK_ROOT, TEST_PYTHON.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const hostRoot = path.resolve(process.env.MAIBOT_HOST_ROOT);
const sdkRoot = path.resolve(process.env.SDK_ROOT);

async function main() {
  const backend = spawn(process.env.TEST_PYTHON || 'python', ['-u', path.join(__dirname, 'serve_webui.py'),
    '--host-root', hostRoot, '--sdk-root', sdkRoot], {stdio:['ignore', 'pipe', 'pipe']});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      let stdout = '', stderr = '';
      const timer = setTimeout(() => reject(new Error('Test backend startup timed out: ' + stderr)), 20000);
      backend.stderr.on('data', data => {stderr += data;});
      backend.stdout.on('data', data => {
        stdout += data;
        const match = stdout.match(/SERVER_URL=(http:\/\/127\.0\.0\.1:\d+)/);
        if(match){clearTimeout(timer);resolve(match[1]);}
      });
      backend.once('error', error => {clearTimeout(timer);reject(error);});
      backend.once('exit', code => {clearTimeout(timer);reject(new Error(`Test backend exited ${code}: ${stderr}`));});
    });
    browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
    const host = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    host.on('pageerror', error => errors.push(error.message));
    host.on('console', message => {if(message.text().includes('Ignored call to'))errors.push(message.text());});
    await host.goto(url);
    await host.setContent('<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style><iframe id="plugin" sandbox="allow-scripts allow-forms"></iframe>');
    const source = fs.readFileSync(path.join(hostRoot,'dashboard/src/lib/plugin-custom-page.ts'),'utf8').replace(/\r\n/g,'\n');
    const policyText = source.match(/const POLICY =([\s\S]*?)\n\nconst SDK/)[1];
    const policy = [...policyText.matchAll(/"([^"]*)"/g)].map(match=>match[1]).join('');
    const sdk = source.match(/const SDK = `([\s\S]*?)`\n\nexport function customPageDocument/)[1].replace('__MAIBOT_NONCE__','"test-nonce"');
    const document = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${policy}"><script>${sdk}</script></head><body>${fs.readFileSync(path.join(root,'webui/index.html'),'utf8')}</body></html>`;
    await host.evaluate(document => {
      window.addEventListener('message', event => {
        if(event.data?.type!=='maibot:ready')return;
        const channel=new MessageChannel();
        channel.port1.onmessage=async event=>{
          const message=event.data;if(!message.id)return;
          try{
            const response=await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(message)});
            channel.port1.postMessage({id:message.id,...await response.json()});
          }catch(error){channel.port1.postMessage({id:message.id,error:error.message});}
        };
        event.source.postMessage({type:'maibot:connect',page:{layout:'viewport'},context:{language:'zh-CN',theme:'dark',dashboardStyle:'modern',styles:''},fonts:[]},'*',[channel.port2]);
      });
      window.document.querySelector('iframe').srcdoc=document;
    }, document);
    const page = host.frameLocator('#plugin');
    await page.locator('#count').filter({hasText:'0 条'}).waitFor();
    await page.locator('#add').click();
    await page.locator('#task-text').fill('每日喝水 <script>danger</script>');
    await page.locator('#user-id').fill('10001');
    await page.locator('#repeat-daily').check();
    await page.locator('#save').click();
    await page.locator('#editor').waitFor({state:'hidden'});
    await page.locator('#rows tr').filter({hasText:'每日喝水'}).waitFor();
    assert.equal(await page.locator('#rows script').count(),0);
    await page.locator('[data-edit]').click();
    await page.locator('#task-text').fill('每天开会');
    await page.locator('#save').click();
    await page.locator('#editor').waitFor({state:'hidden'});
    await page.locator('#rows tr').filter({hasText:'每天开会'}).waitFor();
    await page.locator('#repeat').selectOption('once');
    await page.locator('#rows .empty').waitFor();
    await page.locator('#repeat').selectOption('daily');
    await page.locator('[data-delete]').waitFor();
    await page.locator('[data-delete]').click();
    await page.locator('#keep-task').click();
    assert.equal(await page.locator('#rows [data-delete]').count(),1);

    await host.setViewportSize({width:390,height:480});
    await page.locator('[data-edit]').click();
    const formBounds = await page.locator('#editor').boundingBox();
    const saveBounds = await page.locator('#save').boundingBox();
    assert(formBounds.y >= 0 && formBounds.y + formBounds.height <= 480);
    assert(saveBounds.y >= 0 && saveBounds.y + saveBounds.height <= 480);
    await page.locator('#remind-at').fill('2020-01-01T10:00');
    await page.locator('#save').click();
    await page.locator('#form-error').filter({hasText:'提醒时间必须晚于当前时间'}).waitFor();
    await page.locator('#cancel-form').click();
    const bodyWidth = await page.locator('body').evaluate(element=>element.scrollWidth);
    assert(bodyWidth <= 390, `Body overflows: ${bodyWidth}`);
    await page.locator('[data-delete]').click();
    await page.locator('#confirm-delete').click();
    await page.locator('#delete-dialog').waitFor({state:'hidden'});
    await page.locator('#rows .empty').waitFor();
    const emptyWidth = await page.locator('.table-wrap').evaluate(element=>({scroll:element.scrollWidth,client:element.clientWidth}));
    assert(emptyWidth.scroll <= emptyWidth.client + 1, 'Empty state must remain visible without horizontal scrolling');
    assert.deepEqual(errors,[]);
    if(process.env.UI_SCREENSHOT){await host.screenshot({path:process.env.UI_SCREENSHOT});}
    console.log('PASS actual Host CSP/SDK iframe: create/edit/filter, escaped text, delete confirmation/cancel, visible errors, desktop/mobile bounds');
  } finally {
    if(browser)await browser.close();
    backend.kill();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
