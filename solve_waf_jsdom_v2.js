// WAF acw_sc__v2 求解器 — jsdom 版 (对混淆 WAF 脚本保真度最高)
// 用法: node solve_waf_jsdom_v2.js <renderData.json> [waf_script_0.js waf_script_1.js ...]
// 若不传脚本文件, 默认读取当前目录 waf_script_0.js .. waf_script_9.js 中存在的
// 输出: stdout 输出 acw_sc__v2=xxx (供 python subprocess 捕获)
// 依赖: npm install jsdom

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const renderDataPath = process.argv[2];
const renderDataRaw = fs.readFileSync(renderDataPath, 'utf8');

// 脚本文件: 命令行指定或自动发现
let scriptFiles = process.argv.slice(3);
if (scriptFiles.length === 0) {
  for (let i = 0; i < 10; i++) {
    const p = path.join(path.dirname(renderDataPath), `waf_script_${i}.js`);
    if (fs.existsSync(p)) scriptFiles.push(p);
  }
}

// 拼一个模拟挑战页的 HTML: renderData textarea + 全部内联脚本
const scriptsHtml = scriptFiles
  .map((f) => `<script>${fs.readFileSync(f, 'utf8')}</script>`)
  .join('\n');

const html = `<!doctype html><html><head></head><body>
<textarea id="renderData" style="display:none">${renderDataRaw}</textarea>
${scriptsHtml}
</body></html>`;

const dom = new JSDOM(html, {
  url: 'https://www.leisu.com/guide/swot',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
});

// 轮询等待 cookie 出现 (最多3秒)
let waited = 0;
const timer = setInterval(() => {
  const c = dom.window.document.cookie || '';
  if (c.includes('acw_sc__v2=') || waited > 3000) {
    clearInterval(timer);
    let out = '';
    const m = c.match(/acw_sc__v2=([^;]+)/);
    if (m) out = 'acw_sc__v2=' + m[1];
    process.stdout.write(out);
    process.exit(0);
  }
  waited += 50;
}, 50);
