"""Guardrails as LangChain agent middleware.

    InputGuardrail     before the coordinator runs: blocks card numbers, PINs, passwords and
                       security codes typed into the chat (removing them from the conversation
                       before any model, store or trace sees them), masks contact details, then
                       asks Bedrock (if configured) about prompt attacks, harmful content and
                       denied topics.
    OutputGuardrail    after the coordinator answers: masks card/IBAN/account numbers and internal
                       ids, then asks Bedrock about harmful content, denied topics and whether the
                       answer is grounded in what the subagents reported.
    ContactMasking     on the service subagent's tool results: the model sees r***@example.com,
                       not the full email or phone number (it never needs them).
    Call limits        stop a runaway loop of model or tool calls (a cost guardrail).

Every check is recorded on the turn's cost meter, so the chat and the ledger show what fired.
"""

from __future__ import annotations

import asyncio

from langchain.agents.middleware import (AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware,
                                         hook_config)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from guardrails import pii
from guardrails.bedrock import BedrockGuard
from observability import costs

BLOCKED_SECRET_REPLY = (
    "For your security, please don't type card numbers, PINs, passwords or security codes into this "
    "chat. I've removed it from our conversation and haven't acted on that message. Please send your "
    "request again without it. If you're asked for a security code, enter it at the separate "
    "\"Security code\" prompt, not in the chat.")

BEDROCK_INPUT_REPLIES = {
    "PROMPT_ATTACK": "I can't help with that. I can help with your balances, transactions, transfers "
                     "between your accounts, your address and service requests.",
    "topic:Investment advice": "I can't give investment advice. I can show you your balances and "
                               "spending, or move money between your current and savings accounts.",
    "topic:Tax and legal advice": "I can't give tax or legal advice. For that, please speak to a "
                                  "qualified adviser. I can help with your accounts and transactions.",
}
BEDROCK_DEFAULT_REPLY = "Sorry, I can't help with that request. I can help with your accounts and transactions."
BEDROCK_OUTPUT_FALLBACK = "Sorry, I can't share that response. Please ask me another way or contact the bank."


def _text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


def _last(messages, cls):
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], cls):
            return i, messages[i]
    return None, None


def _input_reply(findings: list[str]) -> str:
    for f in findings:
        if f.startswith("PROMPT_ATTACK"):
            return BEDROCK_INPUT_REPLIES["PROMPT_ATTACK"]
        if f in BEDROCK_INPUT_REPLIES:
            return BEDROCK_INPUT_REPLIES[f]
    return BEDROCK_DEFAULT_REPLY


def _record(layer: str, source: str, mode: str, findings: list[str], applied: bool, **extra) -> None:
    meter = costs.current()
    if meter is not None:
        meter.add_guard(layer, source, mode, findings, applied, **extra)


class InputGuardrail(AgentMiddleware):
    def __init__(self, bedrock: BedrockGuard | None = None):
        super().__init__()
        self.bedrock = bedrock

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state, runtime):
        messages = state.get("messages", [])
        idx, human = _last(messages, HumanMessage)
        if human is None:
            return None
        original = _text(human)

        # 1. Rules: secrets are removed from the conversation and the turn stops here.
        cleaned, blocked = pii.redact(original, pii.INPUT_BLOCK)
        cleaned, masked = pii.redact(cleaned, pii.INPUT_MASK)
        updates = []
        if blocked or masked:
            updates.append(HumanMessage(content=cleaned, id=human.id))  # same id: replaces it in place
            _record("rules", "INPUT", "enforce", sorted({m.kind for m in blocked + masked}), True)
        if blocked:
            return {"messages": updates + [AIMessage(content=BLOCKED_SECRET_REPLY)], "jump_to": "end"}

        # 2. Bedrock: prompt attacks, harmful content, denied topics.
        if self.bedrock is not None:
            res = self.bedrock.check("INPUT", cleaned)
            apply = res.intervened and res.blocked and self.bedrock.enforce
            _record("bedrock", "INPUT", self.bedrock.mode, res.findings, apply, cost_usd=res.cost_usd,
                    units=res.units, latency_ms=res.latency_ms, error=res.error)
            if apply:
                if any(f.startswith("pii:") and f.endswith(":blocked") for f in res.findings):
                    # A secret the rules missed: take it out of the conversation as well.
                    updates = [HumanMessage(content="[message removed: it contained sensitive data]", id=human.id)]
                    reply = BLOCKED_SECRET_REPLY
                else:
                    reply = _input_reply(res.findings)
                return {"messages": updates + [AIMessage(content=reply)], "jump_to": "end"}
        return {"messages": updates} if updates else None

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state, runtime):
        return await asyncio.to_thread(self.before_agent, state, runtime)


class OutputGuardrail(AgentMiddleware):
    def __init__(self, bedrock: BedrockGuard | None = None):
        super().__init__()
        self.bedrock = bedrock

    def after_agent(self, state, runtime):
        messages = state.get("messages", [])
        idx, answer = _last(messages, AIMessage)
        if answer is None or answer.tool_calls:
            return None
        original = _text(answer)
        text, masked = pii.redact(original, pii.OUTPUT_MASK, style="partial")
        if masked:
            _record("rules", "OUTPUT", "enforce", sorted({m.kind for m in masked}), True)

        if self.bedrock is not None and text.strip():
            h_idx, human = _last(messages, HumanMessage)
            # What the subagents reported this turn: the facts the answer should rest on.
            reports = [_text(m) for m in messages[(h_idx or 0) + 1:] if isinstance(m, ToolMessage)]
            res = self.bedrock.check("OUTPUT", text, grounding_source="\n\n".join(reports) or None,
                                     query=_text(human) if human is not None else None)
            apply = res.intervened and self.bedrock.enforce
            _record("bedrock", "OUTPUT", self.bedrock.mode, res.findings, apply, cost_usd=res.cost_usd,
                    units=res.units, latency_ms=res.latency_ms, error=res.error)
            if apply:
                text = BEDROCK_OUTPUT_FALLBACK if res.blocked or not res.output_text else res.output_text

        if text != original:
            return {"messages": [AIMessage(content=text, id=answer.id)]}
        return None

    async def aafter_agent(self, state, runtime):
        return await asyncio.to_thread(self.after_agent, state, runtime)


class ContactMasking(AgentMiddleware):
    """Mask emails and phone numbers in tool results before the model reads them."""

    def _mask(self, result):
        if not isinstance(result, ToolMessage):
            return result
        content = result.content
        if isinstance(content, str):
            new = pii.redact(content, pii.CONTACT, style="partial")[0]
        else:
            new = [dict(b, text=pii.redact(b["text"], pii.CONTACT, style="partial")[0])
                   if isinstance(b, dict) and isinstance(b.get("text"), str) else b for b in content]
        if new != content:
            result = result.model_copy(update={"content": new})
        return result

    def wrap_tool_call(self, request, handler):
        return self._mask(handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._mask(await handler(request))


def coordinator_guardrails(bedrock: BedrockGuard | None = None) -> list[AgentMiddleware]:
    return [InputGuardrail(bedrock), OutputGuardrail(bedrock),
            # A normal turn is 2-4 coordinator calls; this only stops loops.
            ModelCallLimitMiddleware(run_limit=8, exit_behavior="end")]


def subagent_guardrails(mask_contacts: bool = False) -> list[AgentMiddleware]:
    limits = [ModelCallLimitMiddleware(run_limit=8, exit_behavior="end"),
              ToolCallLimitMiddleware(run_limit=8, exit_behavior="end")]
    return ([ContactMasking()] if mask_contacts else []) + limits


__all__ = ["BLOCKED_SECRET_REPLY", "ContactMasking", "InputGuardrail", "OutputGuardrail",
           "coordinator_guardrails", "subagent_guardrails"]
