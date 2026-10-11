"""Provider routing/failover without credentials or external network calls."""
import json
from types import SimpleNamespace

import pytest

from hellabandz.agents import llm, reviews


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'QS_LLM_PROVIDER',
                'QS_GEMINI_MODEL', 'QS_OPENAI_MODEL', 'QS_OPENAI_FALLBACK', 'QS_GEMINI_TIMEOUT_S',
                'QS_GEMINI_COOLDOWN_S'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, '_gemini_retry_at', 0.)


@pytest.fixture
def client(monkeypatch):
    calls, queue = [], []
    import openai
    class Fake:
        def __init__(self, **settings):
            def create(**request):
                calls.append((settings, request))
                value = queue.pop(0)
                if isinstance(value, Exception):
                    raise value
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value))],
                                       model=request['model'], usage=None)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    monkeypatch.setattr(openai, 'OpenAI', Fake)
    return calls, queue


def keys(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'gemini-test')
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-test')


def test_gemini_default_alias_and_explicit_openai(monkeypatch, client):
    calls, queue = client
    monkeypatch.setenv('GOOGLE_API_KEY', 'google-test')
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-test')
    queue.extend(['{"ok":true}', '{}'])
    result, meta = llm.chat_json('s', 'u', timeout=20)
    assert result['ok'] and meta['provider'] == 'gemini'
    settings, request = calls[0]
    assert settings['api_key'] == 'google-test'
    assert settings['base_url'] == llm.GEMINI_BASE_URL
    assert settings['timeout'] == 8 and settings['max_retries'] == 0
    assert request['model'] == 'gemini-3.8-flash'
    assert request['response_format'] == {'type': 'json_object'}
    llm.chat_json('s', 'u', provider='openai')
    assert calls[1][0]['api_key'] == 'openai-test'
    assert calls[1][0]['base_url'] == 'https://api.openai.com/v1'


@pytest.mark.parametrize('failure', [TimeoutError('secret-payload'), ConnectionError('secret-payload'),
                                    RuntimeError('secret-payload'), '{malformed', '[]', ''])
def test_primary_failure_switches_to_openai_and_reports_actual_provider(monkeypatch, client, failure):
    calls, queue = client
    keys(monkeypatch)
    queue.extend([failure, '{"ok":true}'])
    result, meta = llm.chat_json('s', 'u', timeout=20)
    assert result['ok'] and meta['provider'] == 'openai'
    assert meta['model'] == 'gpt-4.1-mini' and meta['fallback_from'] == 'gemini'
    assert 'secret' not in json.dumps(meta)
    assert calls[1][0]['max_retries'] == 0
    assert 0 < calls[1][0]['timeout'] <= 20


def test_circuit_skips_primary_then_recovers(monkeypatch, client):
    calls, queue = client
    keys(monkeypatch)
    clock = [100.]
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock[0])
    queue.extend([TimeoutError(), '{}', '{}', '{}'])
    assert llm.chat_json('s', 'u')[1]['provider'] == 'openai'
    clock[0] += 1
    assert llm.chat_json('s', 'u')[1]['fallback_reason'] == 'gemini_circuit_open'
    assert len(calls) == 3 and calls[2][1]['model'] == 'gpt-4.1-mini'
    clock[0] += 60
    assert llm.chat_json('s', 'u')[1]['provider'] == 'gemini'
    assert llm._gemini_retry_at == 0


def test_remaining_deadline_and_short_call_reserve_backup(monkeypatch):
    keys(monkeypatch)
    clock, calls = [100.], []
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock[0])
    def call(system, user, temperature, timeout, retries, provider):
        calls.append((timeout, retries, provider))
        if provider == 'gemini':
            clock[0] += 2
            raise TimeoutError()
        return {}, {'provider': provider, 'model': 'test'}
    monkeypatch.setattr(llm, '_call_json', call)
    _, meta = llm.chat_json('s', 'u', timeout=5)
    assert calls == [(2.5, 0, 'gemini'), (3., 0, 'openai')]
    assert meta['latency_s'] == 2


def test_deadline_exhaustion_never_starts_backup(monkeypatch):
    keys(monkeypatch)
    clock, calls = [100.], []
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock[0])
    def call(*args):
        calls.append(args)
        clock[0] += 5
        raise TimeoutError()
    monkeypatch.setattr(llm, '_call_json', call)
    with pytest.raises(TimeoutError, match='deadline exhausted'):
        llm.chat_json('s', 'u', timeout=5)
    assert len(calls) == 1


def test_no_backup_key_or_disabled_backup_never_switches(monkeypatch, client):
    calls, queue = client
    monkeypatch.setenv('GEMINI_API_KEY', 'gemini-test')
    queue.append(TimeoutError('private-value'))
    with pytest.raises(RuntimeError, match='unconfigured') as error:
        llm.chat_json('s', 'u')
    assert 'private-value' not in str(error.value) and len(calls) == 1
    monkeypatch.setattr(llm, '_gemini_retry_at', 0.)
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-test')
    monkeypatch.setenv('QS_OPENAI_FALLBACK', '0')
    queue.append(TimeoutError())
    with pytest.raises(RuntimeError, match='disabled'):
        llm.chat_json('s', 'u')
    assert len(calls) == 2


def test_offline_selection_and_unconfigured_primary_can_use_authorized_backup(monkeypatch, client):
    calls, queue = client
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-test')
    assert llm.available()
    queue.append('{}')
    assert llm.chat_json('s', 'u')[1]['provider'] == 'openai'
    assert len(calls) == 1
    monkeypatch.setenv('QS_LLM_PROVIDER', 'offline')
    assert not llm.available()
    with pytest.raises(ValueError, match='offline'):
        llm.chat_json('s', 'u')


def test_review_auto_uses_gemini_and_preserves_fallback_label(monkeypatch):
    keys(monkeypatch)
    selected = []
    def fake(system, user, **kwargs):
        selected.append(kwargs['provider'])
        rows = json.loads(user)
        return {'reviews': [{'review_id': r['review_id'], 'sentiment': .2, 'themes': ['quality']} for r in rows]}, {
            'provider': 'openai', 'model': 'backup-test', 'fallback_from': 'gemini'}
    monkeypatch.setattr(llm, 'chat_json', fake)
    data = reviews.example()
    data['provider'] = 'auto'
    body = reviews.ReviewRequest.model_validate({k: data[k] for k in ('reviews', 'as_of', 'provider')})
    result = reviews.analyze(body)
    assert selected == ['gemini'] and result['provider'] == 'openai' and result['model'] == 'backup-test'
