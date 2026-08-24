import json
import os
from typing import TYPE_CHECKING

from schemas import IntentResult

if TYPE_CHECKING:
    from openai import OpenAI


SYSTEM_PROMPT = """
你是售后客服系统的意图识别器。

允许的意图：
- exchange：换货
- refund：退款
- logistics：查询物流
- complaint：投诉
- unknown：无法判断

请提取：
- intent
- request_kind：action 表示用户要执行查询、换货、退款或投诉流程；information 表示用户只咨询政策、条件、期限或状态含义
- product
- reason
- order_id
- missing_information

如果某个信息没有出现，使用 null。
如果换货或退款缺少订单号，将 "order_id" 放入 missing_information。
信息咨询不要求 order_id，且不得把咨询误判为业务操作。

示例：
- “我要换货” → exchange + action
- “换货期限多久” → exchange + information
- “我要退款” → refund + action
- “退款条件是什么” → refund + information
- “查询 A1001 的物流” → logistics + action
- “已签收是什么意思” → logistics + information
- “我要投诉” → complaint + action
- “投诉怎么处理” → complaint + information

只输出合法 JSON，不要输出解释。

示例 JSON：
{
  "intent": "exchange",
  "product": "耳机",
  "reason": "左边没有声音",
  "order_id": null,
  "missing_information": ["order_id"]
}
"""


def create_client() -> "OpenAI":
    """Create an LLM client only when intent interpretation is requested."""
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY is not configured; set it before calling interpret_intent()."
        )

    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError(
            "The OpenAI SDK is not installed; install the project requirements first."
        ) from error

    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    )


def parse_intent_response(content: str) -> IntentResult:
    """Parse a JSON model response and validate it against the intent schema."""
    if not content:
        raise RuntimeError("模型返回了空内容")

    data = json.loads(content)
    return IntentResult.model_validate(data)


def interpret_intent(user_message: str) -> IntentResult:
    client = create_client()
    response = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        max_tokens=300,
        extra_body={"thinking": {"type": "disabled"}},
    )

    content = response.choices[0].message.content
    return parse_intent_response(content)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=False)
    message = input("用户：")
    result = interpret_intent(message)
    print(result.model_dump_json(indent=2))
