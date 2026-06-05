"""
Career OS — Agente WhatsApp v4.1
Webhook que recebe mensagens do Z-API, processa com Claude e SEMPRE responde.
Regras: feedback obrigatório + CTA em toda resposta.
Suporta: texto, áudio/voz, imagem (com fallback).
v4.1: self-ping thread anti-sleep (sem serviço externo, zero custo)
"""

from flask import Flask, request, jsonify
import urllib.request
import json
import os
import threading
import time

app = Flask(__name__)

ANTHROPIC_KEY      = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
ZAPI_INSTANCE      = os.environ["ZAPI_INSTANCE"]
ZAPI_TOKEN         = os.environ["ZAPI_TOKEN"]
ZAPI_CLIENT_TOKEN  = os.environ["ZAPI_CLIENT_TOKEN"]
ZAPI_BASE          = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

RENDER_URL = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")
PING_INTERVAL = 4 * 60  # 4 minutos — Render dorme após 15min sem request

NOTION_BASES = {
    "clientes":  "375e130d4df481b9bebbc1ffbb13ccbe",
    "vagas":     "375e130d4df4819d9087edda8b0ee4e1",
    "sessoes":   "375e130d4df481a4abf1d67e638c51fc",
    "outreach":  "375e130d4df481358443d76650f560a0",
    "ativos":    "375e130d4df481d080bfd641cbc80892",
    "relatorio": "375e130d4df4819dbba1dba611c6af1e",
}

SYSTEM_PROMPT = """Você é o agente de IA do Career OS — sistema de recolocação executiva de Luiz Vechiato.

Você recebe mensagens via WhatsApp e executa ações no sistema Career OS.

CAPACIDADES:
- Registrar insights, ideias e notas no Notion
- Informar sobre vagas, sessões e status do Career OS
- Criar lembretes e próximos passos

══════════════════════════════════════
REGRA OBRIGATÓRIA — TODA RESPOSTA DEVE:
1. Confirmar o que foi feito (ou não foi feito e por quê)
2. Terminar com um CTA claro — uma pergunta ou sugestão de próximo passo
══════════════════════════════════════

EXEMPLOS DE CTA:
- Após registrar nota: "Quer adicionar mais detalhes ou registrar outra coisa?"
- Após responder pergunta: "Posso buscar mais informações ou executar alguma ação?"
- Quando incerto: "Quer que eu registre isso como nota ou prefere outra ação?"
- Quando não conseguiu: "Tenta reformular ou me diz como posso ajudar agora."

REGRAS ADICIONAIS:
- NUNCA deixe a conversa sem próximo passo claro
- Se a intenção não ficou clara, pergunte de forma direta e objetiva
- Confirmações devem ser explícitas: "✅ Feito: [o que foi feito]"
- Erros devem ser claros: "❌ Não consegui: [motivo] — [alternativa]"
- Use português brasileiro, tom direto e profissional
- Respostas curtas — WhatsApp não é email

FORMATO DE RESPOSTA (sempre JSON válido):
{
  "resposta": "mensagem para WhatsApp com confirmação + CTA no final",
  "acao": "registrar_nota | nenhuma",
  "dados": { "titulo": "...", "conteudo": "...", "tipo": "insight|ideia|vaga|outreach" }
}"""


# ── Self-ping anti-sleep ─────────────────────────────────────────────────────

def self_ping_loop():
    """Pinga o próprio /health a cada 4 min para evitar sleep do Render free tier."""
    # Espera 60s para o servidor subir antes do primeiro ping
    time.sleep(60)
    while True:
        try:
            req = urllib.request.Request(f"{RENDER_URL}/health")
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"🔁 Self-ping OK — {r.status}")
        except Exception as e:
            print(f"⚠️  Self-ping falhou: {e}")
        time.sleep(PING_INTERVAL)

def iniciar_self_ping():
    t = threading.Thread(target=self_ping_loop, daemon=True)
    t.start()
    print(f"🔁 Self-ping iniciado (intervalo: {PING_INTERVAL//60}min)")


# ── Claude ───────────────────────────────────────────────────────────────────

def claude(mensagem):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": mensagem}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["content"][0]["text"]


# ── Notion ───────────────────────────────────────────────────────────────────

def notion_criar_nota(titulo, conteudo, tipo="insight"):
    etapa_map = {
        "insight": "E1 · Diagnóstico",
        "ideia": "E2 · Posicionamento",
        "vaga": "E4 · Radar e Fit",
        "outreach": "E5 · Outreach",
    }
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": NOTION_BASES["sessoes"]},
        "properties": {
            "Sessão": {"title": [{"text": {"content": f"📱 {titulo}"}}]},
            "Etapa": {"select": {"name": etapa_map.get(tipo, "E1 · Diagnóstico")}},
            "Status": {"select": {"name": "Realizada"}},
            "Próximos passos": {"rich_text": [{"text": {"content": conteudo}}]},
        }
    }
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("url", "")


# ── Z-API ────────────────────────────────────────────────────────────────────

def zapi_enviar(telefone, mensagem):
    headers = {"Content-Type": "application/json", "Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": telefone, "message": mensagem}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            print(f"✅ Enviado: {result.get('messageId','?')}")
            return result
    except Exception as e:
        print(f"❌ Erro Z-API envio: {e}")
        return None


# ── Parsing de mensagem ──────────────────────────────────────────────────────

def extrair_mensagem(data):
    texto = (
        data.get("text", {}).get("message") or
        data.get("body") or
        data.get("message") or ""
    )
    if texto:
        return texto.strip(), "texto"
    if data.get("audio") or data.get("type") in ("AudioMessage", "PTT"):
        duracao = data.get("audio", {}).get("duration", "?")
        return f"[ÁUDIO {duracao}s]", "audio"
    if data.get("image"):
        caption = data.get("image", {}).get("caption", "")
        return f"[IMAGEM{': ' + caption if caption else ''}]", "imagem"
    if data.get("document"):
        nome = data.get("document", {}).get("fileName", "arquivo")
        return f"[DOCUMENTO: {nome}]", "documento"
    return "", "desconhecido"


# ── Rotas ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = ""
    try:
        data = request.get_json(force=True)
        print(f"📩 Recebido: {json.dumps(data)[:300]}")

        if data.get("fromMe"):
            return jsonify({"status": "ignored"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        texto, tipo = extrair_mensagem(data)
        print(f"Tipo={tipo} | De={telefone} | '{texto[:80]}'")

        # ── Áudio ────────────────────────────────────────────────────────
        if tipo == "audio":
            zapi_enviar(telefone,
                "🎙️ *Recebi seu áudio!*\n\n"
                "Ainda não processo voz diretamente. "
                "Me manda em texto e executo na hora.\n\n"
                "_O que você queria registrar ou perguntar?_"
            )
            return jsonify({"status": "ok", "acao": "audio_sem_suporte"}), 200

        # ── Imagem ───────────────────────────────────────────────────────
        if tipo == "imagem":
            zapi_enviar(telefone,
                "🖼️ *Recebi sua imagem!*\n\n"
                "Ainda não processo imagens. "
                "Descreve em texto o que quer registrar?\n\n"
                "_Me manda o contexto e registro no Notion._"
            )
            return jsonify({"status": "ok", "acao": "imagem_sem_suporte"}), 200

        # ── Sem conteúdo ─────────────────────────────────────────────────
        if not texto:
            zapi_enviar(telefone,
                "Recebi sua mensagem mas não identifiquei o conteúdo. 🤔\n\n"
                "_Tenta mandar em texto — o que posso fazer por você?_"
            )
            return jsonify({"status": "ok", "acao": "sem_conteudo"}), 200

        # ── Texto: Claude ────────────────────────────────────────────────
        resposta_raw = claude(texto)
        print(f"🤖 Claude: {resposta_raw[:200]}")

        try:
            rj = json.loads(resposta_raw)
        except Exception:
            rj = {"resposta": resposta_raw, "acao": "nenhuma", "dados": {}}

        resposta_texto = rj.get("resposta", resposta_raw)
        acao = rj.get("acao", "nenhuma")
        dados = rj.get("dados", {})

        # ── Ação Notion ──────────────────────────────────────────────────
        if acao == "registrar_nota":
            titulo = dados.get("titulo", texto[:60])
            conteudo = dados.get("conteudo", texto)
            tipo_nota = dados.get("tipo", "insight")
            try:
                url = notion_criar_nota(titulo, conteudo, tipo_nota)
                sufixo = f"\n\n✅ *Registrado no Notion:* _{titulo}_" if url else \
                         "\n\n⚠️ _Processado, mas não recebi confirmação do Notion._"
                resposta_texto += sufixo
            except Exception as e:
                print(f"Erro Notion: {e}")
                resposta_texto += "\n\n❌ _Não consegui registrar no Notion agora. Tenta novamente?_"

        zapi_enviar(telefone, resposta_texto)
        return jsonify({"status": "ok", "acao": acao}), 200

    except Exception as e:
        print(f"❌ Erro geral: {e}")
        if telefone:
            zapi_enviar(telefone,
                "❌ Ocorreu um erro interno.\n\n"
                "_Tenta novamente em instantes ou reformula a mensagem._"
            )
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Career OS Agent online ⚡", "version": "4.1"}), 200


# ── Startup ──────────────────────────────────────────────────────────────────

iniciar_self_ping()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
