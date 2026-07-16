"""Minimal GenericAgent model configuration.

Copy this file to vendor/GenericAgent/mykey.py and replace the placeholders.
For a local OpenAI-compatible endpoint that ignores authentication, use apikey="EMPTY".
"""

native_oai_config = {
    "name": "primary",
    "apikey": "REPLACE_ME",
    "apibase": "https://api.example.com/v1",
    "model": "REPLACE_ME",
    "api_mode": "chat_completions",
    "max_retries": 3,
    "connect_timeout": 10,
    "read_timeout": 300,
}
