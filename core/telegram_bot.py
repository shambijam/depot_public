"""
SNIPER_X Telegram Bot Controller
================================
Module de contrôle à distance du bot via Telegram.

Commandes disponibles:
- /status    : Voir le statut du bot (+ état pause)
- /positions : Voir les positions ouvertes
- /balance   : Voir le solde du compte
- /pause     : Mettre en pause le trading (pas de nouveaux trades)
- /resume    : Reprendre le trading
- /stop      : Arrêter le bot complètement
- /close_all : Fermer toutes les positions
- /help      : Afficher l'aide

Date: 04 Février 2026
"""

import threading
import asyncio
import logging
import signal
import os
from datetime import datetime, UTC
from typing import Optional, Dict, Any, Callable
from functools import wraps

try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot non installé. Installez avec: pip install python-telegram-bot")


class TelegramBotController:
    """
    Contrôleur Telegram pour SNIPER_X.
    Permet de contrôler le bot de trading à distance via commandes Telegram.
    """

    def __init__(
        self,
        bot_token: str,
        authorized_chat_ids: list,
        config_manager=None,
        mt5_connector=None,
        logger=None
    ):
        """
        Initialise le contrôleur Telegram.

        Args:
            bot_token: Token du bot Telegram (obtenu via @BotFather)
            authorized_chat_ids: Liste des chat_id autorisés à contrôler le bot
            config_manager: Instance ConfigManager (optionnel)
            mt5_connector: Instance MT5Connector (optionnel)
            logger: Logger (optionnel)
        """
        self.bot_token = bot_token
        self.authorized_chat_ids = [int(cid) for cid in authorized_chat_ids]
        self.config_manager = config_manager
        self.mt5_connector = mt5_connector
        self.logger = logger or logging.getLogger(__name__)

        # État du bot
        self._bot_running = True
        self._stop_callback: Optional[Callable] = None
        self._start_callback: Optional[Callable] = None
        self._pause_callback: Optional[Callable] = None
        self._resume_callback: Optional[Callable] = None
        self._is_paused_callback: Optional[Callable] = None

        # Application Telegram
        self._app: Optional[Application] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if not TELEGRAM_AVAILABLE:
            self.logger.error("❌ python-telegram-bot non disponible!")
            return

        self.logger.info(f"✅ TelegramBotController initialisé | Chat IDs autorisés: {self.authorized_chat_ids}")

    def set_callbacks(
        self,
        stop_callback: Optional[Callable] = None,
        start_callback: Optional[Callable] = None,
        pause_callback: Optional[Callable] = None,
        resume_callback: Optional[Callable] = None,
        is_paused_callback: Optional[Callable] = None
    ):
        """Définit les callbacks pour arrêter/démarrer/pause/resume le bot."""
        self._stop_callback = stop_callback
        self._start_callback = start_callback
        self._pause_callback = pause_callback
        self._resume_callback = resume_callback
        self._is_paused_callback = is_paused_callback

    def _is_authorized(self, chat_id: int) -> bool:
        """Vérifie si le chat_id est autorisé."""
        return chat_id in self.authorized_chat_ids

    def _auth_required(func):
        """Décorateur pour vérifier l'autorisation."""
        @wraps(func)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id
            if not self._is_authorized(chat_id):
                self.logger.warning(f"⚠️ Accès non autorisé depuis chat_id: {chat_id}")
                await update.message.reply_text(
                    "⛔ Accès refusé.\n"
                    f"Votre Chat ID: `{chat_id}`\n"
                    "Contactez l'administrateur pour être autorisé.",
                    parse_mode="Markdown"
                )
                return
            return await func(self, update, context)
        return wrapper

    # ═══════════════════════════════════════════════════════════════
    # COMMANDES TELEGRAM
    # ═══════════════════════════════════════════════════════════════

    @_auth_required
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Affiche l'aide des commandes disponibles."""
        help_text = """
🎯 *SNIPER_X Bot Controller*

*Commandes disponibles:*

📊 *Informations*
/status - Statut du bot
/positions - Positions ouvertes
/balance - Solde du compte
/summary - Résumé complet

⚙️ *Contrôle*
/pause - Mettre en pause le trading
/resume - Reprendre le trading
/stop - Arrêter le bot complètement
/close\\_all - Fermer toutes les positions

❓ *Aide*
/help - Cette aide
/ping - Test de connexion
        """
        await update.message.reply_text(help_text, parse_mode="Markdown")

    @_auth_required
    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Test de connexion."""
        await update.message.reply_text("🏓 Pong! Bot opérationnel.")

    @_auth_required
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Affiche le statut du bot."""
        try:
            status = "🟢 EN COURS" if self._bot_running else "🔴 ARRÊTÉ"

            # Vérifier l'état de pause
            is_paused = self._is_paused_callback() if self._is_paused_callback else False
            trading_status = "⏸️ EN PAUSE" if is_paused else "▶️ ACTIF"

            mt5_status = "❓ Non connecté"
            if self.mt5_connector:
                try:
                    account_info = self.mt5_connector.get_account_info()
                    if account_info:
                        mt5_status = "🟢 Connecté"
                    else:
                        mt5_status = "🔴 Déconnecté"
                except:
                    mt5_status = "🔴 Erreur"

            message = f"""
📊 *Statut SNIPER_X*

🤖 Bot: {status}
📈 Trading: {trading_status}
📡 MT5: {mt5_status}
⏰ Heure: `{datetime.now(UTC).strftime('%H:%M:%S UTC')}`
            """
            await update.message.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            self.logger.error(f"Erreur cmd_status: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Affiche le solde du compte."""
        try:
            if not self.mt5_connector:
                await update.message.reply_text("❌ MT5 non connecté")
                return

            account_info = self.mt5_connector.get_account_info()
            if not account_info:
                await update.message.reply_text("❌ Impossible de récupérer les infos du compte")
                return

            # AccountInfo est un named tuple, pas un dict
            balance = account_info.balance
            equity = account_info.equity
            profit = account_info.profit
            margin = account_info.margin
            margin_free = account_info.margin_free

            profit_emoji = "🟢" if profit >= 0 else "🔴"

            message = f"""
💰 *Solde du Compte*

💵 Balance: `${balance:,.2f}`
📊 Équité: `${equity:,.2f}`
{profit_emoji} Profit: `${profit:,.2f}`
🔒 Marge utilisée: `${margin:,.2f}`
🆓 Marge libre: `${margin_free:,.2f}`
            """
            await update.message.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            self.logger.error(f"Erreur cmd_balance: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Affiche les positions ouvertes."""
        try:
            if not self.mt5_connector:
                await update.message.reply_text("❌ MT5 non connecté")
                return

            positions = self.mt5_connector.get_positions()

            if not positions or len(positions) == 0:
                await update.message.reply_text("📭 Aucune position ouverte")
                return

            message = f"📈 *Positions Ouvertes* ({len(positions)})\n\n"

            for pos in positions[:10]:  # Limiter à 10 positions
                # TradePosition est un named tuple, pas un dict
                symbol = pos.symbol if hasattr(pos, 'symbol') else pos.get("symbol", "?") if isinstance(pos, dict) else "?"
                pos_type_val = pos.type if hasattr(pos, 'type') else pos.get("type", -1) if isinstance(pos, dict) else -1
                pos_type = "🟢 BUY" if pos_type_val == 0 else "🔴 SELL"
                volume = pos.volume if hasattr(pos, 'volume') else pos.get("volume", 0) if isinstance(pos, dict) else 0
                profit = pos.profit if hasattr(pos, 'profit') else pos.get("profit", 0) if isinstance(pos, dict) else 0
                profit_emoji = "+" if profit >= 0 else ""

                message += f"`{symbol}` {pos_type} | Vol: {volume} | P&L: {profit_emoji}${profit:.2f}\n"

            if len(positions) > 10:
                message += f"\n_...et {len(positions) - 10} autres positions_"

            await update.message.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            self.logger.error(f"Erreur cmd_positions: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Affiche un résumé complet."""
        await self.cmd_status(update, context)
        await self.cmd_balance(update, context)
        await self.cmd_positions(update, context)

    @_auth_required
    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Arrête le bot de trading."""
        try:
            await update.message.reply_text("⏳ Arrêt du bot en cours...")

            self._bot_running = False

            if self._stop_callback:
                self._stop_callback()
                await update.message.reply_text("🔴 Bot arrêté avec succès!")
            else:
                # Envoyer signal d'arrêt
                await update.message.reply_text(
                    "⚠️ Callback d'arrêt non défini.\n"
                    "Le bot sera arrêté au prochain cycle."
                )

        except Exception as e:
            self.logger.error(f"Erreur cmd_stop: {e}")
            await update.message.reply_text(f"❌ Erreur lors de l'arrêt: {e}")

    @_auth_required
    async def cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Démarre le bot de trading."""
        try:
            if self._bot_running:
                await update.message.reply_text("ℹ️ Le bot est déjà en cours d'exécution.")
                return

            await update.message.reply_text("⏳ Démarrage du bot en cours...")

            self._bot_running = True

            if self._start_callback:
                self._start_callback()
                await update.message.reply_text("🟢 Bot démarré avec succès!")
            else:
                await update.message.reply_text(
                    "⚠️ Callback de démarrage non défini.\n"
                    "Redémarrez manuellement le bot."
                )

        except Exception as e:
            self.logger.error(f"Erreur cmd_start_bot: {e}")
            await update.message.reply_text(f"❌ Erreur lors du démarrage: {e}")

    @_auth_required
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Met en pause le trading (pas de nouveaux trades)."""
        try:
            # Vérifier si déjà en pause
            if self._is_paused_callback and self._is_paused_callback():
                await update.message.reply_text("ℹ️ Le trading est déjà en pause.")
                return

            if self._pause_callback:
                self._pause_callback()
                await update.message.reply_text(
                    "⏸️ *Trading en PAUSE*\n\n"
                    "• Aucun nouveau trade ne sera ouvert\n"
                    "• Les positions existantes restent ouvertes\n"
                    "• Le bot reste connecté\n\n"
                    "Utilisez /resume pour reprendre.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("⚠️ Callback pause non configuré.")

        except Exception as e:
            self.logger.error(f"Erreur cmd_pause: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reprend le trading après une pause."""
        try:
            # Vérifier si pas en pause
            if self._is_paused_callback and not self._is_paused_callback():
                await update.message.reply_text("ℹ️ Le trading n'est pas en pause.")
                return

            if self._resume_callback:
                self._resume_callback()
                await update.message.reply_text(
                    "▶️ *Trading REPRIS*\n\n"
                    "Le bot reprend le trading normalement.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("⚠️ Callback resume non configuré.")

        except Exception as e:
            self.logger.error(f"Erreur cmd_resume: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ferme toutes les positions ouvertes."""
        try:
            if not self.mt5_connector:
                await update.message.reply_text("❌ MT5 non connecté")
                return

            positions = self.mt5_connector.get_positions()

            if not positions or len(positions) == 0:
                await update.message.reply_text("📭 Aucune position à fermer")
                return

            await update.message.reply_text(
                f"⚠️ *Confirmation requise*\n\n"
                f"Vous allez fermer {len(positions)} position(s).\n\n"
                f"Envoyez `/confirm_close` pour confirmer.",
                parse_mode="Markdown"
            )

            # Stocker l'état pour confirmation
            context.user_data["pending_close_all"] = True

        except Exception as e:
            self.logger.error(f"Erreur cmd_close_all: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    @_auth_required
    async def cmd_confirm_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirme la fermeture de toutes les positions."""
        try:
            if not context.user_data.get("pending_close_all"):
                await update.message.reply_text("❌ Aucune fermeture en attente. Utilisez d'abord /close_all")
                return

            context.user_data["pending_close_all"] = False

            if not self.mt5_connector:
                await update.message.reply_text("❌ MT5 non connecté")
                return

            positions = self.mt5_connector.get_positions()
            closed_count = 0
            errors = []

            for pos in positions:
                try:
                    # TradePosition est un named tuple
                    ticket = pos.ticket if hasattr(pos, 'ticket') else pos.get("ticket") if isinstance(pos, dict) else None
                    if ticket:
                        result = self.mt5_connector.close_position(ticket)
                        if result:
                            closed_count += 1
                        else:
                            errors.append(f"Ticket {ticket}")
                except Exception as e:
                    pos_ticket = pos.ticket if hasattr(pos, 'ticket') else pos.get('ticket', '?') if isinstance(pos, dict) else '?'
                    errors.append(f"Ticket {pos_ticket}: {e}")

            message = f"✅ {closed_count} position(s) fermée(s)"
            if errors:
                message += f"\n\n❌ Erreurs:\n" + "\n".join(errors[:5])

            await update.message.reply_text(message)

        except Exception as e:
            self.logger.error(f"Erreur cmd_confirm_close: {e}")
            await update.message.reply_text(f"❌ Erreur: {e}")

    # ═══════════════════════════════════════════════════════════════
    # GESTION DU BOT TELEGRAM
    # ═══════════════════════════════════════════════════════════════

    def _setup_handlers(self):
        """Configure les handlers de commandes."""
        if not self._app:
            return

        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(CommandHandler("start", self.cmd_help))
        self._app.add_handler(CommandHandler("ping", self.cmd_ping))
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("balance", self.cmd_balance))
        self._app.add_handler(CommandHandler("positions", self.cmd_positions))
        self._app.add_handler(CommandHandler("summary", self.cmd_summary))
        self._app.add_handler(CommandHandler("stop", self.cmd_stop))
        self._app.add_handler(CommandHandler("start_bot", self.cmd_start_bot))
        self._app.add_handler(CommandHandler("pause", self.cmd_pause))
        self._app.add_handler(CommandHandler("resume", self.cmd_resume))
        self._app.add_handler(CommandHandler("close_all", self.cmd_close_all))
        self._app.add_handler(CommandHandler("confirm_close", self.cmd_confirm_close))

        self.logger.info("✅ Handlers Telegram configurés")

    def _run_bot_async(self):
        """Lance le bot Telegram dans un thread séparé."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            self._app = Application.builder().token(self.bot_token).build()
            self._setup_handlers()

            self.logger.info("🚀 Démarrage du bot Telegram...")
            self._loop.run_until_complete(self._app.initialize())
            self._loop.run_until_complete(self._app.start())
            self._loop.run_until_complete(self._app.updater.start_polling())
            self.logger.info("✅ Bot Telegram démarré et en écoute!")

            # Garder le loop actif
            self._loop.run_forever()

        except Exception as e:
            self.logger.error(f"❌ Erreur bot Telegram: {e}", exc_info=True)
        finally:
            if self._loop and self._loop.is_running():
                self._loop.stop()

    def start(self):
        """Démarre le bot Telegram dans un thread séparé."""
        if not TELEGRAM_AVAILABLE:
            self.logger.error("❌ Impossible de démarrer: python-telegram-bot non installé")
            return False

        if self._thread and self._thread.is_alive():
            self.logger.warning("⚠️ Bot Telegram déjà en cours d'exécution")
            return True

        self._thread = threading.Thread(target=self._run_bot_async, daemon=True)
        self._thread.start()
        self.logger.info("🚀 Thread bot Telegram démarré")
        return True

    def stop(self):
        """Arrête le bot Telegram."""
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)

            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)

            self.logger.info("🔴 Bot Telegram arrêté")
        except Exception as e:
            self.logger.error(f"Erreur arrêt bot Telegram: {e}")

    async def send_message(self, message: str, parse_mode: str = "Markdown"):
        """
        Envoie un message à tous les chat_ids autorisés.

        Args:
            message: Message à envoyer
            parse_mode: Mode de parsing (Markdown, HTML)
        """
        if not TELEGRAM_AVAILABLE:
            return

        try:
            bot = Bot(token=self.bot_token)
            for chat_id in self.authorized_chat_ids:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode=parse_mode
                    )
                except Exception as e:
                    self.logger.error(f"Erreur envoi message à {chat_id}: {e}")
        except Exception as e:
            self.logger.error(f"Erreur send_message: {e}")

    def send_message_sync(self, message: str, parse_mode: str = "Markdown"):
        """
        Version synchrone de send_message.
        Peut être appelée depuis du code non-async.
        """
        if not TELEGRAM_AVAILABLE:
            return

        try:
            import requests

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

            for chat_id in self.authorized_chat_ids:
                try:
                    payload = {
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": parse_mode
                    }
                    response = requests.post(url, json=payload, timeout=10)

                    if response.status_code != 200:
                        self.logger.warning(f"Erreur Telegram API: {response.text}")

                except Exception as e:
                    self.logger.error(f"Erreur envoi message à {chat_id}: {e}")

        except Exception as e:
            self.logger.error(f"Erreur send_message_sync: {e}")

    @property
    def is_running(self) -> bool:
        """Retourne True si le bot Telegram est en cours d'exécution."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def bot_status(self) -> bool:
        """Retourne l'état du bot de trading."""
        return self._bot_running

    @bot_status.setter
    def bot_status(self, value: bool):
        """Définit l'état du bot de trading."""
        self._bot_running = value


# ═══════════════════════════════════════════════════════════════
# FONCTION UTILITAIRE POUR CRÉER LE BOT
# ═══════════════════════════════════════════════════════════════

def create_telegram_controller(
    config_manager=None,
    mt5_connector=None,
    logger=None
) -> Optional[TelegramBotController]:
    """
    Crée et configure le contrôleur Telegram depuis la config.

    Args:
        config_manager: Instance ConfigManager
        mt5_connector: Instance MT5Connector
        logger: Logger

    Returns:
        TelegramBotController ou None si désactivé/erreur
    """
    if not config_manager:
        if logger:
            logger.warning("ConfigManager requis pour créer TelegramBotController")
        return None

    telegram_config = config_manager.get("telegram", {})

    if not telegram_config.get("enabled", False):
        if logger:
            logger.info("Telegram désactivé dans la configuration")
        return None

    bot_token = telegram_config.get("bot_token")
    chat_ids = telegram_config.get("authorized_chat_ids", [])

    if not bot_token:
        if logger:
            logger.error("❌ bot_token manquant dans telegram_config.json")
        return None

    if not chat_ids:
        if logger:
            logger.error("❌ authorized_chat_ids manquant dans telegram_config.json")
        return None

    controller = TelegramBotController(
        bot_token=bot_token,
        authorized_chat_ids=chat_ids,
        config_manager=config_manager,
        mt5_connector=mt5_connector,
        logger=logger
    )

    return controller
