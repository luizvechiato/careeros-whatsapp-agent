"""
Career OS – Agente WhatsApp v5.15
Webhook Z-API → Claude → Make → Asana/Notion/Calendar
v5.15:
  - extrair_json mais robusto (extrai JSON mesmo com texto antes/depois)
  - câmbio/clima pré-buscados antes de chamar Claude (não depende de tool_use)
  - PTAX BCB para cotação histórica ("ontem", "fechamento")
  - proteção extra contra resposta_texto com JSON vazado
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
from urllib.parse import quote
import json
import re
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
SERPER_API_KEY    = os.environ.get("SERPER_API_KEY", "")
RENDER_URL        = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")

ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
PROJECTS_REGISTRY_DB = "376e130d4df4810b9d48df2644609585"
sent_message_ids = set()
processed_message_ids = set()
pending_context = {}
conversation_history = {}
CONV_TTL = 1800
CONV_MAX = 10
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
    payload = {"parent": {"database_id": PROJECTS_REGISTRY_DB}, "properties": {"Nome Oficial": {"title": [{"text": {"content": nome}}]}, "Aliases": {"rich_text": [{"text": {"content": aliases_extra or nome.lower()}}]}, "Status": {"select": {"name": "ideia"}}, "Tipo": {"select": {"name": "projeto"}}, "Descricao": {"rich_text": [{"text": {"content": descricao or "Criado via WhatsApp"}}]}, "Criado em": {"date": {"start": today}}}}
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
def ontem_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3) - timedelta(days=1)).strftime("%d/%m/%Y")

# ─── BUSCA WEB ────────────────────────────────────────────────────────────────

WEATHER_TERMS  = ["temperatura", "clima", "tempo", "chuva", "previsão", "frio", "calor", "graus", "celsius", "weather", "quente", "faz frio"]
CURRENCY_TERMS = ["dólar", "dollar", "usd", "euro", "eur", "bitcoin", "btc", "cotação", "câmbio", "cambio", "libra", "gbp", "yuan", "cny", "cotacao", "dolar", "moeda"]
HISTORICAL_TERMS = ["ontem", "yesterday", "fechamento", "fechou", "dia anterior", "ptax", "anterior", "semana passada"]
CITIES_MAP = {
    "são paulo": "Sao+Paulo", "sp capital": "Sao+Paulo", "sao paulo": "Sao+Paulo",
    "rio de janeiro": "Rio+de+Janeiro", "rio": "Rio+de+Janeiro",
    "belo horizonte": "Belo+Horizonte", "bh": "Belo+Horizonte",
    "brasília": "Brasilia", "brasilia": "Brasilia",
    "curitiba": "Curitiba", "fortaleza": "Fortaleza", "salvador": "Salvador",
    "manaus": "Manaus", "porto alegre": "Porto+Alegre", "recife": "Recife",
    "campinas": "Campinas", "guarulhos": "Guarulhos",
}

def buscar_clima(query_lower):
    city = "Sao+Paulo"
    for k, v in CITIES_MAP.items():
        if k in query_lower:
            city = v
            break
    try:
        req = urllib.request.Request(
            f"https://wttr.in/{city}?format=j1",
            headers={"User-Agent": "curl/7.64.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        cur = data["current_condition"][0]
        temp = cur["temp_C"]
        feels = cur["FeelsLikeC"]
        desc = cur["weatherDesc"][0]["value"]
        humidity = cur["humidity"]
        city_name = city.replace("+", " ")
        return f"🌡️ {city_name}: {temp}°C (sensação {feels}°C)\n☁️ {desc} | Umidade: {humidity}%"
    except Exception as e:
        return f"Não consegui obter dados de clima: {e}"

def buscar_ptax_ontem(moeda="USD"):
    """Cotação PTAX oficial do Banco Central do Brasil para o dia anterior."""
    try:
        dt_ontem = (datetime.now(timezone.utc) - timedelta(hours=3) - timedelta(days=1))
        # Pula fim de semana (PTAX só em dias úteis)
        dias_atras = 1
        while dt_ontem.weekday() >= 5:  # Sab=5, Dom=6
            dias_atras += 1
            dt_ontem = (datetime.now(timezone.utc) - timedelta(hours=3) - timedelta(days=dias_atras))
        data_ptax = dt_ontem.strftime("%m-%d-%Y")
        url = (f"https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
               f"CotacaoMoedaDia(moeda=@moeda,dataCotacao=@dataCotacao)"
               f"?@moeda='{moeda}'&@dataCotacao='{data_ptax}'"
               f"&$format=json&$select=cotacaoCompra,cotacaoVenda,dataHoraCotacao")
        req = urllib.request.Request(url, headers={"User-Agent": "python-requests/2.31.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        values = data.get("value", [])
        if values:
            last = values[-1]
            compra = last["cotacaoCompra"]
            venda = last["cotacaoVenda"]
            dt_str = last["dataHoraCotacao"][:10]
            nome_moeda = {"USD": "Dólar (PTAX BCB)", "EUR": "Euro (PTAX BCB)", "GBP": "Libra (PTAX BCB)"}.get(moeda, moeda)
            return f"💱 {nome_moeda} — fechamento {dt_str}: Compra R$ {compra:.4f} | Venda R$ {venda:.4f}"
        return None
    except Exception as e:
        print(f"PTAX error ({moeda}): {e}")
        return None

def buscar_cambio(query_lower):
    """Cotações de câmbio: PTAX BCB para histórico, AwesomeAPI para tempo real."""
    pede_historico = any(t in query_lower for t in HISTORICAL_TERMS)
    results = []

    # USD
    if any(t in query_lower for t in ["dólar", "dollar", "usd", "dolar"]):
        if pede_historico:
            ptax = buscar_ptax_ontem("USD")
            if ptax:
                results.append(ptax)
        if not results:
            # Tempo real via AwesomeAPI
            try:
                req = urllib.request.Request(
                    "https://economia.awesomeapi.com.br/json/last/USD-BRL",
                    headers={"User-Agent": "python-requests/2.31.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                v = data.get("USDBRL", {})
                bid = float(v.get("bid", 0))
                ask = float(v.get("ask", 0))
                pct = float(v.get("pctChange", 0))
                arrow = "↑" if pct >= 0 else "↓"
                results.append(f"💱 Dólar: R$ {bid:.2f} (compra) / R$ {ask:.2f} (venda) {arrow}{abs(pct):.2f}% hoje")
            except Exception as e:
                results.append(f"Dólar indisponível ({e})")

    # EUR
    if any(t in query_lower for t in ["euro", "eur"]):
        if pede_historico:
            ptax = buscar_ptax_ontem("EUR")
            if ptax: results.append(ptax)
        if not any("Euro" in r for r in results):
            try:
                req = urllib.request.Request(
                    "https://economia.awesomeapi.com.br/json/last/EUR-BRL",
                    headers={"User-Agent": "python-requests/2.31.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                v = data.get("EURBRL", {})
                bid = float(v.get("bid", 0))
                pct = float(v.get("pctChange", 0))
                arrow = "↑" if pct >= 0 else "↓"
                results.append(f"💱 Euro: R$ {bid:.2f} {arrow}{abs(pct):.2f}% hoje")
            except Exception as e:
                results.append(f"Euro indisponível ({e})")

    # BTC
    if any(t in query_lower for t in ["bitcoin", "btc"]):
        try:
            req = urllib.request.Request(
                "https://economia.awesomeapi.com.br/json/last/BTC-BRL",
                headers={"User-Agent": "python-requests/2.31.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            v = data.get("BTCBRL", {})
            bid = float(v.get("bid", 0))
            pct = float(v.get("pctChange", 0))
            arrow = "↑" if pct >= 0 else "↓"
            results.append(f"₿ Bitcoin: R$ {bid:,.0f} {arrow}{abs(pct):.2f}% hoje")
        except Exception as e:
            results.append(f"Bitcoin indisponível ({e})")

    # GBP
    if any(t in query_lower for t in ["libra", "gbp"]):
        if pede_historico:
            ptax = buscar_ptax_ontem("GBP")
            if ptax: results.append(ptax)
        if not any("Libra" in r for r in results):
            try:
                req = urllib.request.Request(
                    "https://economia.awesomeapi.com.br/json/last/GBP-BRL",
                    headers={"User-Agent": "python-requests/2.31.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                v = data.get("GBPBRL", {})
                bid = float(v.get("bid", 0))
                results.append(f"💱 Libra: R$ {bid:.2f}")
            except Exception as e:
                results.append(f"Libra indisponível ({e})")

    # Câmbio genérico sem moeda específica
    if not results:
        try:
            req = urllib.request.Request(
                "https://economia.awesomeapi.com.br/json/last/USD-BRL,EUR-BRL",
                headers={"User-Agent": "python-requests/2.31.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            for key, v in data.items():
                bid = float(v.get("bid", 0))
                pct = float(v.get("pctChange", 0))
                arrow = "↑" if pct >= 0 else "↓"
                results.append(f"💱 {v.get('name', key)}: R$ {bid:.2f} {arrow}{abs(pct):.2f}%")
        except Exception as e:
            results.append(f"Câmbio indisponível: {e}")

    return "\n".join(results) + f"\nAtualizado: {agora_br()}" if results else "Cotação indisponível."

def buscar_duckduckgo(query):
    try:
        encoded = quote(query)
        req = urllib.request.Request(
            f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1",
            headers={"User-Agent": "python-requests/2.31.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        parts = []
        if data.get("Answer"):
            parts.append(f"Resposta: {data['Answer']}")
        if data.get("AbstractText"):
            parts.append(data["AbstractText"][:500])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                parts.append(f"• {topic['Text'][:200]}")
        return "\n".join(parts) if parts else ""
    except Exception as e:
        return f"Erro DDG: {e}"

def buscar_serper(query):
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "gl": "br", "hl": "pt-br", "num": 5}
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    parts = []
    if data.get("answerBox"):
        ab = data["answerBox"]
        ans = ab.get("answer") or ab.get("snippet") or (ab.get("snippetHighlighted") or [""])[0]
        if ans: parts.append(f"Resposta direta: {ans}")
    if data.get("knowledgeGraph", {}).get("description"):
        parts.append(data["knowledgeGraph"]["description"][:300])
    for item in data.get("organic", [])[:4]:
        if item.get("snippet"):
            parts.append(f"• {item.get('title', '')}: {item['snippet']}")
    return "\n".join(parts) if parts else "Sem resultados relevantes."

def buscar_web(query):
    query_lower = query.lower()
    if any(t in query_lower for t in CURRENCY_TERMS):
        return buscar_cambio(query_lower)
    if any(t in query_lower for t in WEATHER_TERMS):
        return buscar_clima(query_lower)
    if SERPER_API_KEY:
        try:
            return buscar_serper(query)
        except Exception as e:
            print(f"Serper error: {e}")
    result = buscar_duckduckgo(query)
    return result if result else "Não encontrei informações suficientes sobre isso."

# ─── PRÉ-BUSCA DE CONTEXTO ─────────────────────────────────────────────────

def prefetch_contexto(texto_lower):
    """
    Busca dados em tempo real ANTES de chamar Claude.
    Injeta no contexto para que Claude use dados reais mesmo sem tool_use.
    """
    if any(t in texto_lower for t in CURRENCY_TERMS):
        try:
            dados = buscar_cambio(texto_lower)
            print(f"Pre-fetch câmbio: {dados[:100]}")
            return f"\n\n[DADOS REAIS EM TEMPO REAL — USE ESTES NA RESPOSTA:\n{dados}]"
        except Exception as e:
            print(f"Pre-fetch câmbio error: {e}")
    elif any(t in texto_lower for t in WEATHER_TERMS):
        try:
            dados = buscar_clima(texto_lower)
            print(f"Pre-fetch clima: {dados[:100]}")
            return f"\n\n[DADOS REAIS EM TEMPO REAL — USE ESTES NA RESPOSTA:\n{dados}]"
        except Exception as e:
            print(f"Pre-fetch clima error: {e}")
    return ""

# ─── TOOLS DEFINITION ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "buscar_web",
        "description": "Busca na web informações atuais: cotações de câmbio (dólar, euro, bitcoin), clima, notícias, preços, eventos. Use SEMPRE que a pergunta precisar de dados em tempo real.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termos de busca. Ex: 'cotação dólar hoje', 'temperatura São Paulo', 'notícias IA'"
                }
            },
            "required": ["query"]
        }
    }
]

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o assistente de IA do Luiz Vechiato no WhatsApp, integrado ao Career OS.

Você é inteligente, versátil e útil. Responda QUALQUER pergunta com inteligência e precisão.

REGRAS ABSOLUTAS:
1. NUNCA sugira links ou URLs para o usuário consultar. Ele não precisa de links — precisa da RESPOSTA.
2. Se a mensagem contiver [DADOS REAIS EM TEMPO REAL], USE esses dados diretamente na resposta. Não diga que não tem acesso.
3. SEMPRE sintetize a resposta com os dados reais disponíveis no contexto.
4. Para informações em tempo real (clima, cotações, notícias): use buscar_web OU dados já injetados no contexto.
5. Para gerenciamento de projetos, tarefas, agenda, notas: classifique e registre.
6. Para perguntas gerais e análises: responda diretamente com conhecimento.

PROJETOS CONHECIDOS:
{projetos_lista}

TIPOS DE AÇÃO (escolha o mais adequado):
- tarefa: criar no Asana do projeto
- agenda: criar no Google Calendar (com data, hora, participantes)
- nota: registrar no Notion do projeto
- ideia: registrar como Ideia no hub Consultorias Estratégicas
- criar_projeto: criar novo projeto no Asana + Notion
- conversa: responder diretamente (perguntas, dúvidas, análises, buscas, bate-papo)

REGRAS DE PROJETO:
1. Identifique o projeto pelo nome ou alias
2. Se ação requer projeto mas nenhum foi identificado, use projetos=[] (sistema perguntará)
3. Múltiplos projetos: ["Projeto A", "Projeto B"] - cria em ambos
4. Projeto desconhecido mencionado: use tipo=criar_projeto
5. Ideias sem projeto: tipo=ideia, projetos=["Consultorias Estratégicas"]

RESOLUÇÃO DE DATAS (hoje={hoje}, amanhã={amanha}):
- Resolva datas relativas: "amanhã", "sexta", "semana que vem"
- Formato: DD/MM/YYYY HH:MM
- Hora não mencionada: use null

RESPOSTA OBRIGATÓRIA — retorne APENAS o JSON abaixo, sem texto antes, sem texto depois, sem markdown, sem ```:
{{"resposta": "resposta completa e direta para o usuario (sem links, sem sugestão de consultar outros lugares)", "tipo": "tarefa|agenda|nota|ideia|criar_projeto|conversa", "titulo": "titulo curto", "detalhes": "detalhes adicionais", "projetos": ["Nome do Projeto"], "data_agenda": "DD/MM/YYYY HH:MM ou null", "participantes": [], "novo_projeto": "nome ou null"}}

IMPORTANTE: A resposta deve começar com {{ e terminar com }}. Nenhum texto fora do JSON."""

def montar_system_prompt():
    projetos = get_projects()
    lista = "\n".join([f"- {n} (aliases: {', '.join(i['aliases'][:3])})" for n, i in projetos.items()]) if projetos else "- Consultorias Estratégicas\n- Career OS\n- Caronas Fácil\n- Casamento Laura"
    return SYSTEM_PROMPT.format(projetos_lista=lista, hoje=hoje_br(), amanha=amanha_br())

def extrair_json(texto):
    """Extrai JSON mesmo quando há texto antes/depois. v5.15"""
    texto = texto.strip()
    # Remove markdown fences
    texto = re.sub(r'^```(?:json)?\s*\n?', '', texto, flags=re.MULTILINE)
    texto = re.sub(r'\n?```\s*$', '', texto, flags=re.MULTILINE)
    texto = texto.strip()
    # Tenta parse direto
    try:
        return json.loads(texto)
    except Exception:
        pass
    # Encontra primeiro { e último } para extrair JSON aninhado
    start = texto.find('{')
    if start >= 0:
        end = texto.rfind('}')
        if end > start:
            try:
                return json.loads(texto[start:end+1])
            except Exception:
                pass
    raise ValueError(f"JSON não encontrado: {texto[:120]}")

def get_conv_history(telefone):
    conv = conversation_history.get(telefone)
    if not conv:
        return []
    age = time.time() - conv.get("ts", 0)
    if age > CONV_TTL:
        del conversation_history[telefone]
        return []
    return conv.get("messages", [])

def save_conv_history(telefone, user_msg, assistant_msg):
    conv = conversation_history.get(telefone, {"messages": [], "ts": 0})
    messages = conv["messages"]
    messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": assistant_msg})
    if len(messages) > CONV_MAX:
        messages = messages[-CONV_MAX:]
    conversation_history[telefone] = {"messages": messages, "ts": time.time()}
    if len(conversation_history) > 500:
        cutoff = time.time() - CONV_TTL
        stale = [p for p, c in conversation_history.items() if c.get("ts", 0) < cutoff]
        for p in stale:
            del conversation_history[p]

def claude(mensagem, historico=None):
    """Claude com tool use para busca web e histórico de conversa."""
    system = montar_system_prompt()
    messages = []
    if historico:
        for msg in historico:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": mensagem})

    for iteration in range(4):
        headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "system": system,
            "tools": TOOLS,
            "messages": messages
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())

        stop_reason = resp.get("stop_reason")
        content = resp.get("content", [])
        print(f"Claude iter {iteration}: stop_reason={stop_reason}, blocks={[b.get('type') for b in content]}")

        if stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": content})
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    print(f"Tool call: {block['name']}({json.dumps(block['input'])[:100]})")
                    if block["name"] == "buscar_web":
                        result = buscar_web(block["input"].get("query", ""))
                    else:
                        result = "Ferramenta desconhecida"
                    print(f"Tool result: {result[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            for block in content:
                if block.get("type") == "text":
                    return block["text"]
            return ""

    return '{"resposta": "Desculpe, erro no processamento.", "tipo": "conversa", "titulo": "", "detalhes": "", "projetos": [], "data_agenda": null, "participantes": [], "novo_projeto": null}'

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
        raise Exception(f"Audio indisponivel: {last_error}")
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

def resolver_projeto(texto):
    projetos_ativos = [(n, i) for n, i in get_projects().items() if i["status"] == "ativo"]
    texto_lower = texto.lower().strip()
    nums = re.findall(r'\b(\d+)\b', texto_lower)
    if nums:
        idx = int(nums[0]) - 1
        if 0 <= idx < len(projetos_ativos):
            return projetos_ativos[idx][0]
    for nome, info in projetos_ativos:
        for alias in info.get("aliases", []):
            if alias and len(alias) >= 3 and (alias in texto_lower or texto_lower in alias):
                return nome
    return None

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

def limpar_resposta_texto(texto):
    """Garante que resposta_texto não contenha JSON vazado."""
    if texto and '{' in texto and '"tipo"' in texto:
        try:
            parsed = extrair_json(texto)
            return parsed.get("resposta", texto)
        except Exception:
            pass
    return texto

@app.route("/webhook", methods=["POST"])
def webhook():
    recebido_em = agora_br()
    try:
        data = request.get_json(force=True)
        print(f"[{recebido_em}] Webhook: {json.dumps(data)[:300]}")
        msg_id = data.get("messageId") or data.get("id")

        if msg_id and msg_id in sent_message_ids:
            return jsonify({"status": "ignored_own"}), 200
        if msg_id and msg_id in processed_message_ids:
            print(f"[{recebido_em}] Webhook duplicado ignorado: {msg_id}")
            return jsonify({"status": "already_processed"}), 200
        if msg_id:
            processed_message_ids.add(msg_id)
            if len(processed_message_ids) > 1000:
                oldest = list(processed_message_ids)[:200]
                for o in oldest: processed_message_ids.discard(o)

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

        # ── PENDING CONTEXT ──
        if telefone in pending_context:
            pending = pending_context[telefone]
            pending_age = time.time() - pending.get("ts", 0)
            if pending_age < 600:
                projeto_selecionado = resolver_projeto(texto)
                if projeto_selecionado:
                    del pending_context[telefone]
                    tipo = pending["tipo"]
                    titulo = pending["titulo"]
                    detalhes = pending["detalhes"]
                    data_agenda = pending["data_agenda"]
                    participantes = pending["participantes"]
                    projetos_alvo = [projeto_selecionado]
                    zapi_enviar(telefone, f"Recebi as {recebido_em[11:]}. Processando...")
                    erros = []
                    if MAKE_WEBHOOK_URL:
                        for proj in projetos_alvo:
                            try:
                                make_enviar({"tipo": tipo, "text": titulo, "detalhes": detalhes, "projeto": proj, "data_agenda": data_agenda, "participantes": participantes, "fonte": "whatsapp", "recebido_em": recebido_em})
                                print(f"[{recebido_em}] Make OK (pending): {tipo} -> {proj}")
                            except Exception as e:
                                erros.append(proj)
                    if not erros:
                        info = f"\nData: {data_agenda}" if data_agenda else ""
                        info += f"\nParticipantes: {', '.join(participantes)}" if participantes else ""
                        zapi_enviar(telefone, f"{tipo.capitalize()} registrada!\n{titulo}\nProjeto: {projeto_selecionado}{info}\n{recebido_em}")
                    else:
                        zapi_enviar(telefone, f"Falhou em: {', '.join(erros)}. Tente novamente.")
                    return jsonify({"status": "ok", "tipo": tipo, "recebido_em": recebido_em}), 200
                else:
                    projetos_ativos = [n for n, i in get_projects().items() if i["status"] == "ativo"]
                    opcoes = "\n".join([f"{i+1}. {p}" for i, p in enumerate(projetos_ativos)])
                    zapi_enviar(telefone, f"Não identifiquei o projeto. Responda com o número ou nome:\n\n{opcoes}")
                    return jsonify({"status": "ok", "tipo": "aguardando_projeto"}), 200
            else:
                del pending_context[telefone]

        zapi_enviar(telefone, f"Recebi as {recebido_em[11:]}. Processando...")

        # ── PRÉ-BUSCA: injeta dados reais no contexto ──
        contexto_extra = prefetch_contexto(texto.lower())
        texto_com_contexto = texto + contexto_extra

        historico = get_conv_history(telefone)
        resposta_raw = claude(texto_com_contexto, historico)
        print(f"[{recebido_em}] Claude raw: {resposta_raw[:300]}")

        try:
            r = extrair_json(resposta_raw)
        except Exception as parse_err:
            print(f"[{recebido_em}] Parse error: {parse_err} | raw: {resposta_raw[:100]}")
            r = {"resposta": resposta_raw, "tipo": "conversa", "titulo": "", "detalhes": "", "projetos": [], "data_agenda": None, "participantes": [], "novo_projeto": None}

        resposta_texto = limpar_resposta_texto(r.get("resposta", resposta_raw))
        tipo = r.get("tipo", "conversa")
        titulo = r.get("titulo", texto[:60])
        detalhes = r.get("detalhes", "")
        projetos = r.get("projetos") or []
        data_agenda = r.get("data_agenda")
        participantes = r.get("participantes") or []
        novo_projeto = r.get("novo_projeto")
        if isinstance(projetos, str): projetos = [projetos]

        if tipo == "conversa":
            save_conv_history(telefone, texto, resposta_texto)

        ACOES = {"tarefa", "agenda", "nota", "ideia", "criar_projeto"}

        if tipo == "criar_projeto" and novo_projeto:
            try:
                adicionar_projeto_registry(novo_projeto)
                make_enviar({"tipo": "criar_projeto", "text": novo_projeto, "detalhes": detalhes, "fonte": "whatsapp", "recebido_em": recebido_em})
                zapi_enviar(telefone, f"Novo projeto registrado: {novo_projeto}\nAdicionado ao Projects Registry no Notion.\nAsana + Notion serão criados com o template de governança.")
            except Exception as e:
                zapi_enviar(telefone, f"Erro ao criar projeto '{novo_projeto}': {e}")
            return jsonify({"status": "ok", "tipo": tipo}), 200

        if tipo in ACOES - {"criar_projeto"} and not projetos and tipo not in {"ideia", "conversa", "agenda"}:
            projetos_ativos = [n for n, i in get_projects().items() if i["status"] == "ativo"]
            opcoes = "\n".join([f"{i+1}. {p}" for i, p in enumerate(projetos_ativos)])
            pending_context[telefone] = {
                "tipo": tipo, "titulo": titulo, "detalhes": detalhes,
                "data_agenda": data_agenda, "participantes": participantes, "ts": time.time()
            }
            zapi_enviar(telefone, f"Para qual projeto devo registrar?\n\n{opcoes}\n\nResponda com o número ou nome.")
            return jsonify({"status": "ok", "tipo": "aguardando_projeto"}), 200

        if tipo in ACOES and MAKE_WEBHOOK_URL:
            projetos_alvo = projetos if projetos else ["Consultorias Estratégicas"]
            erros = []
            for proj in projetos_alvo:
                try:
                    make_enviar({"tipo": tipo, "text": titulo, "detalhes": detalhes, "projeto": proj, "data_agenda": data_agenda, "participantes": participantes, "fonte": "whatsapp", "recebido_em": recebido_em})
                    print(f"[{recebido_em}] Make OK: {tipo} -> {proj}")
                except Exception as e:
                    erros.append(proj)
            if not erros:
                info = f"\nData: {data_agenda}" if data_agenda else ""
                info += f"\nParticipantes: {', '.join(participantes)}" if participantes else ""
                zapi_enviar(telefone, f"{tipo.capitalize()} registrada!\n{titulo}\nProjeto: {' + '.join(projetos_alvo)}{info}\n{recebido_em}")
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
    return jsonify({"status": "ok", "version": "5.15", "ts": agora_br(), "serper": bool(SERPER_API_KEY)}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Career OS WhatsApp Agent", "version": "5.15"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
