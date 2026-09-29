// 生图管理 WebUI —— Vue 3 + AstrBot PluginPage Bridge
const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;
const BUILD_TAG = 'BUILD-2026-09-22-2123';
// 面板把插件页装在 sandbox iframe 里：localStorage 不可用、直连 fetch 被 CORS 拦，
// 所以取数走「桥」为主、「直连」竞速兜底。下面两个钩子把静默失败变成可见日志。
try {
  window.addEventListener('error', function (e) { try { console.log('[生图管理] 页面错误: ' + (e && e.message)); } catch (x) {} });
  window.addEventListener('unhandledrejection', function (e) { try { console.log('[生图管理] 未处理异常: ' + (e && e.reason && (e.reason.message || e.reason))); } catch (x) {} });
} catch (e) {}


// ---------- 内联 SVG 图标（24px，stroke 风格，不用 emoji） ----------
const IC = {
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="15" height="15"><path d="M12 5v14M5 12h14"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="14" height="14"><path d="M6 4l14 8-14 8Z" fill="currentColor" stroke="none"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="16" height="16"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" width="14" height="14"><path d="M20 6 9 17l-5-5"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="15" height="15"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="16" height="16"><path d="M12 3v3m0 12v3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M3 12h3m12 0h3M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1"/></svg>',
  table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="16" height="16"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>',
};

// ---------- 数据 ----------
const state = reactive({
  mode: 'sequential',
  providers: [],
  rows: [],
  loading: true,
  busy: {},            // provider id -> true (test/toggle 进行中)
  modal: false,
  editId: '',          // 空 = 新增
  form: {},
  formErr: '',
  confirm: null,       // { title, text, danger, action }
  toasts: [],
  filterProvider: 'all',
  filterOk: 'all',
  injection: { enabled: true, text: '' },
  injectionLoading: false,
  injectionSaving: false,
  llmEnabled: true,
  llmSaving: false,
  activeImage: '',
  activeVideo: '',
  loadErr: '',
  diag: '',
});

const summary = computed(() => {
  const rows = state.rows;
  const today = new Date().toISOString().slice(0, 10);
  const tRows = rows.filter(r => (r.ts || '').startsWith(today));
  const ok = tRows.filter(r => r.ok).length;
  return {
    total: tRows.length,
    ok,
    fail: tRows.length - ok,
    rate: tRows.length ? Math.round(ok / tRows.length * 1000) / 10 : 0,
    tokens: tRows.reduce((s, r) => s + (Number(r.tokens) || 0), 0),
    images: tRows.reduce((s, r) => s + (Number(r.images) || 0), 0),
  };
});

const filteredRows = computed(() => {
  return state.rows.filter(r => {
    if (state.filterProvider !== 'all' && r.provider !== state.filterProvider) return false;
    if (state.filterOk === 'ok' && !r.ok) return false;
    if (state.filterOk === 'fail' && r.ok) return false;
    return true;
  });
});

const enabledCount = computed(() => state.providers.filter(p => p.enable).length);
const imageProviders = computed(() => state.providers.filter(p => (p.capability || 'image') === 'image'));
const videoProviders = computed(() => state.providers.filter(p => p.capability === 'video'));

// ---------- 提示 ----------
let toastSeq = 0;
function toast(msg, kind = 'ok') {
  const id = ++toastSeq;
  state.toasts.push({ id, msg, kind });
  setTimeout(() => {
    const i = state.toasts.findIndex(t => t.id === id);
    if (i >= 0) state.toasts.splice(i, 1);
  }, 3600);
}

// ---------- API ----------
// 插件页与面板同源：优先用 fetch 直连（面板把 JWT 存在 localStorage.token），
// postMessage 桥只作为兜底——桥在启动窗口期/跨域场景容易静默失败。
const API_BASE = '/api/v1/plugins/extensions/astrbot_plugin_apilio_draw';
const bridge = window.AstrBotPluginPage;
const DIAG = [];
function diag(msg) {
  DIAG.push(msg);
  state.diag = DIAG.slice(-3).join(' ｜ ');
  try { console.log('[生图管理]', msg); } catch (e) {}
}

function apiToken() {
  try { return localStorage.getItem('token') || ''; } catch (e) { return '（localStorage 被沙箱禁用）'; }
}

function buildQuery(params) {
  if (!params) return '';
  const usp = new URLSearchParams();
  Object.keys(params).forEach(k => {
    if (params[k] !== undefined && params[k] !== null && params[k] !== '') usp.set(k, params[k]);
  });
  const s = usp.toString();
  return s ? '?' + s : '';
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label + '无响应 ' + ms + 'ms')), ms)),
  ]);
}

async function directFetch(endpoint, init, params) {
  const headers = Object.assign({ 'Accept-Language': 'zh-CN' }, (init && init.headers) || {});
  const t = apiToken();
  if (t && t.indexOf('（') !== 0) headers.Authorization = 'Bearer ' + t;
  const ctl = (typeof AbortController === 'function') ? new AbortController() : null;
  const opt = Object.assign({ cache: 'no-store', headers: headers }, init || {});
  if (ctl) opt.signal = ctl.signal;
  const timer = ctl ? setTimeout(() => ctl.abort(), 9000) : null;
  let r;
  try {
    r = await fetch(API_BASE + '/' + endpoint + buildQuery(params), opt);
  } finally {
    if (timer) clearTimeout(timer);
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (!r.ok) {
    const msg = (data && (data.error || data.message)) || '';
    throw new Error('HTTP ' + r.status + (msg ? ' ' + msg : ''));
  }
  if (data == null) throw new Error('响应不是 JSON：' + text.slice(0, 120));
  return data;
}

// 两条通道并行竞速：
//   桥 = postMessage 交给面板父页面代发（父页面有 JWT，不受沙箱限制，最稳）
//   直连 = 插件页自己 fetch（单独打开页面时可用；沙箱里因 opaque origin 拿不到 token 必失败）
async function raceChannels(jobs, label) {
  if (!jobs.length) throw new Error('没有可用的取数通道');
  try {
    const r = await Promise.any(jobs);
    diag(label + '✓' + r.via);
    return r.d;
  } catch (e) {
    const errs = (e && e.errors ? e.errors : [e]).map(x => (x && x.message) || String(x));
    diag(label + '✗' + errs.join(' / '));
    throw new Error(errs.join(' / '));
  }
}

async function apiGet(endpoint, params) {
  const jobs = [];
  if (bridge && typeof bridge.apiGet === 'function') {
    jobs.push(withTimeout(bridge.apiGet(endpoint, params), 9000, '桥').then(d => ({ via: '桥', d })));
  } else {
    diag('页面里没有 AstrBotPluginPage 桥');
  }
  jobs.push(withTimeout(directFetch(endpoint, null, params), 9000, '直连').then(d => ({ via: '直连', d })));
  return raceChannels(jobs, endpoint);
}

async function apiPost(endpoint, body) {
  const jobs = [];
  if (bridge && typeof bridge.apiPost === 'function') {
    jobs.push(withTimeout(bridge.apiPost(endpoint, body), 15000, '桥').then(d => ({ via: '桥', d })));
  }
  jobs.push(withTimeout(directFetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }), 15000, '直连').then(d => ({ via: '直连', d })));
  return raceChannels(jobs, endpoint);
}

async function loadProviders(retry) {
  try {
    const d = await apiGet('providers');
    if (d && d.success !== false) {
      state.mode = d.mode || 'sequential';
      state.providers = d.providers || [];
      state.llmEnabled = d.llm_enable !== false;
      state.activeImage = d.image_provider_id || '';
      state.activeVideo = d.video_provider_id || '';
      state.loadErr = '';
    } else {
      state.loadErr = (d && d.error) ? String(d.error) : '接口返回 success=false';
    }
  } catch (e) {
    state.loadErr = String(e.message || e);
    if (!retry) { setTimeout(() => loadProviders(true), 1500); return; }
    toast('加载提供商失败: ' + (e.message || e), 'err');
  }
}

async function loadUsage() {
  try {
    const d = await apiGet('usage', { limit: 300 });
    if (d && d.success !== false) state.rows = d.rows || [];
  } catch (e) {
    toast('加载用量日志失败: ' + (e.message || e), 'err');
  }
}

async function refresh() {
  state.loading = true;
  try {
    await Promise.all([loadProviders(), loadUsage(), loadTemplates()]);
    diag('就绪 提供商=' + state.providers.length + ' 日志=' + state.rows.length);
    await nextTick();
    try {
      console.log('[生图管理] DOM 卡片数=' + document.querySelectorAll('.pcard').length +
        ' 首个=' + ((document.querySelector('.pcard .name') || {}).textContent || '').replace(/\s+/g, ' ').trim().slice(0, 46) +
        ' 下拉项=' + document.querySelectorAll('select option').length +
        ' 用量行=' + document.querySelectorAll('table.log tbody tr').length);
    } catch (e) {}
    console.log('[生图管理] 就绪 提供商=' + state.providers.length + ' 日志=' + state.rows.length +
      ' 当前生图=' + (state.activeImage || '自动') + ' 当前生视频=' + (state.activeVideo || '自动'));
  } finally {
    state.loading = false;
  }
}

// ---------- 注入提示词 ----------
async function loadTemplates() {
  try {
    const d = await apiGet('templates');
    if (d && d.success !== false && d.injection) {
      state.injection = {
        enabled: d.injection.enabled !== false,
        text: d.injection.text || '',
      };
    }
  } catch (e) {
    toast('加载注入提示词失败: ' + (e.message || e), 'err');
  }
}

async function saveTemplates() {
  state.injectionSaving = true;
  try {
    const d = await apiPost('templates', { injection: state.injection });
    if (d && d.success === false) throw new Error(d.error || '保存失败');
    toast('注入提示词已保存');
    await loadTemplates();
  } catch (e) {
    toast('保存失败: ' + (e.message || e), 'err');
  } finally {
    state.injectionSaving = false;
  }
}

async function setMode(m) {
  if (state.mode === m) return;
  try {
    const d = await apiPost('mode', { mode: m });
    if (d && d.success === false) throw new Error(d.error || '切换失败');
    state.mode = m;
    toast('已切换为 ' + (m === 'sequential' ? '顺序降级' : '并发容错') + ' 模式');
  } catch (e) {
    toast('切换模式失败: ' + (e.message || e), 'err');
  }
}

async function saveLlm() {
  state.llmSaving = true;
  try {
    const d = await apiPost('llm', { enabled: state.llmEnabled });
    if (d && d.success === false) throw new Error(d.error || '保存失败');
    if (d && typeof d.llm_enable === 'boolean') state.llmEnabled = d.llm_enable;
    toast(state.llmEnabled ? '已允许 AI 调用画图' : '已关闭 AI 调用，仅指令可用');
  } catch (e) {
    state.llmEnabled = !state.llmEnabled;
    toast('切换失败: ' + (e.message || e), 'err');
  } finally {
    state.llmSaving = false;
  }
}

async function toggleProvider(p) {
  state.busy['t' + p.id] = true;
  try {
    const d = await apiPost('provider/toggle', { id: p.id, enable: !p.enable });
    if (d && d.success === false) throw new Error(d.error || '操作失败');
    p.enable = !p.enable;
  } catch (e) {
    toast((e.message || e), 'err');
  } finally {
    state.busy['t' + p.id] = false;
  }
}

async function savePriority(p) {
  try {
    const d = await apiPost('provider/priority', { id: p.id, priority: p.priority });
    if (d && d.success === false) throw new Error(d.error || '设置失败');
    toast('优先级已更新');
  } catch (e) {
    toast((e.message || e), 'err');
  }
}

async function testProvider(p) {
  state.busy['s' + p.id] = true;
  try {
    const d = await apiPost('provider/test', { id: p.id });
    if (d && d.success === false) throw new Error(d.error || '测试失败');
    toast('提供商「' + p.name + '」测试成功（已产生一次真实费用）');
  } catch (e) {
    toast('测试失败: ' + (e.message || e), 'err');
  } finally {
    state.busy['s' + p.id] = false;
  }
}

async function setActive(kind, id) {
  try {
    const d = await apiPost('active', { kind: kind, id: id });
    if (d && d.success === false) throw new Error(d.error || '切换失败');
    if (kind === 'video') { state.activeVideo = id; } else { state.activeImage = id; }
    const hit = state.providers.find(p => p.id === id);
    toast(id ? ('当前' + (kind === 'video' ? '生视频' : '生图') + '模型 → ' + (hit ? hit.name : id)) : '已恢复按优先级自动选路');
  } catch (e) {
    toast('切换失败: ' + (e.message || e), 'err');
  }
}

function typeLabel(t) {
  const m = { openai_compat: 'OpenAI 兼容', template: '自定义模板', bailian_image: '百炼生图', bailian_video: '百炼生视频' };
  return m[t] || t;
}

function askRemove(p) {
  state.confirm = {
    title: '删除提供商',
    text: '确定删除「' + p.name + '」（' + p.id + '）吗？此操作不可撤销。',
    danger: true,
    action: async () => {
      try {
        const d = await apiPost('provider/remove', { id: p.id });
        if (d && d.success === false) throw new Error(d.error || '删除失败');
        state.providers = state.providers.filter(x => x.id !== p.id);
        toast('已删除「' + p.id + '」');
      } catch (e) {
        toast('删除失败: ' + (e.message || e), 'err');
      }
      state.confirm = null;
    },
  };
}

// ---------- 表单 ----------
function emptyForm() {
  return {
    id: '', name: '', type: 'openai_compat', api_base: '', api_key: '', model: '',
    size: '2K', priority: 100, timeout: 180, ref_field: 'image',
    extra_body: '', url: '/images/generations', method: 'POST',
    response_field: 'data.0.b64_json', image_kind: 'b64_json',
    headers: '', body_template: '',
    capability: 'image', negative_prompt: '', watermark: false,
    resolution: '720P', ratio: '16:9', duration: 5, poll_interval: 6, max_wait: 900,
  };
}

function openAdd() {
  state.editId = '';
  state.form = emptyForm();
  state.formErr = '';
  state.modal = true;
}

function openEdit(p) {
  state.editId = p.id;
  state.form = {
    id: p.id || '', name: p.name || '', type: p.type || 'openai_compat',
    api_base: p.api_base || '', api_key: p.api_key || '', model: p.model || '',
    size: p.size || '2K', priority: p.priority != null ? p.priority : 100,
    timeout: p.timeout != null ? p.timeout : 180, ref_field: p.ref_field || 'image',
    extra_body: p.extra_body ? JSON.stringify(p.extra_body) : '',
    url: p.url || '/images/generations', method: p.method || 'POST',
    response_field: p.response_field || 'data.0.b64_json', image_kind: p.image_kind || 'b64_json',
    headers: p.headers ? JSON.stringify(p.headers) : '',
    body_template: p.body_template ? JSON.stringify(p.body_template) : '',
    capability: p.capability || 'image',
    negative_prompt: p.negative_prompt || '', watermark: p.watermark === true,
    resolution: p.resolution || '720P', ratio: p.ratio || '16:9',
    duration: p.duration != null ? p.duration : 5,
    poll_interval: p.poll_interval != null ? p.poll_interval : 6,
    max_wait: p.max_wait != null ? p.max_wait : 900,
  };
  state.formErr = '';
  state.modal = true;
}

function parseJsonField(v, name) {
  if (!v || !String(v).trim()) return undefined;
  try {
    return JSON.parse(String(v).trim());
  } catch (e) {
    throw new Error(name + ' 不是合法的 JSON：' + e.message);
  }
}

async function submitForm() {
  const f = state.form;
  if (!f.type || !f.api_base || !f.api_key || !f.model) {
    state.formErr = '必填项不能为空：type / api_base / api_key / model';
    return;
  }
  try {
    const cfg = {
      id: f.id || f.model, name: f.name || f.id || f.model, type: f.type,
      api_base: f.api_base, api_key: f.api_key, model: f.model,
      size: f.size || undefined, priority: Number(f.priority) || 100,
      timeout: Number(f.timeout) || 180,
      ref_field: f.ref_field || undefined,
    };
    if (f.extra_body) cfg.extra_body = parseJsonField(f.extra_body, 'extra_body');
    if (f.type === 'template') {
      cfg.url = f.url; cfg.method = f.method; cfg.response_field = f.response_field;
      cfg.image_kind = f.image_kind;
      if (f.headers) cfg.headers = parseJsonField(f.headers, 'headers');
      if (f.body_template) cfg.body_template = parseJsonField(f.body_template, 'body_template');
    }
    if (f.type === 'bailian_image') {
      cfg.capability = 'image';
      cfg.watermark = !!f.watermark;
      if (f.negative_prompt) cfg.negative_prompt = f.negative_prompt;
    }
    if (f.type === 'bailian_video') {
      cfg.capability = 'video';
      cfg.resolution = f.resolution || '720P';
      cfg.ratio = f.ratio || '16:9';
      cfg.duration = Number(f.duration) || 5;
      cfg.poll_interval = Number(f.poll_interval) || 6;
      cfg.max_wait = Number(f.max_wait) || 900;
      cfg.watermark = !!f.watermark;
    } else {
      cfg.capability = 'image';
    }
    const d = await apiPost('provider', { config: cfg });
    if (d && d.success === false) throw new Error(d.error || '保存失败');
    state.modal = false;
    toast('已' + (state.editId ? '更新' : '添加') + '提供商「' + cfg.id + '」');
    await loadProviders();
  } catch (e) {
    state.formErr = e.message || String(e);
  }
}

// ---------- 启动 ----------
function syncTheme(ctx) {
  if (ctx && typeof ctx.isDark === 'boolean') {
    document.documentElement.setAttribute('data-theme', ctx.isDark ? 'dark' : 'light');
  }
}

// ⚠️ onMounted 必须在 setup() 内部注册：写在模块顶层时没有活动组件实例，
// 生产版 Vue 会**静默忽略**（不报错、不执行），页面就永远停在"加载中"——这可能就是面板一直空的真凶。
function bootstrap() {
  diag('启动 ' + BUILD_TAG + ' 桥=' + (bridge && typeof bridge.apiGet === 'function' ? '有' : '无'));
  try { console.log('[生图管理] 启动 ' + BUILD_TAG + ' 桥=' + (bridge && typeof bridge.apiGet === 'function' ? '有' : '无')); } catch (e) {}
  if (bridge && typeof bridge.getContext === 'function') syncTheme(bridge.getContext());
  if (bridge && typeof bridge.onContext === 'function') bridge.onContext(syncTheme);
  refresh();
}

const __app = createApp({
  setup() {
    onMounted(bootstrap);
    return {
      IC, state, summary, filteredRows, enabledCount, BUILD_TAG,
      refresh, setMode, toggleProvider, savePriority, testProvider, saveLlm,
      askRemove, openAdd, openEdit, submitForm, closeModal: () => { state.modal = false; },
      closeConfirm: () => { state.confirm = null; },
      saveTemplates,
      setActive, typeLabel, imageProviders, videoProviders,
    };
  },
  template: `
<div class="app">
  <!-- 顶栏 -->
  <header class="topbar">
    <div class="brand">
      <div class="logo" aria-hidden="true" v-html="IC.spark"></div>
      <div>
        <h1>生图管理</h1>
        <div class="sub">多提供商画图 · apilio_draw</div>
      </div>
    </div>
    <div class="top-actions">
      <div class="seg" role="group" aria-label="生图模式">
        <button :class="{active: state.mode === 'sequential'}" @click="setMode('sequential')">顺序降级</button>
        <button :class="{active: state.mode === 'concurrent'}" @click="setMode('concurrent')">并发容错</button>
      </div>
      <div class="llm-toggle" :class="{off: !state.llmEnabled}" :title="state.llmEnabled ? '开启：AI可在聊天中直接调画图' : '关闭：仅 /画图 指令可用，AI 无法调用'">
        <span class="llm-label" v-html="IC.spark"></span>
        <span class="llm-text">AI 调用</span>
        <label class="switch" aria-label="AI 调用开关">
          <input type="checkbox" v-model="state.llmEnabled" @change="saveLlm()" :disabled="state.llmSaving">
          <span class="track"></span>
        </label>
      </div>
      <button class="btn" @click="refresh" :disabled="state.loading">
        <span class="spin" v-if="state.loading" v-html="IC.refresh"></span>
        <span v-else v-html="IC.refresh"></span>
        刷新
      </button>
    </div>
  </header>

  <!-- 加载错误横幅 -->
  <section class="section" v-if="state.loadErr" style="border-color:#7a2b2b">
    <div class="section-body">
      <div style="color:#ff9c9c;font-size:13px;line-height:1.7">
        ⚠ 数据没加载出来：<b>{{ state.loadErr }}</b><br>
        <span style="color:var(--text-3)">排查：① 按 Ctrl+F5 硬刷新；② 确认已登录面板；③ 把这段原文发我。</span>
      </div>
    </div>
  </section>

  <!-- 通道自检 -->
  <section class="section" style="border-color:#2f3f6b">
    <div class="section-body">
      <div style="color:var(--text-3);font-size:12px;line-height:1.7">通道自检：{{ state.diag || '（无）' }} ｜ loading={{ state.loading }} ｜ err={{ state.loadErr || '（无）' }}</div>
    </div>
  </section>

  <!-- 统计 -->
  <section class="stats" aria-label="用量统计">
    <div class="stat"><div class="label">提供商</div><div class="value accent">{{ state.providers.length }}</div><div class="foot">启用 {{ enabledCount }} 个</div></div>
    <div class="stat"><div class="label">今日调用</div><div class="value">{{ summary.total }}</div><div class="foot">成功率 {{ summary.rate }}%</div></div>
    <div class="stat"><div class="label">今日成功</div><div class="value ok">{{ summary.ok }}</div><div class="foot">失败 {{ summary.fail }}</div></div>
    <div class="stat"><div class="label">今日出图</div><div class="value">{{ summary.images }}</div><div class="foot">张</div></div>
    <div class="stat"><div class="label">今日 tokens</div><div class="value">{{ summary.tokens.toLocaleString() }}</div><div class="foot">模型用量</div></div>
  </section>

  <!-- 当前模型切换 -->
  <section class="section">
    <div class="section-head">
      <h2><span v-html="IC.spark"></span>当前模型</h2>
      <div class="actions"><span style="color:var(--text-3);font-size:12.5px">选完立即生效，不用重启</span></div>
    </div>
    <div class="section-body">
      <div class="field">
        <label>生图模型（/画图、AI 生图走这个）</label>
        <select :value="state.activeImage" @change="setActive('image', $event.target.value)">
          <option value="">自动（按优先级依次尝试）</option>
          <option v-for="p in imageProviders" :key="p.id" :value="p.id">{{ p.name }} · {{ p.model }}{{ p.enable ? '' : '（已停用）' }}</option>
        </select>
      </div>
      <div class="field">
        <label>生视频模型（/生视频、AI 生成视频走这个）</label>
        <select :value="state.activeVideo" @change="setActive('video', $event.target.value)">
          <option value="">自动（按优先级依次尝试）</option>
          <option v-for="p in videoProviders" :key="p.id" :value="p.id">{{ p.name }} · {{ p.model }}{{ p.enable ? '' : '（已停用）' }}</option>
        </select>
      </div>
      <div class="hint">设定后优先用该模型；它失败时仍会自动降级到其他启用的同类型模型。</div>
    </div>
  </section>

  <!-- 提供商 -->
  <section class="section">
    <div class="section-head">
      <h2><span v-html="IC.spark"></span>提供商（生图 / 生视频）</h2>
      <div class="actions">
        <button class="btn primary" @click="openAdd()"><span v-html="IC.plus"></span>添加提供商</button>
      </div>
    </div>
    <div class="section-body">
      <div class="providers" v-if="state.providers.length">
        <article class="pcard" :class="{off: !p.enable}" v-for="p in state.providers" :key="p.id">
          <div class="pcard-head">
            <div class="pcard-title">
              <div class="name">
                {{ p.name }}
                <span class="badge" :class="p.capability === 'video' ? 'type-template' : 'type-openai'">{{ p.capability === 'video' ? '生视频' : '生图' }}</span>
                <span class="badge" :class="p.type === 'template' ? 'type-template' : 'type-openai'">{{ typeLabel(p.type) }}</span>
                <span class="badge" :class="p.enable ? 'enabled' : 'disabled'">{{ p.enable ? '启用' : '停用' }}</span>
              </div>
              <div class="id">{{ p.id }}</div>
            </div>
            <label class="switch" :aria-label="'切换 ' + p.name">
              <input type="checkbox" :checked="p.enable" @change="toggleProvider(p)" :disabled="state.busy['t' + p.id]">
              <span class="track"></span>
            </label>
          </div>
          <div class="pcard-meta">
            <div class="row"><span class="k">模型</span><span class="v">{{ p.model }}</span></div>
            <div class="row"><span class="k">{{ p.capability === 'video' ? '规格' : '尺寸' }}</span><span class="v">{{ p.capability === 'video' ? ((p.resolution || '720P') + ' / ' + (p.ratio || '16:9') + ' / ' + (p.duration || 5) + '秒') : (p.size || '默认') }}</span></div>
            <div class="row"><span class="k">接口</span><span class="v">{{ p.api_base }}</span></div>
            <div class="row"><span class="k">Key</span><span class="v">{{ p.api_key || '—' }}</span></div>
            <div class="row"><span class="k">优先级</span>
              <input class="prio-input" type="number" v-model.number="p.priority" @change="savePriority(p)" :aria-label="p.id + ' 优先级'">
              <span style="color:var(--text-3);font-size:12px">越小越先</span>
            </div>
          </div>
          <div class="pcard-actions">
            <button class="btn sm" v-if="(p.capability === 'video' ? state.activeVideo : state.activeImage) !== p.id"
                    @click="setActive(p.capability || 'image', p.id)">设为当前</button>
            <button class="btn sm primary" v-else disabled>当前使用中</button>
            <button class="btn sm" @click="openEdit(p)"><span v-html="IC.edit"></span>编辑</button>
            <button class="btn sm" @click="testProvider(p)" :disabled="state.busy['s' + p.id] || !p.enable"><span v-html="IC.play"></span>{{ state.busy['s' + p.id] ? '测试中…' : '测试' }}</button>
            <button class="btn sm danger" @click="askRemove(p)"><span v-html="IC.trash"></span>删除</button>
          </div>
        </article>
      </div>
      <div class="empty" v-else>暂无提供商，点击右上角「添加提供商」开始</div>
    </div>
  </section>
  <!-- 注入提示词 -->
  <section class="section">
    <div class="section-head">
      <h2><span v-html="IC.edit"></span>注入提示词</h2>
      <div class="actions">
        <label style="display:flex;align-items:center;gap:6px;color:var(--text-2);font-size:13px">
          <input type="checkbox" v-model="state.injection.enabled" style="width:16px;height:16px">
          启用
        </label>
        <button class="btn sm primary" @click="saveTemplates()" :disabled="state.injectionSaving || state.injectionLoading">
          <span v-html="IC.check"></span>{{ state.injectionSaving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
    <div class="section-body">
      <div class="field">
        <label>注入提示词（画图时自动预载到用户描述前，按此规则出图）</label>
        <textarea v-model="state.injection.text" rows="5"
          placeholder="例如：高质量图片，细节丰富，构图完整，光影自然，色彩和谐，层次分明，避免文字与水印。"></textarea>
        <div class="hint">留空则不注入；一条全局规则，所有 /画图 请求都会带上。</div>
      </div>
    </div>
  </section>


  <!-- 用量日志 -->
  <section class="section">
    <div class="section-head">
      <h2><span v-html="IC.table"></span>用量日志</h2>
      <div class="actions">
        <button class="btn sm" @click="loadUsage()"><span v-html="IC.refresh"></span>刷新</button>
      </div>
    </div>
    <div class="section-body">
      <div class="log-toolbar">
        <select v-model="state.filterProvider" aria-label="按提供商筛选">
          <option value="all">全部提供商</option>
          <option v-for="p in state.providers" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
        <select v-model="state.filterOk" aria-label="按结果筛选">
          <option value="all">全部结果</option>
          <option value="ok">成功</option>
          <option value="fail">失败</option>
        </select>
        <span style="color:var(--text-3);font-size:12.5px">最近 {{ filteredRows.length }} 条记录（最多显示 300 条）</span>
      </div>
      <div class="log-wrap">
        <table class="log">
          <thead>
            <tr><th>时间</th><th>提供商</th><th>模型</th><th>结果</th><th>Tokens</th><th>出图</th><th>耗时</th><th>提示词</th><th>错误</th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in filteredRows" :key="i">
              <td class="mono">{{ r.ts }}</td>
              <td class="mono">{{ r.provider || '—' }}</td>
              <td class="mono">{{ r.model || '—' }}</td>
              <td><span class="chip" :class="r.ok ? 'ok' : 'fail'">{{ r.ok ? '成功' : '失败' }}</span></td>
              <td class="mono">{{ r.tokens || 0 }}</td>
              <td class="mono">{{ r.images || 0 }}</td>
              <td class="mono">{{ r.elapsed_ms ? (r.elapsed_ms / 1000).toFixed(1) + 's' : '—' }}</td>
              <td class="prompt-cell" :title="r.prompt">{{ r.prompt || '—' }}</td>
              <td class="prompt-cell" :title="r.err" style="color:var(--danger)">{{ r.err || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="empty" v-if="!filteredRows.length">暂无记录</div>
      </div>
    </div>
  </section>

  <!-- 添加/编辑模态 -->
  <div class="modal-scrim" v-if="state.modal" @click.self="state.modal = false">
    <div class="modal" role="dialog" aria-modal="true" :aria-label="state.editId ? '编辑提供商' : '添加提供商'">
      <div class="modal-head">
        <h3>{{ state.editId ? '编辑提供商' : '添加提供商' }}</h3>
        <button class="btn ghost" @click="state.modal = false" aria-label="关闭"><span v-html="IC.x"></span></button>
      </div>
      <div class="modal-body">
        <div class="form-grid">
          <div class="field">
            <label>ID <span class="req">*</span></label>
            <input v-model="state.form.id" placeholder="如 volcark / qwen-image" :disabled="!!state.editId">
          </div>
          <div class="field">
            <label>显示名称</label>
            <input v-model="state.form.name" placeholder="如 豆包 Seedream">
          </div>
          <div class="field">
            <label>类型 <span class="req">*</span></label>
            <select v-model="state.form.type">
              <option value="openai_compat">OpenAI 兼容（生图）</option>
              <option value="template">自定义模板（生图）</option>
              <option value="bailian_image">百炼 DashScope（生图）</option>
              <option value="bailian_video">百炼 DashScope（生视频）</option>
            </select>
          </div>
          <div class="field">
            <label>尺寸</label>
            <input v-model="state.form.size" placeholder="2K / 1024x1024（x 是字母）">
          </div>
          <div class="field full">
            <label>接口地址 api_base <span class="req">*</span></label>
            <input v-model="state.form.api_base" placeholder="https://ark.cn-beijing.volces.com/api/plan/v3">
          </div>
          <div class="field full">
            <label>API Key <span class="req">*</span></label>
            <input v-model="state.form.api_key" placeholder="sk-... / ark-...">
            <div class="hint">编辑时若显示 *** 掩码且未修改，将保留原 Key</div>
          </div>
          <div class="field full">
            <label>模型名 <span class="req">*</span></label>
            <input v-model="state.form.model" placeholder="doubao-seedream-5.0-lite">
          </div>
          <div class="field"><label>优先级</label><input type="number" v-model="state.form.priority"></div>
          <div class="field"><label>超时（秒）</label><input type="number" v-model="state.form.timeout"></div>
          <div class="field" v-if="state.form.type === 'openai_compat'">
            <label>参考图字段</label>
            <input v-model="state.form.ref_field" placeholder="image">
          </div>
          <div class="field full" v-if="state.form.type === 'openai_compat'">
            <label>附加请求体（JSON，可选）</label>
            <textarea v-model="state.form.extra_body" placeholder='{"watermark": false}'></textarea>
          </div>
          <template v-if="state.form.type === 'bailian_image'">
            <div class="field"><label>水印</label>
              <select v-model="state.form.watermark"><option :value="false">关</option><option :value="true">开</option></select>
            </div>
            <div class="field full"><label>负向提示词（可选）</label><input v-model="state.form.negative_prompt" placeholder="文字、水印、畸形"></div>
            <div class="hint full">api_base 填到根域即可，例如 https://token-plan.cn-beijing.maas.aliyuncs.com</div>
          </template>
          <template v-if="state.form.type === 'bailian_video'">
            <div class="field"><label>分辨率</label>
              <select v-model="state.form.resolution"><option value="720P">720P</option><option value="1080P">1080P</option></select>
            </div>
            <div class="field"><label>画幅</label>
              <select v-model="state.form.ratio"><option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option></select>
            </div>
            <div class="field"><label>时长（秒）</label><input type="number" v-model="state.form.duration" placeholder="5"></div>
            <div class="field"><label>轮询间隔（秒）</label><input type="number" v-model="state.form.poll_interval" placeholder="6"></div>
            <div class="field"><label>最长等待（秒）</label><input type="number" v-model="state.form.max_wait" placeholder="900"></div>
            <div class="field"><label>水印</label>
              <select v-model="state.form.watermark"><option :value="false">关</option><option :value="true">开</option></select>
            </div>
            <div class="hint full">视频是异步任务：提交后会一直轮询到出片再发给你，通常 1~3 分钟。</div>
          </template>
          <template v-if="state.form.type === 'template'">
            <div class="field full"><label>请求路径 url</label><input v-model="state.form.url" placeholder="/images/generations"></div>
            <div class="field"><label>方法</label><input v-model="state.form.method" placeholder="POST"></div>
            <div class="field"><label>响应字段路径</label><input v-model="state.form.response_field" placeholder="data.0.b64_json"></div>
            <div class="field"><label>图片字段类型</label>
              <select v-model="state.form.image_kind">
                <option value="b64_json">base64</option>
                <option value="url">URL</option>
                <option value="data_url">data URL</option>
              </select>
            </div>
            <div class="field full"><label>请求头（JSON，可选，支持 {api_key} 占位）</label><textarea v-model="state.form.headers" placeholder='{"Authorization": "Bearer {api_key}"}'></textarea></div>
            <div class="field full"><label>请求体模板（JSON，支持 {prompt} {size} {image} {model} 占位）</label><textarea v-model="state.form.body_template" placeholder='{"model": "{model}", "prompt": "{prompt}"}'></textarea></div>
          </template>
        </div>
        <div class="field" v-if="state.formErr"><span class="err" v-html="IC.alert"></span><span style="color:var(--danger);font-size:13px"> {{ state.formErr }}</span></div>
      </div>
      <div class="modal-foot">
        <button class="btn" @click="state.modal = false">取消</button>
        <button class="btn primary" @click="submitForm()">保存</button>
      </div>
    </div>
  </div>

  <!-- 确认删除 -->
  <div class="modal-scrim" v-if="state.confirm" @click.self="state.confirm = null">
    <div class="modal" style="max-width:420px" role="alertdialog" aria-modal="true">
      <div class="modal-head"><h3>{{ state.confirm.title }}</h3><button class="btn ghost" @click="state.confirm = null" aria-label="关闭"><span v-html="IC.x"></span></button></div>
      <div class="modal-body"><div style="color:var(--text-2);font-size:14px">{{ state.confirm.text }}</div></div>
      <div class="modal-foot">
        <button class="btn" @click="state.confirm = null">取消</button>
        <button class="btn danger" @click="state.confirm.action()">删除</button>
      </div>
    </div>
  </div>

  <div style="text-align:center;color:var(--text-3);font-size:11.5px;padding:8px 0 28px">{{ BUILD_TAG }}</div>

  <!-- 提示 -->
  <div class="toasts" aria-live="polite">
    <div class="toast" :class="t.kind" v-for="t in state.toasts" :key="t.id">
      <span v-html="t.kind === 'err' ? IC.alert : IC.check"></span>{{ t.msg }}
    </div>
  </div>
</div>
`,
});
try {
  __app.mount('#app');
  console.log('[生图管理] mount 返回');
} catch (e) {
  console.log('[生图管理] mount 抛错: ' + (e && e.message ? e.message : e));
}
