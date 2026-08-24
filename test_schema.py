from pydantic import ValidationError

from schemas import IntentResult


try:
    result = IntentResult(
        intent="repair",
        product="耳机",
        missing_information="order_id",
    )

    print(result.model_dump())

except ValidationError as error:
    print(error)