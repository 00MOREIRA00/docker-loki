# docker-loki

Laboratório local com Loki (armazenamento), Alloy (coleta de logs Docker) e
Grafana (consultas LogQL). O próprio laboratório já gera logs para explorar.

## Executar

No Windows, inicie o Docker Desktop no modo **Linux containers**.
Antes de executar, crie um arquivo `.env` local na raiz com
`GRAFANA_ADMIN_USER` e `GRAFANA_ADMIN_PASSWORD`, definindo seus próprios valores.
Use uma senha forte e exclusiva. O `.env` é ignorado pelo Git e não deve ser publicado.
O Compose exige essas variáveis e interrompe a inicialização quando estão ausentes.
Na raiz:

```powershell
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail=50 loki alloy grafana
```

Grafana: http://localhost:3000 (acesse com os valores definidos no `.env`).
Esses valores inicializam uma instalação nova; alterar o `.env` não troca a senha
de um usuário já persistido no volume do Grafana.
A fonte de dados **Loki** já é cadastrada automaticamente.

Alloy: http://localhost:12345 (interface de diagnóstico).
Loki: http://localhost:3100/ready (retorna HTTP 200 quando estiver pronto).
O Compose inicia as dependências em ordem, mas elas podem levar alguns segundos
para ficarem prontas; o Alloy tenta reenviar caso o Loki ainda esteja iniciando.

## Consultar os logs

No Grafana, abra **Explore**, selecione **Loki**, use o intervalo dos últimos
15 minutos e execute:

```logql
{job="docker"}
```

Para visualizar apenas os logs do Grafana:

```logql
{service_name="grafana"}
```

Para filtrar mensagens contendo uma palavra:

```logql
{job="docker"} |= "error"
```

O Alloy descobre apenas containers deste projeto Compose (`laboratorio-loki`).
Serviços adicionados a este Compose que escrevam em stdout/stderr serão coletados
automaticamente, sem precisar instalar Alloy dentro da aplicação.

## Dashboard de monitoria

Abra http://localhost:3000/d/laboratorio-logs ou navegue em **Dashboards →
Laboratorio Loki → Monitoria de logs - Pedidos**. O dashboard é carregado dos
arquivos em `grafana/provisioning/dashboards` e atualizado a cada 10 segundos.
Ele mostra total de eventos e erros no período selecionado, contagem por nível
em janela móvel de um minuto e as últimas 100 mensagens.

A fonte **Loki** já é provisionada por `datasources/loki.yaml`. O endereço
`http://loki:3100` é usado pelo Grafana dentro da rede Docker. Na interface,
confira em **Connections → Data sources → Loki → Save & test**.

Para investigar, abra **Explore**, selecione Loki e experimente:

```logql
{service_name="log-generator"} | json | level="ERROR"
```

`service_name` seleciona a aplicação; `| json` extrai os campos do evento;
`level="ERROR"` filtra as falhas. Os painéis de contagem usam `count_over_time`
para transformar linhas de log em números. O gráfico extrai somente `level`,
evitando agrupar séries por IDs únicos dos eventos.

Os eventos são simulados e não medem disponibilidade real. Os contadores mostram
zero quando não há logs; isso, sozinho, não distingue ausência de erros de uma
falha na coleta. Alertas e notificações ainda não foram configurados.

O dashboard é mantido em arquivo. Para experimentar alterações na interface,
salve uma cópia com outro nome; alterações permanentes neste dashboard devem
ser feitas no JSON versionado.

## Dashboards por servico

Os tres dashboards sao provisionados na pasta **Laboratorio Loki**:

- [api-pagamentos](http://localhost:3000/d/api-pagamentos)
- [api-confirmacao-pagamentos](http://localhost:3000/d/api-confirmacao-pagamentos)
- [api-assinaturas](http://localhost:3000/d/api-assinaturas)

Cada dashboard mostra eventos, conclusoes bem-sucedidas, falhas da etapa,
linhas ERROR, volume por nivel, duracao media e logs recentes. Pagamentos inclui
retries e desfechos completos do fluxo; os outros mostram falhas por tipo.
Os filtros de ambiente e `request_id` (regex; `.*` para todos) sao preservados
nos links entre os dashboards, assim como o intervalo de tempo.
A janela inicial e de uma hora para permitir investigar logs com o gerador parado.
Sem novos eventos os graficos ficam vazios; isso nao mede disponibilidade.
Os arquivos ficam em `grafana/provisioning/dashboards/json/api-*.json`.

## Simulador de pagamentos distribuido

Python com `logging` da biblioteca padrao, sem dependencias externas. Simula
`api-pagamentos`, `api-confirmacao-pagamentos` e `api-assinaturas` no mesmo processo.
Nao realiza chamadas reais nem registra dados pessoais ou bancarios.

Cada operacao tem um UUID `request_id` preservado entre servicos. `run_id` identifica
a execucao. A saida e JSON Lines em stdout. Os campos `event`, `status`, `outcome`,
`attempt`, `error_type` e `duration_ms` permitem investigar etapas e falhas.
Cada tentativa termina com `payment.attempt.finished`; cada servico tem seu
`*.finished`; toda operacao termina com `flow.finished`, inclusive quando falha.
Duracoes usam relogio monotonico e timestamps representam UTC real.

Configuracao central: `scripts/generator-config.json`, montado no container.
Reinicie o gerador depois de editar para reler o arquivo.

| Campo | Padrao | Efeito |
| --- | --- | --- |
| environment | production | INFO/WARNING/ERROR; homologation habilita DEBUG no logger |
| count | 0 | Total de operacoes; zero executa continuamente |
| concurrency | 10 | Maximo simultaneo; 1 executa sequencialmente |
| burst | 100 | Operacoes iniciais admitidas sem intervalo |
| interval | 5 | Segundos entre novas operacoes depois da rajada |
| max_attempts | 3 | Limite total incluindo a primeira tentativa |
| latency_min_ms / latency_max_ms | 10 / 80 | Latencia aleatoria por etapa |
| scenarios | pesos abaixo | Pesos relativos de sorteio |

A rajada gera centenas de linhas em poucos segundos. Depois entra uma operacao
nova a cada cinco segundos, com varias linhas por operacao. A concorrencia e
limitada e nao existe fila ilimitada de tarefas.

Cenarios e pesos padrao:

- `success` (60): valida, processa, confirma e ativa a assinatura.
- `payment_recovery` (15): primeira tentativa falha; segunda recupera e conclui.
- `payment_failure` (10): esgota tentativas; nao chama confirmacao nem assinatura.
- `confirmation_failure` (8): confirmacao pendente; nao ativa a assinatura.
- `subscription_failure` (7): pagamento confirmado, assinatura falha; fluxo termina com erro.

Pesos nao precisam somar 100; zero desabilita um cenario. Recuperacao exige
`max_attempts >= 2`. Para testar limite de uma tentativa, selecione outro cenario.

```powershell
# Iniciar a rajada e continuar gerando aos poucos.
docker compose up -d log-generator
# Reler configuracao (gera nova rajada).
docker compose restart log-generator
# Parar, concluindo operacoes em andamento no Docker.
docker compose stop log-generator
# Lote finito; count agora conta operacoes, nao linhas.
python scripts/generate_logs.py --count 10 --interval 0
# Sequencial com diagnosticos e cenario especifico.
python scripts/generate_logs.py --count 5 --concurrency 1 --environment homologation --scenario payment_recovery --interval 0
# Salvar JSON Lines UTF-8 no PowerShell.
python scripts/generate_logs.py --count 10 --interval 0 | Set-Content -Encoding UTF8 eventos.jsonl
# Testes de cenarios, correlacao, concorrencia, ambiente, retries e ritmo.
python -m unittest scripts.test_generate_logs -v
```

CLI sobrescreve o JSON; `--config caminho.json` seleciona outro arquivo.
Tambem existem `--burst`, `--interval` e `--max-attempts`.

No Grafana, `service_name="log-generator"` identifica o container, enquanto
`service` e o campo JSON do servico simulado. IDs ficam no conteudo, sem labels
por operacao. Exemplos no Explore:

```logql
{service_name="log-generator"} | json
{service_name="log-generator"} | json | service="api-confirmacao-pagamentos"
{service_name="log-generator"} | json | request_id="COLE-O-UUID-AQUI"
{service_name="log-generator"} | json | event="flow.finished" | status="error"
{service_name="log-generator"} | json | outcome="payment_confirmed_subscription_failed"
```

O dashboard existente continua contando linhas, nao pagamentos unicos. O painel
de erros inclui falhas recuperadas e definitivas. Para contar operacoes, filtre
`event="flow.finished"`; recuperacao termina com status `success`.

## Persistência e encerramento

Os volumes `loki-data`, `alloy-data` e `grafana-data` guardam, respectivamente,
os logs, as posições de leitura e os dados do Grafana. Para parar preservando dados:

```powershell
docker compose down
```

Este ambiente é destinado ao estudo local: portas expostas somente em localhost,
Loki sem autenticação e acesso ao Grafana configurado localmente. O Alloy roda como root para
acessar o socket Docker. A montagem `:ro` não impede operações de escrita pela
API Docker; trate esse acesso como privilegiado. Não há retenção automática
configurada: acompanhe o espaço ocupado pelos logs durante os estudos.

## Referências

- [Tutorial oficial Loki, Alloy e Grafana](https://grafana.com/docs/loki/latest/get-started/quick-start/tutorial/)
- [Coleta Docker no Alloy](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/)
