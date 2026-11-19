import { useEffect, useState } from 'react';
import { API_BASE, getJSON } from '../api';
import { authSession, authenticationLost } from '../authSession';

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
    <p>后台今日调用：{Object.entries(status.limits).map(([key,limit]) => `${({ chat: '模型', embedding: '记忆向量', tts: '语音' })[key]} ${status.background_usage?.[key]?.requests || 0}/${limit}`).join(' · ')}</p>
    <p className="muted">后台预算按上海现实日期重置，不影响用户对话。供应商余额与限流仍然有效。超时用量未知，未完成回执不当作成功。</p>
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
