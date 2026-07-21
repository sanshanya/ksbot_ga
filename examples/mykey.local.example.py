"""Editable GenericAgent model config; the Gate reuses native_oai_config by default."""

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
