"""Thin Claude wrapper: JSON-schema output, a hard per-company call budget, and call logging."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class CallBudget:
    """Counts calls against a cap. spend() raises before the cap is crossed, never after."""
    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def spend(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExceeded(f"budget of {self.limit} exhausted")
        self.used += n


class JsonLLM(Protocol):
    def call_json(self, purpose: str, system: str, user: str, schema: dict,
                  budget: CallBudget, max_tokens: int = 4000) -> dict: ...


@dataclass
class ClaudeJSON:
    model: str
    effort: dict
    use_fallback: bool = True
    on_call: Callable[[dict], None] | None = None   # logging hook (store.log_llm_call)
    total_calls: int = 0
    _client: object = field(default=None, repr=False)

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def call_json(self, purpose, system, user, schema, budget, max_tokens=4000):
        budget.spend()                     # counted even if the call then fails
        self.total_calls += 1
        import anthropic
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort.get(purpose, "low"),
                           "format": {"type": "json_schema", "schema": schema}},
        )
        log = {"purpose": purpose, "model": self.model, "ok": False}
        try:
            client = self._get_client()
            if self.use_fallback:
                resp = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
            log.update(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
            if resp.stop_reason == "refusal":
                raise LLMError(f"{purpose}: model declined")
            if resp.stop_reason == "max_tokens":
                raise LLMError(f"{purpose}: output truncated")
            text = next(b.text for b in resp.content if b.type == "text")
            data = json.loads(text)
            log["ok"] = True
            return data
        except (anthropic.APIError, StopIteration, json.JSONDecodeError, TypeError) as e:
            # TypeError: the SDK raises it when no credential can be resolved
            raise LLMError(f"{purpose}: {type(e).__name__}: {e}") from e
        finally:
            if self.on_call:
                self.on_call(log)


def _retry_delay_seconds(err) -> float | None:
    """RetryInfo.retryDelay from a Gemini 429, e.g. "5755s" -> 5755.0."""
    details = ((getattr(err, "details", None) or {}).get("error") or {}).get("details") or []
    for d in details:
        if "RetryInfo" in d.get("@type", ""):
            try:
                return float(str(d.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                return None
    return None


@dataclass
class GeminiJSON:
    """Google Gemini with JSON-schema output. Tries `models` in order.
    - 503/500/504 or a dropped connection: pause briefly, then the next model.
    - 429 with a long retry delay (a daily quota): that model is skipped for the rest of the run.
    - 429 with a short delay (per-minute limit): wait once and retry the same model.
    Calls are spaced by `min_interval_seconds`. One logical call spends one budget unit."""
    models: list[str]
    rate_limit_wait_seconds: float = 20.0
    min_interval_seconds: float = 5.0
    overload_pause_seconds: float = 5.0
    request_timeout_seconds: int = 90
    on_call: Callable[[dict], None] | None = None
    total_calls: int = 0
    sleep: Callable[[float], None] = field(default=None, repr=False)
    clock: Callable[[], float] = field(default=None, repr=False)
    exhausted: set = field(default_factory=set)
    _client: object = field(default=None, repr=False)
    _last: float | None = field(default=None, repr=False)

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            # SDK retries off: fallback and backoff are handled here, so attempts stay visible.
            # A timeout (milliseconds) stops one hung request from stalling a company for 15 minutes,
            # as seen on 2026-11-17; the timeout surfaces as httpx.TimeoutException -> next model.
            self._client = genai.Client(http_options=types.HttpOptions(
                timeout=self.request_timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=1)))
        return self._client

    def _pace(self, sleep, clock):
        if self._last is not None:
            gap = self.min_interval_seconds - (clock() - self._last)
            if gap > 0:
                sleep(gap)
        self._last = clock()

    def call_json(self, purpose, system, user, schema, budget, max_tokens=4000):
        budget.spend()
        self.total_calls += 1
        import time

        import httpx
        from google.genai import errors, types
        sleep, clock = self.sleep or time.sleep, self.clock or time.monotonic
        config = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_json_schema=schema, max_output_tokens=max(max_tokens, 8192))
        attempts, log = [], {"purpose": purpose, "model": self.models[0], "ok": False}
        try:
            client = self._get_client()
            for model in self.models:
                if model in self.exhausted:
                    attempts.append(f"{model}:skipped_daily_quota")
                    continue
                for retry in range(2):
                    self._pace(sleep, clock)
                    try:
                        resp = client.models.generate_content(model=model, contents=user, config=config)
                    except httpx.HTTPError as e:
                        attempts.append(f"{model}:{type(e).__name__}")
                        sleep(self.overload_pause_seconds)
                        break
                    except errors.APIError as e:
                        attempts.append(f"{model}:{e.code}")
                        if e.code == 429:
                            delay = _retry_delay_seconds(e)
                            if delay is not None and delay > 60:
                                self.exhausted.add(model)       # daily quota: stop asking this model
                                break
                            if retry == 0:
                                sleep(min(delay or self.rate_limit_wait_seconds, 60))
                                continue
                            break
                        if e.code in (500, 503, 504):
                            sleep(self.overload_pause_seconds)
                            break
                        raise LLMError(f"{purpose}: {model} HTTP {e.code}: {e.message}") from e
                    log["model"] = model
                    um = resp.usage_metadata
                    log.update(input_tokens=getattr(um, "prompt_token_count", None),
                               output_tokens=getattr(um, "candidates_token_count", None))
                    reason = str(resp.candidates[0].finish_reason) if resp.candidates else "NO_CANDIDATES"
                    if "MAX_TOKENS" in reason:
                        raise LLMError(f"{purpose}: output truncated")
                    if not resp.text:
                        raise LLMError(f"{purpose}: empty response ({reason})")
                    data = json.loads(resp.text)
                    log["ok"] = True
                    return data
            raise LLMError(f"{purpose}: every model unavailable ({', '.join(attempts)})")
        except json.JSONDecodeError as e:
            raise LLMError(f"{purpose}: invalid JSON: {e}") from e
        except ValueError as e:                       # SDK raises this when no API key is set
            raise LLMError(f"{purpose}: {e}") from e
        finally:
            log["attempts"] = attempts
            if self.on_call:
                self.on_call(log)


def make_llm(llm_cfg: dict):
    """Build the client named by config `llm.provider`."""
    if llm_cfg.get("provider", "anthropic") == "gemini":
        return GeminiJSON(models=list(llm_cfg["gemini_models"]),
                          rate_limit_wait_seconds=llm_cfg.get("rate_limit_wait_seconds", 20),
                          min_interval_seconds=llm_cfg.get("min_interval_seconds", 5),
                          request_timeout_seconds=llm_cfg.get("request_timeout_seconds", 90))
    return ClaudeJSON(model=llm_cfg["model"], effort=llm_cfg["effort"],
                      use_fallback=llm_cfg.get("use_refusal_fallback", True))
