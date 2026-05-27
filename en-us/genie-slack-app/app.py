import os, ssl, slack_sdk, json, asyncio, logging, aiohttp, time
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from typing import Dict, List, Optional
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import GenieAPI


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SLACK_APP_TOKEN = os.environ.get('SLACK_APP_TOKEN')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
DATABRICKS_SPACE_ID = os.environ.get('DATABRICKS_SPACE_ID')
DATABRICKS_HOST = os.environ.get('DATABRICKS_HOST')
DATABRICKS_TOKEN = os.environ.get('DATABRICKS_TOKEN')  # Token automático do Databricks Apps
OAUTH_SERVICE_URL = os.environ.get('OAUTH_SERVICE_URL')

logger.info(f"[boot] DATABRICKS_HOST = {DATABRICKS_HOST}")
logger.info(f"[boot] OAUTH_SERVICE_URL = {OAUTH_SERVICE_URL}")
logger.info(f"[boot] DATABRICKS_CLIENT_ID disponível: {bool(os.environ.get('DATABRICKS_CLIENT_ID'))}")
logger.info(f"[boot] DATABRICKS_CLIENT_SECRET disponível: {bool(os.environ.get('DATABRICKS_CLIENT_SECRET'))}")

# Gerenciamento por usuário
USER_CLIENTS: Dict[str, Dict] = {}  # user_email -> {"workspace_client": ..., "genie_api": ..., "last_used": timestamp}
USER_CONVERSATIONS: Dict[str, str] = {}  # user_email -> conversation_id


def map_slack_email_to_databricks_email(slack_email: str) -> str:
    """
    Map a Slack email to a Databricks email for special cases.

    Useful when a user signs into Slack with a personal email but must
    authenticate against Databricks with their corporate email.

    To add specific mappings, edit the `mapping` dict below. Example:
        mapping = {
            "personal@gmail.com": "corporate@example.com",
        }
    """
    mapping: dict[str, str] = {}
    return mapping.get(slack_email, slack_email)


def get_m2m_token() -> Optional[str]:
    """Obter token M2M (Machine-to-Machine) para comunicação entre apps."""
    client_id = os.environ.get('DATABRICKS_CLIENT_ID')
    client_secret = os.environ.get('DATABRICKS_CLIENT_SECRET')
    
    if not client_id or not client_secret:
        logger.error("[m2m] DATABRICKS_CLIENT_ID ou DATABRICKS_CLIENT_SECRET não disponível")
        return None
    
    try:
        import requests
        token_url = f"{DATABRICKS_HOST}/oidc/v1/token"
        
        data = {
            'grant_type': 'client_credentials',
            'scope': 'all-apis'
        }
        
        resp = requests.post(
            token_url,
            data=data,
            auth=(client_id, client_secret),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        
        if resp.status_code == 200:
            token = resp.json().get('access_token')
            logger.info("[m2m] Token M2M obtido com sucesso")
            return token
        else:
            logger.error(f"[m2m] Erro ao obter token: {resp.status_code}, {resp.text}")
            return None
    except Exception as e:
        logger.exception(f"[m2m] Exceção ao obter token M2M: {e}")
        return None


async def get_user_token(user_email: str) -> Optional[Dict]:
    """Busca o token do usuário no oauth-test-app."""
    url = f"{OAUTH_SERVICE_URL}/oauth/token?user_email={user_email}"
    
    logger.info(f"[auth] Chamando {url}")
    
    # Obter token M2M para autenticar a requisição
    m2m_token = get_m2m_token()
    if not m2m_token:
        logger.error("[auth] Não foi possível obter token M2M")
        return None
    
    try:
        loop = asyncio.get_running_loop()
        
        def _make_request():
            import requests
            headers = {
                "Authorization": f"Bearer {m2m_token}",
                "Content-Type": "application/json"
            }
            resp = requests.get(url, headers=headers, timeout=15)
            return resp
        
        resp = await loop.run_in_executor(None, _make_request)
        
        logger.info(f"[auth] Resposta do oauth-test-app: status={resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get("authenticated"):
                logger.info(f"[auth] Token válido obtido para {user_email}")
                return data
        elif resp.status_code == 401:
            data = resp.json()
            logger.info(f"[auth] Usuário {user_email} não autenticado")
            return {"authenticated": False, "login_url": data.get("login_url")}
        else:
            logger.error(f"[auth] Erro: status={resp.status_code}, body={resp.text[:500]}")
            return None
            
    except Exception as e:
        logger.exception(f"[auth] Exceção ao buscar token para {user_email}: {e}")
        return None


async def get_user_workspace_client(user_email: str) -> tuple[Optional[WorkspaceClient], Optional[GenieAPI], Optional[str]]:
    """
    Retorna (workspace_client, genie_api, error_message) para o usuário.
    Se error_message não for None, significa que o usuário precisa autenticar.
    user_email: email do Slack (usado como chave do cache)
    """
    # Verificar se já existe no cache (usando email do Slack)
    if user_email in USER_CLIENTS:
        client_info = USER_CLIENTS[user_email]
        
        # Verificar se o token ainda é válido (com margem de 5 minutos)
        if "created_at" in client_info:
            age = time.time() - client_info["created_at"]
            # Tokens OAuth expiram em 1 hora, recriar se passou mais de 55 minutos
            if age > 3300:  # 55 minutos
                logger.info(f"[client] Token em cache expirado para {user_email}, removendo do cache")
                del USER_CLIENTS[user_email]
                # Continue para criar um novo client
            else:
                # Atualizar timestamp
                client_info["last_used"] = time.time()
                logger.info(f"[client] Usando client em cache para {user_email} (idade: {int(age/60)} minutos)")
                return client_info["workspace_client"], client_info["genie_api"], None
        else:
            # Cache antigo sem timestamp, usar mesmo assim mas adicionar timestamp
            client_info["last_used"] = time.time()
            logger.info(f"[client] Usando client em cache para {user_email} (sem timestamp)")
            return client_info["workspace_client"], client_info["genie_api"], None
    
    # Mapear email do Slack para email do Databricks
    databricks_email = map_slack_email_to_databricks_email(user_email)
    logger.info(f"[client] Mapeando {user_email} -> {databricks_email}")
    
    # Buscar token do usuário (usando email do Databricks)
    token_data = await get_user_token(databricks_email)
    
    if not token_data:
        return None, None, "Erro ao conectar com o serviço de autenticação. Tente novamente."
    
    if not token_data.get("authenticated"):
        login_url = token_data.get("login_url", "URL não disponível")
        error_msg = f"🔐 Você precisa se autenticar primeiro.\n\nClique aqui para autenticar:\n{login_url}\n\n_(Autenticando como: {databricks_email})_"
        return None, None, error_msg
    
    # Criar workspace client com o token do usuário
    try:
        access_token = token_data["access_token"]
        
        # Criar Config explicitamente para evitar usar credenciais OAuth das env vars
        from databricks.sdk.config import Config
        
        config = Config(
            host=DATABRICKS_HOST,
            token=access_token,
            product="genie-slack-app",
            # Forçar uso apenas do token, ignorando outras credenciais
            auth_type="pat"
        )
        
        workspace_client = WorkspaceClient(config=config)
        
        genie_api = GenieAPI(workspace_client.api_client)
        
        # Armazenar no cache (usando email do Slack como chave)
        current_time = time.time()
        USER_CLIENTS[user_email] = {
            "workspace_client": workspace_client,
            "genie_api": genie_api,
            "created_at": current_time,
            "last_used": current_time,
            "databricks_email": databricks_email
        }
        
        logger.info(f"[client] Novo WorkspaceClient criado para {user_email} (Databricks: {databricks_email})")
        return workspace_client, genie_api, None
        
    except Exception as e:
        logger.exception(f"[client] Erro ao criar WorkspaceClient para {user_email}: {e}")
        return None, None, f"Erro ao criar conexão com Databricks: {str(e)}"


def start_slack_client():
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    client = slack_sdk.WebClient(token=SLACK_BOT_TOKEN, ssl=ssl_context)
    return App(client=client, process_before_response=False)

async def ask_genie(question: str, space_id: str, genie_api: GenieAPI, workspace_client: WorkspaceClient, conversation_id: Optional[str] = None) -> tuple[str, str]:
    """Envia pergunta ao Genie usando o API client e workspace client do usuário."""
    try:
        logger.info(f"[genie] Pergunta: {question}, space_id: {space_id}, conversation_id: {conversation_id}")
        
        loop = asyncio.get_running_loop()
        if conversation_id is None:
            logger.info(f"[genie] Iniciando nova conversa...")
            initial_message = await loop.run_in_executor(None, genie_api.start_conversation_and_wait, space_id, question)
            conversation_id = initial_message.conversation_id
            logger.info(f"[genie] Nova conversa criada: {conversation_id}")
        else:
            logger.info(f"[genie] Continuando conversa existente: {conversation_id}")
            initial_message = await loop.run_in_executor(None, genie_api.create_message_and_wait, space_id, conversation_id, question)

        logger.info(f"[genie] Mensagem ID: {initial_message.id}")
        logger.info(f"[genie] Query result presente: {initial_message.query_result is not None}")

        query_result = None
        if initial_message.query_result is not None:
            query_result = await loop.run_in_executor(None, genie_api.get_message_query_result,
                space_id, initial_message.conversation_id, initial_message.id)
            logger.info(f"[genie] Query result obtido: {query_result is not None}")

        message_content = await loop.run_in_executor(None, genie_api.get_message,
            space_id, initial_message.conversation_id, initial_message.id)
        logger.info(f"[genie] Message content: {message_content.content[:100] if message_content.content else 'None'}")
        logger.info(f"[genie] Attachments: {len(message_content.attachments) if message_content.attachments else 0}")

        if query_result and query_result.statement_response:
            logger.info(f"[genie] Obtendo resultados da query...")
            results = await loop.run_in_executor(None, workspace_client.statement_execution.get_statement,
                query_result.statement_response.statement_id)
            
            query_description = ""
            for attachment in message_content.attachments:
                if attachment.query and attachment.query.description:
                    query_description = attachment.query.description
                    break

            logger.info(f"[genie] Retornando dados da query")
            return json.dumps({
                "columns": results.manifest.schema.as_dict(),
                "data": results.result.as_dict(),
                "query_description": query_description
            }), conversation_id

        if message_content.attachments:
            for attachment in message_content.attachments:
                if attachment.text and attachment.text.content:
                    logger.info(f"[genie] Retornando mensagem de attachment")
                    return json.dumps({"message": attachment.text.content}), conversation_id

        logger.info(f"[genie] Retornando message content direto")
        return json.dumps({"message": message_content.content}), conversation_id
    except Exception as e:
        logger.exception(f"[genie] ERRO em ask_genie: {str(e)}")
        return json.dumps({"error": f"Erro ao processar pergunta: {str(e)}"}), conversation_id

def process_query_results(answer_json: Dict) -> str:
    logger.info(f"[process] Processando resposta: keys={list(answer_json.keys())}")
    
    response = ""
    
    # Verificar se há erro
    if "error" in answer_json:
        logger.error(f"[process] Erro na resposta: {answer_json['error']}")
        return f"❌ {answer_json['error']}"
    
    if "query_description" in answer_json and answer_json["query_description"]:
        response += f"## Descrição da Consulta\n\n{answer_json['query_description']}\n\n"

    if "columns" in answer_json and "data" in answer_json:
        response += "## Resultados da Consulta\n\n"
        columns = answer_json["columns"]
        data = answer_json["data"]
        if isinstance(columns, dict) and "columns" in columns:
            header = "| " + " | ".join(col["name"] for col in columns["columns"]) + " |"
            separator = "|" + "|".join(["-----" for _ in columns["columns"]]) + "|"
            response += header + "\n" + separator + "\n"
            for row in data["data_array"]:
                formatted_row = []
                for value, col in zip(row, columns["columns"]):
                    if value is None:
                        formatted_value = "NULL"
                    elif col["type_name"] in ["DECIMAL", "DOUBLE", "FLOAT"]:
                        formatted_value = f"{float(value):,.2f}"
                    elif col["type_name"] in ["INT", "BIGINT", "LONG"]:
                        formatted_value = f"{int(value):,}"
                    else:
                        formatted_value = str(value)
                    formatted_row.append(formatted_value)
                response += "| " + " | ".join(formatted_row) + " |\n"
        else:
            response += f"Unexpected column format: {columns}\n\n"
    elif "message" in answer_json:
        response += f"{answer_json['message']}\n\n"
    else:
        response += "No data available.\n\n"
    
    return response


app = start_slack_client()

@app.event("message")
def handle_dm_events(body, say, logger):
    """
    Esse cara vai pegar DM pro bot.
    Precisamos filtrar pra não cair em tudo que é mensagem de canal.
    """
    event = body.get("event", {})
    # só DM
    if event.get("channel_type") != "im":
        return
    
    user_id = event.get("user")

    # pegar info do usuário
    user_info = app.client.users_info(user=user_id)
    user_email = user_info["user"]["profile"].get("email")
    user_name  = user_info["user"]["profile"].get("real_name")

    logger.info(f"Mensagem recebida de {user_name} ({user_email})")

    text = event.get("text", "").strip()

    # o Slack às vezes manda mensagem "bot_message"
    if event.get("bot_id"):
        return

    try:
        # Obter workspace client e genie api do usuário
        workspace_client, genie_api, error_msg = asyncio.run(
            get_user_workspace_client(user_email)
        )
        
        if error_msg:
            # Usuário não autenticado
            say(error_msg)
            return
        
        # Obter conversation_id do usuário
        conversation_id = USER_CONVERSATIONS.get(user_email)
        
        # Fazer pergunta ao Genie
        answer, new_conversation_id = asyncio.run(
            ask_genie(text, DATABRICKS_SPACE_ID, genie_api, workspace_client, conversation_id)
        )
        
        # Atualizar conversation_id do usuário
        USER_CONVERSATIONS[user_email] = new_conversation_id
        
        answer_json = json.loads(answer)
        response = process_query_results(answer_json)
    except Exception as e:
        logger.error(f"Error processing DM: {str(e)}")
        response = "Não consegui falar com o Genie agora."

    # responder na própria DM
    say(f"<@{user_id}>,\n------------------------------------------------\n{response}\n------------------------------------------------\n")

@app.command("/genie-demo")
def command(ack, say, command):
    ack({"response_type": "in_channel"})

    print("command:")
    print(command)
    
    print(f"Received: {command['text']} from user:<@{command['user_id']}>, channel:{command['channel_id']}, team:{command['team_id']}")

    user_email = app.client.users_info(user=command['user_id'])['user']['profile']['email']
    print(f"User email: {user_email}")

    try:
        # Obter workspace client e genie api do usuário
        workspace_client, genie_api, error_msg = asyncio.run(
            get_user_workspace_client(user_email)
        )
        
        if error_msg:
            # Usuário não autenticado
            say(error_msg)
            return
        
        # Obter conversation_id do usuário
        conversation_id = USER_CONVERSATIONS.get(user_email)
        
        # Fazer pergunta ao Genie
        answer, new_conversation_id = asyncio.run(
            ask_genie(command['text'], DATABRICKS_SPACE_ID, genie_api, workspace_client, conversation_id)
        )
        
        # Atualizar conversation_id do usuário
        USER_CONVERSATIONS[user_email] = new_conversation_id

        answer_json = json.loads(answer)
        print(answer_json)
        response = process_query_results(answer_json)
    except json.JSONDecodeError:
        logger.error(f"Failed to decode response from the server.")
        response = "Erro ao processar resposta do servidor."
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}")
        response = "Não consegui falar com o Genie agora."
    
    output_text = response

    say(f"<@{command['user_id']}>,\n------------------------------------------------\n{output_text}\n------------------------------------------------\n")

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()