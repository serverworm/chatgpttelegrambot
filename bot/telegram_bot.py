import logging
import os
import itertools
import asyncio

import telegram
from telegram import constants
from telegram import Message, MessageEntity, Update, InlineQueryResultArticle, InputTextMessageContent, BotCommand, ChatMember
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, Application, CallbackContext

from pydub import AudioSegment
from openai_helper import OpenAIHelper
from usage_tracker import UsageTracker


def message_text(message: Message) -> str:
    """
    Returns the text of a message, excluding any bot commands.
    """
    message_text = message.text
    if message_text is None:
        return ''

    for _, text in sorted(message.parse_entities([MessageEntity.BOT_COMMAND]).items(), key=(lambda item: item[0].offset)):
        message_text = message_text.replace(text, '').strip()

    return message_text if len(message_text) > 0 else ''


class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.openai = openai
        self.commands = [
            BotCommand(command='help', description='Список команд\n\n'),
            BotCommand(command='reset', description='Создание нового диалога\n'),
            BotCommand(command='image', description='Генерация изображения из описания \(например, /image cat\)\n'),
            BotCommand(command='stats', description='Просмотр статистики использования бота\n'),
            BotCommand(command='resend', description='Повторный ответ на последнее сообщение\n')
        ]
        self.disallowed_message = "⛔ Извините, вам ограничен доступ к данному боту."
        self.budget_limit_message = "😢 Извините, вы исчерпали свой месячный лимит токенов для использования."
        self.usage = {}
        self.last_message = {}

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """
        commands = [f'/{command.command} \- {command.description}' for command in self.commands]
        help_text = "***☀ Приветик\! \nЯ ChatGPT\-бот\, задай абсолютно любой вопрос прямо сейчас\!\n\n\n📘 " + "\n🔹 ".join(commands) + '\n\n\n' + "✨ Проект поддерживается @forgotten\_server***"
        # 'Отправь мне голосовое сообщение или файл, а я расшифрую его для тебя!'
        await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN_V2)


    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Returns token usage statistics for current day and month.
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не разрешается запрашивать статистику их использования')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
            f'запросил статистику их использования')
        
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        transcribe_durations = self.usage[user_id].get_current_transcription_duration()
        cost_today, cost_month = self.usage[user_id].get_current_cost()
        
        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        budget = await self.get_remaining_budget(update)

        text_current_conversation = f"*ℹ Статистика текущего диалога:*\n"+\
                     f"{chat_messages} сообщений хранится в истории диалога (не более 180 минут)\n"+\
                     f"{chat_token_length} использованных токенов в данном диалоге.\n"+\
                     f"----------------------------\n"
        text_today = f"*ℹ Статистика использования за сегодня:*\n"+\
                     f"{tokens_today} использованных токенов в боте.\n"+\
                     f"{images_today} изображений сгенерированно.\n"+\
                     f"{transcribe_durations[0]} минуты и {transcribe_durations[1]} секунды использованно.\n"+\
                     f"💰 На общую сумму ${cost_today:.2f}\n"+\
                     f"----------------------------\n"
        text_month = f"*ℹ Статистика использования за месяц:*\n"+\
                     f"{tokens_month} использованных токенов в боте.\n"+\
                     f"{images_month} изображений сгенерированно.\n"+\
                     f"{transcribe_durations[2]} минуты и {transcribe_durations[3]} секунды использованно.\n"+\
                     f"💰 На общую сумму ${cost_month:.2f}"
        # text_budget filled with conditional content
        text_budget = "\n\n"
        if budget < float('inf'):
            text_budget += f"Ваш оставшийся бюджет в размере ${budget:.2f} в этом месяце.\n"
        # add OpenAI account information for admin request
        if self.is_admin(update):
            text_budget += f"Ваш счет в OpenAI составляет ${self.openai.get_billing_current_month():.2f} в этом месяце."
        
        usage_text = text_current_conversation + text_today + text_month + text_budget
        await update.message.reply_text(usage_text, parse_mode=constants.ParseMode.MARKDOWN)

    async def resend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resend the last request
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'Пользователь {update.message.from_user.name}  (id: {update.message.from_user.id})'
                            f' не разрешается повторно отправлять сообщение')
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id
        if chat_id not in self.last_message:
            logging.warning(f'Пользователь {update.message.from_user.name} (id: {update.message.from_user.id})'
                            f' у него нет ничего для повторной отправки')
            await context.bot.send_message(chat_id=chat_id, text="*ℹ Мне не на что ответить повторно, поскольку вы пока не задали ни одного вопроса.*", parse_mode=constants.ParseMode.MARKDOWN_V2)
            return

        # Update message text, clear self.last_message and send the request to prompt
        logging.info(f'Повторная отправка последнего запроса от пользователя: {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')
        with update.message._unfrozen() as message:
            message.text = self.last_message.pop(chat_id)

        await self.prompt(update=update, context=context)

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation.
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не разрешается сбрасывать разговор')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'Сброс диалога для пользователя {update.message.from_user.name} '
            f'(id: {update.message.from_user.id})...')

        chat_id = update.effective_chat.id
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await context.bot.send_message(chat_id=chat_id, text='🥳 Новый диалог создан!')

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs
        """
        if not self.config['enable_image_generation'] or not await self.check_allowed_and_within_budget(update, context):
            return

        chat_id = update.effective_chat.id
        image_query = message_text(update.message)
        if image_query == '':
            await context.bot.send_message(chat_id=chat_id, text='***🥺Пожалуйста\, чуть\-чуть подробнее\!***\n' + '\nК примеру, так сгенерирую тебе котика ``` /image cat``` ', parse_mode=constants.ParseMode.MARKDOWN_V2)
            return

        logging.info(f'Запрос на создание нового изображения, полученный от пользователя {update.message.from_user.name} '
            f'(id: {update.message.from_user.id})')

        async def _generate():
            try:
                image_url, image_size = await self.openai.generate_image(prompt=image_query)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    photo=image_url
                )
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    text=f'😢 Не удалось сгенерировать изображение: {str(e)}',
                    parse_mode=constants.ParseMode.MARKDOWN
                )

        await self.wrap_with_indicator(update, context, constants.ChatAction.UPLOAD_PHOTO, _generate)

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Transcribe audio messages.
        """
        if not self.config['enable_transcription'] or not await self.check_allowed_and_within_budget(update, context):
            return

        if self.is_group_chat(update) and self.config['ignore_group_transcriptions']:
            logging.info(f'Транскрипция, исходящая из группового чата, игнорируется...')
            return

        chat_id = update.effective_chat.id
        filename = update.message.effective_attachment.file_unique_id

        async def _execute():
            filename_mp3 = f'{filename}.mp3'

            try:
                media_file = await context.bot.get_file(update.message.effective_attachment.file_id)
                await media_file.download_to_drive(filename)
            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    text=f'😢 Не удалось загрузить аудиофайл: {str(e)}. Убедитесь, что файл не слишком большой. (максимум 20 МБ)',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return

            # detect and extract audio from the attachment with pydub
            try:
                audio_track = AudioSegment.from_file(filename)
                audio_track.export(filename_mp3, format="mp3")
                logging.info(f'Новый запрос на расшифровку, полученный от пользователя {update.message.from_user.name} '
                    f'(id: {update.message.from_user.id})')

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    reply_to_message_id=update.message.message_id,
                    text='😢 Неподдерживаемый тип файла'
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

            # send decoded audio to openai
            try:

                # Transcribe the audio file
                transcript = await self.openai.transcribe(filename_mp3)

                # add transcription seconds to usage tracker
                transcription_price = self.config['transcription_price']
                self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                # add guest chat request to guest usage tracker
                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                if self.config['voice_reply_transcript']:

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f'_Transcript:_\n"{transcript}"'
                    chunks = self.split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            reply_to_message_id=update.message.message_id if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                else:
                    # Get the response of the transcript
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=transcript)

                    # add chat request to users usage tracker
                    self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                    # add guest chat request to guest usage tracker
                    if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                        self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f'_Transcript:_\n"{transcript}"\n\n_Answer:_\n{response}'
                    chunks = self.split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            reply_to_message_id=update.message.message_id if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    text=f'😢 Не удалось расшифровать текст: {str(e)}',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            finally:
                # Cleanup files
                if os.path.exists(filename_mp3):
                    os.remove(filename_mp3)
                if os.path.exists(filename):
                    os.remove(filename)

        await self.wrap_with_indicator(update, context, constants.ChatAction.TYPING, _execute)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        React to incoming messages and respond accordingly.
        """
        if not await self.check_allowed_and_within_budget(update, context):
            return
        
        logging.info(f'Новое сообщение, полученное от пользователя {update.message.from_user.name} (id: {update.message.from_user.id})')
        chat_id = update.effective_chat.id
        user_id = update.message.from_user.id
        prompt = update.message.text
        self.last_message[chat_id] = prompt

        if self.is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']
            if prompt.lower().startswith(trigger_keyword.lower()):
                prompt = prompt[len(trigger_keyword):].strip()
            else:
                if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                    logging.info('Сообщение - это ответ боту, разрешающий...')
                else:
                    logging.warning('Сообщение не начинается с ключевого слова trigger, игнорируется...')
                    return

        try:
            if self.config['stream']:
                await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
                is_group_chat = self.is_group_chat(update)

                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                chunk = 0

                async for content, tokens in stream_response:
                    if len(content.strip()) == 0:
                        continue

                    chunks = self.split_into_chunks(content)
                    if len(chunks) > 1:
                        content = chunks[-1]
                        if chunk != len(chunks) - 1:
                            chunk += 1
                            try:
                                await self.edit_message_with_retry(context, chat_id, sent_message.message_id, chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await context.bot.send_message(
                                    chat_id=sent_message.chat_id,
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    if is_group_chat:
                        # group chats have stricter flood limits
                        cutoff = 180 if len(content) > 1000 else 120 if len(content) > 200 else 90 if len(content) > 50 else 50
                    else:
                        cutoff = 90 if len(content) > 1000 else 45 if len(content) > 200 else 25 if len(content) > 50 else 15

                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await context.bot.send_message(
                                chat_id=chat_id,
                                reply_to_message_id=update.message.message_id,
                                text=content
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await self.edit_message_with_retry(context, chat_id, sent_message.message_id,
                                                               text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

            else:
                async def _reply():
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    chunks = self.split_into_chunks(response)

                    for index, chunk in enumerate(chunks):
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                reply_to_message_id=update.message.message_id if index == 0 else None,
                                text=chunk,
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    reply_to_message_id=update.message.message_id if index == 0 else None,
                                    text=chunk
                                )
                            except Exception as e:
                                raise e

                await self.wrap_with_indicator(update, context, constants.ChatAction.TYPING, _reply)

            try:
                # add chat request to users usage tracker
                self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                # add guest chat request to guest usage tracker
                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])
            except:
                pass

        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                text=f'Failed to get response: {str(e)}',
                parse_mode=constants.ParseMode.MARKDOWN
            )

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query

        if query == '':
            return

        results = [
            InlineQueryResultArticle(
                id=query,
                title='Ask ChatGPT',
                input_message_content=InputTextMessageContent(query),
                description=query,
                thumb_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png'
            )
        ]

        await update.inline_query.answer(results)

    async def edit_message_with_retry(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                      message_id: int, text: str, markdown: bool = True):
        """
        Edit a message with retry logic in case of failure (e.g. broken markdown)
        :param context: The context to use
        :param chat_id: The chat id to edit the message in
        :param message_id: The message id to edit
        :param text: The text to edit the message with
        :param markdown: Whether to use markdown parse mode
        :return: None
        """
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=constants.ParseMode.MARKDOWN if markdown else None
            )
        except telegram.error.BadRequest as e:
            if str(e).startswith("😢 Message is not modified"):
                return
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                )
            except Exception as e:
                logging.warning(f'Failed to edit message: {str(e)}')
                raise e

        except Exception as e:
            logging.warning(str(e))
            raise e

    async def wrap_with_indicator(self, update: Update, context: CallbackContext, chat_action: constants.ChatAction, coroutine):
        """
        Wraps a coroutine while repeatedly sending a chat action to the user.
        """
        task = context.application.create_task(coroutine(), update=update)
        while not task.done():
            context.application.create_task(update.effective_chat.send_action(chat_action))
            try:
                await asyncio.wait_for(asyncio.shield(task), 4.5)
            except asyncio.TimeoutError:
                pass

    async def send_disallowed_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Sends the disallowed message to the user.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.disallowed_message,
            disable_web_page_preview=True
        )

    async def send_budget_reached_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Sends the budget reached message to the user.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.budget_limit_message
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handles errors in the telegram-python-bot library.
        """
        logging.error(f'Исключение при обработке обновления: {context.error}')

    def is_group_chat(self, update: Update) -> bool:
        """
        Checks if the message was sent from a group chat
        """
        return update.effective_chat.type in [
            constants.ChatType.GROUP,
            constants.ChatType.SUPERGROUP
        ]

    async def is_user_in_group(self, update: Update, context: CallbackContext, user_id: int) -> bool:
        """
        Checks if user_id is a member of the group
        """
        try:
            chat_member = await context.bot.get_chat_member(update.message.chat_id, user_id)
            return chat_member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
        except telegram.error.BadRequest as e:
            if str(e) == "User not found":
                return False
            else:
                raise e
        except Exception as e:
            raise e

    async def is_allowed(self, update: Update, context: CallbackContext) -> bool:
        """
        Checks if the user is allowed to use the bot.
        """
        if self.config['allowed_user_ids'] == '*':
            return True
        
        if self.is_admin(update):
            return True
        
        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        # Check if user is allowed
        if str(update.message.from_user.id) in allowed_user_ids:
            return True

        # Check if it's a group a chat with at least one authorized member
        if self.is_group_chat(update):
            admin_user_ids = self.config['admin_user_ids'].split(',')
            for user in itertools.chain(allowed_user_ids, admin_user_ids):
                if await self.is_user_in_group(update, context, user):
                    logging.info(f'{user} является членом клуба. Разрешение сообщения в групповом чате...')
                    return True
            logging.info(f'Сообщения группового чата от пользователя {update.message.from_user.name} '
                f'(id: {update.message.from_user.id}) не допускаются')

        return False

    def is_admin(self, update: Update) -> bool:
        """
        Checks if the user is the admin of the bot.
        The first user in the user list is the admin.
        """
        if self.config['admin_user_ids'] == '-':
            logging.info('Пользователь-администратор не определен.')
            return False

        admin_user_ids = self.config['admin_user_ids'].split(',')

        # Check if user is in the admin user list
        if str(update.message.from_user.id) in admin_user_ids:
            return True

        return False

    async def get_remaining_budget(self, update: Update) -> float:
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        if self.is_admin(update):
            return float('inf')

        if self.config['monthly_user_budgets'] == '*':
            return float('inf')

        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        if str(user_id) in allowed_user_ids:
            # find budget for allowed user
            user_index = allowed_user_ids.index(str(user_id))
            user_budgets = self.config['monthly_user_budgets'].split(',')
            # check if user is included in budgets list
            if len(user_budgets) <= user_index:
                logging.warning(f'No budget set for user: {update.message.from_user.name} ({user_id}).')
                return 0.0
            user_budget = float(user_budgets[user_index])
            cost_month = self.usage[user_id].get_current_cost()[1]
            remaining_budget = user_budget - cost_month
            return remaining_budget
        else:
            return 0.0

    async def is_within_budget(self, update: Update, context: CallbackContext) -> bool:
        """
        Checks if the user reached their monthly usage limit.
        Initializes UsageTracker for user and guest when needed.
        """
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        if self.is_admin(update):
            return True

        if self.config['monthly_user_budgets'] == '*':
            return True

        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        if str(user_id) in allowed_user_ids:
            # find budget for allowed user
            user_index = allowed_user_ids.index(str(user_id))
            user_budgets = self.config['monthly_user_budgets'].split(',')
            # check if user is included in budgets list
            if len(user_budgets) <= user_index:
                logging.warning(f'Бюджет для пользователя не установлен: {update.message.from_user.name} ({user_id}).')
                return False
            user_budget = float(user_budgets[user_index])
            cost_month = self.usage[user_id].get_current_cost()[1]
            # Check if allowed user is within budget
            return user_budget > cost_month

        # Check if group member is within budget
        if self.is_group_chat(update):
            admin_user_ids = self.config['admin_user_ids'].split(',')
            for user in itertools.chain(allowed_user_ids, admin_user_ids):
                if await self.is_user_in_group(update, context, user):
                    if 'guests' not in self.usage:
                        self.usage['guests'] = UsageTracker('guests', 'all guest users in group chats')
                    if self.config['monthly_guest_budget'] >= self.usage['guests'].get_current_cost()[1]:
                        return True
                    logging.warning('Ежемесячный гостевой бюджет на групповые чаты израсходован.')
                    return False
            logging.info(f'Сообщения группового чата от пользователя {update.message.from_user.name} '
                f'(id: {update.message.from_user.id}) не допускаются')
        return False

    async def check_allowed_and_within_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """
        Checks if the user is allowed to use the bot and if they are within their budget
        :param update: Telegram update object
        :param context: Telegram context object
        :return: Boolean indicating if the user is allowed to use the bot
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не разрешается использовать бота')
            await self.send_disallowed_message(update, context)
            return False

        if not await self.is_within_budget(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'достигнут лимит их использования')
            await self.send_budget_reached_message(update, context)
            return False

        return True

    def split_into_chunks(self, text: str, chunk_size: int = 4096) -> list[str]:
        """
        Splits a string into chunks of a given size.
        """
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        await application.bot.set_my_commands(self.commands)

    def run(self):
        """
        Runs the bot indefinitely until the user presses Ctrl+C
        """
        application = ApplicationBuilder() \
            .token(self.config['token']) \
            .proxy_url(self.config['proxy']) \
            .get_updates_proxy_url(self.config['proxy']) \
            .post_init(self.post_init) \
            .concurrent_updates(True) \
            .build()

        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('help', self.help))
        application.add_handler(CommandHandler('image', self.image))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('stats', self.stats))
        application.add_handler(CommandHandler('resend', self.resend))
        application.add_handler(MessageHandler(
            filters.AUDIO | filters.VOICE | filters.Document.AUDIO |
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            self.transcribe))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP
        ]))

        application.add_error_handler(self.error_handler)

        application.run_polling()
