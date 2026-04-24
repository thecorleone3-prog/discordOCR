import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

from auto_cargaConOCR import procesar_comprobante_completo

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# ==========================================================
# 🏗 CONFIGURACIÓN MULTI-SERVIDOR (LOCAL EN MEMORIA)
# ==========================================================
CONFIG_GUILDS = {
    1256002493331017800: {  # <-- CROWN
        "CANAL_CARGAS_ID": 1446983648091046032,
        "CANAL_PREMIOS_ID": 1284336904095010826,
        "BACKEND_WEBHOOK": "https://gpt-kommo-bot-production.up.railway.app/webhook-ocr/crown",
        "SHEETS_WEBHOOK_URL": "https://v1-production-9eba.up.railway.app/ocr/crown"
    }
}


def get_config(guild_id: int):
    return CONFIG_GUILDS.get(guild_id)


# ==========================================================
# 🤖 BOT
# ==========================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================================
# 📅 ON READY
# ==========================================================
@bot.event
async def on_ready():
    print(f"✅ Bot online como {bot.user}")


# ==========================================================
# 🔔 EVENTO MENSAJE (PREMIOS)
# ==========================================================
@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if not message.guild:
        return

    config = get_config(message.guild.id)
    if not config:
        return

    # Ignorar canal de cargas
    if message.channel.id == config["CANAL_CARGAS_ID"]:
        return await bot.process_commands(message)

    # Solo webhooks
    if message.webhook_id is None:
        return

    # Canal premios
    if message.channel.id == config["CANAL_PREMIOS_ID"]:
        print(f"🎉 Webhook detectado en premios ({message.guild.name})")

    await bot.process_commands(message)


# ==========================================================
# 💰 LISTENER AUTO-CARGA (OCR)
# ==========================================================
@bot.listen("on_message")
async def listener_autocarga(message: discord.Message):

    if message.webhook_id is None:
        return

    if not message.guild:
        return

    config = get_config(message.guild.id)
    if not config:
        return

    if message.channel.id != config["CANAL_CARGAS_ID"]:
        return

    if not message.attachments:
        return

    async def tarea_autocarga(attachment, msg, config):

        if not any(attachment.filename.lower().endswith(e) for e in ['.png', '.jpg', '.jpeg']):
            return

        await msg.add_reaction("⏳")

        resultado = await procesar_comprobante_completo(
            attachment.url,
            msg.content,
            msg.embeds,
            config   # 🔥 PASAMOS CONFIG DEL SERVIDOR
        )

        try:
            await msg.remove_reaction("⏳", bot.user)
        except:
            pass

        reacciones = {
            "exito": "🤖",
            "duplicado": "🔄",
            "pendiente": "❓",
            "error_ocr": "❌",
            "error_descarga": "⚠️",
            "error_servidor": "🚨",
            "error_critico": "💀"
        }

        await msg.add_reaction(reacciones.get(resultado, "⚠️"))

    for attachment in message.attachments:
        asyncio.create_task(tarea_autocarga(attachment, message, config))


# ==========================================================
# 🚀 EJECUCIÓN
# ==========================================================
if __name__ == "__main__":
    bot.run(TOKEN)