"""Public turn errors never contain provider payloads, credentials or user text."""


def public_error(error):
    if hasattr(error, 'code'):
        messages = {
            'timeout': '模型回复超时，请稍后重试。',
            'connection': '模型接口连接失败，请检查网络与代理后重试。',
            'authentication': '模型接口认证失败，请检查 API 密钥。',
            'configuration': '模型接口配置无效，请检查模型名称和接口设置。',
            'rate_limit': '模型供应商限流或余额不足，请稍后重试并核对供应商用量。',
        }
        if error.code in messages:
            return {'code': 'model_' + error.code, 'message': messages[error.code]}
    name = type(error).__name__
    if name == 'BudgetExceeded':
        return {'code': 'budget_exhausted', 'message': '今日模型调用额度已用完，请查看设置中的服务用量。消息已接收，模型回复尚未完成。'}
    if name in ('APITimeoutError', 'APIConnectionError', 'ConnectTimeout', 'ReadTimeout'):
        return {'code': 'model_unreachable', 'message': '模型接口连接失败或超时，请检查网络与代理后重试。'}
    if name == 'AuthenticationError':
        return {'code': 'model_auth', 'message': '模型接口认证失败，请检查实际使用的 API 密钥。'}
    if name == 'RateLimitError':
        return {'code': 'model_rate_limit', 'message': '模型供应商限流或额度不足，请稍后重试并核对供应商用量。'}
    return {'code': 'processing_failed', 'message': '本轮处理失败，请运行一键诊断并检查后端日志。'}
