"""路由层模型注册表 —— 集中管理可用模型（fable5-lite）。

路由层只在本地的两个 DeepSeek 模型之间做选择：
- deepseek-v4-flash：常规任务，快速响应
- deepseek-v4-pro：复杂任务，更高准确性

两者的 API 密钥都来自环境变量 V4_API_KEY（DeepSeek 统一 API Key），
V4_API_KEY 是唯一需要的 API 密钥。

⚠️ 关键约定：每条目的 "id" 必须是 API 实际接受的模型名，会被**原样**放进
API 请求的 model 字段。不能用别称 / 简称（例如不要把
"deepseek-v4-pro" 写成 "deepseek-pro"），否则 API 会返回 HTTP 400
（invalid_request_error: unsupported model name）。
"""

AVAILABLE_MODELS = [
    {
        "id": "deepseek-v4-flash",
        "env_key": "V4_API_KEY",
        "description": "常规任务，快速响应"
    },
    {
        "id": "deepseek-v4-pro",
        "env_key": "V4_API_KEY",
        "description": "复杂任务，更高准确性"
    }
]
