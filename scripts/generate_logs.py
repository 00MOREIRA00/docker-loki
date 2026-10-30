"""Simulate distributed payments as JSON Lines, using only Python's standard library."""
import argparse
import asyncio
import json
import logging
import math
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SCENARIOS = ('success', 'payment_recovery', 'payment_failure', 'confirmation_failure', 'subscription_failure')

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(dict(timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'), level=record.levelname, **record.payload, message=record.getMessage()), ensure_ascii=False)

def configure_logger(environment, stream=None):
    logger = logging.getLogger('payment-simulator')
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if environment == 'homologation' else logging.INFO)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger

def load_config(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('generator-config.json'))
    parser.add_argument('--environment', choices=('production', 'homologation'))
    for key in ('count', 'concurrency', 'burst', 'max-attempts'):
        parser.add_argument('--' + key, type=int)
    parser.add_argument('--interval', type=float)
    parser.add_argument('--scenario', choices=SCENARIOS)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        for key in ('environment', 'count', 'concurrency', 'burst', 'max_attempts', 'interval'):
            if getattr(args, key) is not None:
                config[key] = getattr(args, key)
        if args.scenario:
            config['scenarios'] = {args.scenario: 1}
        if config['environment'] not in ('production', 'homologation'):
            raise ValueError('Invalid environment')
        for key in ('count', 'concurrency', 'burst', 'max_attempts'):
            if type(config[key]) is not int or config[key] < (1 if key in ('concurrency', 'max_attempts') else 0):
                raise ValueError('Invalid ' + key)
        for key in ('interval', 'latency_min_ms', 'latency_max_ms'):
            if type(config[key]) not in (int, float) or not math.isfinite(config[key]) or config[key] < 0:
                raise ValueError('Invalid ' + key)
        if config['latency_min_ms'] > config['latency_max_ms']:
            raise ValueError('Minimum latency exceeds maximum')
        weights = config['scenarios']
        if not isinstance(weights, dict) or not weights or set(weights) - set(SCENARIOS):
            raise ValueError('Invalid scenarios')
        if any(type(w) not in (int, float) or not math.isfinite(w) or w < 0 for w in weights.values()) or not 0 < sum(weights.values()) < float('inf'):
            raise ValueError('Invalid scenario weights')
        if weights.get('payment_recovery', 0) and config['max_attempts'] < 2:
            raise ValueError('payment_recovery requires max_attempts >= 2')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return config

async def payment_flow(config, logger, scenario, run_id):
    request_id = str(uuid4())
    started = time.perf_counter()
    service = 'api-pagamentos'

    def emit(level, event, message, status, **fields):
        logger.log(getattr(logging, level), message, extra={'payload': dict(service=service, environment=config['environment'], request_id=request_id, run_id=run_id, scenario=scenario, event=event, status=status, **fields)})

    def duration(since):
        return round((time.perf_counter() - since) * 1000, 3)

    async def pause():
        await asyncio.sleep(random.uniform(config['latency_min_ms'], config['latency_max_ms']) / 1000)

    def finish(status, outcome, **fields):
        nonlocal service
        service = 'api-pagamentos'
        emit('INFO' if status == 'success' else 'ERROR', 'flow.finished', 'Fluxo de pagamento encerrado', status, outcome=outcome, duration_ms=duration(started), **fields)

    emit('INFO', 'flow.started', 'Fluxo de pagamento iniciado', 'started')
    payment_started = time.perf_counter()
    emit('INFO', 'payment.started', 'Pagamento iniciado', 'started')
    emit('DEBUG', 'payment.validation.started', 'Validacao tecnica iniciada', 'started')
    await pause()
    emit('INFO', 'payment.validated', 'Pagamento validado', 'success')
    for attempt in range(1, config['max_attempts'] + 1):
        attempt_started = time.perf_counter()
        emit('INFO', 'payment.attempt.started', 'Processamento do pagamento iniciado', 'started', attempt=attempt)
        emit('DEBUG', 'bank.call.started', 'Chamada simulada ao banco iniciada', 'started', attempt=attempt)
        await pause()
        failed = scenario == 'payment_failure' or (scenario == 'payment_recovery' and attempt == 1)
        if not failed:
            emit('INFO', 'payment.attempt.finished', 'Tentativa de processamento concluida', 'success', attempt=attempt, duration_ms=duration(attempt_started))
            emit('INFO', 'payment.finished', 'Pagamento processado apos recuperacao' if attempt > 1 else 'Pagamento processado', 'success', attempt=attempt, duration_ms=duration(payment_started))
            break
        emit('ERROR', 'payment.attempt.finished', 'Falha na comunicacao com o banco', 'error', attempt=attempt, error_type='bank_connection_error', duration_ms=duration(attempt_started))
        if attempt < config['max_attempts']:
            emit('WARNING', 'payment.retry.scheduled', 'Reconexao com o banco agendada', 'retrying', attempt=attempt + 1, error_type='bank_connection_error')
            await pause()
        else:
            emit('ERROR', 'payment.finished', 'Limite de tentativas esgotado; pagamento nao processado', 'error', attempt=attempt, error_type='bank_connection_error', duration_ms=duration(payment_started))
            finish('error', 'payment_failed', error_type='bank_connection_error')
            return
    service = 'api-confirmacao-pagamentos'
    confirmation_started = time.perf_counter()
    emit('INFO', 'confirmation.started', 'Confirmacao do pagamento iniciada', 'started')
    emit('DEBUG', 'confirmation.check', 'Verificando resposta simulada da confirmacao', 'started')
    await pause()
    if scenario == 'confirmation_failure':
        emit('ERROR', 'confirmation.finished', 'Falha na confirmacao do pagamento', 'error', error_type='confirmation_unavailable', duration_ms=duration(confirmation_started))
        finish('error', 'confirmation_pending', error_type='confirmation_unavailable')
        return
    emit('INFO', 'confirmation.finished', 'Pagamento confirmado', 'success', duration_ms=duration(confirmation_started))
    emit('INFO', 'subscription.requested', 'Ativacao da assinatura solicitada', 'started')
    service = 'api-assinaturas'
    subscription_started = time.perf_counter()
    emit('INFO', 'subscription.started', 'Ativacao da assinatura iniciada', 'started')
    emit('DEBUG', 'subscription.check', 'Verificando pre-condicoes simuladas da ativacao', 'started')
    await pause()
    if scenario == 'subscription_failure':
        emit('ERROR', 'subscription.finished', 'Falha ao ativar a assinatura', 'error', error_type='subscription_activation_error', duration_ms=duration(subscription_started))
        finish('error', 'payment_confirmed_subscription_failed', payment_confirmed=True, subscription_activated=False, error_type='subscription_activation_error')
        return
    emit('INFO', 'subscription.finished', 'Assinatura ativada', 'success', duration_ms=duration(subscription_started))
    finish('success', 'completed_after_recovery' if scenario == 'payment_recovery' else 'completed', payment_confirmed=True, subscription_activated=True)

async def simulate(config, logger):
    run_id = str(uuid4())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    pending = set()
    launched = 0
    try:
        while not stop.is_set() and (config['count'] == 0 or launched < config['count']):
            if len(pending) >= config['concurrency']:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            if launched >= config['burst'] and launched > 0 and config['interval']:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=config['interval'])
                    break
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                break
            scenario = random.choices(list(config['scenarios']), weights=list(config['scenarios'].values()))[0]
            pending.add(asyncio.create_task(payment_flow(config, logger, scenario, run_id)))
            launched += 1
    finally:
        if pending:
            await asyncio.gather(*pending)

def main():
    config = load_config()
    try:
        asyncio.run(simulate(config, configure_logger(config['environment'])))
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
