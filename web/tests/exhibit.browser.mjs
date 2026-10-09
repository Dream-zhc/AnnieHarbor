// Optional real-Chromium UI check on the production build. Network responses
// are explicit fixtures, not evidence of real hardware or LiveKit reception.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';
import {realpathSync} from 'node:fs';

const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require(dirname(realpathSync(
  execFileSync('which', ['playwright'], {encoding:'utf8'}).trim())))); }

const browser = await chromium.launch({headless:true, args:['--use-gl=angle', '--use-angle=swiftshader']});
try {
  const page = await browser.newPage({viewport: {width:1920, height:1080}});
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  let accessRequests = 0;
  let wakeState = 'SLEEP';
  const snapshot = id => ({
    schema:'annie.exhibit.v1', generated_at_ms:Date.now(), access_mode:'intranet',
    robot:{robot_id:id, room_id:id==='annie-a'?'roomv2':'roomv3', state:'running', generation:0, ready:true},
    realtime:{state:'connected', wakeword:{enabled:true,state:'WAKE_WAIT',deadline_remaining_ms:4100},
      speech_active:false, speech_output_state:null, viseme:null,
      playback_evidence:null,turn_id:null,
      user_text:id === 'annie-a' ? '内网访客讲话' : 'B 机器人讲话',
      assistant_text:id === 'annie-a' ? '内网直接回复' : 'B 机器人回复',last_turn_timeline:null},
    audio:{frames_received:null,frames_accepted:null,frames_dropped:null,mic_connected:null},
    vision:{description:null,updated_at_ms:null,availability:'no_observation'},
    actions:[], events:[],
    performance:{source:'process_probe',process_cpu_pct:null,rss_peak_mb:null,event_loop_lag_ms:null,samples:0},
    memory:{recalled:null,status:'not_exposed'},
    device_pose:{yaw:null,pitch:null,roll:null,status:'no_feedback'},
  });
  await page.route('**/api/exhibit/session', route => {
    accessRequests++;
    return route.fulfill({json:{configured:false,role:null}});
  });
  await page.route('**/api/exhibit/robots', route => route.fulfill({json:[
    {robot_id:'annie-a',room_id:'roomv2'}, {robot_id:'annie-b',room_id:'roomv3'}]}));
  await page.route('**/api/exhibit/robots/*/snapshot', route => {
    const id = route.request().url().includes('annie-b') ? 'annie-b' : 'annie-a';
    return route.fulfill({json:snapshot(id)});
  });
  await page.route('**/api/exhibit/robots/*/video-config', route =>
    route.fulfill({status:503,body:'unconfigured'}));
  await page.route('**/health', route => route.fulfill({json:{
    ready:true,status:'ready',pid:1,capabilities:{}, robots:[
      {robot_id:'annie-a',room_id:'roomv2',persona_id:'annie',state:'running',
        realtime:{ready:true,state:'connected',wakeword:{enabled:true,state:wakeState}}},
    ],
  }}));
  await page.route('**/api/robots/*/wakeword', route => route.fulfill({json:{
    robot_id:route.request().url().includes('annie-b') ? 'annie-b' : 'annie-a',generation:0,ready:true,
    wakeword:{enabled:true,state:wakeState,deadline_remaining_ms:
      wakeState==='WAKE_WAIT'?4100:wakeState==='FOLLOWUP'?7600:null},
  }}));
  await page.route('**/api/robots/*/session', route => route.fulfill({json:{
    robot_id:route.request().url().includes('annie-b') ? 'annie-b' : 'annie-a',
    room_id:route.request().url().includes('annie-b') ? 'roomv3' : 'roomv2',
    chat_id:'browser-test',persona_id:'annie',generation:0,session_epoch:0,
  }}));
  await page.route('**/models/annie-robot.glb', route => route.fulfill({status:404,body:'missing'}));
  await page.route('https://raw.githubusercontent.com/**', route => route.abort());
  await page.goto(process.env.ANNIE_BROWSER_TEST_URL ?? 'http://127.0.0.1:18780/?view=cockpit', {
    waitUntil:'domcontentloaded',
  });
  await page.locator('.ex-dashboard').waitFor();
  await page.locator('.ex-statusbar .ex-wake-indicator').getByText('已唤醒 · 待提问 · 5s').waitFor();
  await page.getByText('内网直连 · 无需登录').waitFor();
  await page.getByText('内网访客讲话').waitFor();
  await page.getByText('内网直接回复').waitFor();
  await page.getByText('视频源未配置', {exact:true}).waitFor();
  assert.equal(await page.locator('video').count(), 1, 'video receiver mounts without consent');
  assert.equal(await page.locator('.ex-gate').count(), 0);
  assert.equal(await page.getByRole('checkbox').count(), 0);
  assert.equal(await page.getByRole('button', {name:'退出登录'}).count(), 0);
  assert.equal(accessRequests, 0, 'display never depends on credential configuration');
  await page.locator('.ex-hero .ex-twin-canvas').waitFor();
  await page.getByText('本机机械概念模型', {exact:false}).first().waitFor({timeout:10000});
  await page.getByRole('button', {name:'annie-b'}).click();
  await page.getByText('ROOM roomv3').waitFor();
  assert.equal(await page.locator('.ex-shell').count(), 1);
  await page.getByText('B 机器人讲话').waitFor();
  assert.equal(await page.getByText('内网访客讲话').count(), 0);
  await page.getByRole('button', {name:'工作台', exact:true}).click();
  await page.waitForURL('**/?view=workbench');
  await page.locator('.top-robot-switch').waitFor();
  await page.locator('.wake-status').getByText('休眠 · 等待唤醒').waitFor();
  wakeState = 'WAKE_WAIT';
  await page.locator('.wake-status').getByText('已唤醒 · 待提问 · 5s').waitFor({timeout:4000});
  wakeState = 'FOLLOWUP';
  await page.locator('.wake-status').getByText('等待追问 · 8s').waitFor({timeout:4000});
  assert.deepEqual(errors, []);
  console.log('PASS Chromium: two views, wake state transitions, robot switch, intranet direct access, 3D canvas');
} finally { await browser.close(); }
