# Registro de Memória e Pós-Mortem de Erros Recentes (Easypanel & Docker)

## 📌 Histórico de Erros, Causas e Regras de Prevenção

### 1. Incompatibilidade Interna de Pacotes Python (`ImportError: eval_type_backport`)
- **Erro Ocorrido:** `ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'`
- **Causa-Raiz:** O pacote `mcp` importava o método `eval_type_backport`, disponível apenas no `pydantic >= 2.10.0`. O arquivo `pyproject.toml` permitia `pydantic ^2.6.0`, fazendo o `poetry install` dentro do Docker de produção baixar uma versão antiga incompatível do `pydantic`.
- **Regra de Prevenção:** Para qualquer projeto Python que utilize o SDK `mcp`, fixar obrigatoriamente a dependência `pydantic = "^2.10.0"` no `pyproject.toml`.

---

### 2. Divergência de Nomes de Hosts Internos no Docker Network (Erro 502 Bad Gateway)
- **Erro Ocorrido:** `502 Bad Gateway` retornado pelo Traefik/Easypanel.
- **Causa-Raiz:** 
  1. O arquivo `docker-compose.yml` definia os nomes dos serviços como `api` e `redis`.
  2. A interface do Easypanel configurava o roteamento para os hosts internos `whatsapp_general_github` e `whatsapp_general_redis`. Como os nomes não batiam, o DNS interno do Docker não resolvia o IP dos containers.
- **Regra de Prevenção:** No `docker-compose.yml`, os nomes dos serviços devem corresponder rigorosamente aos hosts esperados pelo Easypanel:
  - Serviço API: `whatsapp_general_github`
  - Serviço Redis: `whatsapp_general_redis`
  - `REDIS_URL`: `redis://whatsapp_general_redis:6379`

---

### 3. Validação em Container Limpo Antes do Merge para Main
- **Erro Ocorrido:** Falhas de inicialização ocorriam apenas em produção após o push para a branch `main`.
- **Causa-Raiz:** Testes locais rodando via `pytest` no ambiente host Python 3.12 utilizavam dependências previamente instaladas, mascarando inconsistências do ambiente limpo Docker Python 3.10.
- **Regra de Prevenção:** Antes de mesclar qualquer branch para `main`, executar o build limpo do container localmente via `docker build -t test_app .` e validar a importação dos módulos via `docker run --rm test_app python3 -c "import main"`.

---

### 5. `poetry.lock` Ignorado no `.gitignore` Causal de Cache Retido no Docker Build
- **Erro Ocorrido:** A alteração da versão do Pydantic no `pyproject.toml` não era aplicada no Easypanel, mantendo o erro `ImportError` mesmo após o push.
- **Causa-Raiz:** O arquivo `.gitignore` continha a regra `poetry.lock`. O Dockerfile copiava `COPY pyproject.toml poetry.lock* ./` e executava `RUN poetry install`. Como o `poetry.lock` não existia no repositório Git, o Docker reutilizava a camada de cache antiga com as dependências desatualizadas.
- **Regra de Prevenção:** Nunca adicionar `poetry.lock` no `.gitignore` em projetos com deploy via Dockerfile. O `poetry.lock` deve estar sempre versionado para garantir reprodutibilidade exata dos builds.

