"""Synthetic HMAC event policy tests, not provider/payment verification."""
import hashlib
import hmac
import json
import unittest
from concurrent.futures import ThreadPoolExecutor


KEY = b"disposable-test-key-not-for-use!"
PAYMENT = "a" * 32
OTHER = "b" * 32
NOW = 1_800_000_000


def event(event_id="evt1", event_type="confirmation", sequence=1, payment_id=PAYMENT,
          amount_paise=1200, currency="INR", occurred_at=NOW):
    return dict(event_id=event_id, event_type=event_type, sequence=sequence, payment_id=payment_id,
                amount_paise=amount_paise, currency=currency, occurred_at=occurred_at)


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def sign(body, key=KEY):
    return hmac.new(key, body, hashlib.sha256).hexdigest()


class PaymentSandboxTests(unittest.TestCase):
    def machine(self):
        from ai_earn.payment_sandbox import SandboxPaymentEvents
        return SandboxPaymentEvents(KEY, payment_id=PAYMENT, amount_paise=1200)

    def test_confirmation_settlement_and_refund_are_explicitly_synthetic(self):
        m = self.machine()
        for eid, kind, seq, stage in (("evt1", "confirmation", 1, "sandbox_confirmed"),
                                      ("evt2", "settlement", 2, "sandbox_settled"),
                                      ("evt3", "refund", 3, "sandbox_refunded")):
            body = encode(event(eid, kind, seq))
            self.assertEqual(m.accept(body, sign(body), now=NOW),
                             {"event_id": eid, "acknowledged_stage": stage, "scope": "synthetic_only"})
        self.assertEqual(m.state, "sandbox_refunded")
        first = encode(event())
        self.assertEqual(m.accept(first, sign(first), now=NOW)["acknowledged_stage"], "sandbox_confirmed")
        self.assertEqual(m.state, "sandbox_refunded")

    def test_fail_closed_on_signature_tampering_or_invalid_trusted_configuration(self):
        from ai_earn.payment_sandbox import SandboxPaymentError, SandboxPaymentEvents
        for key in (b"", b"short", "not-bytes", None, b"a" * 129):
            with self.assertRaises(SandboxPaymentError):
                SandboxPaymentEvents(key, payment_id=PAYMENT, amount_paise=1200)
        m = self.machine()
        body = encode(event())
        for signature in ("", "a" * 64, sign(body).upper(), sign(body) + "x", None):
            with self.assertRaises(SandboxPaymentError):
                m.accept(body, signature, now=NOW)
        with self.assertRaises(SandboxPaymentError):
            m.accept(body + b" ", sign(body), now=NOW)
        self.assertEqual(m.state, "sandbox_awaiting")
        self.assertNotIn(KEY.decode(), repr(m))

    def test_reject_invalid_payload_even_when_signed(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        for body in (b"", b"x" * 4097, b"\xff", b"{", b"[]", b' {"event_id":"a","event_id":"b"}',
                     encode({**event(), "extra": "value"}), encode({k: v for k, v in event().items() if k != "sequence"}),
                     encode(event(payment_id=OTHER)), encode(event(amount_paise=1)), encode(event(currency="USD")),
                     encode(event(amount_paise=True)), encode(event(sequence=True)), encode(event(occurred_at=True)),
                     encode(event(event_type="settled")), encode(event(event_id="a b"))):
            with self.subTest(body=body[:80]):
                with self.assertRaises(SandboxPaymentError):
                    m.accept(body, sign(body), now=NOW)
        self.assertEqual(m.state, "sandbox_awaiting")

    def test_replay_conflict_order_and_freshness(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        for bad in (event(event_type="settlement"), event(sequence=2), event(occurred_at=NOW - 301),
                    event(occurred_at=NOW + 301)):
            body = encode(bad)
            with self.assertRaises(SandboxPaymentError):
                m.accept(body, sign(body), now=NOW)
        first = encode(event())
        m.accept(first, sign(first), now=NOW)
        changed = encode(event(event_type="settlement", sequence=2))
        with self.assertRaises(SandboxPaymentError):
            m.accept(changed, sign(changed), now=NOW)
        second = encode(event("evt2", "settlement", 2))
        m.accept(second, sign(second), now=NOW)
        self.assertEqual(m.state, "sandbox_settled")

    def test_failure_is_terminal_and_restart_does_not_persist(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        failed = encode(event("failure", "failure", 1))
        m.accept(failed, sign(failed), now=NOW)
        later = encode(event("confirm", "confirmation", 2))
        with self.assertRaises(SandboxPaymentError):
            m.accept(later, sign(later), now=NOW)
        self.assertEqual(m.state, "sandbox_failed")
        self.assertEqual(self.machine().state, "sandbox_awaiting")

    def test_configuration_and_timestamp_boundaries_are_strict(self):
        from ai_earn.payment_sandbox import SandboxPaymentError, SandboxPaymentEvents
        for payment_id, amount in (("bad", 1), (PAYMENT, -1), (PAYMENT, True), (PAYMENT, 1.0), (PAYMENT, 1_000_000_000_001)):
            with self.assertRaises(SandboxPaymentError):
                SandboxPaymentEvents(KEY, payment_id=payment_id, amount_paise=amount)
        body = encode(event(occurred_at=NOW - 300))
        m = self.machine()
        self.assertEqual(m.accept(body, sign(body), now=NOW)["scope"], "synthetic_only")
        for bad in (True, -1, 4_102_444_801, "now"):
            with self.assertRaises(SandboxPaymentError):
                self.machine().accept(body, sign(body), now=bad)

    def test_freshness_is_rechecked_on_exact_retry_and_ack_is_copied(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        body = encode(event())
        first = m.accept(body, sign(body), now=NOW)
        first["acknowledged_stage"] = "tampered"
        self.assertEqual(m.accept(body, sign(body), now=NOW)["acknowledged_stage"], "sandbox_confirmed")
        with self.assertRaises(SandboxPaymentError):
            m.accept(body, sign(body), now=NOW + 301)
        self.assertEqual(m.state, "sandbox_confirmed")

    def test_unaccepted_bad_message_does_not_reserve_event_id(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        bad = encode(event(payment_id=OTHER))
        with self.assertRaises(SandboxPaymentError):
            m.accept(bad, sign(bad), now=NOW)
        good = encode(event())
        self.assertEqual(m.accept(good, sign(good), now=NOW)["acknowledged_stage"], "sandbox_confirmed")

    def test_concurrent_different_terminal_events_cannot_both_win(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        bodies = [encode(event("confirm", "confirmation", 1)), encode(event("failure", "failure", 1))]
        def call(body):
            try:
                return m.accept(body, sign(body), now=NOW)
            except SandboxPaymentError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, bodies))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertIn(m.state, ("sandbox_confirmed", "sandbox_failed"))

    def test_refuses_non_bytes_and_json_nonfinite_numbers(self):
        from ai_earn.payment_sandbox import SandboxPaymentError
        m = self.machine()
        for body in (None, "not bytes", bytearray(b"hello")):
            with self.assertRaises(SandboxPaymentError):
                m.accept(body, "a" * 64, now=NOW)
        body = b'{"event_id":"x","event_type":"confirmation","sequence":1,"payment_id":"' + PAYMENT.encode() + b'","amount_paise":NaN,"currency":"INR","occurred_at":1800000000}'
        with self.assertRaises(SandboxPaymentError):
            m.accept(body, sign(body), now=NOW)
        self.assertEqual(m.state, "sandbox_awaiting")

    def test_same_event_concurrency_is_idempotent(self):
        m = self.machine()
        body = encode(event())
        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: m.accept(body, sign(body), now=NOW), range(16)))
        self.assertTrue(all(item == outputs[0] for item in outputs))
        self.assertEqual(m.state, "sandbox_confirmed")
