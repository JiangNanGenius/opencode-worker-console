"""Built-in provider metadata. Credentials remain in OpenCode's private auth store."""

ARK_PROVIDER = 'volcengine-agent-plan'
ARK_BASE_URL = 'https://ark.cn-beijing.volces.com/api/plan/v3'


def ark_provider():
    """Responses API; the configurable Auto alias is not a guaranteed 1M model.

    The provider's console controls what ark-code-latest resolves to. A named K3
    request guarantees the model and supports the documented 1,024,000 window.
    Maximum reasoning was verified against both endpoints, not inferred from
    similarly named models on a different provider.
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
                ('ark-code-latest', 256000, 32000),
                ('kimi-k3', 1024000, 65536),
            )
        },
    }
