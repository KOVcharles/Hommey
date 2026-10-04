/* Actual template and scripts; deterministic local API fixtures, no business writes.
 * Run with Playwright on NODE_PATH: node tests/check_ambient_workspace_ui.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'webui_new/static');
const output = path.join(root, 'tmp/ambient-workspace-qa');
fs.mkdirSync(output, { recursive: true });
const sessions = [
    {session_id:'trip', title:'上海 · 学术交流', updated_at:'2026-10-03T10:00:00'},
    {session_id:'policy', title:'版面费报销需要哪些材料', updated_at:'2026-10-02T09:00:00'},
    {session_id:'meeting', title:'会议注册费报销条件', updated_at:'2026-09-30T08:00:00'},
];
const trip = {origin:'重庆', destination:'上海', start_date:'2026-10-12', end_date:'2026-10-14', trip_purpose:'学术交流'};
const passed = [];
function pass(name) {passed.push(name);console.log('PASS',name);}
(async () => {
    const browser = await chromium.launch({channel:'msedge',headless:true});
    try {
        const page = await browser.newPage({viewport:{width:1440,height:1000}});
        const errors=[];
        let failTrip=false;
        page.on('pageerror',e=>errors.push(e.message));
        await page.addInitScript(()=>{
            localStorage.setItem('hommey.access_token','test.'+btoa(JSON.stringify({sub:'ambient',exp:9999999999}))+'.test');
            localStorage.setItem('hommey.theme','light');
            sessionStorage.setItem('hommey.session.ambient','trip');
        });
        await page.route('**/*',async route=>{
            const req=route.request(),url=new URL(req.url()),p=url.pathname;
            if(p.startsWith('/static/')) {
                const file=path.resolve(assets,'.'+p.slice('/static'.length));
                if(!file.startsWith(assets+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:''});
                const ext=path.extname(file);
                return route.fulfill({body:fs.readFileSync(file),contentType:({'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'})[ext]||'application/octet-stream'});
            }
            if(p==='/chat/ambient')return route.fulfill({contentType:'text/html; charset=utf-8',body:fs.readFileSync(path.join(root,'webui_new/templates/chat.html'),'utf8').replaceAll('{{ user_id }}','ambient').replaceAll("{{ user_id[0:1].upper() if user_id else 'U' }}",'A')});
            let body={};
            if(p.endsWith('/status'))body={initialized:true};
            if(p.endsWith('/summary'))body={name_display:'测试账户',preferences:[],role:'user'};
            if(p.endsWith('/sessions'))body=req.method()==='POST'?{session_id:'new'}:{sessions};
            if(p.endsWith('/activate'))body={messages:[{role:'user',content:'示例问题'},{role:'assistant',content:'示例回复'}]};
            if(p.endsWith('/execution-plans'))body={plans:[]};
            if(p.endsWith('/trip/active')) {
                if(failTrip)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{message:'暂时无法读取'}})});
                body={active_trip:url.searchParams.get('session_id')==='trip'?trip:null};
            }
            if(p.endsWith('/attachments'))body={attachments:[]};
            return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
        });
        await page.goto('http://hommey.test/chat/ambient');
        await page.locator('#initOverlay').waitFor({state:'hidden'});
        const openCenter=async()=>{
            const view=await page.locator('#appShell').getAttribute('data-view');
            await page.locator(view!=='home'?'#workspaceChatEntry':page.viewportSize().width<=760?'#mobileHandle':'#sidebarToggle').click();
            await page.waitForFunction(()=>document.activeElement.id==='sidebarClose');
        };
        await page.screenshot({path:path.join(output,'home-desktop.png'),animations:'disabled'});
        await openCenter();
        assert.equal(await page.locator('.sidebar').count(),0);
        assert.equal(await page.locator('#workspaceTripDestination').innerText(),'上海');
        assert.equal(await page.locator('.utility-item').count(),6);
        await page.screenshot({path:path.join(output,'center-desktop.png'),animations:'disabled'});
        await page.keyboard.press('Shift+Tab');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'settingsButton');
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'sidebarClose');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#sidebar').getAttribute('aria-hidden'),'true');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'sidebarToggle');
        pass('Original six-cell center, session-scoped trip, focus trap and Escape restoration');
        await page.locator('#homeInput').fill('保留首页草稿');
        await openCenter();
        await page.locator('.utility-item[data-quick-trip-open]').click();
        assert.equal(await page.locator('#quickTripLayer').evaluate(el=>el.classList.contains('is-inline')),true);
        assert.equal(await page.locator('#homeComposer').isVisible(),false);
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#homeInput').inputValue(),'保留首页草稿');
        assert.equal(await page.locator('#quickTripForm').count(),1);
        pass('In-place planning opens/closes without losing the homepage draft or duplicating forms');
        await openCenter();
        await page.locator('#searchToggle').click();
        await page.locator('#historySearch').fill('版面费');
        assert.equal(await page.locator('#historyList .session-row:visible').count(),1);
        await page.locator('#historyList .session-open:visible').click();
        await page.locator('#chatInput').fill('保留对话草稿');
        await openCenter();
        await page.locator('#workspaceTripEmpty').waitFor({state:'visible'});
        assert.equal(await page.locator('#workspaceTripDetails').isVisible(),false);
        await page.locator('#sidebarClose').click();
        assert.equal(await page.locator('#chatInput').inputValue(),'保留对话草稿');
        pass('Search opens the selected session; another session never inherits a previous trip; draft retained');
        failTrip=true;
        await openCenter();
        await page.waitForFunction(()=>document.querySelector('#workspaceTripEmpty strong').textContent==='暂时无法读取行程');
        assert.equal(await page.locator('#workspaceTrip').isDisabled(),true);
        failTrip=false;
        await page.locator('#sidebarClose').click();
        pass('Unavailable trip data shows an error instead of a fabricated or stale itinerary');
        await page.locator('#homeButton').click();
        for(const width of [390,320]) {
            await page.setViewportSize({width,height:844});
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
            await openCenter();
            await page.locator('#settingsButton').scrollIntoViewIfNeeded();
            const bounds=await page.locator('#settingsButton').boundingBox();
            assert.ok(bounds.y+bounds.height<=844);
            assert.equal(await page.locator('.workspace-shell').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
            await page.locator('.workspace-shell').evaluate(el=>el.scrollTop=0);
            if(width===390)await page.screenshot({path:path.join(output,'center-mobile.png'),animations:'disabled'});
            await page.locator('#sidebarClose').click();
        }
        pass('390px and 320px layouts fit and every tile remains reachable');
        await page.setViewportSize({width:1440,height:1000});
        await page.evaluate(()=>document.documentElement.dataset.theme='dark');
        await page.emulateMedia({reducedMotion:'reduce'});
        await openCenter();
        assert.ok(await page.locator('#sidebar').evaluate(el=>getComputedStyle(el).transitionDuration.split(',').every(value=>parseFloat(value)<=.001)));
        assert.equal(await page.locator('.workspace').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(32, 32, 29)');
        await page.screenshot({path:path.join(output,'center-dark.png'),animations:'disabled'});
        assert.deepEqual(errors,[]);
        pass('Dark mode, reduced motion, and no JavaScript errors');
        fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed,errors},null,2));
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
