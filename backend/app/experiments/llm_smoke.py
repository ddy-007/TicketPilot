"""Real-model capability probes using synthetic data only."""

import argparse
import json
import sys
import time
from typing import Literal
from urllib.parse import urlparse

from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import ConfigurationError, Settings, get_settings


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: Literal["m0"]


class SmokeDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["QUEUE_PUBLISH_TIMEOUT"]
    summary: str = Field(min_length=1, max_length=200)


class ProbeError(ValueError):
    pass


def client(settings: Settings) -> OpenAI:
    settings.require_llm()
    return OpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
    )


def is_deepseek_api(settings: Settings) -> bool:
    return urlparse(settings.llm_base_url).hostname == "api.deepseek.com"


def completion_options(settings: Settings) -> dict:
    # Verified on the configured API. These probes exercise non-thinking tools.
    if is_deepseek_api(settings):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def metadata(response) -> dict:
    usage = response.usage
    return {
        "model": response.model,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
        if usage
        else None,
        "usage_unknown": usage is None,
    }


def completion(llm, settings, calls, **kwargs):
    entry = {"state": "IN_FLIGHT", "model": None, "usage": None, "usage_unknown": True}
    calls.append(entry)
    started = time.monotonic()
    try:
        response = llm.chat.completions.create(**kwargs)
        entry.update(metadata(response), state="COMPLETED")
        # Provider model identifiers are free text; do not echo configured secrets.
        for secret in (settings.llm_api_key.get_secret_value(), settings.llm_base_url):
            if secret and isinstance(entry["model"], str):
                entry["model"] = entry["model"].replace(secret, "[REDACTED]")
        return response
    except BaseException:
        entry["state"] = "FAILED"
        raise
    finally:
        entry["duration_ms"] = round((time.monotonic() - started) * 1000)


def require_ok(response) -> None:
    require_complete(response)
    message = response.choices[0].message
    if message.refusal or message.tool_calls or (message.content or "").strip() != "OK":
        raise ProbeError("Expected a final OK response.")


def require_complete(response, expected_finish: str = "stop") -> None:
    if not response.choices or response.choices[0].finish_reason != expected_finish:
        raise ProbeError("Incomplete or unexpected model response.")


def run_model_smoke(*, calls=None) -> dict:
    calls = [] if calls is None else calls
    settings = get_settings()
    with client(settings) as llm:
        response = completion(
            llm,
            settings,
            calls,
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=128,
            **completion_options(settings),
        )
    require_ok(response)
    return {
        "kind": "text",
        "status": "passed",
        **{key: calls[-1][key] for key in ("model", "usage", "usage_unknown")},
        "calls": calls,
    }


def run_tool_smoke(*, calls=None) -> dict:
    calls = [] if calls is None else calls
    settings = get_settings()
    function = {
        "name": "get_demo_status",
        "description": "Read synthetic probe status. This tool has no write effects.",
        "parameters": ToolArguments.model_json_schema(),
    }
    tool_options = {}
    if not is_deepseek_api(settings):
        function["strict"] = True
        tool_options["parallel_tool_calls"] = False
    messages = [{"role": "user", "content": "Read demo probe m0 using get_demo_status."}]
    with client(settings) as llm:
        response = completion(
            llm,
            settings,
            calls,
            model=settings.llm_model,
            messages=messages,
            tools=[{"type": "function", "function": function}],
            tool_choice={"type": "function", "function": {"name": "get_demo_status"}},
            max_tokens=128,
            **completion_options(settings),
            **tool_options,
        )
        message = response.choices[0].message
        require_complete(response, "tool_calls")
        tool_calls = message.tool_calls or []
        if len(tool_calls) != 1 or tool_calls[0].function.name != "get_demo_status":
            raise ProbeError("Expected exactly one registered read-only tool.")
        args = ToolArguments.model_validate_json(tool_calls[0].function.arguments)
        tool_result = {"probe_id": args.probe_id, "status": "OK"}
        messages.append(
            message.model_dump(include={"role", "content", "tool_calls"}, exclude_none=True)
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_calls[0].id,
                "content": json.dumps(tool_result),
            }
        )
        messages.append({"role": "user", "content": "If the probe status is OK, reply only OK."})
        final = completion(
            llm,
            settings,
            calls,
            model=settings.llm_model,
            messages=messages,
            max_tokens=128,
            **completion_options(settings),
        )
    require_ok(final)
    return {
        "kind": "tool",
        "status": "passed",
        "tool": "get_demo_status",
        "calls": calls,
    }


def run_structured_smoke(*, calls=None) -> dict:
    calls = [] if calls is None else calls
    settings = get_settings()
    schema = SmokeDiagnosis.model_json_schema()
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "smoke_diagnosis", "strict": True, "schema": schema},
    }
    mode = "json_schema+pydantic"
    if is_deepseek_api(settings):
        response_format = {"type": "json_object"}
        mode = "json_object+pydantic"
    with client(settings) as llm:
        response = completion(
            llm,
            settings,
            calls,
            model=settings.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "Return only a JSON object conforming to this schema: "
                    + json.dumps(schema),
                },
                {
                    "role": "user",
                    "content": "Synthetic test: queue publishing timed out. Return the code "
                    "QUEUE_PUBLISH_TIMEOUT and a short summary.",
                },
            ],
            response_format=response_format,
            max_tokens=256,
            **completion_options(settings),
        )
    require_complete(response)
    message = response.choices[0].message
    if message.refusal:
        raise ProbeError("Model refused structured output.")
    SmokeDiagnosis.model_validate_json(message.content or "")
    return {
        "kind": "structured",
        "status": "passed",
        "validation_mode": mode,
        **{key: calls[-1][key] for key in ("model", "usage", "usage_unknown")},
        "calls": calls,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="M0 real model probes; synthetic data only")
    parser.add_argument("--kind", choices=("text", "tool", "structured", "all"), default="all")
    args = parser.parse_args()
    runners = {
        "text": run_model_smoke,
        "tool": run_tool_smoke,
        "structured": run_structured_smoke,
    }
    failed = False
    for name in runners if args.kind == "all" else [args.kind]:
        started = time.monotonic()
        calls = []
        try:
            result = runners[name](calls=calls)
        except ConfigurationError as exc:
            result = {"error": "CONFIGURATION_ERROR", "message": str(exc)}
        except AuthenticationError:
            result = {"error": "LLM_AUTH_ERROR"}
        except APITimeoutError:
            result = {"error": "LLM_TIMEOUT", "usage_unknown": True}
        except APIConnectionError:
            result = {"error": "LLM_CONNECTION_ERROR", "usage_unknown": True}
        except APIError as exc:
            result = {"error": "LLM_API_ERROR", "status_code": getattr(exc, "status_code", None)}
        except (ProbeError, ValidationError, IndexError, json.JSONDecodeError):
            result = {"error": "LLM_PROTOCOL_ERROR"}
        result.update(
            kind=name,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            calls=calls,
            model_call_count=len(calls),
            known_total_tokens=sum((call["usage"] or {}).get("total_tokens", 0) for call in calls),
        )
        if calls:
            result["usage_unknown"] = any(call["usage_unknown"] for call in calls)
        print(json.dumps(result, ensure_ascii=False))
        failed |= "error" in result
        if result.get("error") == "CONFIGURATION_ERROR":
            break
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
