"""Built-in provider metadata. Credentials remain in OpenCode's private auth store."""
import re

ARK_PROVIDER = 'volcengine-agent-plan'
ARK_BASE_URL = 'https://ark.cn-beijing.volces.com/api/plan/v3'
# Alibaba Cloud Model Studio Token Plan, team edition. The Anthropic-compatible
# base URL is the one Alibaba documents for OpenCode; the OpenAI-compatible base
# is intentionally not used so one endpoint and one accepted reasoning parameter
# are verified instead of implied.
BAILIAN_PROVIDER = 'bailian-token-plan'
BAILIAN_BASE_URL = 'https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic/v1'
# Highest reasoning effort Alibaba documents for the fixed Qwen3.8 models in
# OpenCode. There is no separate "max" API value: the official example sets
# effort=xhigh directly, so a profile variant named "max" keeps its label while
# mapping to the same xhigh request option.
BAILIAN_REASONING_EFFORT = 'xhigh'
# Providers whose runtime credential is a key-file reference from
# ``provider_api_key_files`` rather than the OpenCode auth store. A new
# key-file provider must be added here and declared in server.runtime_overlay so
# credential provenance and the data plane stay aligned.
KEY_FILE_PROVIDERS = (BAILIAN_PROVIDER,)


def ark_provider():
    """Responses API with a long-context client ceiling for the Auto alias.

    The provider's console controls what ark-code-latest resolves to. Agent Plan
    Auto accepted input beyond the 256k OpenCode example in a live check, so the
    client ceiling is 1,024,000 to avoid premature local compaction. A named K3
    request guarantees the documented 1,024,000-capable model. The evolving and
    DeepSeek copies are documented at the same 1,024,000 context and 65,536
    output with text+image input and maximum reasoning, following the same Ark
    convention. Maximum reasoning was verified against the Auto and K3
    endpoints, not inferred from similarly named models on a different provider.
    """
    return {
        'name': 'Volcengine Agent Plan', 'npm': '@ai-sdk/openai',
        'options': {'baseURL': ARK_BASE_URL},
        'models': {
            model: {
                'name': model, 'reasoning': True, 'tool_call': True,
                'modalities': {'input': ['text', 'image'], 'output': ['text']},
                'limit': {'context': context, 'output': output},
                'variants': {'max': {'reasoningEffort': 'max'}},
            }
            # The native/Ark Kimi K3 entries are retired: no runtime catalog is
            # declared for them even when the Ark Auto route is enabled. Their
            # historical usage, pricing evidence and task records are retained.
            for model, context, output in (
                ('ark-code-latest', 1024000, 32000),
                ('doubao-seed-evolving', 1024000, 65536),
                ('deepseek-v4.1-flash', 1024000, 65536),
            )
        },
    }


def valid_key_file(value):
    """True for a plain absolute credential-file path usable in {file:...}.

    The bridge never reads the secret; this only keeps a malformed or
    substitution-like value out of the private runtime overlay.
    """
    if not isinstance(value, str) or not value or len(value) > 512 or not value.startswith('/'):
        return False
    if any(token in value for token in ('{', '}', '\n', '\r', '\x00', '://')):
        return False
    return bool(re.fullmatch(r'/[^\s{}]+', value))


def valid_loopback_base_url(value):
    """True only for the local pool's plain loopback base URL.

    The pool proxy owns the path to the fixed upstream endpoint. Keeping the
    provider base URL itself restricted to ``http://127.0.0.1:<port>`` means a
    private setting cannot turn the runtime into a caller-controlled proxy.
    """
    if not isinstance(value, str) or len(value) > 64:
        return False
    match = re.fullmatch(r'http://127\.0\.0\.1:([0-9]{1,5})', value)
    if not match:
        return False
    return 1 <= int(match.group(1)) <= 65535


def bailian_provider(api_key_file=None, base_url=None):
    """Anthropic-compatible Token Plan provider with the two fixed Qwen3.8 models.

    Only fixed models are declared; the opaque ``auto`` alias is deliberately
    absent so automatic routing can never select an unknown-cost model. The
    documented team-plan ceiling (983,616 context in thinking mode, 131,072
    output) is used, and the highest documented reasoning effort is applied both
    as the default option and as the named ``max`` variant. No secret value is
    embedded: when supplied, the key stays in an owner-only file referenced by
    OpenCode's own ``{file:...}`` substitution.

    ``base_url`` is only accepted for the local credential-pool listener. The
    disabled pool and every caller that does not pass it keep the fixed
    Alibaba endpoint exactly as before. A caller that supplies an invalid pool
    URL fails closed rather than falling back to Alibaba with the local server
    password as the provider key.
    """
    if base_url is not None and not valid_loopback_base_url(base_url):
        raise ValueError('bailian provider base_url must be a loopback pool URL')
    options = {'baseURL': base_url if base_url is not None else BAILIAN_BASE_URL}
    if valid_key_file(api_key_file):
        options['apiKey'] = '{file:' + api_key_file + '}'
    return {
        'name': 'Alibaba Cloud Model Studio', 'npm': '@ai-sdk/anthropic',
        'options': options,
        'models': {
            model: {
                'name': name, 'reasoning': True, 'tool_call': True,
                'modalities': {'input': ['text', 'image'], 'output': ['text']},
                'limit': {'context': 983616, 'output': 131072},
                'options': {'effort': BAILIAN_REASONING_EFFORT},
                'variants': {'max': {'effort': BAILIAN_REASONING_EFFORT}},
            }
            for model, name in (
                ('qwen3.8-max', 'Qwen3.8 Max'),
                ('qwen3.8-flash', 'Qwen3.8 Flash'),
            )
        },
    }
