import asyncio
import io
import json
import unittest
from datetime import datetime
from scripts.generate_logs import SCENARIOS, configure_logger, load_config, payment_flow, simulate

class PaymentTests(unittest.TestCase):
    def config(self, **overrides):
        config = load_config([])
        config.update(latency_min_ms=0, latency_max_ms=0)
        config.update(overrides)
        return config

    def flow(self, scenario, environment='production'):
        output = io.StringIO()
        asyncio.run(payment_flow(self.config(environment=environment), configure_logger(environment, output), scenario, 'test-run'))
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_scenarios_and_environment_filter(self):
        for environment in ('production', 'homologation'):
            for scenario in SCENARIOS:
                with self.subTest(environment=environment, scenario=scenario):
                    events = self.flow(scenario, environment)
                    self.assertEqual(len({e['request_id'] for e in events}), 1)
                    self.assertEqual(events[0]['event'], 'flow.started')
                    self.assertEqual(events[-1]['event'], 'flow.finished')
                    self.assertEqual(any(e['level'] == 'DEBUG' for e in events), environment == 'homologation')
                    times = [datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')) for e in events]
                    self.assertEqual(times, sorted(times))
                    for event in events:
                        self.assertTrue({'timestamp', 'level', 'service', 'environment', 'request_id', 'message', 'event', 'status'} <= event.keys())
                        if event['event'].endswith('.finished'):
                            self.assertGreaterEqual(event['duration_ms'], 0)
                    if scenario == 'payment_failure':
                        self.assertEqual({e['service'] for e in events}, {'api-pagamentos'})
                        attempts = [e for e in events if e['event'] == 'payment.attempt.finished']
                        self.assertEqual([e['attempt'] for e in attempts], [1, 2, 3])
                        self.assertTrue(all(e['status'] == 'error' for e in attempts))
                    elif scenario == 'confirmation_failure':
                        self.assertNotIn('api-assinaturas', {e['service'] for e in events})
                    elif scenario == 'subscription_failure':
                        self.assertTrue(events[-1]['payment_confirmed'])
                        self.assertFalse(events[-1]['subscription_activated'])
                        self.assertEqual(events[-1]['status'], 'error')
                    else:
                        self.assertEqual(events[-1]['status'], 'success')
                    if scenario == 'payment_recovery':
                        self.assertEqual([e['status'] for e in events if e['event'] == 'payment.attempt.finished'], ['error', 'success'])
                        self.assertTrue(any(e['level'] == 'WARNING' for e in events))

    def test_unique_ids_and_complete_concurrent_operations(self):
        for concurrency in (1, 7):
            output = io.StringIO()
            asyncio.run(simulate(self.config(count=40, burst=40, interval=0, concurrency=concurrency), configure_logger('production', output)))
            grouped = {}
            for line in output.getvalue().splitlines():
                event = json.loads(line)
                grouped.setdefault(event['request_id'], []).append(event)
            self.assertEqual(len(grouped), 40)
            for events in grouped.values():
                self.assertEqual(events[0]['event'], 'flow.started')
                self.assertEqual(events[-1]['event'], 'flow.finished')
                self.assertEqual(len({e['scenario'] for e in events}), 1)

    def test_burst_then_pacing(self):
        output = io.StringIO()
        asyncio.run(simulate(self.config(count=4, burst=2, interval=.04), configure_logger('production', output)))
        starts = [json.loads(line) for line in output.getvalue().splitlines() if json.loads(line)['event'] == 'flow.started']
        times = [datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).timestamp() for e in starts]
        self.assertLess(times[1] - times[0], .03)
        self.assertGreaterEqual(times[2] - times[1], .03)
        self.assertGreaterEqual(times[3] - times[2], .03)

    def test_retry_limit_and_invalid_config(self):
        output = io.StringIO()
        asyncio.run(payment_flow(self.config(max_attempts=1), configure_logger('production', output), 'payment_failure', 'test'))
        self.assertEqual(sum(json.loads(line)['event'] == 'payment.attempt.finished' for line in output.getvalue().splitlines()), 1)
        for args in (['--count', '-1'], ['--interval', 'nan'], ['--max-attempts', '1'], ['--concurrency', '0']):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                load_config(args)

if __name__ == '__main__':
    unittest.main()
