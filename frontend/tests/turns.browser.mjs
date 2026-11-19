import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const project = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const vite = await createServer({ root: project, envDir: false, envPrefix: 'EMBER_SYNTHETIC_',
  define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('http://localhost:8000') },
  server: { host: '127.0.0.1', port: 0 } });
await vite.listen();
let browser;
try {
  browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) });
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('autoPlay', 'true');
    window.audioStarts = 0;
    window.Audio = class {
      async play() { if (!this.src?.startsWith('data:audio/wav;base64,UklGRiY')) window.audioStarts += 1; }
      pause() {}
      removeAttribute() {}
      load() {}
    };
  });
  let socket, current = null;
  const sent = [], history = [];
  const now = Date.parse('2026-10-21T17:40:00+08:00');
  let pipeline = { phase: 'budget_wait', pending_memories: 2, batches: [{ id: 'synthetic-memory-batch', stage: 'encoding', index: 0, total: 1 }],
    encoding_retry: { code: 'budget_wait', attempts: 0, next_attempt_at: now/1000+3600 } };
  const memoryActions = [];
  let failMemoryRetry = true;
  const emit = message => socket.send(JSON.stringify(message));
  const config = () => ({ display_name: '依鸣', logical_time: '2026-10-21 17:40:00',
    is_thinking: current && (['running', 'stopping'].includes(current.status) || current.released === false), timezone: 'Asia/Shanghai', turn: current,
    state: { scene: { communication_mode: 'unknown' } }, capabilities: { cancel_turn: true, correlated_audio: true, message_receipts: true },
    service: { enabled: true, foreground_unlimited: true, limits: { chat: 150, embedding: 200, tts: 200 },
      background_usage: { chat: { requests: 150 } }, usage: { chat: { requests: 999 } }, jobs: [] },
    status: { database: { ready: true }, long_term_memory: { ready: true }, scene: { ready: true }, memory_pipeline: pipeline, paused: false } });
  await page.route('http://localhost:8000/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/auth/devices') return route.fulfill({ json: { items: [] } });
    if (url.pathname === '/memory/tasks') {
      if (route.request().method() === 'POST') {
        const data = route.request().postDataJSON();
        memoryActions.push(data.action);
        if (data.action === 'retry' && failMemoryRetry) {
          failMemoryRetry = false;
          return route.fulfill({ status: 503, json: { detail: '合成重试暂未保存，资料保留' } });
        }
        pipeline = { ...pipeline, phase: data.action === 'pause' ? 'paused' : 'waiting', encoding_retry: {} };
      }
      return route.fulfill({ json: { status: pipeline } });
    }
    return route.fulfill({ json: url.pathname === '/config' ? config() : [...history].reverse() });
  });
  await page.route('**/models/**', route => route.abort());
  await page.routeWebSocket('**/ws/chat*', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const event = JSON.parse(raw);
      if (event.type === 'cancel_turn') {
        assert.equal(event.turn_id, current.turn_id);
        if (current.status !== 'completed') { current.status = 'stopping'; current.phase = 'stopping'; }
        emit({ type: 'turn.stopping', ...current });
        return;
      }
      if (!event.content || event.type) return;
      sent.push(event);
      assert.ok(!current || (!['running', 'stopping'].includes(current.status) && current.released !== false), 'A new input arrived before the worker ended');
      current = { turn_id: event.retry_reply ? `retry-${sent.length}` : event.client_request_id, user_message_id: event.client_request_id,
        message_id: `reply-${sent.length}`, timestamp: now + sent.length * 1000, status: 'running', phase: 'generating', released: false };
      if (!event.retry_reply) history.push({ id: history.length + 1, role: 'user', content: event.content, timestamp: current.timestamp,
        message_id: event.client_request_id, turn_id: current.turn_id, completion_status: 'completed' });
      emit({ type: 'message', sender: 'user', content: event.content, message_id: event.client_request_id,
        client_request_id: event.client_request_id, timestamp: current.timestamp, turn_id: current.turn_id });
      emit({ type: 'message.persisted', message_id: event.client_request_id });
      emit({ type: 'turn.started', ...current });
      emit({ type: 'message', sender: 'ai', mode: 'start', content: '', ...current });
      emit({ type: 'message', sender: 'ai', mode: 'append', content: '<response>已说出的合成片段', ...current });
    });
  });
  const finish = (status = 'interrupted') => {
    const turn = { ...current, status, released: true };
    const text = status === 'completed' ? '已完成的合成回复' : '已说出的合成片段';
    current = turn;
    history.push({ id: history.length + 1, role: 'ai', content: text, timestamp: turn.timestamp,
      message_id: turn.message_id, turn_id: turn.turn_id, completion_status: status });
    emit({ type: 'message', sender: 'ai', mode: 'replace', content: text, ...turn, completion_status: status });
    emit({ type: 'message.persisted', message_id: turn.message_id });
    emit({ type: 'turn.finished', ...turn });
    return turn;
  };
  const address = vite.httpServer.address();
  await page.goto(`http://127.0.0.1:${address.port}`);
  await page.locator('.connection-online').waitFor();
  const input = page.getByRole('textbox', { name: '对依鸣说' });
  await input.fill('第一条合成问题');
  await input.press('Enter');
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  await input.fill('停止后发送的第二条');
  await page.getByRole('button', { name: '打断并发送', exact: true }).click();
  await page.getByRole('button', { name: '正在停止…', exact: true }).waitFor();
  assert.equal(sent.length, 1);
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.getByRole('textbox', { name: '对依鸣说' }).scrollIntoViewIfNeeded();
  const composer = await page.locator('.composer').boundingBox();
  assert.ok(composer.y + composer.height <= 769, 'composer should remain reachable during stop and queued send');
  await input.fill('继续编辑的新草稿');
  const first = finish();
  await page.getByText('停止后发送的第二条', { exact: true }).waitFor();
  assert.equal(sent.length, 2);
  assert.equal(await input.inputValue(), '继续编辑的新草稿');
  emit({ type: 'message', sender: 'ai', mode: 'append', message_id: first.message_id, turn_id: first.turn_id, content: '不应出现的迟到文字' });
  emit({ type: 'audio', message_id: first.message_id, turn_id: first.turn_id, audio_base64: 'late' });
  assert.equal(await page.getByText(/不应出现的迟到文字/).count(), 0);
  assert.equal(await page.evaluate(() => window.audioStarts), 0);

  await page.getByRole('button', { name: '停止生成', exact: true }).click();
  await page.getByRole('button', { name: '正在停止…', exact: true }).waitFor();
  finish();
  await page.getByRole('button', { name: '发送', exact: true }).waitFor();
  assert.equal(await input.inputValue(), '继续编辑的新草稿');
  assert.equal(await page.locator('.interrupted-label').count(), 2);

  await input.press('Enter');
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  await input.fill('取消自动发送后留下的草稿');
  await page.getByRole('button', { name: '打断并发送', exact: true }).click();
  await page.getByRole('button', { name: '取消待发', exact: true }).click();
  finish();
  await page.getByRole('button', { name: '发送', exact: true }).waitFor();
  assert.equal(sent.length, 3);
  assert.equal(await input.inputValue(), '取消自动发送后留下的草稿');
  await page.reload();
  await page.locator('.interrupted-label').nth(2).waitFor();
  assert.equal(await input.inputValue(), '取消自动发送后留下的草稿');

  await input.press('Enter');
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  await input.fill('断线后不能自动发送的草稿');
  await page.getByRole('button', { name: '打断并发送', exact: true }).click();
  socket.close({ code: 1012, reason: 'synthetic disconnect' });
  await page.locator('.connection-reconnecting').waitFor();
  await page.locator('.connection-online').waitFor();
  finish();
  await page.getByRole('button', { name: '发送', exact: true }).waitFor();
  assert.equal(sent.length, 4);
  assert.equal(await input.inputValue(), '断线后不能自动发送的草稿');
  assert.equal(await page.locator('.queued-draft').count(), 0);
  assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isEnabled(), true);
  await input.press('Enter');
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  current.status = 'completed';
  current.released = false;
  await input.fill('收尾确认后才能发送的下一条');
  await page.getByRole('button', { name: '打断并发送', exact: true }).click();
  await page.getByRole('button', { name: '正在停止…', exact: true }).waitFor();
  assert.equal(sent.length, 5);
  finish('completed');
  await page.getByText('收尾确认后才能发送的下一条', { exact: true }).waitFor();
  assert.equal(sent.length, 6);
  finish('completed');
  await page.waitForFunction(count => document.querySelectorAll('.message-user').length === count, sent.length);
  const userMessagesBeforeProactive = await page.locator('.message-user').count();
  await page.getByRole('button', { name: '自动声音：开', exact: true }).waitFor();
  current = { turn_id: 'synthetic-proactive-turn', message_id: 'synthetic-proactive-reply',
    timestamp: now + 100000, source: 'proactive', status: 'running', phase: 'generating', released: false };
  emit({ type: 'turn.started', ...current });
  emit({ type: 'message', sender: 'ai', mode: 'start', content: '', ...current });
  emit({ type: 'message', sender: 'ai', mode: 'append', content: '<response>合成主动话题</response>', ...current });
  const proactive = finish('completed');
  emit({ type: 'audio', message_id: proactive.message_id, turn_id: proactive.turn_id, audio_base64: 'synthetic' });
  await page.waitForFunction(() => window.audioStarts === 1);
  assert.equal(await page.locator('.message-user').count(), userMessagesBeforeProactive);
  await input.fill('模型超时后重试这一句');
  await input.press('Enter');
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  current.error = { code: 'model_timeout', message: '模型回复超时，请稍后重试。' };
  const failed = finish('failed');
  await page.getByText('模型回复超时，请稍后重试。', { exact: true }).waitFor();
  const usersBeforeRetry = await page.locator('.message-user').count();
  await page.getByRole('button', { name: '重试回复', exact: true }).click();
  await page.getByRole('button', { name: '停止生成', exact: true }).waitFor();
  assert.equal(sent.at(-1).client_request_id, failed.user_message_id);
  assert.equal(sent.at(-1).retry_reply, true);
  assert.equal(await page.locator('.message-user').count(), usersBeforeRetry);
  finish('completed');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const settings = page.getByRole('dialog', { name: '设置' });
  await settings.getByText('用户对话：应用不设每日额度', { exact: true }).waitFor();
  await settings.getByText(/后台今日调用：模型 150\/150/).waitFor();
  await settings.getByText(/后台预算暂停，聊天仍可继续/).waitFor();
  await settings.getByRole('button', { name: '暂停后台整理', exact: true }).click();
  await settings.getByRole('button', { name: '继续后台整理', exact: true }).click();
  await settings.getByRole('button', { name: '重试这一批', exact: true }).click();
  await settings.getByText('合成重试暂未保存，资料保留', { exact: true }).waitFor();
  await settings.getByRole('button', { name: '重试这一批', exact: true }).click();
  assert.deepEqual(memoryActions, ['pause', 'resume', 'retry', 'retry']);
  await page.keyboard.press('Escape');
  assert.deepEqual(errors, []);
  await mkdir(resolve(project, 'test-results'), { recursive: true });
  await page.screenshot({ path: resolve(project, 'test-results', 'turn-interrupted.png'), fullPage: true });
  console.log(JSON.stringify({ result: 'passed', scenarios: ['wait for terminal before next send', 'edit draft while stopping',
    'reject stale text/audio', 'cancel queued send', 'reload interrupted records', 'disconnect cancels automatic follow-up', 'completion waits for worker release',
    'proactive turn and audio without fake user input', 'same user ID reply retry', 'foreground unlimited display', 'memory pause resume and failed retry recovery'] }));
} finally {
  await browser?.close();
  await vite.close();
}
