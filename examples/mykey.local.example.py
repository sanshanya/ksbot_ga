"""Minimal GenericAgent model config for a local OpenAI-compatible endpoint.

Copy this file to vendor/GenericAgent/mykey.py and adjust the endpoint/model:

    Copy-Item examples\mykey.local.example.py vendor\GenericAgent\mykey.py

For a local endpoint that ignores authentication, use apikey="EMPTY".
The Gate also reads this config by default (GA_GATE_CONFIG_KEY=native_oai_config),
so you do not need to duplicate model settings in .env unless the Gate should use
a different endpoint.
"""

native_oai_config = {
    "name": "local-glm-5.2",
    "apikey": "EMPTY",
    "apibase": "http://127.0.0.1:30000/v1",
    "model": "GLM-5.2",
    "api_mode": "chat_completions",
    "max_retries": 3,
    "connect_timeout": 10,
    "read_timeout": 300,
}
