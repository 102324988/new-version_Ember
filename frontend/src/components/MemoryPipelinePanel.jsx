import { useEffect, useState } from 'react';
import { API_BASE, getJSON } from '../api';

const phases = { waiting: '等待整理', encoding: '正在编码', storing: '生成向量或入库中',
  vectorizing: '生成向量', committing: '等待数据库提交',
  completed: '上批已完成', completed_empty: '上批已完成，无新增', paused: '后台整理已暂停',
  budget_wait: '后台预算暂停，聊天仍可继续', backoff: '退避等待', needs_attention: '需要处理' };
const errors = { budget_wait: '后台预算不足', circuit_wait: '后台通道连续失败，暂时暂停',
  authentication: '模型认证失败，请修正密钥', configuration: '模型配置无效', timeout: '模型请求超时',
  connection: '模型连接失败', rate_limit: '供应商限流', invalid_result: '格式或来源校验失败',
  input_budget: '整理输入超过预算，请检查提示词或来源元数据',
  output_limit: '整理结果被截断，已缩小批次等待重试',
  database: '数据库暂时不可用', vector_invalid: '向量生成结果无效', unavailable: '模型服务未完成请求',
  source_changed: '来源发生变化，请核对待办', background_busy: '上一网络请求尚未退出' };
const time = value => value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '暂无';

export default function MemoryPipelinePanel({ initial, epoch }) {
  const [status, setStatus] = useState(initial);
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const result = await getJSON(`${API_BASE}/memory/tasks?epoch=${encodeURIComponent(epoch || '')}`);
        if (active && result.epoch === epoch) { setStatus(result.status); setError(''); }
      } catch (failure) { if (active) setError(failure.message); }
    };
    refresh();
    const timer = setInterval(refresh, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [epoch]);
  const control = async (action, batch_id = '') => {
    setWorking(true); setError('');
    try {
      const result = await getJSON(`${API_BASE}/memory/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, batch_id, epoch }) });
      if (result.epoch !== epoch) throw new Error('世界已切换，请重新打开设置');
      setStatus(result.status);
    } catch (failure) { setError(failure.message); }
    finally { setWorking(false); }
  };
  const retry = status.encoding_retry || {};
  return <div>
    <p role="status">记忆整理：{phases[status.phase] || '等待同步'}</p>
    <p className="muted">记忆整理待办 {status.pending_batches || 0} 批 · 待入库 {status.pending_memories || 0} 条</p>
    {status.journal_bytes > 0 && <p className="muted">待处理原始日志 {(status.journal_bytes / 1024).toFixed(1)} KB</p>}
    <p className="muted">最近成功：{time(status.last_success_at)}</p>
    {retry.code && <p>{errors[retry.code] || '整理未完成，资料已保留'} · 已失败 {retry.attempts || 0} 次{!retry.needs_attention && ` · 下次尝试 ${time(retry.next_attempt_at)}`}</p>}
    {error && <p role="alert" className="inline-error">{error}</p>}
    <button disabled={working} onClick={() => control(status.phase === 'paused' ? 'resume' : 'pause')}>{status.phase === 'paused' ? '继续后台整理' : '暂停后台整理'}</button>
    {status.batches?.map(batch => <div key={batch.id} className="setting-row"><span>{batch.stage === 'encoding' ? `编码 ${batch.index}/${batch.total}` : '待向量或入库'}{batch.created_at ? ` · 创建于 ${time(batch.created_at)}` : ''}</span>
      <button disabled={working || status.phase === 'encoding'} onClick={() => control('retry', batch.id)}>重试这一批</button></div>)}
    <p className="muted">重试沿用原任务，仍遵守后台预算。原始资料在入库确认前保留。</p>
  </div>;
}
