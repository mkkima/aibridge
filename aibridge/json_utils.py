import json
import re

async def parse_or_repair_json(raw_text: str, bridge_function=None) -> dict:
    """
    Attempts to parse JSON from raw_text.
    If it fails, it tries to extract JSON block from markdown.
    If that fails and bridge_function is provided, asks LLM to repair it.
    """
    # Attempt 1: Direct parse
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        last_error = str(e)

    # Attempt 2: Extract from markdown block
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            last_error = str(e)

    # Attempt 3: Naive extraction of first { to last }
    start = raw_text.find('{')
    end = raw_text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw_text[start:end+1])
        except json.JSONDecodeError as e:
            last_error = str(e)

    # Attempt 4: LLM Repair
    if bridge_function:
        from .debate_prompts import REPAIR_JSON_PROMPT
        repair_prompt = REPAIR_JSON_PROMPT.format(raw_text=raw_text, error_msg=last_error)
        try:
            print("[JSON Repair] Пробуем восстановить JSON через LLM...")
            repaired_text = await bridge_function(repair_prompt, "chatgpt")
            # Clear markdown from repaired text if any
            repaired_text = repaired_text.strip()
            if repaired_text.startswith("```json"):
                repaired_text = repaired_text[7:]
            if repaired_text.startswith("```"):
                repaired_text = repaired_text[3:]
            if repaired_text.endswith("```"):
                repaired_text = repaired_text[:-3]
            return json.loads(repaired_text.strip())
        except Exception as e:
            raise Exception(f"Failed to repair JSON: {e}")

    raise ValueError(f"Could not parse valid JSON from text: {last_error}")
