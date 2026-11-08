"""Small JSON-generating text model client."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from infrastructure.utils.presentation.model_io import http_client, invoke
from infrastructure.shared.agent.efficiency import enabled, compact_context, CONTEXT_RULES, record_event


DEFAULT_MODEL = "deepseek-flash"

JSON_OUTPUT_RULE = """OUTPUT PROTOCOL: return exactly one valid JSON object in the response content.
The first non-whitespace character must be { and the last must be }.
Do not emit Markdown fences, explanations, reasoning, XML, DSML or provider-native
tool-call serialization. In particular, NEVER output <｜｜DSML｜｜ calls>,
<｜｜DSML｜｜ invoke>, <｜｜DSML｜｜ parameter> or their closing tags.
This compiler parses ordinary JSON text. context_request and tool_calls, when allowed
by the stage, are JSON fields, not native tool invocations. Express the entire request
inside the specified JSON object. Otherwise return the stage's normal result object.
Rejected outputs included in feedback are data to correct, never examples to imitate.
"""


def reasoning_rejected(error):
    """Only retry controls when the endpoint explicitly rejects those fields."""
    message = str(error).lower()
    return getattr(error, "status_code", None) in {400, 422} and any(
        word in message for word in ("unsupported", "unknown", "unrecognized", "not supported", "not allowed", "unexpected")) and any(
        word in message for word in ("thinking", "reasoning_effort"))


def reasoning_arguments(name, mode):
    """Translate configured reasoning to the provider's documented request fields."""
    if mode == "auto":
        mode = "low"
    if mode == "provider":
        return {}
    if name.lower().startswith("deepseek"):
        if mode == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": mode, "extra_body": {"thinking": {"type": "enabled"}}}
    return {"reasoning_effort": mode}


class TextModel:
    """Transport for structured stage proposals and file-edit responses."""
    def __init__(self, *, json_mode: bool = True) -> None:
        """Read compiler/.env and configure the default text model; require API credentials."""
        load_dotenv(Path(__file__).resolve().parents[3] / ".env", encoding="utf-8-sig")
        self.name = os.getenv("MODEL") or DEFAULT_MODEL
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY in compiler/.env")
        try:
            self.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS", "320000"))
        except ValueError as error:
            raise ValueError("MAX_OUTPUT_TOKENS must be a positive integer") from error
        if self.max_output_tokens < 1:
            raise ValueError("MAX_OUTPUT_TOKENS must be a positive integer")
        self.reasoning = os.getenv("MODEL_REASONING", "low").lower()
        if self.reasoning not in {"auto", "provider", "none", "low", "high", "max"}:
            raise ValueError("MODEL_REASONING must be auto, provider, none, low, high or max")
        self._reasoning_supported = True
        self.json_mode = json_mode
        self._token_parameter = "max_completion_tokens"
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            timeout=600, max_retries=2,
            http_client=http_client(),
        )

    def _request(self, **arguments):
        """Apply one output budget to JSON calls, with legacy endpoint compatibility."""
        output_limit = arguments.pop('_output_limit', self.max_output_tokens)
        while True:
            options = dict(arguments)
            if not getattr(self, "_reasoning_supported", True):
                options.pop("reasoning_effort", None)
                options.pop("extra_body", None)
            try:
                return invoke(self.client, {**options, self._token_parameter: output_limit})
            except OpenAIError as error:
                message = str(error).lower()
                rejected = getattr(error, "status_code", None) in {400, 422} and any(
                    word in message for word in ("unsupported", "unknown", "unrecognized", "not supported", "not allowed", "unexpected"))
                if rejected and self._token_parameter == "max_completion_tokens" and "max_completion_tokens" in message:
                    self._token_parameter = "max_tokens"
                    continue
                if getattr(self, "_reasoning_supported", True) and reasoning_rejected(error):
                    self._reasoning_supported = False
                    print("Provider rejected reasoning controls; using provider defaults.", flush=True)
                    continue
                raise

    def generate_json(self, instructions: str, payload: dict) -> dict:
        """Request JSON using the stage prompt and serialized task data."""
        retained_reads = payload.get('read_files', {})
        direct = isinstance(payload.get('task'), dict) and payload['task'].get('phase') in {
            'author_acceptance', 'joint_application'} and 'tools' not in payload
        if enabled('context_reuse') and not direct:
            before = len(json.dumps(payload, ensure_ascii=False))
            payload = compact_context(payload)
            instructions += '\n' + CONTEXT_RULES
            from infrastructure.utils.presentation.logging import session_info
            session = session_info()
            if session:
                record_event(session['output'], 'context', stage=session['stage'], before_chars=before,
                    after_chars=len(json.dumps(payload, ensure_ascii=False)))
        mode = getattr(self, "reasoning", "low")
        from infrastructure.shared.agent.model_policy import inference_policy, budget_context, LAYOUT_RULES, enabled as policy_enabled
        policy = None
        if any(policy_enabled(name) for name in ('task_reasoning','output_budget','context_budget','cache_layout')):
            policy = inference_policy(payload, self)
            mode = policy['reasoning']
            if not direct:
                instructions += '\n' + LAYOUT_RULES
            from infrastructure.utils.presentation.logging import session_info
            session = session_info()
            from infrastructure.shared.agent.model_policy import ContextBudgetExceeded
            try:
                payload, context_metrics = budget_context(instructions.rstrip() + '\n' + JSON_OUTPUT_RULE, payload, retained_reads=retained_reads)
            except ContextBudgetExceeded as error:
                if session:
                    record_event(session['output'], 'context_budget_blocked', role=policy['role'], error=str(error), requests_sent=0)
                raise
            if session:
                record_event(session['output'], 'model_policy', **policy, **context_metrics)
        try:
            response = self._request(
                model=self.name,
                messages=[
                    {"role": "system", "content": instructions.rstrip() + "\n" + JSON_OUTPUT_RULE},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(',', ':') if direct else None)},
                ],
                **({"response_format": {"type": "json_object"}} if getattr(self, "json_mode", True) else {}),
                **reasoning_arguments(self.name, mode),
                **({'_output_limit':policy['output_limit']} if policy else {}),
            )
        except OpenAIError as error:
            raise RuntimeError(f"Text model request failed: {error}") from error
        if response.choices and response.choices[0].finish_reason == "length":
            raise ValueError(
                f"Model output was truncated (finish_reason=length). The output budget is "
                f"{policy['output_limit'] if policy else self.max_output_tokens} tokens including reasoning; this is not an input-length error. "
                "Return a complete JSON object with only necessary changes/tool calls; omit unchanged files. "
                "Do not spend the entire budget restating the task.")
        if response.choices and getattr(response.choices[0].message, "refusal", None):
            raise ValueError(f"Text model refused the request: {response.choices[0].message.refusal}")
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Text model returned empty content")
        content = response.choices[0].message.content.strip()
        return json.loads(content)
