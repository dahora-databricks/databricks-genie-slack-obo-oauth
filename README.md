# Genie Slack App with OAuth Multi-User Authentication

Este projeto implementa uma integração entre Slack e Databricks Genie com autenticação OAuth por usuário, permitindo que cada usuário do Slack interaja com o Genie usando suas próprias credenciais do Databricks.

## Arquitetura

O projeto consiste em dois Databricks Apps:

### 1. oauth-test-app
Serviço de autenticação OAuth que:
- Gerencia o fluxo de autenticação OAuth 2.0 com Databricks
- Mantém tokens de acesso em memória para cada usuário
- Expõe APIs para gerar URLs de login e consultar tokens
- Remove tokens expirados automaticamente (após 1 hora)

### 2. genie-slack-app
Bot do Slack que:
- Recebe mensagens dos usuários via Slack
- Busca credenciais do usuário no oauth-test-app
- Cria um WorkspaceClient por usuário
- Interage com o Databricks Genie usando as credenciais do usuário
- Mantém cache de clientes por usuário

## Fluxo de Autenticação

1. Usuário envia mensagem no Slack
2. Bot verifica se há token válido para o usuário
3. Se não houver, retorna URL de autenticação
4. Usuário clica na URL e autentica no Databricks
5. Token é armazenado no oauth-test-app
6. Bot usa o token para criar WorkspaceClient e consultar Genie

## Comunicação Entre Apps

Os apps se comunicam usando OAuth Client Credentials (M2M):
- `genie-slack-app` obtém token M2M usando `DATABRICKS_CLIENT_ID` e `DATABRICKS_CLIENT_SECRET`
- Usa este token para autenticar chamadas ao `oauth-test-app`
- Credenciais M2M são automaticamente injetadas pelo Databricks Apps

## Configuração

### 1. Criar OAuth App Connection no Databricks

1. Vá em **Settings** > **Developer** > **App connections**
2. Clique em **Create app connection**
3. Configure:
   - **Name**: oauth-test-app
   - **Redirect URLs**: `https://your-oauth-app.databricksapps.com/oauth/callback`
   - **Scopes**: `all-apis`
4. Copie o **Client ID** e **Client Secret**

### 2. Configurar Slack App

1. Crie uma app em https://api.slack.com/apps
2. Habilite **Socket Mode**
3. Configure **OAuth & Permissions**:
   - Bot Token Scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
4. Configure **Event Subscriptions**:
   - Subscribe to bot events: `app_mention`, `message.im`
5. Instale a app no workspace
6. Copie o **Bot Token** (xoxb-...) e **App Token** (xapp-...)

### 3. Criar Space do Genie

1. No Databricks, vá em **Genie**
2. Crie um novo Space
3. Copie o **Space ID** da URL

### 4. Configurar os Apps

Para cada app, copie o arquivo `app.yaml.example` para `app.yaml` e preencha os valores:

#### oauth-test-app/app.yaml
```yaml
env:
  - name: "DATABRICKS_HOST"
    value: "https://your-workspace.cloud.databricks.com"
  - name: "CLIENT_ID"
    value: "seu-oauth-client-id"
  - name: "CLIENT_SECRET"
    value: "seu-oauth-client-secret"
  - name: "OAUTH_REDIRECT_URI"
    value: "https://your-oauth-app.databricksapps.com/oauth/callback"
```

#### genie-slack-app/app.yaml
```yaml
env:
  - name: "SLACK_BOT_TOKEN"
    value: "xoxb-..."
  - name: "SLACK_APP_TOKEN"
    value: "xapp-..."
  - name: "DATABRICKS_SPACE_ID"
    value: "seu-space-id"
  - name: "DATABRICKS_HOST"
    value: "https://your-workspace.cloud.databricks.com"
  - name: "OAUTH_SERVICE_URL"
    value: "https://your-oauth-app.databricksapps.com"
```

### 5. Deploy

#### Via Databricks CLI

```bash
# Autenticar
databricks auth login --host https://your-workspace.cloud.databricks.com

# Deploy oauth-test-app
cd oauth-test-app
databricks apps deploy oauth-test-app --source-code-path .

# Deploy genie-slack-app
cd ../genie-slack-app
databricks apps deploy genie-slack-app --source-code-path .
```

#### Via REST API

```bash
# Obter token
TOKEN=$(databricks auth token --host https://your-workspace.cloud.databricks.com)

# Deploy oauth-test-app
cd oauth-test-app
tar -czf app.tar.gz app.py app.yaml requirements.txt
curl -X POST \
  "https://your-workspace.cloud.databricks.com/api/2.0/apps/oauth-test-app/deployments" \
  -H "Authorization: Bearer $TOKEN" \
  -F "source_code_path=@app.tar.gz"

# Deploy genie-slack-app
cd ../genie-slack-app
tar -czf app.tar.gz app.py app.yaml requirements.txt
curl -X POST \
  "https://your-workspace.cloud.databricks.com/api/2.0/apps/genie-slack-app/deployments" \
  -H "Authorization: Bearer $TOKEN" \
  -F "source_code_path=@app.tar.gz"
```

### 6. Configurar Permissões

No Databricks, configure permissões para que os apps possam se comunicar:

1. Vá em **Settings** > **Developer** > **Databricks Apps**
2. Para `oauth-test-app`:
   - Adicione `genie-slack-app` com permissão **CAN_USE**
3. Para `genie-slack-app`:
   - Adicione `oauth-test-app` com permissão **CAN_USE**

## Uso

1. Abra o Slack e envie uma mensagem direta para o bot
2. Se não estiver autenticado, receberá uma URL de login
3. Clique na URL e autentique no Databricks
4. Volte ao Slack e envie sua pergunta
5. O bot responderá usando o Genie com suas credenciais

## Mapeamento de E-mails

O código inclui uma função `map_slack_email_to_databricks_email()` que permite mapear e-mails do Slack para e-mails do Databricks. Exemplo:

```python
def map_slack_email_to_databricks_email(slack_email: str) -> str:
    """Mapeia e-mail do Slack para e-mail do Databricks."""
    mapping = {
        "thvieira@outlook.com": "thiago.vieira@databricks.com",
    }
    return mapping.get(slack_email, slack_email)
```

## Estrutura do Projeto

```
.
├── README.md
├── .gitignore
├── genie-slack-app/
│   ├── app.py              # Bot do Slack
│   ├── app.yaml            # Config com secrets (gitignored)
│   ├── app.yaml.example    # Template de configuração
│   └── requirements.txt
└── oauth-test-app/
    ├── app.py              # Serviço OAuth
    ├── app.yaml            # Config com secrets (gitignored)
    ├── app.yaml.example    # Template de configuração
    └── requirements.txt
```

## Detalhes Técnicos

### Gerenciamento de Tokens
- Tokens OAuth expiram em 1 hora
- Cache de WorkspaceClient é invalidado após 55 minutos
- Tokens expirados são removidos automaticamente

### Segurança
- Autenticação M2M entre apps usando OAuth Client Credentials
- Tokens armazenados apenas em memória (não persistidos)
- Cada usuário tem seu próprio WorkspaceClient isolado

### APIs do oauth-test-app

- `GET /oauth/login?user_email=email@example.com` - Gera URL de login
- `GET /oauth/token?user_email=email@example.com` - Retorna token ou URL de login
- `GET /oauth/callback` - Callback do OAuth (usado pelo Databricks)
- `GET /oauth/status?user_email=email@example.com` - Verifica status de autenticação

## Troubleshooting

### "Erro ao conectar com o serviço de autenticação"
- Verifique se o `OAUTH_SERVICE_URL` está correto
- Verifique se as permissões entre apps estão configuradas
- Verifique logs do `oauth-test-app`

### "Invalid Token"
- Token expirou (válido por 1 hora)
- Envie nova mensagem para obter novo token

### "No data available"
- Verifique se o Space ID está correto
- Verifique se o usuário tem acesso ao Genie Space
- Verifique logs do `genie-slack-app`

## Licença

MIT

