"""
Career OS — Agente WhatsApp v3
Webhook que recebe mensagens do Z-API, processa com Claude e SEMPRE responde.
Suporta: texto, áudio/voz, sticker, imagem (com fallback)
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
import json
import os

app = Flask(__name__)

ANTHROPIC_KEY      = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
ZAPI_INSTANCE      = os.environ["ZAPI_INSTANCE"]
ZAPI_TOKEN         = os.environ["ZAPI_TOKEN"]
ZAPI_CLIENT_TOKEN  = os.environ["ZAPI_CLIENT_TOKEN"]
ZAPI_BASE          = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

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
- Registrar insights, ideias e notas no Notion (base Sessões)
- Informar sobre vagas, clientes e sessões (responda com base no que sabe)
- Criar lembretes e próximos passos
- Responder perguntas sobre o status do Career OS

REGRAS IMPORTANTES:
1. SEMPRE responda — nunca deixe uma mensagem sem retorno
2. Se não entender a intenção, peça esclarecimento de forma direta
3. Se executar uma ação (registrar nota, etc.), confirme explicitamente o que foi feito
4. Se NÃO conseguir executar algo, explique o motivo e sugira o que o usuário pode fazer
5. Use português brasileiro, tom direto e profissional
6. Respostas curtas — WhatsApp não é email

FORMATO DE RESPOSTA (sempre JSON):
{
  "resposta": "texto curto para enviar no WhatsApp",
  "acao": "registrar_nota | nenhuma",
  "dados": { "titulo": "...", "conteudo": "...", "tipo": "insight|ideia|vaga|outreach" }
}

EXEMPLOS DE RESPOSTAS:
- Mensagem vaga → pergunte o que a pessoa quer fazer
- Pedido de registro → confirme com "✅ Registrado: [título]"
- Pergunta sobre vagas → responda com o que sabe ou diga que não tem acesso em tempo real
- Pedido impossível → explique e sugira alternativa"""


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
    with urllib.request.urlopen(req, timeout=15) as r:
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


def zapi_enviar(telefone, mensagem):
    headers = {
        "Content-Type": "application/json",
        "Client-Token": ZAPI_CLIENT_TOKEN,
    }
    payload = {"phone": telefone, "message": mensagem}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Erro Z-API envio: {e}")
        return None


def extrair_mensagem(data):
    """
    Extrai o texto e tipo da mensagem do payload Z-API.
    Retorna (texto, tipo) onde tipo pode ser: texto, audio, imagem, documento, desconhecido
    """
    # Mensagem de texto
    texto = (
        data.get("text", {}).get("message") or
        data.get("body") or
        data.get("message") or ""
    )
    if texto:
        return texto.strip(), "texto"

    # Mensagem de áudio/voz
    if data.get("audio") or data.get("type") in ("AudioMessage", "PTT"):
        duracao = data.get("audio", {}).get("duration", "?")
        return f"[ÁUDIO de {duracao}s — transcrição não disponível]", "audio"

    # Imagem
    if data.get("image") or data.get("type") == "ImageMessage":
        caption = data.get("image", {}).get("caption", "")
        return f"[IMAGEM{': ' + caption if caption else ''}]", "imagem"

    # Documento
    if data.get("document") or data.get("type") == "DocumentMessage":
        nome = data.get("document", {}).get("fileName", "arquivo")
        return f"[DOCUMENTO: {nome}]", "documento"

    # Sticker / reação / outros
    return "", "desconhecido"


@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = "desconhecido"
    try:
        data = request.get_json(force=True)
        print(f"Webhook recebido: {json.dumps(data)[:300]}")

        # Ignorar mensagens enviadas pelo próprio bot
        if data.get("fromMe"):
            return jsonify({"status": "ignored"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        texto, tipo = extrair_mensagem(data)
        print(f"Tipo: {tipo} | De: {telefone} | Texto: {texto[:100]}")

        # ── Mensagem de áudio ────────────────────────────────────────────
        if tipo == "audio":
            resposta = (
                "🎙️ Recebi seu áudio!\n\n"
                "Ainda não consigo processar mensagens de voz diretamente. "
                "Me manda em *texto* e executo na hora — registrar nota, consultar vagas, o que precisar."
            )
            zapi_enviar(telefone, resposta)
            return jsonify({"status": "ok", "acao": "audio_nao_suportado"}), 200

        # ── Imagem ───────────────────────────────────────────────────────
        if tipo == "imagem":
            resposta = (
                "🖼️ Recebi sua imagem!\n\n"
                "Ainda não processo imagens. Se quiser registrar algo sobre ela, "
                "me descreve em texto."
            )
            zapi_enviar(telefone, resposta)
            return jsonify({"status": "ok", "acao": "imagem_nao_suportada"}), 200

        # ── Sem conteúdo processável ─────────────────────────────────────
        if not texto:
            resposta = "Recebi sua mensagem, mas não consegui identificar o conteúdo. Tenta mandar em texto! 👍"
            zapi_enviar(telefone, resposta)
            return jsonify({"status": "ok", "acao": "sem_conteudo"}), 200

        # ── Texto: processa com Claude ───────────────────────────────────
        resposta_raw = claude(texto)
        print(f"Claude respondeu: {resposta_raw[:200]}")

        try:
            resposta_json = json.loads(resposta_raw)
        except Exception:
            resposta_json = {"resposta": resposta_raw, "acao": "nenhuma", "dados": {}}

        resposta_texto = resposta_json.get("resposta", resposta_raw)
        acao = resposta_json.get("acao", "nenhuma")
        dados = resposta_json.get("dados", {})

        # ── Executar ação no Notion ──────────────────────────────────────
        if acao == "registrar_nota":
            titulo = dados.get("titulo", texto[:60])
            conteudo = dados.get("conteudo", texto)
            tipo_nota = dados.get("tipo", "insight")
            try:
                url_notion = notion_criar_nota(titulo, conteudo, tipo_nota)
                if url_notion:
                    resposta_texto += f"\n\n✅ *Registrado no Notion:* _{titulo}_"
                else:
                    resposta_texto += "\n\n⚠️ Tentei registrar no Notion mas não recebi confirmação."
            except Exception as e:
                print(f"Erro Notion: {e}")
                resposta_texto += "\n\n⚠️ Não consegui registrar no Notion agora. Tenta novamente?"

        # ── Enviar resposta ──────────────────────────────────────────────
        resultado_envio = zapi_enviar(telefone, resposta_texto)
        if resultado_envio:
            print(f"Resposta enviada: {resultado_envio.get('messageId','?')}")
        else:
            print("Falha ao enviar resposta via Z-API")

        return jsonify({"status": "ok", "acao": acao}), 200

    except Exception as e:
        print(f"Erro geral no webhook: {e}")
        # Tentar enviar mensagem de erro para o usuário
        if telefone and telefone != "desconhecido":
            try:
                zapi_enviar(telefone, "❌ Ocorreu um erro interno. Tenta novamente em instantes.")
            except Exception:
                pass
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Career OS Agent online ⚡", "version": "3.0"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
