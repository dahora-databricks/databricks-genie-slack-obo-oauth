import os
import json
import urllib.parse
import logging
import asyncio
import aiohttp
import time
from aiohttp import web
from typing import Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oauth-test")

DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST") 
OAUTH_CLIENT_ID = os.environ.get("CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI")

logger.info(f"[boot] DATABRICKS_HOST = {DATABRICKS_HOST}")
logger.info(f"[boot] OAUTH_CLIENT_ID = {OAUTH_CLIENT_ID}")
logger.info(f"[boot] REDIRECT_URI    = {REDIRECT_URI}")

# Armazenamento em memória: user_email -> token info
USER_TOKENS: Dict[str, Dict] = {}

# Limpeza de tokens expirados a cada 10 minutos
TOKEN_CLEANUP_INTERVAL = 600


def build_login_url(user_email: str) -> str:
    """Gera URL de login OAuth SEM state (vamos extrair o email do token depois)."""
    base = f"{DATABRICKS_HOST}/oidc/oauth2/v2.0/authorize"
    
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "all-apis",
        # NÃO usar state - o Databricks Apps adiciona automaticamente
    }
    return base + "?" + urllib.parse.urlencode(params)


def cleanup_expired_tokens():
    """Remove tokens expirados ou com mais de 1 hora do armazenamento."""
    current_time = time.time()
    expired_users = []
    
    for user_email, token_info in USER_TOKENS.items():
        if current_time >= token_info["expires_at"]:
            expired_users.append(user_email)
    
    for user_email in expired_users:
        logger.info(f"[cleanup] Removendo token expirado para {user_email}")
        del USER_TOKENS[user_email]
    
    if expired_users:
        logger.info(f"[cleanup] Removidos {len(expired_users)} tokens expirados")


async def token_cleanup_task():
    """Task assíncrona para limpeza periódica de tokens."""
    while True:
        await asyncio.sleep(TOKEN_CLEANUP_INTERVAL)
        cleanup_expired_tokens()


async def oauth_callback(request: web.Request):
    code = request.query.get("code")
    state = request.query.get("state")

    logger.info(f"[callback] code={code} state={state}")

    if not code:
        return web.Response(text="Faltou ?code=... (esse endpoint é chamado pelo Databricks)", status=400)

    # montar troca de token
    token_url = f"{DATABRICKS_HOST}/oidc/oauth2/v2.0/token"
    
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=data) as resp:
                resp_text = await resp.text()
                logger.info(f"[callback] token resp status={resp.status}")
                logger.info(f"[callback] token resp body={resp_text}")

                if resp.status != 200:
                    # mostra o erro do Databricks no browser também
                    return web.Response(
                        text=f"Erro ao trocar code por token:\nstatus={resp.status}\nbody={resp_text}",
                        status=500,
                    )

                token_json = json.loads(resp_text)
    except Exception as e:
        logger.exception("[callback] exceção trocando code por token")
        return web.Response(text=f"Exceção no callback: {e}", status=500)

    # Extrair email do usuário do token JWT
    access_token = token_json.get("access_token")
    
    # Decodificar JWT para extrair o email (sem verificar assinatura - apenas ler)
    try:
        import base64
        # JWT tem 3 partes separadas por '.' - pegamos a segunda parte (payload)
        payload_part = access_token.split('.')[1]
        # Adicionar padding se necessário
        padding = 4 - len(payload_part) % 4
        if padding != 4:
            payload_part += '=' * padding
        # Decodificar base64
        payload_bytes = base64.urlsafe_b64decode(payload_part)
        payload = json.loads(payload_bytes)
        
        # Extrair email do payload
        user_email = payload.get('sub') or payload.get('email') or payload.get('preferred_username')
        
        if not user_email:
            logger.error(f"[callback] Não foi possível extrair email do token. Payload: {payload}")
            return web.Response(text="Erro: não foi possível identificar o usuário no token", status=500)
        
        logger.info(f"[callback] Email extraído do token: {user_email}")
        
    except Exception as e:
        logger.exception(f"[callback] Erro ao decodificar JWT: {e}")
        return web.Response(text=f"Erro ao processar token: {e}", status=500)
    
    expires_in = token_json.get("expires_in", 3600)  # default 1 hora
    
    # Limitar a 1 hora mesmo que o token dure mais
    expires_in = min(expires_in, 3600)
    
    USER_TOKENS[user_email] = {
        "access_token": access_token,
        "refresh_token": token_json.get("refresh_token"),
        "token_type": token_json.get("token_type", "Bearer"),
        "expires_at": time.time() + expires_in,
        "expires_in": expires_in,
    }
    
    logger.info("===================================")
    logger.info(f"TOKEN ARMAZENADO PARA: {user_email}")
    logger.info(f"TOKEN DE ACESSO (Bearer): {access_token}")
    logger.info(f"EXPIRA EM: {expires_in}s")
    logger.info("TOKEN COMPLETO:")
    logger.info(json.dumps(token_json, indent=2))
    logger.info("===================================")

    return web.Response(text=f"✅ Autenticação bem-sucedida!\n\nUsuário: {user_email}\nToken armazenado e válido por {expires_in} segundos.\n\nVocê pode fechar esta janela e voltar ao Slack.")


async def oauth_login(request: web.Request):
    """Endpoint GET /oauth/login?user_email=xxx - Retorna URL de login."""
    user_email = request.query.get("user_email")
    
    if not user_email:
        return web.json_response(
            {"error": "Parâmetro 'user_email' é obrigatório"},
            status=400
        )
    
    login_url = build_login_url(user_email)
    
    logger.info(f"[login] Gerando URL de login para {user_email}")
    
    return web.json_response({
        "login_url": login_url,
        "user_email": user_email,
        "message": "Acesse a URL para autenticar"
    })


async def oauth_token(request: web.Request):
    """Endpoint GET /oauth/token?user_email=xxx - Retorna token do usuário."""
    user_email = request.query.get("user_email")
    
    if not user_email:
        return web.json_response(
            {"error": "Parâmetro 'user_email' é obrigatório"},
            status=400
        )
    
    # Verificar se usuário tem token
    token_info = USER_TOKENS.get(user_email)
    
    if not token_info:
        # Usuário não autenticado, retornar URL de login
        login_url = build_login_url(user_email)
        logger.info(f"[token] Usuário {user_email} não autenticado, enviando URL de login")
        return web.json_response(
            {
                "authenticated": False,
                "login_url": login_url,
                "message": "Usuário não autenticado. Use a URL de login."
            },
            status=401
        )
    
    # Verificar se token ainda é válido
    if time.time() >= token_info["expires_at"]:
        # Token expirado, remover e retornar URL de login
        del USER_TOKENS[user_email]
        login_url = build_login_url(user_email)
        logger.info(f"[token] Token expirado para {user_email}, enviando URL de login")
        return web.json_response(
            {
                "authenticated": False,
                "login_url": login_url,
                "message": "Token expirado. Use a URL de login para reautenticar."
            },
            status=401
        )
    
    # Token válido
    logger.info(f"[token] Retornando token válido para {user_email}")
    return web.json_response({
        "authenticated": True,
        "access_token": token_info["access_token"],
        "token_type": token_info["token_type"],
        "expires_in": int(token_info["expires_at"] - time.time()),
        "user_email": user_email
    })


async def oauth_status(request: web.Request):
    """Endpoint GET /oauth/status?user_email=xxx - Verifica status de autenticação."""
    user_email = request.query.get("user_email")
    
    if not user_email:
        return web.json_response(
            {"error": "Parâmetro 'user_email' é obrigatório"},
            status=400
        )
    
    token_info = USER_TOKENS.get(user_email)
    
    if not token_info:
        return web.json_response({
            "authenticated": False,
            "user_email": user_email,
            "message": "Usuário não autenticado"
        })
    
    # Verificar expiração
    time_remaining = int(token_info["expires_at"] - time.time())
    if time_remaining <= 0:
        del USER_TOKENS[user_email]
        return web.json_response({
            "authenticated": False,
            "user_email": user_email,
            "message": "Token expirado"
        })
    
    return web.json_response({
        "authenticated": True,
        "user_email": user_email,
        "expires_in": time_remaining,
        "message": "Usuário autenticado"
    })


async def main():
    app = web.Application()
    
    # Adicionar rotas
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/oauth/login", oauth_login)
    app.router.add_get("/oauth/token", oauth_token)
    app.router.add_get("/oauth/status", oauth_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()

    logger.info("====================================================================")
    logger.info("OAuth Test App iniciado com sucesso!")
    logger.info("====================================================================")
    logger.info("Endpoints disponíveis:")
    logger.info("  GET /oauth/login?user_email=xxx   - Gera URL de login")
    logger.info("  GET /oauth/token?user_email=xxx   - Retorna token do usuário")
    logger.info("  GET /oauth/status?user_email=xxx  - Verifica status de autenticação")
    logger.info("  GET /oauth/callback               - Callback do OAuth (usado pelo Databricks)")
    logger.info("====================================================================")

    # Iniciar task de limpeza de tokens
    asyncio.create_task(token_cleanup_task())
    logger.info("[cleanup] Task de limpeza de tokens iniciada (intervalo: 10 minutos)")

    # keep alive
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
