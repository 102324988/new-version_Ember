import { useEffect, useState } from 'react';
import { API_BASE, getJSON } from '../api';

const endpoint = API_BASE + '/api/persona';
const json = { 'Content-Type': 'application/json' };

function revisionLabel(revision) {
  return revision === 0 ? '基准人设' : `版本 ${revision}`;
}

function PersonaEditor() {
  const [snapshot, setSnapshot] = useState(null);
  const [core, setCore] = useState('');
  const [style, setStyle] = useState('');
  const [message, setMessage] = useState('');
  const [restore, setRestore] = useState('');
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [preview, setPreview] = useState(null);
  const [trial, setTrial] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const active = snapshot?.active;
  const dirty = active && (core !== active.core_persona || style !== active.speaking_style);

  function receive(result, keepDraft = false) {
    setSnapshot(result);
    if (!keepDraft) {
      setCore(result.active.core_persona);
      setStyle(result.active.speaking_style);
    }
    setRestore(result.versions.find(row => row.revision !== result.active.revision)?.revision ?? '');
    setConfirmRestore(false);
    setPreview(null);
    setTrial(null);
  }

  useEffect(() => {
    const cancel = new AbortController();
    getJSON(endpoint, { signal: cancel.signal })
      .then(result => { if (!cancel.signal.aborted) receive(result); })
      .catch(failure => { if (!cancel.signal.aborted) setError(failure.message); });
    return () => cancel.abort();
  }, []);

  const draft = active && { expected_revision: active.revision, core_persona: core, speaking_style: style };

  async function run(label, request) {
    setBusy(label);
    setError('');
    setNotice('');
    try { await request(); }
    catch (failure) { setError(failure.message); }
    finally { setBusy(''); }
  }

  function refresh() {
    return run('正在读取人设…', async () => {
      receive(await getJSON(endpoint), !!dirty);
      if (dirty) setNotice('已读取当前版本，草稿仍保留。请重新预览差异后再应用。');
    });
  }

  function send(path, body, method = 'POST', timeout = 10000) {
    return getJSON(endpoint + path, { method, headers: json, body: JSON.stringify(body), timeout });
  }

  function edit(set, value) {
    set(value);
    setPreview(null);
    setTrial(null);
    setConfirmRestore(false);
    setNotice('');
  }

  if (!active) return error
    ? <><p role="alert">{error}</p><button type="button" disabled={!!busy} onClick={refresh}>重试读取人设</button></>
    : <p role="status">正在读取人设…</p>;

  return <>
    <p className="muted">当前使用 {revisionLabel(active.revision)}。微调依鸣时，已有聊天、关系和世界状态会继续沿用。</p>
    <fieldset className="persona-fields" disabled={!!busy}>
      <label>核心人设
        <textarea rows={10} maxLength={12000} value={core} onChange={event => edit(setCore, event.target.value)} />
      </label>
      <label>说话方式
        <textarea rows={4} maxLength={1600} value={style} placeholder="留空则沿用当前表达规则"
          onChange={event => edit(setStyle, event.target.value)} />
      </label>
      <div className="action-row">
        <button type="button" disabled={!core.trim()} onClick={() => run('正在生成差异…', async () => {
          setPreview(await send('/preview', draft));
        })}>预览提示词</button>
        <button type="button" disabled={!dirty || !core.trim()} onClick={() => run('正在应用…', async () => {
          const saved = await send('', draft, 'PUT');
          receive(saved);
          setNotice(`${revisionLabel(saved.active.revision)} 已保存，下轮对话生效。`);
        })}>应用到正式对话</button>
        <button type="button" onClick={refresh}>读取当前版本</button>
      </div>
      {preview && <div className="persona-preview">
        <p>{preview.diff ? '当前版与草稿版的差异' : '草稿与当前版的有效提示词一致。'}</p>
        {preview.diff && <pre>{preview.diff}</pre>}
        <details><summary>查看草稿版提示词</summary>
          <p className="muted">正式对话还会加入本轮时间、场景、历史和可用工具。</p>
          <pre>{preview.effective}</pre>
        </details>
      </div>}
      <details>
        <summary>隔离试聊</summary>
        <p className="muted">使用合成前文对比两版，会调用模型。试聊不进入正式聊天或记忆。</p>
        <label>测试消息
          <textarea rows={2} maxLength={800} value={message} placeholder="输入一句想试的话"
            onChange={event => { setMessage(event.target.value); setTrial(null); }} />
        </label>
        <button type="button" disabled={!core.trim() || !message.trim()} onClick={() => run('正在生成试聊回复…', async () => {
          setTrial(null);
          setTrial(await send('/trial', { ...draft, message }, 'POST', 135000));
        })}>比较两版回复</button>
        {trial && <div className="persona-replies">
          <div><strong>{revisionLabel(trial.revision)}</strong><p>{trial.current || '（角色没有开口）'}</p></div>
          <div><strong>草稿版</strong><p>{trial.draft || '（角色没有开口）'}</p></div>
        </div>}
      </details>
      {snapshot.versions.length > 1 && <details>
        <summary>恢复历史版本</summary>
        <label>选择要恢复的人设
          <select value={restore} onChange={event => { setRestore(Number(event.target.value)); setConfirmRestore(false); }}>
            {snapshot.versions.filter(row => row.revision !== active.revision).map(row =>
              <option value={row.revision} key={row.revision}>{revisionLabel(row.revision)}
                {row.created_at ? ` · ${new Date(row.created_at).toLocaleString('zh-CN')}` : ''}
              </option>)}
          </select>
        </label>
        {confirmRestore && <p>{dirty ? '恢复会替换当前草稿。' : '恢复会新增一条版本记录。'}</p>}
        <div className="action-row">
          <button type="button" disabled={restore === ''} onClick={() => {
            if (!confirmRestore) { setConfirmRestore(true); return; }
            run('正在恢复人设…', async () => {
              receive(await send('/restore', { expected_revision: active.revision, revision: restore }));
              setNotice(`已恢复为${revisionLabel(restore)}的内容，下轮对话生效。`);
            });
          }}>{confirmRestore ? '确认恢复' : '恢复所选版本'}</button>
          {confirmRestore && <button type="button" onClick={() => setConfirmRestore(false)}>取消</button>}
        </div>
      </details>}
    </fieldset>
    {busy && <p role="status">{busy}</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </>;
}

export default function PersonaSettings() {
  const [opened, setOpened] = useState(false);
  return <section className="settings-section">
    <h3>人设</h3>
    <details onToggle={event => { if (event.target.open) setOpened(true); }}>
      <summary>编辑人设与说话方式</summary>
      {opened && <PersonaEditor />}
    </details>
  </section>;
}
