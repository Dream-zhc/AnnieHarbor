// 可选 Chromium 实浏览器布局回归；只读 Runtime，不发送声音或修改会话。
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';
import {realpathSync} from 'node:fs';

const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{({chromium}=require(dirname(realpathSync(
  execFileSync('which',['playwright'],{encoding:'utf8'}).trim()))));}
const browser=await chromium.launch({headless:true});
try{
  const page=await browser.newPage({viewport:{width:1440,height:900}});
  const errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.goto(process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/',{
    waitUntil:'domcontentloaded',
  });
  await page.getByRole('group',{name:'切换机器人'}).getByRole('button').first().waitFor();
  assert.equal(await page.locator('.rail-link').count(),0);
  const [left,right]=await Promise.all([
    page.locator('.cockpit-stage').boundingBox(),
    page.locator('.cockpit-hud').boundingBox(),
  ]);
  assert.ok(left&&right);
  const proportion=left.width/(left.width+right.width);
  assert.ok(proportion>=.60&&proportion<=.70,`对话区比例错误: ${proportion}`);
  await page.getByRole('button',{name:'展开历史会话'}).click();
  await page.getByRole('button',{name:'＋ 新建会话'}).waitFor();
  await page.getByRole('button',{name:'收起历史会话'}).click();
  await page.getByRole('button',{name:'开发者'}).click();
  await page.getByRole('dialog',{name:'开发者诊断'}).waitFor();
  await page.getByRole('button',{name:'关闭开发者诊断'}).click();
  assert.equal(await page.getByRole('dialog',{name:'开发者诊断'}).count(),0);
  const robots=page.getByRole('group',{name:'切换机器人'}).getByRole('button');
  if(await robots.count()>1){
    await robots.nth(1).click();
    assert.equal(await robots.nth(1).getAttribute('aria-pressed'),'true');
  }
  await page.setViewportSize({width:390,height:844});
  const [mobileLeft,mobileRight]=await Promise.all([
    page.locator('.cockpit-stage').boundingBox(),
    page.locator('.cockpit-hud').boundingBox(),
  ]);
  assert.ok(mobileLeft&&mobileRight&&mobileRight.y>mobileLeft.y+mobileLeft.height-2,
    '窄屏 HUD 应位于对话区下方');
  assert.deepEqual(errors,[]);
  console.log('PASS Chromium cockpit: 65/35, history, drawer, robot switching, mobile');
}finally{await browser.close();}
