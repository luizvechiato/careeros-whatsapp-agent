"""
Career OS — Agente WhatsApp v5.9
Webhook Z-API → Claude → Make → Asana/Notion/Calendar
v5.9: fix Groq 403 (User-Agent header para bypass Cloudflare WAF)
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_KEY", "")
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN", "")
ZAPI_INSTANCE     = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN        = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.environ.get("ZAPI_CLIENT_TOKEN", "")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
MAKE_WEBHOOK_URL  = os.environ.get("MAKE_WEBHOOK_URL", "")
RENDER_URL        = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")

ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
PROJECTS_REGISTRY_DB = "376e130d4df4810b9d48df2644609585"
sent_message_ids = set()
_projects_cache = {}
_projects_cache_ts = 0

def get_projects():
    global _projects_cache, _projects_cache_ts
    now = time.time()
    if now - _projects_cache_ts < 600 and _projects_cache:
        return _projects_cache
    try:
        headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
        req = urllib.request.Request(f"https://api.notion.com/v1/databases/{PROJECTS_REGISTRY_DB}/query", data=json.dumps({"page_size": 50}).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        projetos = {}
        for page in data.get("results", []):
            props = page["properties"]
            nome = props["Nome Oficial"]["title"][0]["text"]["content"] if props["Nome Oficial"]["title"] else ""
            aliases_raw = props["Aliases"]["rich_text"][0]["text"]["content"] if props["Aliases"]["rich_text"] else ""
            aliases = [a.strip().lower() for a in aliases_raw.split(",")]
            aliases.append(nome.lower())
            status = props["Status"]["select"]["name"] if props["Status"]["select"] else "ativo"
            asana_id = props["Asana ID"]["rich_text"][0]["text"]["content"] if props["Asana ID"]["rich_text"] else ""
            projetos[nome] = {"nome": nome, "aliases": aliases, "status": status, "asana_id": asana_id, "notion_page_id": page["id"]}
        _projects_cache = projetos
        _projects_cache_ts = now
        print(f"Projects cache: {list(projetos.keys())}")
    except Exception as e:
        print(f"Erro projetos: {e}")
    return _projects_cache

def adicionar_projeto_registry(nome, aliases_extra="", descricao=""):
    global _projects_cache_ts
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    payload = {"parent": {"database_id": PROJECTS_REGISTRY_DB}, "properties": {"Nome Oficial": {"title": [{"text": {"content": nome}}]}, "Aliases": {"rich_text": [{"text": {"content": aliases_extra or nome.lower()}}]}, "Status": {"select": {"name": "ideia"}}, "Tipo": {"select": {"name": "projeto"}}, "Descrição": {"rich_text": [{"text": {"content": descricao or f"Criado via WhatsApp"}}]}, "Criado em": {"date": {"start": today}}}}
    req = urllib.request.Request("https://api.notion.com/v1/pages", data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        result = json.loads(r.read())
    _projects_cache_ts = 0
    return result["id"]

def agora_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M:%S")
def hoje_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m/%Y")
def amanha_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3) + timedelta(days=1)).strftime("%d/%m/%Y")

SYSTEM_PROMPT = """Você é o assistente de IA do Career OS, integrado ao WhatsApp de Luiz Vechiato.
Você é inteligente, direto e conversacional. Gerencia projetos, tarefas, agendas, notas e ideias.

PROJETOS CONHECIDOS:
{projetos_lista}

TIPOS DE AÇÃO:
- tarefa → criar no Asana do projeto
- agenda → criar no Google Calendar (com data, hora e participantes)
- nota → registrar no Notion do projeto
- ideia → registrar como Ideia no hub Consultorias Estratégicas
- criar_projeto → criar novo projeto no Asana + Notion com template de governança
- conversa → responder diretamente

REGRAS DE PROJETO:
1. Identifique o projeto pelo nome ou alias
2. Se ação requer projeto e nenhum identificado, defina projetos como [] (sistema vai perguntar)
3. Uma ação pode ter múltiplos projetos: ["Projeto A", "Projeto B"] — cria em duplicidade
4. Se nome de projeto desconhecido for mencionado, use tipo=criar_projeto
5. Ideias sem projeto específico → tipo=ideia, projetos=["Consultorias Estratégicas"]

RESOLUÇÃO DE DATAS (hoje é {hoje}, amanhã é {amanha}):
- Resolva datas relativas para DD/MM/YYYY HH:MM
- Se hora não mencionada, use null

RESPONDA SEMPRE COM JSON:
{"resposta": "texto WhatsApp", "tipo": "tarefa|agenda|nota|ideia|criar_projeto|conversa", "titulo": "título", "detalhes": "detalhes", "projetos": ["Projeto"], "data_agenda": "DD/MM/YYYY HH:MM ou null", "participantes": [], "novo_projeto": "nome ou null"}"""

def montar_system_prompt():
    projetos = get_projects()
    lista = "\n".join([f"- {n} (aliases: {', '.join(i['aliases'][:3])})" for n,i in projetos.items()]) if projetos else "- Consultorias Estratégicas\n- Career OS\n- Caronas Fácil\n- Casamento Laura"
    return SYSTEM_PROMPT.format(projetos_lista=lista, hoje=hoje_br(), amanha=amanha_br())

def claude(mensagem):
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "system": montar_system_prompt(), "messages": [{"role": "user", "content": mensagem}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["content"][0]["text"]

def groq_transcrever(audio_url):
    download_headers = {"client-token": ZAPI_CLIENT_TOKEN, "User-Agent": "Mozilla/5.0 (compatible; CareerOS/1.0)"}
    audio_bytes = None
    last_error = None
    for tentativa in range(3):
        try:
            req = urllib.request.Request(audio_url, headers=download_headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                audio_bytes = r.read()
            if audio_bytes:
                print(f"Audio OK: {len(audio_bytes)} bytes")
                break
        except Exception as e:
            last_error = e
            print(f"Audio tentativa {tentativa+1}/3: {e}")
            if "403" not in str(e): time.sleep(3*(tentativa+1))
    if not audio_bytes:
        raise Exception(f"Audio indisponível: {last_error}")
    boundary = "----CareerOSBoundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.ogg\"\r\nContent-Type: audio/ogg\r\n\r\n").encode() + audio_bytes + (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-large-v3-turbo\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "python-requests/2.31.0",
            "Accept": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("text", "")

def make_enviar(payload):
    req = urllib.request.Request(MAKE_WEBHOOK_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r: return r.read()

def zapi_enviar(telefone, mensagem):
    headers = {"Content-Type": "application/json", "client-token": ZAPI_CLIENT_TOKEN}
    req = urllib.request.Request(f"{ZAPI_BASE}/send-text", data=json.dumps({"phone": telefone, "message": mensagem}).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            msg_id = result.get("zaapId") or result.get("messageId")
            if msg_id: sent_message_ids.add(msg_id)
            return result
    except Exception as e:
        print(f"Erro Z-API: {e}")
        return None

@app.route("/webhook", methods=["POST"])
def webhook():
    recebido_em = agora_br()
    try:
        data = request.get_json(force=True)
        print(f"[{recebido_em}] Webhook: {json.dumps(data)[:300]}")
        msg_id = data.get("messageId") or data.get("id")
        if msg_id and msg_id in sent_message_ids: return jsonify({"status": "ignored_own"}), 200
        if data.get("fromMe") and not data.get("phone"): return jsonify({"status": "ignored_fromMe"}), 200
        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone: return jsonify({"status": "no_phone"}), 200
        texto = ""
        audio_url = (data.get("audio", {}).get("audioUrl") or data.get("audio", {}).get("url") or (data.get("type") == "audio" and data.get("body")))
        if audio_url and isinstance(audio_url, str) and audio_url.startswith("http"):
            print(f"[{recebido_em}] Audio: {audio_url[:80]}")
            try:
                texto = groq_transcrever(audio_url)
                print(f"[{recebido_em}] Transcricao: {texto[:100]}")
            except Exception as e:
                print(f"[{recebido_em}] Erro Groq: {e}")
                zapi_enviar(telefone, "Nao consegui transcrever o audio. Tente enviar como texto.")
                return jsonify({"status": "groq_error"}), 200
        else:
            texto = (data.get("text", {}).get("message") or data.get("body") or data.get("message") or "")
        if not texto: return jsonify({"status": "no_message"}), 200
        print(f"[{recebido_em}] De {telefone}: {texto[:100]}")
        zapi_enviar(telefone, f"Recebi as {recebido_em[11:]}. Processando...")
        resposta_raw = claude(texto)
        try:
            r = json.loads(resposta_raw)
        except:
            r = {"resposta": resposta_raw, "tipo": "conversa", "titulo": "", "detalhes": "", "projetos": [], "data_agenda": None, "participantes": [], "novo_projeto": None}
        resposta_texto = r.get("resposta", resposta_raw)
        tipo = r.get("tipo", "conversa")
        titulo = r.get("titulo", texto[:60])
        detalhes = r.get("detalhes", "")
        projetos = r.get("projetos") or []
        data_agenda = r.get("data_agenda")
        participantes = r.get("participantes") or []
        novo_projeto = r.get("novo_projeto")
        if isinstance(projetos, str): projetos = [projetos]
        ACOES = {"tarefa", "agenda", "nota", "ideia", "criar_projeto"}
        if tipo == "criar_projeto" and novo_projeto:
            try:
                adicionar_projeto_registry(novo_projeto)
                make_enviar({"tipo": "criar_projeto", "text": novo_projeto, "detalhes": detalhes, "fonte": "whatsapp", "recebido_em": recebido_em})
                zapi_enviar(telefone, f"Novo projeto registrado: {novo_projeto}")
            except Exception as e:
                zapi_enviar(telefone, f"Erro ao criar projeto: {e}")
            return jsonify({"status": "ok", "tipo": tipo}), 200
        if tipo in ACOES - {"criar_projeto"} and not projetos and tipo not in {"ideia", "conversa"}:
            projetos_ativos = [n for n, i in get_projects().items() if i["status"] == "ativo"]
            opcoes = "\n".join([f"{i+1}. {p}" for i, p in enumerate(projetos_ativos)])
            zapi_enviar(telefone, f"Para qual projeto?\n\n{opcoes}\n\nResponda com o numero ou nome.")
            return jsonify({"status": "ok", "tipo": "aguardando_projeto"}), 200
        if tipo in ACOES and MAKE_WEBHOOK_URL:
            projetos_alvo = projetos if projetos else ["Consultorias Estrategicas"]
            erros = []
            for proj in projetos_alvo:
                try:
                    make_enviar({"tipo": tipo, "text": titulo, "detalhes": detalhes, "projeto": proj, "data_agenda": data_agenda, "participantes": participantes, "fonte": "whatsapp", "recebido_em": recebido_em})
                    print(f"[{recebido_em}] Make OK: {tipo} -> {proj}")
                except Exception as e:
                    erros.append(proj)
            if not erros:
                info = f"\n{data_agenda}" if data_agenda else ""
                zapi_enviar(telefone, f"Registrado! {titulo} em {' + '.join(projetos_alvo)}{info}")
            else:
                zapi_enviar(telefone, f"Falhou em: {', '.join(erros)}. Tente novamente.")
        else:
            zapi_enviar(telefone, resposta_texto)
        return jsonify({"status": "ok", "tipo": tipo, "recebido_em": recebido_em}), 200
    except Exception as e:
        print(f"[{recebido_em}] Erro: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def self_ping():
    time.sleep(30)
    while True:
        try: urllib.request.urlopen(f"{RENDER_URL}/health", timeout=10)
        except: pass
        time.sleep(240)

threading.Thread(target=self_ping, daemon=True).start()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "5.9", "ts": agora_br()}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Career OS WhatsApp Agent", "version": "5.9"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
