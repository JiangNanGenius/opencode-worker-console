"""Billing classification: exact hidden monthly exhaustion vs unrelated failures."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnostics

MONTHLY_MESSAGE = ("You've reached your monthly usage limit for this billing cycle. "
                   "Your quota will be refreshed in the next cycle. To continue now, purchase extra "
                   "usage or upgrade your plan: "
                   "https://www.kimi.com/membership/subscription?tab=quota")


def monthly_error(status=403, message=MONTHLY_MESSAGE, message_id='msg_monthly'):
    return {'info': {'id': message_id, 'role': 'assistant', 'providerID': 'kimi-for-coding',
                     'modelID': 'kimi-for-coding',
                     'time': {'created': 1789683900000, 'completed': 1789683956000},
                     'error': {'name': 'APIError', 'data': {
                         'message': message, 'statusCode': status, 'isRetryable': False}}},
            'parts': []}


class BillingClassificationTests(unittest.TestCase):
    def test_exact_real_monthly_403_is_billing_with_reason_kind(self):
        error = diagnostics.from_messages([monthly_error()])[0]
        self.assertTrue(error['billing'])
        self.assertEqual(error['billing_reason'], diagnostics.MONTHLY_REASON)
        self.assertEqual(error['source'], 'model')
        self.assertEqual(error['code'], 'APIError')
        self.assertEqual(error['http_status'], 403)
        self.assertFalse(error['retryable'])
        self.assertEqual(error['suggested_action'], 'inspect_partial_work_and_reselect_profile')
        self.assertEqual(error['provider'], 'kimi-for-coding')
        self.assertAlmostEqual(error['occurred_at'], 1789683956)

    def test_is_billing_covers_402_and_monthly(self):
        self.assertTrue(diagnostics.is_billing({'data': {'statusCode': 402}}))
        self.assertTrue(diagnostics.is_billing(monthly_error()['info']['error']))
        self.assertEqual(diagnostics.billing_kind(monthly_error()['info']['error']),
                         diagnostics.MONTHLY_REASON)
        self.assertEqual(diagnostics.billing_kind({'data': {'statusCode': 402,
                                                            'message': 'Insufficient Balance'}}),
                         diagnostics.BALANCE_REASON)

    def test_generic_403_auth_and_429_rate_limit_never_billing(self):
        for status, message in [
            (403, 'Forbidden'),
            (403, 'Invalid API key'),
            (403, 'authentication failed for this credential'),
            (401, 'Unauthorized'),
            (429, 'Rate limit exceeded, retry later'),
            # Even a monthly phrase on a rate-limit response stays a rate limit.
            (429, MONTHLY_MESSAGE),
            (500, 'Internal server error'),
        ]:
            raw = {'data': {'statusCode': status, 'message': message}}
            self.assertIsNone(diagnostics.billing_kind(raw), (status, message))
            self.assertFalse(diagnostics.is_billing(raw), (status, message))

    def test_monthly_wording_alone_is_not_exhaustion(self):
        for message in ['You do not have permission to view monthly quota',
                        'Your monthly quota is 1000 requests',
                        'Monthly quota status unavailable',
                        'Contact support about your monthly plan']:
            raw = {'data': {'statusCode': 403, 'message': message}}
            self.assertIsNone(diagnostics.billing_kind(raw), message)
            self.assertFalse(diagnostics.is_billing(raw), message)

    def test_monthly_message_variants_require_exhaustion_wording(self):
        for message in ['You reached your monthly quota for this plan.',
                        'Monthly usage quota exceeded.',
                        'Your monthly quota will be refreshed in the next cycle.',
                        'monthly limit for this billing cycle reached']:
            raw = {'data': {'message': message}}
            self.assertEqual(diagnostics.billing_kind(raw), diagnostics.MONTHLY_REASON, message)

    def test_occurred_at_prefers_completion_over_creation(self):
        finished = {'time': {'created': 1789683900000, 'completed': 1789683956000}}
        pending = {'time': {'created': 1789683956000}}
        self.assertAlmostEqual(diagnostics.occurred_at(finished), 1789683956)
        self.assertAlmostEqual(diagnostics.occurred_at(pending), 1789683956)

    def test_tool_output_phrase_never_billing(self):
        messages = [{'info': {'id': 'msg_tool', 'role': 'assistant', 'providerID': 'kimi-for-coding'},
                     'parts': [{'type': 'tool', 'tool': 'bash', 'callID': 'c1', 'state': {
                         'status': 'error', 'input': {'command': 'cat provider.log'},
                         'error': MONTHLY_MESSAGE}}]}]
        errors = diagnostics.from_messages(messages)
        self.assertEqual(len(errors), 1)
        self.assertIsNone(errors[0].get('billing'))
        self.assertNotIn('billing_reason', errors[0])

    def test_non_billing_model_error_keeps_provider_action(self):
        raw = {'data': {'statusCode': 429, 'message': 'Rate limit exceeded', 'isRetryable': True}}
        error = diagnostics.from_messages([{'info': {'id': 'msg_r', 'role': 'assistant',
                                                     'error': raw}, 'parts': []}])[0]
        self.assertNotIn('billing', error)
        self.assertEqual(error['suggested_action'], 'inspect_model_error')


if __name__ == '__main__':
    unittest.main()
