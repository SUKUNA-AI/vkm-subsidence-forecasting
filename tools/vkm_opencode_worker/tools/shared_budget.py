"""OpenCode aggregate accounting using the existing pinned Delegate ledger.

No credentials, network, proxy, new budget or background service. The owner must
qualify complete provider-request interception before any live launch. Every
admission is durably POSSIBLY_SENT before returning a permit; retries are denied.
Keep this object in the long-lived Python supervisor, never a per-call process.

API: SharedSession.begin(host_budget, current_client_record, profile, session_cap,
                         task_id, runtime_sec, receipt_path)
     before_attempt(id, estimated_cost_upper, request_pins)
     after_attempt(id, generation_id, cost, evidence_sha256,
                   evidence_kind='generation_record')
     finish(no_inflight=True), abort(reason), close()
     reconcile_pending(..., evidence_records, no_inflight=True)

host_budget: ledger_path, total_budget_usd='20', key_fingerprint (SHA256 only),
key_remaining_usd (owner-verified finite key limit), diagnostic_reserve_usd='.025'.
client record: client_path/client_sha256, policy_path/policy_sha256,
optional requested_reasoning. Evidence is verified by the native collector, NOT
an LLM or key-usage delta. This module validates its binding/shape and records the
attestation; it cannot independently authenticate arbitrary caller statements.

The original Delegate lock is held for the complete bounded session. Only an
isolated module copy receives thread/path-checked reentrancy, so existing
reserve/settle execute under that same actual lock. Other Delegate processes
remain serialized. A crash leaves the original lock/lease/hold for explicit
owner recovery; no stale/foreign lock is deleted here. Reconciliation does NOT
automatically unblock the ledger after an unknown outcome.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, localcontext
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import types
import uuid

TOTAL = Decimal('20')
DIAGNOSTIC = Decimal('.025')
SCHEMA = 'vkm.opencode.shared-budget/1'
KINDS = frozenset({'main', 'title', 'summary', 'compaction', 'tool_followup'})
EVIDENCE_KINDS = frozenset({'generation_record', 'response_usage'})


class BudgetError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(value, code):
    if not value:
        raise BudgetError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _path(value):
    return Path(value).resolve(strict=False)


def _load_backend(record):
    _need(isinstance(record, dict), 'CLIENT_RECORD_REQUIRED')
    _need(set(record) <= {'client_path', 'client_sha256', 'policy_path', 'policy_sha256', 'requested_reasoning'},
          'UNKNOWN_CLIENT_RECORD_FIELDS')
    _need(all(name in record for name in ('client_path', 'policy_path')), 'CLIENT_LOCATION_REQUIRED')
    for field in ('client_sha256', 'policy_sha256'):
        _need(_hex(record.get(field)), 'CLIENT_PIN_REQUIRED')
    path = _path(record['client_path'])
    raw = path.read_bytes()
    _need(_sha(raw) == record['client_sha256'], 'CLIENT_PIN_MISMATCH')
    policy = _path(record['policy_path'])
    _need(policy == path.parents[1] / 'references' / 'DELEGATION_POLICY_V2.json', 'POLICY_LOCATION_MISMATCH')
    _need(_sha(policy.read_bytes()) == record['policy_sha256'], 'POLICY_PIN_MISMATCH')
    module = types.ModuleType('vkm_private_ledger_backend_' + uuid.uuid4().hex)
    module.__file__ = str(path)
    # Executes only the owner's explicitly pinned existing client, no model code.
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    for name in ('locked', 'reserve', 'settle', 'load_json', 'write_json', 'money',
                 'encoded', 'profile_spec', '_ledger_fields', '_ledger_money_sum'):
        _need(callable(getattr(module, name, None)), 'CLIENT_LEDGER_INTERFACE_MISMATCH')
    return module


def _difference(left, right):
    values = [left, right]
    nonzero = [x for x in values if x]
    precision = 28 if not nonzero else max(28, max(x.adjusted() for x in nonzero)
        - min(x.as_tuple().exponent for x in nonzero) + 5)
    _need(precision <= 10000, 'INVALID_COST_PRECISION')
    with localcontext() as context:
        context.prec = precision
        return left - right


def _synchronized(method):
    # RPC handlers may be different threads. One small in-process mutex serializes
    # this session only; the existing actual ledger lock still spans the runtime.
    def call(self, *args, **kwargs):
        with self._api_lock:
            previous = self._thread
            self._thread = threading.get_ident()
            try:
                return method(self, *args, **kwargs)
            finally:
                self._thread = previous
    return call


class SharedSession:
    """One existing-ledger reservation for a serialized, instrumented runtime."""
    @classmethod
    def begin(cls, host_budget, current_client_record, profile, session_cap,
              task_id, runtime_sec, receipt_path):
        obj = cls()
        obj._initialize(host_budget, current_client_record, receipt_path)
        _need(type(runtime_sec) is int and 1 <= runtime_sec <= 1700, 'RUNTIME_LEASE_LIMIT')
        _need(isinstance(task_id, str) and task_id and len(task_id) <= 256, 'TASK_ID_REQUIRED')
        spec = obj.backend.profile_spec(profile)
        cap = obj.backend.money(session_cap)
        _need(0 < cap <= obj.backend.money(spec[3]), 'SESSION_CAP_EXCEEDED')
        _need(not obj.receipt_path.exists(), 'RECEIPT_ALREADY_EXISTS')
        obj.cap = cap
        obj._deadline = time.monotonic() + runtime_sec
        obj._state = {'schema': SCHEMA, 'task_id': task_id, 'profile': profile,
                      'model': spec[0], 'provider_only': spec[1], 'session_cap_usd': str(cap),
                      'total_budget_usd': str(TOTAL), 'diagnostic_reserve_usd': str(DIAGNOSTIC),
                      'ledger_path': str(obj.ledger), 'key_fingerprint': obj.key_id,
                      'client_record': dict(obj.record), 'runtime_sec': runtime_sec,
                      'state': 'PREPARING', 'attempts': [], 'violations': [],
                      'scientific_acceptance': 'NOT_IMPLIED', 'key_usage_delta_is_settlement': False}
        try:
            obj._acquire()
            ledger = obj.backend.load_json(obj.ledger)
            budget, spent, holds, _, _ = obj.backend._ledger_fields(ledger)
            _need(budget == TOTAL and ledger['key_id'] == obj.key_id, 'SHARED_LEDGER_IDENTITY_MISMATCH')
            _need(ledger.get('owner_local_cap_only', False) is False, 'SECOND_BUDGET_REJECTED')
            _need(ledger['blocked'] is False, 'UNKNOWN_EXECUTION_COST')
            _need(not holds, 'PENDING_EXECUTION_COST')
            amount = obj.backend._ledger_money_sum((spent, cap, DIAGNOSTIC))
            _need(amount <= TOTAL and obj.backend._ledger_money_sum((cap, DIAGNOSTIC)) <= obj.key_remaining,
                  'DIAGNOSTIC_RESERVE_PROTECTED')
            obj._expected_spent = spent
            obj._state['spent_before_usd'] = str(spent)
            obj._state['ledger_before_sha256'] = _sha(obj.ledger.read_bytes())
            obj._save()
            token = obj.backend.reserve(obj.ledger, '20', obj.key_id, obj.key_remaining,
                                        cap, profile=profile, lease_seconds=runtime_sec + 60)
            obj._state.update(reservation_token=token, state='OPEN')
            obj._save()
            return obj
        except BaseException:
            if obj._state.get('reservation_token'):
                obj._unknown('BEGIN_FAILED_AFTER_RESERVATION')
            else:
                obj._release()
            raise

    def _initialize(self, host, record, receipt_path):
        _need(isinstance(host, dict), 'HOST_BUDGET_REQUIRED')
        self.backend = _load_backend(record)
        self.record = dict(record)
        self.record['client_path'] = str(_path(record['client_path']))
        self.record['policy_path'] = str(_path(record['policy_path']))
        self.ledger = _path(host['ledger_path'])
        _need(self.ledger.is_file(), 'EXISTING_LEDGER_REQUIRED')
        _need(self.backend.money(host.get('total_budget_usd')) == TOTAL, 'SECOND_BUDGET_REJECTED')
        _need(self.backend.money(host.get('diagnostic_reserve_usd')) == DIAGNOSTIC, 'DIAGNOSTIC_RESERVE_REQUIRED')
        self.key_id = host.get('key_fingerprint')
        _need(_hex(self.key_id), 'KEY_FINGERPRINT_REQUIRED')
        self.key_remaining = self.backend.money(host.get('key_remaining_usd'))
        _need(self.key_remaining > 0, 'FINITE_KEY_REMAINING_REQUIRED')
        self.receipt_path = _path(receipt_path)
        _need(self.receipt_path.parent.is_dir(), 'RECEIPT_PARENT_REQUIRED')
        forbidden = {self.ledger, Path(str(self.ledger) + '.lock'), _path(record['client_path']), _path(record['policy_path'])}
        _need(self.receipt_path not in forbidden, 'RECEIPT_TARGET_CONFLICT')
        self._original_locked = self.backend.locked
        self._outer = None
        self._owned = False
        self._thread = threading.get_ident()
        self._api_lock = threading.RLock()
        self._receipt_sha = None
        self._state = {}
        self.backend.locked = self._reentrant_locked

    def _check_client_pins(self):
        _need(_sha(Path(self.record['client_path']).read_bytes()) == self.record['client_sha256'], 'CLIENT_PIN_CHANGED')
        _need(_sha(Path(self.record['policy_path']).read_bytes()) == self.record['policy_sha256'], 'POLICY_PIN_CHANGED')

    def _check_lock(self):
        _need(self._owned and self._thread == threading.get_ident(), 'LEDGER_LOCK_NOT_OWNED')
        path = Path(str(self.ledger) + '.lock')
        try:
            info = path.lstat()
            same = (info.st_dev, info.st_ino, path.read_bytes()) == self._lock_signature
        except OSError:
            same = False
        _need(same, 'LEDGER_LOCK_OWNERSHIP_LOST')

    @contextmanager
    def _reentrant_locked(self, path):
        if self._owned and self._thread == threading.get_ident() and _path(path) == self.ledger:
            self._check_lock()
            yield
        else:
            with self._original_locked(path):
                yield

    def _acquire(self):
        _need(not self._owned, 'SESSION_ALREADY_LOCKED')
        self._outer = self._original_locked(self.ledger)
        self._outer.__enter__()
        self._owned = True
        path = Path(str(self.ledger) + '.lock')
        info = path.lstat()
        self._lock_signature = (info.st_dev, info.st_ino, path.read_bytes())

    def _release(self):
        outer, self._outer = self._outer, None
        if outer is not None:
            try:
                outer.__exit__(None, None, None)
            finally:
                self._owned = False
                self.backend.locked = self._original_locked

    def _save(self):
        if self._receipt_sha is not None:
            _need(self.receipt_path.is_file() and _sha(self.receipt_path.read_bytes()) == self._receipt_sha,
                  'RECEIPT_CHANGED_EXTERNALLY')
        if self._receipt_sha is None:
            # Atomic first ownership acquisition: never replace a foreign receipt.
            with self.receipt_path.open('xb') as stream:
                stream.write(self.backend.encoded(self._state))
                stream.flush()
                os.fsync(stream.fileno())
        else:
            self.backend.write_json(self.receipt_path, self._state)
        self._receipt_sha = _sha(self.receipt_path.read_bytes())

    def _financial_identity(self, check_active=True):
        self._check_lock()
        ledger = self.backend.load_json(self.ledger)
        budget, spent, holds, _, _ = self.backend._ledger_fields(ledger)
        _need(budget == TOTAL and ledger['key_id'] == self.key_id, 'SHARED_LEDGER_IDENTITY_MISMATCH')
        token = self._state.get('reservation_token')
        _need(token in holds and self.backend.money(holds[token]) == self.cap, 'RESERVATION_IDENTITY_MISMATCH')
        _need(set(holds) == {token}, 'FOREIGN_RESERVATION_DURING_SERIAL_SESSION')
        _need(spent == self._expected_spent, 'LEDGER_CHANGED_DURING_SERIAL_SESSION')
        if check_active:
            _need(self.backend._lease_is_confirmed_active(ledger.get('reservation_leases', {}).get(token)),
                  'RESERVATION_LEASE_UNCONFIRMED')
        return ledger

    @_synchronized
    def before_attempt(self, attempt_id, estimated_cost_upper, request_pins, *, request_body=None):
        self._check_lock()
        self._check_client_pins()
        _need(self._state['state'] == 'OPEN' and not self._state['violations'], 'SESSION_ACCOUNTING_STOPPED')
        _need(time.monotonic() < self._deadline, 'RUNTIME_LEASE_EXPIRED')
        ledger = self._financial_identity()
        _need(ledger['blocked'] is False, 'UNKNOWN_EXECUTION_COST')
        _need(isinstance(attempt_id, str) and attempt_id and len(attempt_id) <= 256, 'ATTEMPT_ID_REQUIRED')
        attempts = self._state['attempts']
        _need(not any(x['attempt_id'] == attempt_id for x in attempts), 'DUPLICATE_ATTEMPT_ID')
        _need(not any(x['state'] != 'KNOWN' for x in attempts), 'INFLIGHT_OR_UNKNOWN_ATTEMPT')
        _need(isinstance(request_pins, dict), 'REQUEST_PINS_REQUIRED')
        _need(request_pins.get('kind') in KINDS, 'RETRY_OR_UNKNOWN_REQUEST_KIND_DENIED')
        _need(request_pins.get('model') == self._state['model'], 'MODEL_PIN_MISMATCH')
        provider = request_pins.get('provider')
        _need(isinstance(provider, dict) and provider.get('only') == [self._state['provider_only']]
              and provider.get('allow_fallbacks') is False, 'PROVIDER_FALLBACK_PIN_MISMATCH')
        _need(_hex(request_pins.get('request_sha256')), 'REQUEST_DIGEST_REQUIRED')
        _need(type(request_pins.get('retry_count')) is int and request_pins['retry_count'] == 0,
              'RETRIES_DISABLED')
        _need(isinstance(request_body, bytes), 'EXACT_REQUEST_BYTES_REQUIRED')
        _need(_sha(request_body) == request_pins['request_sha256'], 'REQUEST_BODY_DIGEST_MISMATCH')
        try:
            body = json.loads(request_body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise BudgetError('REQUEST_BODY_INVALID_JSON') from None
        _need(isinstance(body, dict), 'REQUEST_BODY_OBJECT_REQUIRED')
        for field in ('model', 'provider', 'reasoning'):
            _need(body.get(field) == request_pins.get(field), 'REQUEST_BODY_PIN_MISMATCH')
        limits = [body[name] for name in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if name in body]
        _need(len(limits) == 1 and type(limits[0]) is int and limits[0] > 0, 'FINITE_OUTPUT_TOKEN_BOUND_REQUIRED')
        if 'requested_reasoning' in self.record:
            _need(request_pins.get('reasoning') == self.record['requested_reasoning'], 'REASONING_PIN_MISMATCH')
        upper = self.backend.money(estimated_cost_upper)
        _need(upper > 0, 'POSITIVE_UPPER_BOUND_REQUIRED')
        used = self.backend._ledger_money_sum(self.backend.money(x['cost_usd']) for x in attempts)
        _need(self.backend._ledger_money_sum((used, upper)) <= self.cap, 'SESSION_BUDGET_EXHAUSTED')
        attempt = {'attempt_id': attempt_id, 'request_sha256': request_pins['request_sha256'],
                   'kind': request_pins['kind'], 'max_output_tokens': limits[0], 'upper_bound_usd': str(upper),
                   'state': 'POSSIBLY_SENT', 'admission_nonce': uuid.uuid4().hex}
        attempts.append(attempt)
        self._save()  # durable admission BEFORE the caller may send HTTP
        return {'admitted': True, **dict(attempt), 'remaining_cap_usd': str(_difference(self.cap, used))}

    @_synchronized
    def after_attempt(self, attempt_id, generation_id, cost, evidence_sha256, evidence_kind='generation_record'):
        self._check_lock()
        _need(self._state['state'] == 'OPEN', 'SESSION_ACCOUNTING_STOPPED')
        attempts = self._state['attempts']
        found = [x for x in attempts if x['attempt_id'] == attempt_id]
        _need(len(found) == 1 and found[0]['state'] == 'POSSIBLY_SENT', 'ATTEMPT_STATE_MISMATCH')
        try:
            known_cost = self.backend.money(cost)
            _need(isinstance(generation_id, str) and generation_id.startswith('gen-') and len(generation_id) <= 256,
                  'GENERATION_EVIDENCE_REQUIRED')
            _need(_hex(evidence_sha256) and evidence_kind in EVIDENCE_KINDS, 'GENERATION_EVIDENCE_REQUIRED')
            _need(not any(x is not found[0] and x.get('generation_id') == generation_id for x in attempts),
                  'DUPLICATE_GENERATION_ID')
        except Exception:
            self._unknown('UNKNOWN_OR_INVALID_GENERATION_COST')
            return self.receipt()
        found[0].update(state='KNOWN', generation_id=generation_id, cost_usd=str(known_cost),
                        evidence_sha256=evidence_sha256, evidence_kind=evidence_kind)
        if known_cost > self.backend.money(found[0]['upper_bound_usd']):
            self._state['violations'].append('ACTUAL_COST_EXCEEDS_REQUEST_UPPER_BOUND')
        self._save()
        return self.receipt()

    def _settle_known(self, total):
        ledger = self._financial_identity(check_active=False)
        token = self._state['reservation_token']
        predicted = copy.deepcopy(ledger)
        del predicted['reservations'][token]
        predicted.get('reservation_leases', {}).pop(token, None)
        predicted['spent_usd'] = str(self.backend._ledger_money_sum((self.backend.money(ledger['spent_usd']), total)))
        if total > self.cap:
            predicted['blocked'] = True
        self._state.update(state='SETTLING', settlement_cost_usd=str(total),
                           expected_settled_ledger_sha256=_sha(self.backend.encoded(predicted)))
        self._save()
        self.backend.settle(self.ledger, token, total)
        actual = self.backend.load_json(self.ledger)
        _need(token not in actual['reservations'], 'SETTLEMENT_NOT_COMMITTED')
        _need(_sha(self.ledger.read_bytes()) == self._state['expected_settled_ledger_sha256'],
              'SETTLEMENT_STATE_AMBIGUOUS')
        self._state.update(state='SETTLED_REVIEW_REQUIRED' if self._state['violations'] else 'SETTLED',
                           settled_ledger_sha256=_sha(self.ledger.read_bytes()), stop_runtime=True)
        self._save()

    @_synchronized
    def finish(self, no_inflight):
        if not self._owned and self._state.get('state') in {'SETTLED', 'SETTLED_REVIEW_REQUIRED', 'UNKNOWN_HOLD', 'SETTLED_IDEMPOTENT_RECOVERY'}:
            return self.receipt()
        self._check_lock()
        if no_inflight is not True or any(x['state'] != 'KNOWN' for x in self._state['attempts']):
            return self._unknown('UNRESOLVED_RUNTIME_OUTCOME')
        _need(self._state['state'] == 'OPEN', 'SESSION_ACCOUNTING_STOPPED')
        try:
            total = self.backend._ledger_money_sum(self.backend.money(x['cost_usd']) for x in self._state['attempts'])
            self._settle_known(total)
            return self.receipt()
        finally:
            self._release()

    def _unknown(self, reason):
        if not self._owned:
            return self.receipt()
        try:
            self._financial_identity(check_active=False)
            self._state.update(state='UNKNOWN_HOLD', reason=reason, stop_runtime=True)
            try:
                self._save()
            finally:
                self.backend.settle(self.ledger, self._state['reservation_token'], None)
            self._state['unknown_ledger_sha256'] = _sha(self.ledger.read_bytes())
            self._save()
            return self.receipt()
        finally:
            self._release()

    @_synchronized
    def abort(self, reason='RUNTIME_ABORTED'):
        _need(isinstance(reason, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', reason), 'SANITIZED_ABORT_REASON_REQUIRED')
        return self._unknown(reason)

    @_synchronized
    def receipt(self):
        return copy.deepcopy(self._state)

    @_synchronized
    def close(self):
        if self._owned:
            return self._unknown('SUPERVISOR_CLOSED_WITHOUT_FINAL_ACCOUNTING')
        return self.receipt()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    @classmethod
    def reconcile_pending(cls, host_budget, current_client_record, receipt_path,
                          evidence_records, no_inflight):
        """Owner-supplied explicit generation evidence; never infer zero/unblock."""
        _need(no_inflight is True, 'NO_INFLIGHT_EVIDENCE_REQUIRED')
        obj = cls()
        obj._initialize(host_budget, current_client_record, receipt_path)
        obj._state = obj.backend.load_json(obj.receipt_path)
        obj._receipt_sha = _sha(obj.receipt_path.read_bytes())
        _need(obj._state.get('schema') == SCHEMA and obj._state.get('key_fingerprint') == obj.key_id,
              'RECONCILIATION_IDENTITY_MISMATCH')
        _need(_path(obj._state.get('ledger_path')) == obj.ledger, 'RECONCILIATION_IDENTITY_MISMATCH')
        for field in ('client_sha256', 'policy_sha256'):
            _need(obj._state.get('client_record', {}).get(field) == current_client_record.get(field),
                  'RECONCILIATION_CLIENT_PIN_MISMATCH')
        obj.cap = obj.backend.money(obj._state['session_cap_usd'])
        obj._expected_spent = obj.backend.money(obj._state['spent_before_usd'])
        _need(obj._state['state'] in {'UNKNOWN_HOLD', 'SETTLING'}, 'NO_PENDING_SESSION')
        obj._acquire()
        try:
            ledger = obj.backend.load_json(obj.ledger)
            token = obj._state['reservation_token']
            if token not in ledger['reservations']:
                _need(obj._state['state'] == 'SETTLING' and _sha(obj.ledger.read_bytes()) == obj._state.get('expected_settled_ledger_sha256'),
                      'SETTLEMENT_STATE_AMBIGUOUS')
                obj._state.update(state='SETTLED_IDEMPOTENT_RECOVERY', stop_runtime=True)
                obj._save()
                return obj.receipt()
            obj._financial_identity(check_active=False)
            _need(isinstance(evidence_records, list), 'GENERATION_EVIDENCE_REQUIRED')
            attempts = obj._state['attempts']
            by_id = {e.get('attempt_id'): e for e in evidence_records if isinstance(e, dict)}
            _need(len(by_id) == len(evidence_records) and set(by_id) == {x['attempt_id'] for x in attempts},
                  'RECONCILIATION_COVERAGE_MISMATCH')
            generations = set()
            for attempt in attempts:
                evidence = by_id[attempt['attempt_id']]
                generation = evidence.get('generation_id')
                cost = obj.backend.money(evidence.get('cost_usd'))
                _need(isinstance(generation, str) and generation.startswith('gen-') and generation not in generations,
                      'GENERATION_EVIDENCE_REQUIRED')
                _need(_hex(evidence.get('evidence_sha256')) and evidence.get('evidence_kind') in EVIDENCE_KINDS,
                      'GENERATION_EVIDENCE_REQUIRED')
                _need(evidence.get('request_sha256') == attempt['request_sha256'], 'RECONCILIATION_REQUEST_MISMATCH')
                if attempt['state'] == 'KNOWN':
                    _need(attempt['generation_id'] == generation and obj.backend.money(attempt['cost_usd']) == cost,
                          'RECONCILIATION_CONTRADICTION')
                generations.add(generation)
                attempt.update(state='KNOWN', generation_id=generation, cost_usd=str(cost),
                               evidence_kind=evidence['evidence_kind'], evidence_sha256=evidence['evidence_sha256'])
            total = obj.backend._ledger_money_sum(obj.backend.money(x['cost_usd']) for x in attempts)
            obj._state['reconciliation_basis'] = 'EXPLICIT_GENERATION_RECORDS_NOT_KEY_USAGE_DELTA'
            obj._settle_known(total)
            # Existing settle intentionally leaves a previous blocked flag set.
            # Only the separate owner-reviewed global recovery may clear it.
            obj._state['ledger_block_remains'] = obj.backend.load_json(obj.ledger)['blocked']
            obj._save()
            return obj.receipt()
        finally:
            obj._release()
