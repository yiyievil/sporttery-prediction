// WAF acw_sc__v2 求解器 — 零依赖版 (不需要 jsdom)
// 用法: node solve_waf_node.js <renderData.json> <script1.js> [script2.js] ...
// 原理: 提供最小 DOM 桩, 在 vm 沙箱中按页面顺序执行阿里云 WAF 挑战脚本,
//       捕获其通过 document.cookie 设置的 acw_sc__v2 值 (当前为 UUID 格式)
// 通用性: 任何装有 Node.js 的机器均可运行, 无 npm 依赖
// 备注: 挑战脚本在设置 cookie 后可能因 DOM 桩不全而报错 — 属正常, cookie 已捕获

const fs = require('fs');
const vm = require('vm');

const renderData = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

let cookieJar = '';

const documentStub = {
  getElementById: (id) => (id === 'renderData' ? { innerHTML: JSON.stringify(renderData) } : null),
  createElement: () => ({
    style: {}, setAttribute() {}, getAttribute() { return null; },
    appendChild() {}, removeChild() {},
    getContext: () => null,
    getElementsByTagName: () => [],
    innerHTML: '',
  }),
  getElementsByTagName: () => [],
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  attachEvent() {}, detachEvent() {},
  documentElement: { style: {}, appendChild() {}, removeChild() {} },
  body: { appendChild() {}, removeChild() {}, style: {} },
  head: { appendChild() {}, removeChild() {} },
  referrer: '',
  URL: 'https://www.leisu.com/',
};
Object.defineProperty(documentStub, 'cookie', {
  set(v) { cookieJar = v; },
  get() { return cookieJar; },
});

const sandbox = {
  document: documentStub,
  location: {
    href: 'https://www.leisu.com/guide/swot',
    protocol: 'https:', host: 'www.leisu.com', hostname: 'www.leisu.com',
    pathname: '/guide/swot', search: '',
    reload() {}, replace() {}, assign() {},
  },
  navigator: {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    appName: 'Netscape', appVersion: '5.0', platform: 'Win32',
    language: 'zh-CN', languages: ['zh-CN', 'zh'],
    cookieEnabled: true, webdriver: false,
  },
  screen: { width: 1920, height: 1080, colorDepth: 24 },
  innerWidth: 1920, innerHeight: 1080,
  addEventListener() {}, removeEventListener() {}, attachEvent() {},
  setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  console: { log() {}, warn() {}, error() {} },
  JSON, Date, Math, RegExp, String, Array, Object, Error, TypeError,
  parseInt, parseFloat, isNaN, isFinite,
  encodeURIComponent, decodeURIComponent, escape, unescape,
};
// window/self/top 均指向沙箱全局 (浏览器行为)
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.top = sandbox;
sandbox.parent = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);

for (const scriptPath of process.argv.slice(3)) {
  const code = fs.readFileSync(scriptPath, 'utf8');
  try {
    vm.runInContext(code, sandbox, { timeout: 5000 });
  } catch (e) {
    // cookie 设置后的报错属正常 (DOM 桩不全), 仅调试用
    process.stderr.write(`[solve_waf_node] ${scriptPath}: ${String(e).slice(0, 150)}\n`);
  }
}

let out = cookieJar || '';
if (out.includes(';')) out = out.split(';')[0];
process.stdout.write(out);
