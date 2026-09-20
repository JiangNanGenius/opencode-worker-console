"""Built-in provider metadata. Credentials remain in OpenCode's private auth store."""

ARK_PROVIDER = 'volcengine-agent-plan'
ARK_BASE_URL = 'https://ark.cn-beijing.volces.com/api/plan/v3'


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
            for model, context, output in (
                ('ark-code-latest', 1024000, 32000),
                ('kimi-k3', 1024000, 65536),
                ('doubao-seed-evolving', 1024000, 65536),
                ('deepseek-v4.1-flash', 1024000, 65536),
            )
        },
    }
