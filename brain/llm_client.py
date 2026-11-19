from openai import OpenAI
import logging
import json
import threading
import os
import hashlib
import httpx
from core.service_policy import reserve, finish
from json_repair import repair_json
from config.settings import settings
from core.turns import TurnCancelled
from brain.reasoning_filter import ReasoningFilter, remove_reasoning
from core.model_errors import ModelCallError, classify, cause_types
from core.http_transport import ConnectFailoverTransport

logger = logging.getLogger(__name__)


class LLMClient:
    """
    LLM 客户端（单例模式）

    所有模块共享同一个实例，复用 HTTP 连接池，减少 TLS 握手开销。
    内部维护三个独立的 OpenAI 客户端：
    - large_client: 大模型（用于对话生成）
    - small_client: 小模型（用于状态更新等轻量任务）
    - embedding_client: 嵌入模型（用于向量化）
    """

    _instance = None
    _lock = threading.Lock()
    _background_slot = threading.BoundedSemaphore(1)

    @staticmethod
    def _provider(model):
        return hashlib.sha256('|'.join(str(getattr(model, k, '')) for k in ('name', 'base_url', 'api_key')).encode()).hexdigest()

    @staticmethod
    def _timeouts(duration):
        return httpx.Timeout(duration, connect=min(5, duration), write=min(10, duration), pool=min(5, duration))

    @staticmethod
    def _http_client():
        if any(os.getenv(key) for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')):
            # Let HTTPX honor proxy and NO_PROXY routing. A custom transport
            # would disable environment-proxy discovery.
            return httpx.Client(follow_redirects=True)
        return httpx.Client(transport=ConnectFailoverTransport(), follow_redirects=True)

    def _memory_request(self, request, timeout, **kwargs):
        """Overall deadline; an abandoned HTTP request cannot start another.

        Only the outer worker can commit data. A late network result is ignored.
        The slot stays held until the actual HTTP call exits.
        """
        if not self._background_slot.acquire(blocking=False):
            raise ModelCallError('background_busy')
        done, result = threading.Event(), {}
        def run():
            try:
                result['value'] = request(timeout=self._timeouts(timeout), **kwargs)
            except Exception as error:
                result['error'] = error
            finally:
                self._background_slot.release()
                done.set()
        threading.Thread(target=run, daemon=True, name='ember-memory-http').start()
        if not done.wait(timeout):
            raise ModelCallError('timeout')
        if 'error' in result:
            raise result['error']
        return result['value']

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        # 避免重复初始化
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            logger.info("[LLMClient] 初始化连接池（单例模式）")
            self.large_client = OpenAI(
                api_key=settings.LARGE_LLM.api_key,
                base_url=settings.LARGE_LLM.base_url,
                max_retries=0,
                http_client=self._http_client(),
            )
            self.small_client = OpenAI(
                api_key=settings.SMALL_LLM.api_key,
                base_url=settings.SMALL_LLM.base_url,
                max_retries=0,
                http_client=self._http_client(),
            )
            self.embedding_client = OpenAI(
                api_key=settings.EMBEDDING_MODEL.api_key,
                base_url=settings.EMBEDDING_MODEL.base_url,
                max_retries=0,
                http_client=self._http_client(),
            )
            self._initialized = True
            # HTTP transport retries only TCP/TLS establishment, before an HTTP
            # request is sent. SDK retries of sent requests remain disabled.

    @classmethod
    def _reset_instance(cls):
        """重置单例实例（仅用于测试）"""
        with cls._lock:
            cls._instance = None

    def _extract_json(self, content):
        if not content or not content.strip():
            logger.warning("LLM 返回空内容，无法解析 JSON")
            return None
        try:
            good_json_string = repair_json(content)
            data = json.loads(good_json_string)
            return data
        except (json.JSONDecodeError, Exception) as e:
            logger.warning('Model JSON parse failed type=%s', type(e).__name__)
            return None

    def _log_usage(self, usage, model_name: str, call_type: str = "dialogue"):
        """记录 Token 消耗日志，兼容 OpenAI 和 Gemini 两种响应格式，包含缓存信息和调用类型

        call_type: dialogue(对话) | proactive(主动发言) | state_update(状态更新) | idle_evolve(闲置演化) | memory_query(记忆检索) | memory_encode(记忆编码)
        """
        if usage is None:
            logger.debug("[LLM Usage] usage 为空")
            return

        # 获取基础 token 数
        p = getattr(usage, "prompt_tokens", None)
        c = getattr(usage, "completion_tokens", None)

        # Gemini 风格: prompt_token_count / candidates_token_count
        if p is None:
            p = getattr(usage, "prompt_token_count", None)
        if c is None:
            c = getattr(usage, "candidates_token_count", None)

        try:
            p = int(p) if p is not None else 0
            c = int(c) if c is not None else 0
        except (TypeError, ValueError):
            p, c = 0, 0

        # 获取缓存命中信息
        cached_tokens = 0
        token_details = getattr(usage, "prompt_tokens_details", None)
        if token_details:
            cached = getattr(token_details, "cached_tokens", 0)
            try:
                cached_tokens = int(cached) if cached else 0
            except (TypeError, ValueError):
                cached_tokens = 0

        # 获取缓存创建信息
        cache_creation_tokens = 0
        cache_creation = getattr(usage, "cache_creation", None)
        if cache_creation:
            created = getattr(
                cache_creation, "ephemeral_5m_input_tokens", None
            ) or getattr(cache_creation, "cache_creation_input_tokens", None)
            try:
                cache_creation_tokens = int(created) if created else 0
            except (TypeError, ValueError):
                cache_creation_tokens = 0

        # 构建包含缓存信息的日志
        cache_info = ""
        if cached_tokens > 0:
            cache_info += f" | CachedHit: {cached_tokens}"
        if cache_creation_tokens > 0:
            cache_info += f" | CacheCreated: {cache_creation_tokens}"

        logger.info(
            f"[LLM Usage] Type: {call_type} | Model: {model_name} | Prompt: {p} | Completion: {c} | Total: {p + c}{cache_info}"
        )

    def one_chat(
        self, model_config, messages, timeout=60, call_type: str = "state_update", raise_errors=False
    ):
        """单次对话，带超时和重试

        call_type: 用于区分调用来源，默认 state_update（非对话类调用）
        """
        client = (
            self.large_client
            if model_config == settings.LARGE_LLM
            else self.small_client
        )

        # Memory JSON is non-streaming and may legitimately take longer than a
        # reply's first token. Its durable worker owns retry/backoff.
        max_retries = 1 if call_type == 'memory_encode' else 2
        for attempt in range(max_retries):
            ticket = reserve('chat', model_config.name, call_type, provider=self._provider(model_config))
            try:
                request = client.chat.completions.create
                duration = min(timeout, float(os.getenv('EMBER_MEMORY_REQUEST_TIMEOUT', '60')
                        if call_type == 'memory_encode' else os.getenv('EMBER_REQUEST_TIMEOUT', '60')))
                if call_type == 'memory_encode':
                    request = lambda **kwargs: self._memory_request(client.chat.completions.create, **kwargs)
                response = request(
                    model=model_config.name,
                    messages=messages,
                    extra_body={"enable_thinking": False},
                    stream=False,
                    temperature=settings.LLM_TEMPERATURE,
                    **({'max_tokens': max(128, int(os.getenv('EMBER_MEMORY_OUTPUT_TOKENS', '2048')))} if call_type == 'memory_encode' else {}),
                    timeout=duration if call_type == 'memory_encode' else self._timeouts(duration),
                )
                choice = response.choices[0]
                full_response = choice.message.content
                usage = getattr(response, "usage", None) or getattr(
                    response, "usage_metadata", None
                )
                self._log_usage(usage, model_config.name, call_type)
                if call_type == 'memory_encode' and choice.finish_reason == 'length':
                    finish(ticket, usage, status='incomplete', error_code='output_limit')
                    ticket = None
                    raise ModelCallError('output_limit')
                finish(ticket, usage)
                return remove_reasoning(full_response)
            except Exception as e:
                code, retry_at = classify(e)
                finish(ticket, status='not_sent' if code == 'background_busy' else 'failed', error_code=code)
                logger.warning('Model request failed type=%s code=%s attempt=%s/%s causes=%s', call_type, code, attempt + 1, max_retries, cause_types(e))
                if attempt == max_retries - 1:
                    if raise_errors:
                        raise ModelCallError(code, retry_at) from None
                    return None
        return None

    def stream_chat(self, model_config, messages, call_type: str = "dialogue", cancel_token=None):
        """流式对话

        call_type: 用于区分调用来源，默认 dialogue（对话类调用）
        """
        client = (
            self.large_client
            if model_config == settings.LARGE_LLM
            else self.small_client
        )

        response = None
        ticket = reserve('chat', model_config.name, call_type, provider=self._provider(model_config))
        outcome = 'failed'
        usage = None
        try:
            if cancel_token:
                cancel_token.check()
            response = client.chat.completions.create(
                model=model_config.name,
                messages=messages,
                extra_body={"enable_thinking": False},
                stream=True,
                stream_options={"include_usage": True},
                temperature=settings.LLM_TEMPERATURE,
                timeout=self._timeouts(float(os.getenv('EMBER_REQUEST_TIMEOUT', '60'))),
            )
            if cancel_token and hasattr(response, 'close'):
                cancel_token.attach_stream(response.close)

            last_usage = None
            public_content = ReasoningFilter()
            for chunk in response:
                if cancel_token:
                    cancel_token.check()
                if getattr(chunk, "usage", None):
                    last_usage = chunk.usage

                # include_usage=True 时最后一个 chunk choices 为空列表，跳过
                if not chunk.choices:
                    continue

                # reasoning_content/reasoning are private provider channels.
                # Some gateways ignore enable_thinking=False or put <think>
                # blocks in content; filter before any downstream consumer.
                content = getattr(chunk.choices[0].delta, 'content', None)
                if isinstance(content, str):
                    visible = public_content.feed(content)
                    if visible:
                        yield visible

            if cancel_token:
                cancel_token.check()
            usage = (
                last_usage
                or getattr(response, "usage", None)
                or getattr(response, "usage_metadata", None)
            )
            self._log_usage(usage, model_config.name, call_type)
            outcome = 'completed'

        except TurnCancelled:
            raise
        except Exception as e:
            code, _ = classify(e)
            if cancel_token:
                cancel_token.check()
                raise
            yield f"[Error]: {code}"
            logger.warning('Streaming request failed code=%s', code)
        finally:
            finish(ticket, usage, outcome, error_code=locals().get('code', ''))
            if cancel_token:
                cancel_token.detach_stream()
            if response is not None and hasattr(response, 'close'):
                response.close()

    def get_embedding(self, model_config, text: str, raise_errors=False):
        client = self.embedding_client
        ticket = None
        try:
            ticket = reserve('embedding', model_config.name, 'embedding', provider=self._provider(model_config))
            response = client.embeddings.create(
                model=model_config.name,
                input=text,
                dimensions=1536,
                timeout=self._timeouts(float(os.getenv('EMBER_REQUEST_TIMEOUT', '60'))),
            )
            finish(ticket, getattr(response, 'usage', None))
            return response.data[0].embedding
        except Exception as e:
            code, retry_at = classify(e)
            finish(ticket, status='failed', error_code=code)
            logger.warning('Embedding request failed code=%s', code)
            if raise_errors:
                if type(e).__name__ == 'BudgetExceeded':
                    raise
                raise ModelCallError(code, retry_at) from None
            return None
