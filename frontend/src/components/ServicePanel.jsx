import { useEffect, useState } from 'react';
import { API_BASE, getJSON } from '../api';
import { authSession, authenticationLost } from '../authSession';

const kinds = { chat: '模型', embedding: '记忆向量', tts: '语音', memory: '记忆整理', per_minute: '每分钟调用（每类）' };
const quotaKeys = Object.keys(kinds);

function BackgroundQuotas({ status }) {
  const [draft, setDraft] = useState({});
  const [saved, setSaved] = useState(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const effective = saved && saved.quota_revision >= status.quota_revision ? saved.background_quotas : status.background_quotas;
  const values = { ...effective, ...draft };
  const hasEdits = Object.keys(draft).length > 0;

  const change = (key, value) => {
    setDraft(previous => ({ ...previous, [key]: value }));
    setNotice('');
    setError('');
  };
  const update = async overrides => {
    const entries = Object.entries(overrides);
    if (entries.some(([, value]) => value !== null && (!/^[0-9]+$/.test(String(value)) || Number(value) > 2147483647))) {
      setError('请输入 0 至 2147483647 的整数，或勾选「不限额」。');
      return;
    }
    const body = Object.fromEntries(entries.map(([key, value]) => [key, value === null ? null : Number(value)]));
    setSaving(true);
    setNotice('');
    setError('');
    try {
      const result = await getJSON(API_BASE + '/service/background-quotas', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      setSaved(result);
      setDraft({});
      setNotice('后台限额已保存，立即生效。');
    } catch (failure) { setError(failure.message); }
    finally { setSaving(false); }
  };

  return <form className="quota-settings" onSubmit={event => { event.preventDefault(); if (hasEdits) update(draft); }} noValidate>
    <fieldset disabled={saving}>
      <legend>后台调用限额</legend>
      {quotaKeys.every(key => effective[key] === null) && <p>当前五项后台调用均不限额。</p>}
      <p className="muted">要设置上限，先取消对应的「不限额」，输入次数后保存。填 0 会暂停对应调用。每日上限按上海时间重置。</p>
      {quotaKeys.map(key => <div className="quota-row" key={key}>
        <label htmlFor={'quota-' + key}>{kinds[key]}{key === 'per_minute' ? '' : '每日上限'}</label>
        <input id={'quota-' + key} type="number" inputMode="numeric" min="0" max="2147483647" step="1"
          value={values[key] ?? ''} disabled={values[key] === null}
          placeholder={values[key] === null ? '不限额' : '输入次数'}
          onChange={event => change(key, event.target.value)} />
        <label className="quota-unlimited"><input type="checkbox" checked={values[key] === null}
          aria-label={kinds[key] + '不限额'}
          onChange={event => change(key, event.target.checked ? null : effective[key] ?? '')} />不限额</label>
      </div>)}
      <div className="action-row">
        <button type="submit" disabled={!hasEdits}>{saving ? '正在保存…' : '保存限额'}</button>
        <button type="button" disabled={quotaKeys.every(key => effective[key] === null)}
          onClick={() => update(Object.fromEntries(quotaKeys.map(key => [key, null])))}>解除全部后台限额</button>
      </div>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </form>;
}

export default function ServicePanel({ status }) {
  const [devices, setDevices] = useState([]);
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);
  useEffect(() => { getJSON(`${API_BASE}/auth/devices`).then(data => setDevices(data.items)).catch(failure => setError(failure.message)); }, []);
  const revoke = async id => {
    setWorking(true); setError('');
    try {
      await getJSON(`${API_BASE}/auth/devices/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
      if (id === authSession().session_id) authenticationLost();
      else setDevices(items => items.filter(item => item.id !== id));
    } catch (failure) { setError(failure.message); }
    finally { setWorking(false); }
  };
  return <section className="settings-section"><h3>连接与服务</h3>
    {error && <p role="alert">{error}</p>}
    <p>用户对话：{status.foreground_unlimited ? '应用不设每日额度' : '服务正在更新，请核对后端版本'}</p>
    <p>后台今日调用：{Object.entries(status.limits).map(([key, limit]) => `${kinds[key]} ${status.background_usage?.[key]?.requests || 0}/${limit === null ? '不限额' : limit}`).join(' · ')}</p>
    {status.background_quotas && <BackgroundQuotas status={status} />}
    <p className="muted">供应商按自己的规则计费和限流。超时的调用用量可能未知。</p>
    {status.reset_at && <p className="muted">后台预算重置：{new Date(status.reset_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</p>}
    {!!status.breakdown?.length && <details><summary>查看今日调用明细</summary>{status.breakdown.map(row => <p key={`${row.origin}:${row.kind}:${row.call_type}:${row.status}`}>
      {({ foreground: '用户对话', background: '后台', legacy: '升级前记录' })[row.origin]} · {row.call_type} · {({ completed: '成功', incomplete: '输出截断', failed: '失败', pending: '待核对', not_sent: '未发出（不扣预算）' })[row.status] || row.status} {row.requests} 次 · 输入 {row.input_tokens} / 输出 {row.output_tokens} Token · 用量未知 {row.unknown_usage} 次
    </p>)}</details>}
    {status.error && <p role="status">{status.error}</p>}
    <p>最近自动备份：{status.jobs.find(item => item.status === 'completed') ? '已保存，可在存档中查看' : '尚未完成'}</p>
    {devices.map(item => <div className="setting-row" key={item.id}><span>{item.label}{item.id === authSession().session_id ? '（当前设备）' : ''}</span>
      <button disabled={working} onClick={() => revoke(item.id)}>{item.id === authSession().session_id ? '断开此设备' : '撤销连接'}</button></div>)}
  </section>;
}
