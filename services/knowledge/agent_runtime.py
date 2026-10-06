"""Per-answer deadline, actual invocation traces, and in-memory tool receipts."""
import asyncio
import copy
import time

from langchain.agents.middleware import AgentMiddleware

import execution_budget

TOTAL_SECONDS = 75
FINAL_RESERVE_SECONDS = 15
MODEL_SECONDS = 60


class AgentExecution:
    def __init__(self, details, model_limit=10, *, total_seconds=None, final_reserve=None, started=None):
        self.details = details
        self.started = time.monotonic() if started is None else started
        total = TOTAL_SECONDS if total_seconds is None else total_seconds
        reserve = FINAL_RESERVE_SECONDS if final_reserve is None else final_reserve
        self.deadline = self.started + total
        self.work_deadline = self.deadline - reserve
        self.model_limit = model_limit
        self.model_calls = 0
        self.completed_tools = []  # Never persisted: may contain private chat or memory text.
        self.last_model_messages = []  # Valid input prefix, excluding pending tool requests.
        self.last_model_tools = []
        details.setdefault('model_calls', [])
        details.setdefault('tool_calls', [])
        details.setdefault('agent', {}).update(total_timeout_seconds=total, final_reserve_seconds=reserve,
                                               model_limit=model_limit)

    def remaining(self, final=False):
        value = (self.deadline if final else self.work_deadline) - time.monotonic()
        if value <= 0:
            raise execution_budget.DeadlineExceeded('agent_deadline')
        return value

    async def call_model(self, handler, *, final=False):
        seconds = min(MODEL_SECONDS, self.remaining(final))
        # A normal loop always leaves one model call for a tool-free final response.
        if self.model_calls >= self.model_limit - (0 if final else 1):
            raise execution_budget.DeadlineExceeded('model_call_budget')
        self.model_calls += 1
        started = time.monotonic()
        row = {'stage': 'agent_final' if final else 'agent', 'status': 'running',
               'tool_calls': 0, 'usage': {}, 'timeout_seconds': round(seconds, 3)}
        self.details['model_calls'].append(row)
        try:
            with execution_budget.until(self.deadline if final else self.work_deadline):
                output = await asyncio.wait_for(handler(), timeout=seconds)
            messages = output.result if hasattr(output, 'result') else [output]
            for message in messages:
                if getattr(message, 'type', '') == 'ai':
                    row['tool_calls'] += len(getattr(message, 'tool_calls', []))
                    row['usage'] = copy.deepcopy(getattr(message, 'usage_metadata', None) or {})
            row['status'] = 'ok'
            return output
        except TimeoutError as exc:
            row.update(status='timeout', error='TimeoutError')
            raise execution_budget.DeadlineExceeded('model_timeout') from exc
        except BaseException as exc:
            row.update(status='error', error=type(exc).__name__)
            raise
        finally:
            row['elapsed_ms'] = round((time.monotonic() - started) * 1000)


class ExecutionMiddleware(AgentMiddleware):
    def __init__(self, execution):
        self.execution = execution

    async def awrap_model_call(self, request, handler):
        # Keep the exact valid input in memory for a tool-disabled final turn.
        # Never save messages, reasoning, image data, or tool receipts in Trace.
        self.execution.last_model_messages = copy.deepcopy(
            ([request.system_message] if request.system_message else []) + list(request.messages))
        self.execution.last_model_tools = list(request.tools)
        return await self.execution.call_model(lambda: handler(request))

    async def awrap_tool_call(self, request, handler):
        execution = self.execution
        seconds = execution.remaining()
        started = time.monotonic()
        row = {'name': request.tool_call['name'], 'status': 'running'}
        execution.details['tool_calls'].append(row)
        try:
            with execution_budget.until(execution.work_deadline):
                result = await asyncio.wait_for(handler(request), timeout=seconds)
            row['status'] = getattr(result, 'status', 'success')
            execution.completed_tools.append({
                'call_id': request.tool_call['id'],
                'tool': request.tool_call['name'], 'arguments': copy.deepcopy(request.tool_call.get('args', {})),
                'status': row['status'], 'result': copy.deepcopy(result.content)})
            return result
        except TimeoutError as exc:
            row.update(status='timeout', error='TimeoutError')
            raise execution_budget.DeadlineExceeded('tool_timeout') from exc
        except BaseException as exc:
            row.update(status='error', error=type(exc).__name__)
            raise
        finally:
            row['elapsed_ms'] = round((time.monotonic() - started) * 1000)
