"""
Career OS — Agente WhatsApp
Webhook que recebe mensagens do Z-API, processa com Claude e responde.
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
import json
import os

app = Flask(__name__)

# ── Configurações (via environment variables) ─────────────────────────────
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN   = os.environ["NOTION_TOKEN"]
ZAPI_INSTANCE  = os.environ["ZAPI_INSTANCE"]
ZAPI_TOKEN     = os.environ["ZAPI_TOKEN"]
ZAPI_BASE      = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

NOTION_BASES = {
    "clientes":  "375e130d4df481b9bebbc1ffbb13ccbe",
    "vagas":     "375e130d4df4819d9087edda8b0ee4e1",
    "sessoes":   "375e130d4df481a4abf1d67e638c51fc",
    "outreach":  "375e130d4df481358443d76650f560a0",
    "ativos":    "375e130d4df481d080bfd641cbc80892",
    "relatorio": "375e130d4df4819dbba1dba611c6af1e",
}

SYSTEM_PROMPT = """Você é o agente de IA do Career OS — sistema de recolocação executiva de Luiz Vechiato.

Você recebe mensagens de voz ou texto via WhatsApp e executa ações no sistema Career OS.

CAPACIDADES:
- Registrar insights, ideias e notas no Notion (base Sessões ou Ativos)
- Buscar informações sobre vagas, clientes e sessões
- Criar lembretes e próximos passos
- Responder perguntas sobre o status do Career OS

INSTRUÇÕES:
1. Identifique a INTENÇÃO da mensagem (registrar nota / buscar info / criar item / consultar status)
2. Execute a ação correspondente via ferramentas disponíveis
3. Responda de forma direta e confirmando o que foi feito
4. Use português brasileiro
5. Seja conciso — respostas de WhatsApp devem ser curtas

FORMATO DE RESPOSTA:
Sempre responda com JSON:
{
  "resposta": "texto para enviar no WhatsApp",
  "acao": "registrar_nota | buscar | criar_vaga | nenhuma",
  "dados": { "campos relevantes para executar a ação" }
}"""


def claude(mensagem):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": mensagem}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
        return resp["content"][0]["text"]


def notion_criar_nota(titulo, conteudo, tipo="insight"):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    etapa_map = {
        "insight": "E1 · Diagnóstico",
        "ideia": "E2 · Posicionamento",
        "vaga": "E4 · Radar e Fit",
        "outreach": "E5 · Outreach",
    }
    etapa = etapa_map.get(tipo, "E1 · Diagnóstico")
    payload = {
        "parent": {"database_id": NOTION_BASES["sessoes"]},
        "properties": {
            "Sessão": {"title": [{"text": {"content": f"📱 {titulo}"}}]},
            "Etapa": {"select": {"name": etapa}},
            "Status": {"select": {"name": "Realizada"}},
            "Próximos passos": {"rich_text": [{"text": {"content": conteudo}}]},
        }
    }
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
        return result.get("url", "")


def zapi_enviar(telefone, mensagem):
    headers = {
        "Content-Type": "application/json",
        "client-token": ZAPI_TOKEN,
    }
    payload = {"phone": telefone, "message": mensagem}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Erro Z-API: {e}")
        return None


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print(f"Webhook recebido: {json.dumps(data)[:200]}")

        if data.get("fromMe"):
            return jsonify({"status": "ignored"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        texto = (
            data.get("text", {}).get("message") or
            data.get("body") or
            data.get("message") or ""
        )

        if not texto or not telefone:
            return jsonify({"status": "no_message"}), 200

        print(f"Mensagem de {telefone}: {texto}")

        resposta_raw = claude(texto)

        try:
            resposta_json = json.loads(resposta_raw)
        except:
            resposta_json = {"resposta": resposta_raw, "acao": "nenhuma", "dados": {}}

        resposta_texto = resposta_json.get("resposta", resposta_raw)
        acao = resposta_json.get("acao", "nenhuma")
        dados = resposta_json.get("dados", {})

        url_notion = ""
        if acao == "registrar_nota":
            titulo = dados.get("titulo", texto[:50])
            conteudo = dados.get("conteudo", texto)
            tipo = dados.get("tipo", "insight")
            url_notion = notion_criar_nota(titulo, conteudo, tipo)
            if url_notion:
                resposta_texto += f"\n\n✅ Registrado no Notion."

        zapi_enviar(telefone, resposta_texto)

        return jsonify({"status": "ok", "acao": acao}), 200

    except Exception as e:
        print(f"Erro no webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Career OS Agent online ⚡"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
